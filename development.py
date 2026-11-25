"""Persist and execute the fixed Unity development stages under one worker lease."""

import json
import logging
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from artifacts import RunContext, log_page, redact_text, safe_child
from codex_executor import IMPLEMENT_RULES, REVIEW_RULES, AgentExecutor, CodexExecutor
from git_workspace import GitWorkspace
from pool_scheduler import PoolScheduler, RequestConflict
from process_runner import ProcessRunner, StageError
from pull_requests import HostedPullRequests
from task_store import Store, now, signature
from unity_executor import UnityExecutor, assert_unity_closed
from worker_config import Settings, check_slot, legacy_service, validate_test_selection


STAGES = {
    "prepare": ("preparing", "prepared"),
    "implement": ("implementing", "implemented"),
    "compile": ("compiling", "compiled"),
    "test": ("testing", "tested"),
    "review": ("reviewing", "reviewed"),
    "fix": ("fixing", "implemented"),
    "commit": ("committing", "delivered"),
}
NEXT_STAGE = {"created": "prepare", "prepared": "implement", "implemented": "compile",
              "compiled": "test", "tested": "review"}
FIXABLE = {"compile_failed", "tests_failed", "review_changes", "asset_metadata"}
TERMINAL = {"completed", "cancelled", "failed"}
RETRYABLE = {"git_failed", "timeout", "unity_changed", "workspace_in_use", "license_failed", "package_failed",
             "startup_failed", "test_execution_failed", "not_run", "unsupported", "review_blocked", "dirty_workspace"}


class TaskNotFound(Exception):
    pass


def publishable(job):
    return {key: value for key, value in job.items() if key not in {"signature", "request_id"}}


class DevelopmentWorker:
    def __init__(self, settings, runner=None, agent: AgentExecutor | None = None, pr_adapter=None, *, unavailable=None):
        self.cfg = legacy_service(settings) if isinstance(settings, Settings) else settings
        self.runner = runner or ProcessRunner()
        self.store = Store.open_service(self.cfg)
        self.marker = safe_child(self.cfg.logs, "active.lock")
        self.guard = threading.RLock()
        self.active = {}
        self.stopping = False
        self.store.recover_interrupted()
        self.adapters = {pool.id: HostedPullRequests.from_pool_config(pool.pr) if pool.pr else
                         pr_adapter if pool.id == self.cfg.default_pool else None
                         for pool in self.cfg.pools.values()}
        self.workspaces = {}
        rejected = dict(unavailable or {})
        for slot in self.cfg.slots.values():
            cfg = slot.settings
            self.workspaces[slot.id] = SimpleNamespace(git=GitWorkspace(cfg), unity=UnityExecutor(cfg),
                                                        agent=agent or CodexExecutor(cfg), settings=cfg)
            if not self.cfg.implicit and slot.id not in rejected:
                with self.store.connect() as db:
                    leased = self.store.slot_lease(db, slot.id) is not None
                if error := check_slot(slot, leased=leased):
                    rejected[slot.id] = error
        self.scheduler = PoolScheduler(self, rejected)

    def start(self):
        self.scheduler.start()

    def close(self):
        with self.guard:
            self.stopping = True
            active = list(self.active.values())
            for _, cancel in active:
                cancel.set()
        self.scheduler.close()
        deadline = time.monotonic() + 8
        for thread, _ in active:
            thread.join(max(0, deadline - time.monotonic()))

    def clean_output(self, value):
        if isinstance(value, str):
            return redact_text(value, self.cfg.secrets)
        if isinstance(value, list):
            return [self.clean_output(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean_output(item) for key, item in value.items()}
        return value

    def task(self, task_id):
        with self.store.connect() as db:
            return self.require_task(db, task_id)

    def job(self, job_id):
        with self.store.connect() as db:
            if job := self.store.job(db, job_id):
                return publishable(job)
        raise TaskNotFound

    def pools(self):
        with self.store.connect() as db:
            return [self.scheduler.pool_status(db, pool) for pool in self.cfg.pools.values()]

    def worker(self, *, legacy=False):
        with self.store.connect() as db:
            pool = self.scheduler.pool_status(db, self.cfg.pools[self.cfg.default_pool])
        slots = pool["slots"]
        if legacy:
            slots = [slot for slot in slots if slot["id"] == self.cfg.legacy_slot]
        states = {slot["status"] for slot in slots}
        status = "idle" if "idle" in states else "leased" if "leased" in states else "recovery_required"
        leased = [slot for slot in slots if slot["task_id"]]
        current = leased[0] if len(leased) == 1 else {}
        return {"worker_id": self.cfg.worker_id, "status": status, "task_id": current.get("task_id"),
                "current_stage": current.get("current_stage")}

    def require_task(self, db, task_id):
        if task := self.store.task(db, task_id):
            return task
        raise TaskNotFound

    def previous(self, db, action, task_id, request_id, payload):
        job = self.store.request(db, request_id)
        if job and (job["action"] != action or (task_id and job["task_id"] != task_id)
                    or job["signature"] != signature(payload)):
            raise RequestConflict("Request ID is already bound to a different operation")
        return job

    def new_task(self, task_id, payload, task_type):
        pool_id = payload.get("pool_id", self.cfg.default_pool)
        policy = self.cfg.pools[pool_id].policy
        return {
            "task_id": task_id, "job_id": None, "worker_id": self.cfg.worker_id, "task_type": task_type,
            "pool_id": pool_id, "slot_id": None,
            "workflow_run_id": None, "requirement": "", "plan": "", "acceptance_criteria": [], "project_rules": "",
            **payload, "base_branch": policy.base_branch if task_type == "development" else None,
            "base_commit": None, "task_branch": None, "status": "pending", "current_stage": "created",
            "retry_count": 0, "created_at": now(), "started_at": None, "finished_at": None, "exit_code": None,
            "error_message": None, "failure_kind": None, "log_path": f"tasks/{task_id}", "artifacts": [],
            "agent_session_id": None, "lease": False, "revision": None, "approved_revision": None,
            "approval": None, "changed_files": [], "review_result": None, "stage_result": {},
            "commit_sha": None, "pushed": False, "cancel_requested": False,
        }

    @staticmethod
    def new_job(task_id, request_id, action, payload, *, job_id=None):
        timestamp = now()
        return {
            "job_id": job_id or str(uuid4()), "task_id": task_id, "request_id": request_id,
            "signature": signature(payload), "action": action, "status": "running", "created_at": timestamp,
            "started_at": timestamp, "finished_at": None, "exit_code": None, "error_message": None,
            "stage_result": {},
        }

    def create(self, request_id, payload):
        with self.store.connect(write=True) as db:
            if previous := self.previous(db, "create", None, request_id, payload):
                return publishable(previous)
            pool = self.cfg.pools.get(payload.get("pool_id", self.cfg.default_pool))
            if pool is None:
                raise ValueError("Unknown Unity workspace pool")
            validate_test_selection(payload.get("test_selection"), pool.policy.test_assemblies)
            task_id = str(uuid4())
            task = self.new_task(task_id, payload, "development")
            job = self.new_job(task_id, request_id, "create", payload)
            job.update(status="succeeded", finished_at=now(), stage_result={"status": "pending"})
            task["job_id"] = job["job_id"]
            self.store.save_task(db, task)
            self.store.save_job(db, job)
            return publishable(job)

    def smoke(self, job_id):
        with self.guard:
            with self.store.connect(write=True) as db:
                if previous := self.previous(db, "smoke", job_id, job_id, {}):
                    return publishable(previous)
                if (self.store.slot_lease(db, self.cfg.legacy_slot) or self.scheduler.slot_blocked(self.cfg.legacy_slot)
                        or not self.cfg.slots[self.cfg.legacy_slot].enabled or self.stopping):
                    raise RequestConflict("Worker busy or blocked")
                if self.store.task(db, job_id) or self.store.job(db, job_id):
                    raise RequestConflict("Job ID is already assigned")
                if safe_child(self.cfg.logs, f"{job_id}.log").exists():
                    raise RequestConflict("Job ID already has a log; inspect it instead of resubmitting")
                task = self.new_task(job_id, {}, "startup_smoke")
                job = self.new_job(job_id, job_id, "smoke", {}, job_id=job_id)
                task.update(job_id=job_id, slot_id=self.cfg.legacy_slot, lease=True, status="preparing", current_stage="startup_smoke",
                            started_at=now(), log_path=f"{job_id}.log")
                self.store.save_task(db, task)
                self.store.save_job(db, job)
            return self.spawn(job, {})

    def next_stage(self, task):
        stage, failure = task["current_stage"], task["failure_kind"]
        if task["status"] == "failed":
            if failure == "queue_timeout" and stage == "created":
                return "prepare"
            if failure in FIXABLE and task["retry_count"] < self.cfg.pools[task["pool_id"]].policy.max_fixes:
                return "fix"
            if failure == "unity_changed" and stage in {"compile", "test"}:
                return "compile"
            if stage in {"prepare", "compile", "test", "review"} and failure in RETRYABLE:
                return stage
            if stage in {"compile", "test", "review", "commit"} and failure == "workspace_changed" and not task["commit_sha"]:
                return "compile"
            return None
        return NEXT_STAGE.get(stage)

    def check_action(self, db, task, action, payload):
        status, stage = task["status"], task["current_stage"]
        if self.stopping:
            raise RequestConflict("Worker is shutting down")
        active_job = self.store.active_job(db, task["task_id"])
        if task["slot_id"] and self.scheduler.slot_blocked(task["slot_id"]) and action != "cancel":
            raise RequestConflict("The assigned workspace is unavailable")
        if status == "recovery_required":
            if action == "recover" and task["lease"] and not active_job:
                return
            raise RequestConflict("Verify the previous process tree and recover the task first")
        if action == "cancel":
            if active_job or status not in TERMINAL:
                return
        elif active_job:
            raise RequestConflict("The task already has an active job")
        elif action == "release" and status in TERMINAL and task["lease"]:
            return
        elif action == "approve":
            if status == "awaiting_approval" and stage == "reviewed" and task["lease"]:
                if task["revision"] != payload["revision"]:
                    raise RequestConflict("Approval revision does not match the reviewed revision")
                if payload["create_pr"] and self.adapters.get(task["pool_id"]) is None:
                    raise RequestConflict("This worker has no PR/MR adapter configured")
                return
        elif action == "commit" and task["lease"] and task["approved_revision"]:
            if payload["revision"] != task["approved_revision"]:
                raise RequestConflict("Delivery revision does not match the current approval")
            if stage == "approved" or (stage == "commit" and status == "failed" and task["commit_sha"]
                                       and task["failure_kind"] in {"git_failed", "timeout", "recovered", "push_failed"}):
                return
        elif action == "pr" and status == "completed" and task["pushed"] and task["approval"]["create_pr"]:
            if task["lease"] and self.adapters.get(task["pool_id"]) is not None and not task.get("pull_request"):
                return
        elif action in STAGES or action == "run":
            if task["pool_id"] not in self.cfg.pools:
                raise RequestConflict("The task's workspace pool is no longer configured")
            first = self.next_stage(task)
            if task["task_type"] == "development" and first and (action == first or action == "run"):
                if task["lease"] or (first == "prepare" and task["slot_id"] is None):
                    return
        raise RequestConflict(f"Operation is unavailable at {status}/{stage}")

    def action(self, task_id, action, request_id, payload):
        with self.guard:
            with self.store.connect(write=True) as db:
                if previous := self.previous(db, action, task_id, request_id, payload):
                    return publishable(previous)
                task = self.require_task(db, task_id)
                self.check_action(db, task, action, payload)
                job = self.new_job(task_id, request_id, action, payload)
                if action in {"approve", "cancel"}:
                    active = self.store.active_job(db, task_id)
                    running = active and active["status"] == "running"
                    if action == "approve":
                        task.update(current_stage="approved", approved_revision=payload["revision"],
                                    approval={**payload, "approved_at": now()})
                    elif running:
                        task["cancel_requested"] = True
                    else:
                        if active:
                            active.update(status="cancelled", finished_at=now(), failure_kind="cancelled",
                                          error_message="Cancellation requested")
                            self.store.save_job(db, active)
                        task.update(status="cancelled", current_stage="cancelled", finished_at=now(),
                                    failure_kind="cancelled", error_message="Cancellation requested")
                    if not running:
                        task["job_id"] = job["job_id"]
                    job.update(status="succeeded", finished_at=now(), stage_result={"status": task["status"],
                                                                                      "cancel_requested": bool(running)})
                elif not task["lease"]:
                    self.scheduler.admit(db, task, job, payload.get("queue_wait_seconds"))
                else:
                    task.update(job_id=job["job_id"], cancel_requested=False)
                    task["started_at"] = task["started_at"] or now()
                    if action not in {"release", "recover", "pr"}:
                        task["finished_at"] = None
                self.store.save_task(db, task)
                self.store.save_job(db, job)
            if action == "cancel" and task_id in self.active:
                self.active[task_id][1].set()
            elif job["status"] == "running":
                return self.spawn(job, payload)
            self.scheduler.wake.set()
            return publishable(job)

    def spawn(self, job, payload):
        task_id = job["task_id"]
        cancel = threading.Event()
        thread = threading.Thread(target=self.execute, args=(job, payload, cancel), daemon=True, name=f"unity-{job['action']}")
        self.active[task_id] = thread, cancel
        try:
            thread.start()
        except RuntimeError:
            self.active.pop(task_id, None)
            self.store.recover_interrupted(job["job_id"])
            return self.job(job["job_id"])
        return publishable(job)

    def save(self, job, **change):
        change = self.clean_output(change)
        with self.store.connect(write=True) as db:
            task = self.require_task(db, job["task_id"])
            if self.store.job(db, job["job_id"])["status"] != "running":
                raise StageError("recovery_required", "Task state changed during execution")
            task.update(change)
            self.store.save_task(db, task)
            return task

    def execute(self, job, payload, cancel):
        ctx = None
        error = None
        result = {}
        started = time.monotonic()
        action = job["action"]
        try:
            task = self.task(job["task_id"])

            def save(**change):
                ctx.task.update(self.save(job, **change))

            workspace = self.workspaces[task["slot_id"]]
            ctx = RunContext(workspace.settings, self.runner, task, job, cancel, save)
            ctx.workspace = workspace
            if action == "run":
                result = self.run(ctx)
            elif action == "smoke":
                result = ctx.workspace.unity.run(ctx, "startup_smoke", safe_child(self.cfg.logs, f"{task['task_id']}.log"))
            elif action == "release":
                assert_unity_closed(ctx.settings.project)
                if task["task_type"] == "development":
                    ctx.workspace.git.clean(ctx)
                result = {"released": True}
            elif action == "recover":
                assert_unity_closed(ctx.settings.project)
                if task["task_type"] == "development":
                    ctx.workspace.git.validate(ctx)
                result = {"recovered": True, "operator_note": payload["note"]}
            elif action == "pr":
                result = self.publish_pr(ctx)
            else:
                result = self.run_stage(ctx, action)
        except StageError as exc:
            error = exc
        except Exception as exc:
            logging.getLogger(__name__).error("Worker job %s raised %s", job["job_id"], type(exc).__name__)
            error = StageError("worker_error", f"Worker exception ({type(exc).__name__}); inspect local logs")
        if error and (action == "commit" and error.kind in {"timeout", "cancelled"}
                      or action == "smoke" and error.kind == "workspace_in_use"):
            error.kind = "recovery_required"
        if ctx and action != "smoke" and action != "release":
            try:
                self.report(ctx, error)
            except Exception as exc:
                logging.getLogger(__name__).error("Cannot save job report %s (%s)", job["job_id"], type(exc).__name__)
                if error is None:
                    error = StageError("worker_error", "Cannot save the development report")
        with self.guard:
            try:
                self.finish(job, self.clean_output(result), error, time.monotonic() - started)
            finally:
                self.active.pop(job["task_id"], None)
                self.scheduler.wake.set()

    def finish(self, job, result, error, duration):
        with self.store.connect(write=True) as db:
            if self.store.job(db, job["job_id"])["status"] != "running":
                return
            task = self.require_task(db, job["task_id"])
            action, timestamp = job["action"], now()
            job.update(finished_at=timestamp, duration_seconds=round(duration, 2), status="succeeded",
                       exit_code=0, stage_result=result)
            if error:
                kind = error.kind
                state = kind if kind in {"cancelled", "recovery_required"} else "failed"
                message = self.clean_output(str(error))
                job.update(status=state, exit_code=error.exit_code, error_message=message, failure_kind=kind,
                           stage_result=self.clean_output(error.result))
                if action not in {"release", "recover", "pr"} or kind == "recovery_required":
                    task.update(status=state, finished_at=timestamp, failure_kind=kind, error_message=message,
                                stage_result=job["stage_result"], exit_code=error.exit_code)
                if action == "commit" and kind in {"workspace_changed", "dirty_workspace", "unsafe_change", "asset_metadata"}:
                    task.update(approved_revision=None, approval=None)
            if action == "smoke" and task["status"] != "recovery_required":
                task.update(lease=False, status="failed" if error else "completed", finished_at=timestamp,
                            exit_code=error.exit_code if error else 0)
            if action == "release" and not error:
                task.update(lease=False, released_at=timestamp)
            if action == "recover" and not error:
                task.update(status="failed", failure_kind="recovered", recovery=result, finished_at=timestamp)
            self.store.save_task(db, task)
            self.store.save_job(db, job)

    def run(self, ctx):
        action = self.next_stage(ctx.task)
        while action:
            try:
                result = self.run_stage(ctx, action)
            except StageError as error:
                if error.kind not in FIXABLE or ctx.task["retry_count"] >= ctx.settings.max_fixes:
                    raise
                ctx.save(status="failed", failure_kind=error.kind, error_message=str(error), stage_result=error.result)
                action = "fix"
                continue
            if action == "review":
                return result
            action = self.next_stage(ctx.task)
        raise StageError("worker_error", "No valid next stage")

    def run_stage(self, ctx, stage):
        ctx.check_cancelled()
        running, finished = STAGES[stage]
        ctx.save(status=running, current_stage=stage, finished_at=None)
        result = getattr(self, f"stage_{stage}")(ctx)
        status = "awaiting_approval" if stage == "review" else "completed" if stage == "commit" else running
        ctx.save(status=status, current_stage=finished, stage_result=result, failure_kind=None, error_message=None,
                 exit_code=0, finished_at=now() if stage == "commit" else None)
        return result

    def stage_prepare(self, ctx):
        ctx.workspace.unity.validate()
        task = ctx.task
        branch = "ai/task-" + task["task_id"].replace("-", "")
        prepared = ctx.workspace.git.prepare(ctx, branch)
        ctx.save(**prepared)
        return {"base_commit": prepared["base_commit"], "task_branch": branch}

    @staticmethod
    def brief(task):
        return {key: task[key] for key in ("requirement", "plan", "acceptance_criteria", "project_rules")}

    def agent_implementation(self, ctx, diagnostics=None):
        ctx.workspace.git.checkout_is(ctx, ctx.task, ctx.task["base_commit"])
        ctx.save(revision=None, approved_revision=None, approval=None, compile_report=None, test_report=None,
                 review_result=None, agent_session_id=None)
        info = self.brief(ctx.task)
        if ctx.task.get("test_selection"):
            info["test_selection"] = ctx.task["test_selection"]
        if diagnostics:
            info["previous_failure"] = diagnostics
        try:
            result = ctx.workspace.agent.execute(ctx, IMPLEMENT_RULES + "\n\n" + json.dumps(info, ensure_ascii=False))
        except StageError as error:
            ctx.save(agent_session_id=error.result.get("session_id"))
            raise
        ctx.save(agent_session_id=result.get("session_id"))
        snapshot = ctx.workspace.git.snapshot(ctx, ctx.task)
        if not snapshot.changes:
            raise StageError("no_changes", "Codex finished without a deliverable Git change", result=result)
        ctx.save(revision=snapshot.tree, changed_files=snapshot.changes, implementation_result={
            **result, "changed_files": [entry["path"] for entry in snapshot.changes],
        })
        return ctx.task["implementation_result"]

    def stage_implement(self, ctx):
        return self.agent_implementation(ctx)

    def stage_fix(self, ctx):
        result = ctx.task["stage_result"]
        diagnostics = {"kind": ctx.task["failure_kind"], "message": ctx.task["error_message"], "result": result}
        if log := result.get("log"):
            path = safe_child(ctx.directory, log)
            diagnostics["log_tail"] = log_page(path, 0, 16_384, self.cfg.secrets, tail=True)["text"]
        ctx.save(retry_count=ctx.task["retry_count"] + 1)
        return self.agent_implementation(ctx, diagnostics)

    def stage_compile(self, ctx):
        before = ctx.workspace.git.snapshot(ctx, ctx.task)
        if not before.changes:
            raise StageError("no_changes", "No Git differences to validate")
        ctx.save(revision=before.tree, approved_revision=None, approval=None, changed_files=before.changes,
                 compile_report=None, test_report=None, review_result=None)
        result = ctx.workspace.unity.run(ctx, "compile")
        self.assert_same_revision(ctx, before.tree, unity=True)
        result["revision"] = before.tree
        ctx.save(compile_report=result)
        return result

    def assert_same_revision(self, ctx, revision, *, unity=False):
        if ctx.workspace.git.snapshot(ctx, ctx.task).tree != revision:
            kind = "unity_changed" if unity else "workspace_changed"
            raise StageError(kind, "Workspace changed during or after validation. Inspect the changes and recompile.")

    def stage_test(self, ctx):
        revision = ctx.task["compile_report"]["revision"]
        self.assert_same_revision(ctx, revision)
        selection = ctx.task.get("test_selection")
        try:
            validate_test_selection(selection, ctx.settings.test_assemblies)
        except ValueError as error:
            raise StageError("unsupported", str(error)) from error
        selected_modes = tuple(mode for mode in ctx.settings.test_modes if not selection or mode in selection)
        reports = {mode: {"status": "not_run", "reason": "No approved scope for this mode" if selection
                          else "Disabled in Worker configuration"}
                   for mode in ("editmode", "playmode") if mode not in selected_modes}
        ctx.save(test_report=reports)
        for mode in selected_modes:
            if selection:
                reports[mode] = report = {"status": "running", "revision": revision, "runs": []}
                for scope in selection[mode]:
                    try:
                        result = ctx.workspace.unity.run(ctx, mode, scope=scope)
                    except StageError as error:
                        report["runs"].append({"status": error.kind, "scope": scope, **error.result})
                        report["status"] = error.kind
                        ctx.save(test_report=reports)
                        raise
                    report["runs"].append({"status": "passed", "scope": scope, **result})
                    ctx.save(test_report=reports)
                    self.assert_same_revision(ctx, revision, unity=True)
                report["status"] = "passed"
                ctx.save(test_report=reports)
                continue
            try:
                reports[mode] = {"status": "passed", **ctx.workspace.unity.run(ctx, mode)}
            except StageError as error:
                reports[mode] = {"status": error.kind, **error.result}
                ctx.save(test_report=reports)
                raise
            ctx.save(test_report=reports)
            self.assert_same_revision(ctx, revision, unity=True)
        return reports

    def stage_review(self, ctx):
        task = ctx.task
        snapshot = ctx.workspace.git.snapshot(ctx, task, check_meta=True)
        if snapshot.tree != task["revision"]:
            raise StageError("workspace_changed", "Workspace changed after validation. Recompile before review.")
        diff = self.clean_output(ctx.workspace.git.diff(ctx, task, snapshot).decode("utf-8", errors="replace"))
        patch_file = ctx.artifact("change", ".diff", "diff")
        patch_file.write_text(diff, encoding="utf-8", newline="\n")
        inputs = {
            **self.brief(task), "revision": snapshot.tree, "changed_files": snapshot.changes, "diff": diff,
            "compile_report": task["compile_report"], "test_report": task["test_report"],
        }
        review_input = ctx.artifact("review-input", ".json", "review_input")
        review_input.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            review = ctx.workspace.agent.execute(ctx, REVIEW_RULES + "\n\n" + json.dumps(inputs, ensure_ascii=False), review=True)
        except StageError as error:
            ctx.save(review_result={"verdict": "blocked", "summary": str(error), "issues": [], **error.result})
            raise
        if review.get("session_id") and review["session_id"] == task["agent_session_id"]:
            raise StageError("review_blocked", "Review reused the implementation session")
        self.assert_same_revision(ctx, snapshot.tree)
        ctx.save(review_result=review)
        if review["verdict"] != "pass":
            kind = "review_changes" if review["verdict"] == "request_changes" else "review_blocked"
            raise StageError(kind, "Review did not pass", result=review)
        return review

    def stage_commit(self, ctx):
        task = ctx.task
        if not task["commit_sha"]:
            snapshot = ctx.workspace.git.snapshot(ctx, task, check_meta=True)
            if snapshot.tree != task["approved_revision"]:
                raise StageError("workspace_changed", "Reviewed files changed after approval. Revalidate and obtain a new approval.")
            commit = ctx.workspace.git.commit(ctx, task, snapshot, task["approval"]["commit_message"] or f"AI task {task['task_id']}")
            ctx.save(commit_sha=commit)
        ctx.workspace.git.push(ctx, task)
        delivery = {
            "task_branch": task["task_branch"], "base_branch": task["base_branch"], "commit_sha": task["commit_sha"],
            "remote": ctx.settings.git_remote, "pushed": True,
        }
        ctx.save(pushed=True, delivery=delivery)
        if task["approval"]["create_pr"]:
            try:
                delivery["pull_request"] = self.publish_pr(ctx)
            except StageError as error:
                delivery["pr_error"] = self.clean_output(str(error))
                ctx.save(pr_error=delivery["pr_error"])
        ctx.save(delivery=delivery)
        return delivery

    def publish_pr(self, ctx):
        ctx.check_cancelled()
        adapter = self.adapters[ctx.task["pool_id"]]
        if adapter is None:
            raise StageError("pr_failed", "The approved PR/MR adapter is unavailable")
        result = adapter.create(ctx.task)
        ctx.save(pull_request=result, pr_error=None)
        return result

    def report(self, ctx, error):
        task = ctx.task
        data = {key: value for key, value in task.items() if key not in {"remote_identity", "artifacts"}}
        if error:
            data["operation_error"] = {"kind": error.kind, "message": str(error), "result": error.result}
            if ctx.job["action"] not in {"recover", "pr"}:
                data.update(status=error.kind if error.kind in {"cancelled", "recovery_required"} else "failed",
                            failure_kind=error.kind, error_message=str(error), finished_at=now(), exit_code=error.exit_code)
        path = ctx.artifact("development-report", ".json", "report")
        path.write_text(json.dumps(self.clean_output(data), ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.save(report=path.name)
