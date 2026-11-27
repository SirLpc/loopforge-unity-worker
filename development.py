"""Persist and execute the fixed Unity development stages under one worker lease."""

import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

from artifacts import RunContext, log_page, redact_text, safe_child
from codex_executor import IMPLEMENT_RULES, REVIEW_RULES, STAGED_FEATURE_REVIEW_RULES, AgentExecutor, CodexExecutor
from git_workspace import GitWorkspace
from pool_scheduler import PoolScheduler, RequestConflict
from process_runner import ProcessRunner, StageError
from planning import assert_baseline, clarify_requirements, investigate
from pull_requests import HostedPullRequests
from task_store import Store, now, signature
from unity_executor import UnityExecutor, assert_unity_closed
from worker_config import Settings, check_slot, legacy_service, validate_test_selection
from workflow_approval import authenticate_approval, check_freshness
from workspace_storage import CacheName, clean_cache, cleanup_remnants, measure_directory, volume_usage


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
STAGED_FEATURE_NEXT_STAGE = {**NEXT_STAGE, "compiled": "review", "reviewed": "test", "tested": None}
FIXABLE = {"compile_failed", "tests_failed", "review_changes", "asset_metadata"}
TERMINAL = {"completed", "cancelled", "failed"}
RETRYABLE = {"git_failed", "timeout", "unity_changed", "workspace_in_use", "license_failed", "package_failed",
             "startup_failed", "test_execution_failed", "not_run", "unsupported", "review_blocked", "dirty_workspace"}
REWORK_RETRYABLE = {"git_failed", "timeout", "workspace_in_use", "dirty_workspace", "workspace_changed",
                    "unsupported", "recovered", "report_io_failed"}


class TaskNotFound(Exception):
    pass


class SlotNotFound(Exception):
    pass


def publishable(job):
    return {key: value for key, value in job.items() if key not in {"signature", "request_id"}}


def check_predecessor(task, after_job):
    if after_job is not None and task.get("workflow_mode") == "staged-feature" and task["job_id"] != str(after_job):
        raise RequestConflict("Worker advanced past the expected predecessor")


def plan_binding(task):
    return {"slot_id": task["slot_id"], "base_commit": task["base_commit"],
            "plan_digest": task["planning"]["plan_digest"]}


def freeze_plan(task):
    plan = task["planning"]
    task.update(status="pending", current_stage="plan_accepted", plan=plan["plan"],
                acceptance_criteria=plan["acceptance_criteria"], approved_plan_digest=plan["plan_digest"])
    if plan["test_selection"]:
        task["test_selection"] = plan["test_selection"]


def validation_reset(task):
    fields = dict(approved_revision=None, approval=None, compile_report=None, test_report=None, review_result=None)
    if task.get("workflow_mode") == "staged-feature":
        fields["verification"] = None
    return fields


def workflow_pr_outcome(task, error, timestamp):
    if error is None:
        return dict(status="completed", current_stage="delivered", stage_result=task["delivery"],
                    finished_at=timestamp, failure_kind=None, error_message=None, exit_code=0)
    if error.kind == "pr_failed":
        message = str(error)
        return dict(status="failed", finished_at=timestamp, failure_kind=error.kind, error_message=message,
                    exit_code=error.exit_code, pr_error=message,
                    delivery={**task["delivery"], "mr_status": "failed", "pr_error": message})
    return {}


def stage_finalizes_with_job(task, action):
    stage = task["current_stage"]
    return task.get("workflow_mode") == "staged-feature" and (
        (action == "rework" and stage == "fix") or (action in {"run", "test"} and stage == "test")
        or (action == stage and stage in {"compile", "review"})
    )


class DevelopmentWorker:
    def __init__(self, settings, runner=None, agent: AgentExecutor | None = None, pr_adapter=None, *, unavailable=None):
        self.cfg = legacy_service(settings) if isinstance(settings, Settings) else settings
        self.runner = runner or ProcessRunner()
        self.adapters = {pool.id: HostedPullRequests.from_pool_config(pool.pr) if pool.pr else
                         pr_adapter if pool.id == self.cfg.default_pool else None
                         for pool in self.cfg.pools.values()}
        delivery = {pool_id: [adapter.provider, adapter.api, adapter.repository]
                    for pool_id, adapter in self.adapters.items() if isinstance(adapter, HostedPullRequests)}
        self.store = Store.open_service(self.cfg, delivery=delivery)
        self.marker = safe_child(self.cfg.logs, "active.lock")
        self.guard = threading.RLock()
        self.active = {}
        self.maintenance = set()
        self.cleanup_remnants = {}
        self.stopping = False
        self.store.recover_interrupted()
        self.workspaces = {}
        rejected = dict(unavailable or {})
        self.scheduler = PoolScheduler(self, rejected)
        slots = self.cfg.slots
        for slot in slots.values():
            cfg = replace(slot.settings, sibling_checkouts=tuple(peer.settings.checkout for peer in slots.values()
                                                                  if peer.id != slot.id))
            self.workspaces[slot.id] = SimpleNamespace(git=GitWorkspace(cfg),
                                                        unity=UnityExecutor(cfg, permit=self.scheduler.unity_permit),
                                                        agent=agent or CodexExecutor(cfg), settings=cfg)
            if slot.id not in rejected:
                self.check_cleanup_remnants(slot.id, cfg.project)
            if not self.cfg.implicit and slot.id not in rejected:
                with self.store.connect() as db:
                    leased = self.store.slot_lease(db, slot.id) is not None
                if error := check_slot(slot, leased=leased):
                    rejected[slot.id] = error

    def start(self):
        self.scheduler.start()

    def check_cleanup_remnants(self, slot_id, project):
        try:
            remnants = cleanup_remnants(project)
            if not remnants:
                return
            reason = "cleanup_remnant"
        except (OSError, StageError):
            remnants = ()
            reason = "workspace_unavailable"
        with self.guard:
            self.scheduler.unavailable[slot_id] = reason
            if remnants:
                self.cleanup_remnants[slot_id] = [str(path) for path in remnants]

    def shutdown_when_idle(self):
        with self.guard:
            self.stopping = True
            return not self.active and not self.maintenance

    def close(self):
        with self.guard:
            self.stopping = True
            active = list(self.active.values())
            for _, cancel in active:
                cancel.set()
        self.scheduler.close()
        self.scheduler.wake_editors()
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

    def task(self, task_id, *, include_implementation_correction=False):
        with self.store.connect() as db:
            if include_implementation_correction:
                db.execute("BEGIN")
            task = self.require_task(db, task_id)
            if include_implementation_correction and task.get("workflow_mode") == "staged-feature":
                task["implementation_correction"] = self.implementation_correction(db, task)
                if task["status"] == "failed" and task.get("failure_kind") in {"review_changes", "tests_failed"}:
                    request = {"workflow_run_id": task["workflow_run_id"],
                               "implementation_attempt": task["implementation_attempt"] + 1, "feedback": ""}
                    try:
                        self.check_action(db, task, "rework", request, after_job=task["job_id"])
                    except RequestConflict:
                        pass
                    else:
                        task["rework_job_id"] = task["job_id"]
            return task

    def implementation_correction(self, db, task):
        correction = {"plan_digest": task.get("approved_plan_digest"),
                      "implementation_attempt": task["implementation_attempt"]}
        following = None
        for job_id, action, status, failure in self.store.staged_job_history(db, task["task_id"]):
            if action == "compile":
                correction.setdefault("compile_job_id", job_id)
            if action in {"implement", "rework"}:
                correction["edit_job_id"] = job_id
                break
            if action in {"run", "accept-plan"}:
                break
            if status == "failed" and "fix" not in correction:
                if following == "fix" and failure in FIXABLE:
                    correction["fix"] = job_id
                elif following == "compile" and failure in {"workspace_changed", "unity_changed"}:
                    correction.setdefault("recompile", job_id)
            following = action
        return correction

    def recovered_delivery_release(self, db, task):
        if (task.get("workflow_mode") != "staged-feature" or task["current_stage"] != "delivered"
                or task["status"] != "failed" or task.get("failure_kind") != "recovered"):
            return False
        confirmed = False
        for job_id, action, status, _ in self.store.staged_job_history(db, task["task_id"]):
            if action == "release":
                if status == "recovery_required" and self.store.recovery_after(db, task["task_id"], job_id):
                    confirmed = True
            elif action != "recover":
                return confirmed and action in {"commit", "pr"} and status == "succeeded"
        return False

    def job(self, job_id):
        with self.store.connect() as db:
            if job := self.store.job(db, job_id):
                return publishable(job)
        raise TaskNotFound

    def staged_operation(self, task_id, request_id):
        with self.store.connect() as db:
            task = self.require_task(db, task_id)
            if task.get("workflow_mode") != "staged-feature":
                raise TaskNotFound
            job = self.store.request(db, request_id)
            if not job or job["task_id"] != task_id:
                return {"job": None, "recovered_by": None}
            recovered = None
            if job["status"] == "recovery_required":
                recovered = self.store.recovery_after(db, task_id, job["job_id"])
            return {"job": publishable(job), "recovered_by": recovered}

    def pools(self):
        with self.store.connect() as db:
            return [self.scheduler.pool_status(db, pool) for pool in self.cfg.pools.values()]

    def storage_overview(self) -> dict:
        with self.guard:
            pools = self.pools()
            maintenance = self.maintenance.copy()
            remnants = self.cleanup_remnants.copy()
        slots = self.cfg.slots
        for pool in pools:
            for slot in pool["slots"]:
                if slot["id"] in maintenance:
                    slot["status"] = "maintenance"
                if slot["id"] in remnants:
                    slot["cleanup_remnants"] = remnants[slot["id"]]
                path = slots[slot["id"]].settings.checkout
                slot.update(path=str(path), volume=volume_usage(path))
        state = {"path": str(self.cfg.logs), "volume": volume_usage(self.cfg.logs)}
        return self.clean_output({"pools": pools, "state": state})

    def storage_measure(self, slot_id: str | None = None, cache: CacheName | None = None) -> dict:
        if slot_id is None:
            root, groups = self.cfg.logs, {}
        else:
            slot = self.cfg.slots.get(slot_id)
            if slot is None:
                raise SlotNotFound
            if cache is None:
                root = slot.settings.checkout
                groups = {name: slot.settings.project / name for name in get_args(CacheName)}
                groups[".git"] = root / ".git"
            else:
                root = slot.settings.project / cache
                groups = {cache: root}
        return self.clean_output({**measure_directory(root, groups), "volume": volume_usage(root)})

    def clean_storage(self, slot_id: str, cache: CacheName) -> dict:
        logger = logging.getLogger("uvicorn.error")
        with self.guard:
            workspace = self.workspaces.get(slot_id)
            if workspace is None:
                raise SlotNotFound
            label = self.clean_output(f"{slot_id}/{cache}")
            logger.info("Cache cleanup requested for %s", label)
            with self.store.connect() as db:
                if self.stopping or self.scheduler.slot_blocked(slot_id) or self.store.slot_lease(db, slot_id):
                    logger.info("Cache cleanup blocked for %s", label)
                    raise RequestConflict("Worker busy or blocked")
            self.maintenance.add(slot_id)
        try:
            with TemporaryDirectory(prefix="cache-probe-", dir=self.cfg.logs) as temporary:
                directory = Path(temporary)
                def run(argv, label, **kwargs):
                    return self.runner.run(argv, prefix=directory / label, **kwargs)
                result = clean_cache(workspace.git, SimpleNamespace(directory=directory, run=run), cache)
            logger.info("Cache cleanup %s for %s", result["status"], label)
            return self.clean_output(result)
        except StageError as error:
            logger.info("Cache cleanup failed for %s (%s)", label, error.kind)
            raise
        except OSError as error:
            logger.info("Cache cleanup failed for %s (filesystem error)", label)
            raise StageError("cleanup_failed", "Could not complete cache maintenance") from error
        finally:
            self.check_cleanup_remnants(slot_id, workspace.settings.project)
            with self.guard:
                self.maintenance.remove(slot_id)
                self.scheduler.wake.set()

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
                "current_stage": current.get("current_stage"),
                **({"capabilities": ["staged-feature"]} if self.cfg.legacy.workflow_approval_key and not legacy else {})}

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
        task = {
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
        if task.get("workflow_mode") == "staged-feature":
            task.update(implementation_attempt=0, verification=None)
        return task

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
            if not payload.get("planning_profile"):
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
                        or self.store.has_queued(db, self.cfg.default_pool)
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
        staged_feature = task.get("workflow_mode") == "staged-feature"
        if task.get("planning_profile") and not task.get("approved_plan_digest"):
            return None
        if stage == "plan_accepted":
            return "implement"
        if task["status"] == "failed":
            if failure == "queue_timeout" and stage == "created":
                return "prepare"
            if stage == "fix" and task.get("rework_pending"):
                return None
            if staged_feature and stage == "implement" and failure in {"git_failed", "timeout"}:
                return "implement"
            if staged_feature and stage in {"compile", "review", "test"} and failure == "report_io_failed":
                return stage
            if staged_feature and failure == "recovered" and stage in {"reviewed", "tested"} and not task.get("verification"):
                return "test"
            if failure in FIXABLE and task["retry_count"] < self.cfg.pools[task["pool_id"]].policy.max_fixes:
                return "fix"
            if failure == "unity_changed" and stage in {"compile", "test"}:
                return "compile"
            if stage in {"prepare", "compile", "test", "review"}:
                if failure in RETRYABLE or (staged_feature and failure == "recovered"):
                    return stage
            if stage in {"compile", "test", "review", "commit"} and failure == "workspace_changed" and not task["commit_sha"]:
                return "compile"
            return None
        return (STAGED_FEATURE_NEXT_STAGE if staged_feature else NEXT_STAGE).get(stage)

    def check_action(self, db, task, action, payload, *, after_job=None):
        status, stage = task["status"], task["current_stage"]
        staged_feature = task.get("workflow_mode") == "staged-feature"
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
        elif action == "clarify" and staged_feature:
            if (status in {"pending", "failed"} and stage in {"created", "clarify"}
                    and (task["lease"] or task["slot_id"] is None)):
                return
        elif action == "plan" and task.get("planning_profile") and not task.get("approved_plan_digest"):
            if staged_feature and not (task["lease"] and task.get("clarification")):
                raise RequestConflict("Clarify the requirement in the assigned workspace before planning")
            if (status in {"pending", "failed", "awaiting_plan_approval"}
                    and (task["lease"] or task["slot_id"] is None)):
                return
        elif action == "accept-plan" and status == "awaiting_plan_approval" and task["lease"]:
            expected = plan_binding(task)
            if expected != payload:
                raise RequestConflict("The plan, leased workspace or Git baseline has changed")
            return
        elif action == "release" and status in TERMINAL and task["lease"]:
            if self.store.has_task_permit(db, task["task_id"]):
                raise RequestConflict("Confirm the previous Unity process stopped before releasing the workspace")
            return
        elif action == "rework" and staged_feature and task["lease"] and not task["commit_sha"]:
            if payload["workflow_run_id"] != task["workflow_run_id"]:
                raise RequestConflict("Rework does not match the current workflow run")
            attempt = task.get("implementation_attempt", 0)
            if (status == "failed" and stage == "fix" and task.get("rework_pending")
                    and task["failure_kind"] in REWORK_RETRYABLE):
                if (payload["implementation_attempt"] != attempt or payload["feedback"] != task["rework_feedback"]):
                    raise RequestConflict("Use the same implementation attempt and feedback to resume rework")
                return
            review = task.get("review_result") or {}
            review_changes = (status == "failed" and stage == "review" and task.get("failure_kind") == "review_changes"
                              and review.get("verdict") == "request_changes" and task.get("revision")
                              and review.get("revision") == task["revision"])
            tests_failed = (status == "failed" and stage == "test" and task.get("failure_kind") == "tests_failed"
                            and review.get("verdict") == "pass" and task.get("revision")
                            and review.get("revision") == task["revision"])
            accepted = status == "awaiting_approval" and stage in {"tested", "approved"} and task.get("verification")
            if review_changes or tests_failed or accepted:
                if review_changes or tests_failed:
                    job = self.store.job(db, str(after_job)) if after_job is not None else None
                    verdict = (job or {}).get("stage_result") or {}
                    if (not job or job["task_id"] != task["task_id"] or job["action"] != stage
                            or job["status"] != "failed" or job.get("failure_kind") != task["failure_kind"]):
                        raise RequestConflict("Rework requires the current failed review or test job")
                    if review_changes and (verdict.get("verdict") != "request_changes"
                                           or verdict.get("revision") != task["revision"]):
                        raise RequestConflict("Rework requires a change request for the current revision")
                    if tests_failed:
                        report = (task.get("test_report") or {}).get(verdict.get("mode")) or {}
                        runs = report.get("runs") or []
                        tested = runs[-1] if runs else {}
                        cases = verdict.get("failed_tests") or ()
                        if (report.get("status") != "tests_failed" or report.get("revision") != task["revision"]
                                or tested.get("status") != "tests_failed" or not verdict.get("test_results")
                                or tested.get("test_results") != verdict["test_results"]
                                or tested.get("failed_tests") != cases
                                or not any(isinstance(case, dict) and isinstance(case.get("name"), str)
                                           and case["name"].strip() for case in cases)):
                            raise RequestConflict("Rework requires named failed tests for the current revision")
                if payload["implementation_attempt"] != attempt + 1:
                    raise RequestConflict("Rework does not match the next implementation attempt")
                if task["retry_count"] >= self.cfg.pools[task["pool_id"]].policy.max_fixes:
                    raise RequestConflict("The task has reached its correction limit")
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

    def workflow_delivery_action(self, db, task, claims):
        if task.get("workflow_mode") != "staged-feature" or task.get("planning_profile") != "feature":
            raise RequestConflict("Workflow delivery requires a staged feature")
        verification = task.get("verification") or {}
        expected = {
            "project_id": task["workflow_project_id"], "workflow_id": task["workflow_project_id"] + "-feature",
            "workflow_run_id": task["workflow_run_id"], "task_id": task["task_id"], "pool_id": task["pool_id"],
            "slot_id": task["slot_id"], "base_commit": task["base_commit"], "plan_digest": task.get("approved_plan_digest"),
            "revision": task["revision"], "implementation_attempt": task["implementation_attempt"],
            "test_job_id": verification.get("test_job_id"),
        }
        if (any(claims[key] != value for key, value in expected.items())
                or any(verification.get(key) != claims[key] for key in ("revision", "implementation_attempt"))
                or (task.get("planning") or {}).get("plan_digest") != claims["plan_digest"]):
            raise RequestConflict("Approval does not match the verified task, plan and assignment")
        tested = self.store.job(db, claims["test_job_id"])
        if (not tested or tested["task_id"] != task["task_id"] or tested["action"] not in {"run", "test"}
                or tested["status"] != "succeeded"):
            raise RequestConflict("Approval requires the current successful test job")
        slot = self.cfg.slots.get(task["slot_id"])
        lease = self.store.slot_lease(db, task["slot_id"])
        if (self.stopping or not slot or slot.pool_id != task["pool_id"] or not lease or lease["task_id"] != task["task_id"]
                or self.scheduler.slot_blocked(slot.id) or self.store.active_job(db, task["task_id"])):
            raise RequestConflict("The assigned workspace is unavailable or busy")
        stage, status = task["current_stage"], task["status"]
        if (stage, status) == ("tested", "awaiting_approval") and not task["commit_sha"]:
            return "commit"
        approval = task.get("approval") or {}
        if (approval.get("claims") != claims or task["approved_revision"] != claims["revision"]
                or status != "failed"):
            raise RequestConflict("Delivery is unavailable for this approval")
        failure = task.get("failure_kind")
        if task["pushed"] and task["commit_sha"] and failure in {"pr_failed", "recovered", "report_io_failed", "worker_error"}:
            return "pr" if approval["create_pr"] else "commit"
        if not task["pushed"] and stage in {"approved", "commit"}:
            if (failure in {"git_failed", "push_failed", "recovered", "push_ambiguous", "push_conflict"}
                    or (task["commit_sha"] and task.get("push_state") != "ready")):
                return "commit"
        raise RequestConflict("Delivery is unavailable at the current stage")

    def workflow_deliver(self, task_id, request_id, envelope, *, after_job=None):
        claims = authenticate_approval(envelope, self.cfg.legacy.workflow_approval_key)
        payload = {"workflow_approval": claims}
        with self.guard:
            with self.store.connect(write=True) as db:
                previous = self.store.request(db, request_id)
                if previous:
                    if (previous["action"] not in {"commit", "pr"} or previous["task_id"] != task_id
                            or previous["signature"] != signature(payload)):
                        raise RequestConflict("Request ID is already bound to a different operation")
                    return publishable(previous)
                check_freshness(envelope, time.time())
                task = self.require_task(db, task_id)
                check_predecessor(task, after_job)
                action = self.workflow_delivery_action(db, task, claims)
                job = self.new_job(task_id, request_id, action, payload)
                if not task.get("approval"):
                    task.update(current_stage="approved", approved_revision=claims["revision"],
                                approval={"claims": claims, "approved_at": now(), "commit_message": "",
                                          "create_pr": self.adapters.get(task["pool_id"]) is not None})
                task.update(job_id=job["job_id"], cancel_requested=False, finished_at=None)
                self.store.save_task(db, task)
                self.store.save_job(db, job)
            return self.spawn(job, {})

    def action(self, task_id, action, request_id, payload, *, after_job=None):
        with self.guard:
            with self.store.connect(write=True) as db:
                task = self.store.task(db, task_id)
                if task and task.get("workflow_mode") == "staged-feature" and action in {"approve", "commit", "pr"}:
                    raise RequestConflict("Staged features require LoopForge workflow delivery approval")
                if previous := self.previous(db, action, task_id, request_id, payload):
                    return publishable(previous)
                if task is None:
                    raise TaskNotFound
                check_predecessor(task, after_job)
                self.check_action(db, task, action, payload, after_job=after_job)
                job = self.new_job(task_id, request_id, action, payload)
                inline_accept = action == "accept-plan" and task.get("workflow_mode") != "staged-feature"
                if inline_accept or action in {"approve", "cancel"}:
                    active = self.store.active_job(db, task_id)
                    running = active and active["status"] == "running"
                    if inline_accept:
                        freeze_plan(task)
                    elif action == "approve":
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
                if action == "plan":
                    task.update(planning_feedback=payload["feedback"], planning=None,
                                plan_version=task.get("plan_version", 0) + 1)
                if action == "rework":
                    if not task.get("rework_pending"):
                        task.update(rework_pending=True, implementation_attempt=payload["implementation_attempt"],
                                    rework_feedback=payload["feedback"], retry_count=task["retry_count"] + 1)
                    task.update(**validation_reset(task), status="fixing", current_stage="fix", revision=None, changed_files=[],
                                implementation_result=None, agent_session_id=None, stage_result={}, failure_kind=None,
                                error_message=None, exit_code=None)
                self.store.save_task(db, task)
                self.store.save_job(db, job)
            if action == "cancel" and task_id in self.active:
                self.active[task_id][1].set()
                self.scheduler.wake_editors()
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
            elif action == "rework":
                result = self.run_stage(ctx, "fix")
            elif action == "clarify":
                result = self.build_clarification(ctx)
            elif action == "plan":
                result = self.build_plan(ctx)
            elif action == "accept-plan":
                assert_baseline(ctx)
                result = {"status": "pending", **plan_binding(task)}
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
        if ctx and action not in {"smoke", "release", "accept-plan"}:
            try:
                self.report(ctx, error)
            except Exception as exc:
                logging.getLogger(__name__).error("Cannot save job report %s (%s)", job["job_id"], type(exc).__name__)
                if error is None:
                    staged_delivery = ctx.task.get("workflow_mode") == "staged-feature" and action in {"commit", "pr"}
                    retry_report = staged_delivery or stage_finalizes_with_job(ctx.task, action)
                    kind = "report_io_failed" if isinstance(exc, OSError) and retry_report else "worker_error"
                    error = StageError(kind, "Cannot save the development report")
        with self.guard:
            try:
                cancellable = action == "accept-plan" or (ctx and stage_finalizes_with_job(ctx.task, action))
                if cancellable and cancel.is_set() and (error is None or error.kind != "recovery_required"):
                    error = StageError("cancelled", "Cancellation requested")
                self.finish(job, self.clean_output(result), error, time.monotonic() - started)
            finally:
                self.active.pop(job["task_id"], None)
                self.scheduler.wake.set()
                self.scheduler.wake_editors()

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
                preserve_task = (action in {"release", "recover", "pr"}
                                 or (action == "accept-plan" and kind != "cancelled"))
                if not preserve_task or kind == "recovery_required":
                    task.update(status=state, finished_at=timestamp, failure_kind=kind, error_message=message,
                                stage_result=job["stage_result"], exit_code=error.exit_code)
                if kind == "baseline_asset_metadata":
                    # The preflight runs before creating a task branch.
                    task.update(lease=False, released_at=timestamp)
                if action == "commit" and kind in {"workspace_changed", "dirty_workspace", "unsafe_change", "asset_metadata"}:
                    task.update(approved_revision=None, approval=None)
                if (task.get("workflow_mode") == "staged-feature" and kind in {"workspace_changed", "unity_changed"}
                        and task["current_stage"] in {"compile", "review", "test", "commit"}):
                    task.update(validation_reset(task))
            if action == "smoke" and task["status"] != "recovery_required":
                task.update(lease=False, status="failed" if error else "completed", finished_at=timestamp,
                            exit_code=error.exit_code if error else 0)
            if action == "release" and not error:
                if self.recovered_delivery_release(db, task):
                    task.update(status="completed", finished_at=timestamp, failure_kind=None, error_message=None,
                                stage_result=task["delivery"], exit_code=0)
                task.update(lease=False, released_at=timestamp)
            if action == "clarify" and not error:
                task.update(status="pending", current_stage="clarified", clarification=result,
                            failure_kind=None, error_message=None, exit_code=0)
            if action == "plan" and not error:
                task.update(status="awaiting_plan_approval", current_stage="plan_ready", planning=result,
                            failure_kind=None, error_message=None, exit_code=0)
            if action == "accept-plan" and not error:
                freeze_plan(task)
            if not error and stage_finalizes_with_job(task, action):
                running, finished = STAGES[task["current_stage"]]
                status = "awaiting_approval" if finished == "tested" else running
                task.update(status=status, current_stage=finished, stage_result=result,
                            failure_kind=None, error_message=None, exit_code=0)
                if finished == "tested":
                    task["verification"] = {"revision": task["revision"], "test_job_id": job["job_id"],
                                            "implementation_attempt": task.get("implementation_attempt", 0)}
                elif action == "rework":
                    task.pop("rework_pending", None)
            if action == "pr" and task.get("workflow_mode") == "staged-feature":
                task.update(self.clean_output(workflow_pr_outcome(task, error, timestamp)))
                if error and error.kind == "report_io_failed":
                    task.update(status="failed", failure_kind=error.kind, error_message=message,
                                finished_at=timestamp, exit_code=error.exit_code)
            if action == "recover" and not error:
                self.store.clear_recovered_permits(db, task["task_id"])
                task.update(status="failed", failure_kind="recovered", recovery=result, finished_at=timestamp)
            self.store.save_task(db, task)
            self.store.save_job(db, job)

    def run(self, ctx):
        action = self.next_stage(ctx.task)
        last_stage = "test" if ctx.task.get("workflow_mode") == "staged-feature" else "review"
        while action:
            try:
                result = self.run_stage(ctx, action)
            except StageError as error:
                if error.kind not in FIXABLE or ctx.task["retry_count"] >= ctx.settings.max_fixes:
                    raise
                ctx.save(status="failed", failure_kind=error.kind, error_message=str(error), stage_result=error.result)
                action = "fix"
                continue
            if action == last_stage:
                return result
            action = self.next_stage(ctx.task)
        raise StageError("worker_error", "No valid next stage")

    def run_stage(self, ctx, stage):
        ctx.check_cancelled()
        running, finished = STAGES[stage]
        ctx.save(status=running, current_stage=stage, finished_at=None)
        result = getattr(self, f"stage_{stage}")(ctx)
        if stage_finalizes_with_job(ctx.task, ctx.job["action"]):
            return result
        approval_stage = "test" if ctx.task.get("workflow_mode") == "staged-feature" else "review"
        status = "awaiting_approval" if stage == approval_stage else "completed" if stage == "commit" else running
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

    def build_clarification(self, ctx):
        ctx.save(status="clarifying", current_stage="clarify", finished_at=None)
        if not ctx.task["base_commit"]:
            self.stage_prepare(ctx)
        clarification = self.clean_output(clarify_requirements(ctx))
        path = ctx.artifact("unity-clarification", ".json", "clarification")
        path.write_text(json.dumps(clarification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return clarification

    def build_plan(self, ctx):
        ctx.save(status="planning", current_stage="plan", finished_at=None)
        if not ctx.task["base_commit"]:
            self.stage_prepare(ctx)
        plan = self.clean_output(investigate(ctx))
        plan["plan_digest"] = signature({**plan, "task_id": ctx.task["task_id"],
                                         "slot_id": ctx.task["slot_id"], "base_commit": ctx.task["base_commit"],
                                         "plan_version": ctx.task["plan_version"]})
        path = ctx.artifact("unity-plan", ".json", "planning")
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return plan

    @staticmethod
    def brief(task):
        briefing = {key: task[key] for key in ("requirement", "plan", "acceptance_criteria", "project_rules")}
        if scope := (task.get("planning") or {}).get("scope"):
            briefing["scope"] = scope
        return briefing

    def agent_implementation(self, ctx, diagnostics=None):
        ctx.workspace.git.checkout_is(ctx, ctx.task, ctx.task["base_commit"])
        ctx.save(**validation_reset(ctx.task), revision=None, agent_session_id=None)
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
        if ctx.task.get("planning_profile"):
            ctx.workspace.git.clean(ctx)
        return self.agent_implementation(ctx)

    def stage_fix(self, ctx):
        if ctx.job["action"] == "rework":
            return self.agent_implementation(ctx, {"kind": "rework_requested", "feedback": ctx.task["rework_feedback"]})
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
        ctx.save(**validation_reset(ctx.task), revision=before.tree, changed_files=before.changes)
        result = self.run_unity_for_revision(ctx, "compile", before.tree)
        result["revision"] = before.tree
        ctx.save(compile_report=result)
        return result

    def assert_same_revision(self, ctx, revision, *, unity=False):
        if ctx.workspace.git.snapshot(ctx, ctx.task).tree != revision:
            kind = "unity_changed" if unity else "workspace_changed"
            raise StageError(kind, "Workspace changed during or after validation. Inspect the changes and recompile.")

    def run_unity_for_revision(self, ctx, mode, revision, **kwargs):
        try:
            result = ctx.workspace.unity.run(ctx, mode, **kwargs)
        except StageError as error:
            if (ctx.task.get("workflow_mode") == "staged-feature"
                    and error.kind not in {"cancelled", "recovery_required", "workspace_in_use"}):
                try:
                    self.assert_same_revision(ctx, revision, unity=True)
                except StageError as blocker:
                    blocker.result["unity_failure"] = {
                        "failure_kind": error.kind, "error_message": str(error),
                        "exit_code": error.exit_code, "result": error.result,
                    }
                    raise blocker from error
            raise
        self.assert_same_revision(ctx, revision, unity=True)
        return result

    def stage_test(self, ctx):
        revision = ctx.task["compile_report"]["revision"]
        staged_feature = ctx.task.get("workflow_mode") == "staged-feature"
        if staged_feature:
            review = ctx.task.get("review_result") or {}
            if review.get("verdict") != "pass" or review.get("revision") != revision:
                raise StageError("workspace_changed", "Recompile and review the same revision before testing.")
        self.assert_same_revision(ctx, revision)
        selection = ctx.task.get("test_selection")
        if staged_feature and not selection:
            raise StageError("unsupported", "Staged features require an approved EditMode test scope")
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
                        result = self.run_unity_for_revision(ctx, mode, revision, scope=scope)
                    except StageError as error:
                        report["runs"].append({"status": error.kind, "scope": scope, **error.result})
                        report["status"] = error.kind
                        ctx.save(test_report=reports)
                        raise
                    report["runs"].append({"status": "passed", "scope": scope, **result})
                    ctx.save(test_report=reports)

                report["status"] = "passed"
                ctx.save(test_report=reports)
                continue
            try:
                reports[mode] = {"status": "passed", **self.run_unity_for_revision(ctx, mode, revision)}
            except StageError as error:
                reports[mode] = {"status": error.kind, **error.result}
                ctx.save(test_report=reports)
                raise
            ctx.save(test_report=reports)

        return reports

    def stage_review(self, ctx):
        task = ctx.task
        staged_feature = task.get("workflow_mode") == "staged-feature"
        snapshot = ctx.workspace.git.snapshot(ctx, task, check_meta=True)
        compiled_revision = (task.get("compile_report") or {}).get("revision")
        if snapshot.tree != task["revision"] or (staged_feature and snapshot.tree != compiled_revision):
            raise StageError("workspace_changed", "Workspace changed after validation. Recompile before review.")
        diff = self.clean_output(ctx.workspace.git.diff(ctx, task, snapshot).decode("utf-8", errors="replace"))
        patch_file = ctx.artifact("change", ".diff", "diff")
        patch_file.write_text(diff, encoding="utf-8", newline="\n")
        inputs = {
            **self.brief(task), "revision": snapshot.tree, "changed_files": snapshot.changes, "diff": diff,
            "compile_report": task["compile_report"], "test_report": task["test_report"],
        }
        rules = REVIEW_RULES
        if staged_feature:
            rules += "\n\n" + STAGED_FEATURE_REVIEW_RULES
            inputs.update(review_phase="before_tests", test_selection=task.get("test_selection"))
        review_input = ctx.artifact("review-input", ".json", "review_input")
        review_input.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            review = ctx.workspace.agent.execute(ctx, rules + "\n\n" + json.dumps(inputs, ensure_ascii=False), review=True)
        except StageError as error:
            ctx.save(review_result={"verdict": "blocked", "summary": str(error), "issues": [], **error.result})
            raise
        if review.get("session_id") and review["session_id"] == task["agent_session_id"]:
            raise StageError("review_blocked", "Review reused the implementation session")
        self.assert_same_revision(ctx, snapshot.tree)
        if staged_feature:
            review["revision"] = snapshot.tree
        ctx.save(review_result=review)
        if review["verdict"] != "pass":
            kind = "review_changes" if review["verdict"] == "request_changes" else "review_blocked"
            raise StageError(kind, "Review did not pass", result=review)
        return review

    def stage_commit(self, ctx):
        task = ctx.task
        staged = task.get("workflow_mode") == "staged-feature"
        if staged and task["pushed"]:
            return task["delivery"]
        if not task["commit_sha"]:
            snapshot = ctx.workspace.git.snapshot(ctx, task, check_meta=True)
            if snapshot.tree != task["approved_revision"]:
                raise StageError("workspace_changed", "Reviewed files changed after approval. Revalidate and obtain a new approval.")
            commit = ctx.workspace.git.commit(ctx, task, snapshot, task["approval"]["commit_message"] or f"AI task {task['task_id']}")
            ctx.save(commit_sha=commit, **({"push_state": "ready"} if staged else {}))
        if staged and task.get("push_state") != "ready":
            # A pre-journal staged task with a saved SHA has the same uncertainty as an interrupted push.
            tip = ctx.workspace.git.remote_task_tip(ctx, task)
            if tip is None:
                raise StageError("push_ambiguous", "A previous push may have completed. The task branch is absent. "
                                 "An operator must reconcile it against the recorded commit.")
            if tip != task["commit_sha"]:
                raise StageError("push_conflict", "The remote task branch references a different commit")
        else:
            ctx.workspace.git.push(ctx, task)
        delivery = {
            "task_branch": task["task_branch"], "base_branch": task["base_branch"], "commit_sha": task["commit_sha"],
            "remote": ctx.settings.git_remote, "pushed": True,
        }
        if staged:
            delivery["mr_status"] = "pending"
        ctx.save(pushed=True, delivery=delivery, **({"push_state": "confirmed"} if staged else {}))
        if task["approval"]["create_pr"]:
            try:
                delivery["pull_request"] = self.publish_pr(ctx)
                if staged:
                    delivery["mr_status"] = "created"
            except StageError as error:
                delivery["pr_error"] = self.clean_output(str(error))
                if staged:
                    delivery["mr_status"] = "failed"
                    ctx.save(pr_error=delivery["pr_error"], delivery=delivery)
                    raise
                ctx.save(pr_error=delivery["pr_error"])
        ctx.save(delivery=delivery)
        return delivery

    def publish_pr(self, ctx):
        ctx.check_cancelled()
        task = ctx.task
        staged = task.get("workflow_mode") == "staged-feature"
        if staged and task.get("pull_request"):
            return task["pull_request"]
        adapter = self.adapters[task["pool_id"]]
        if adapter is None:
            raise StageError("pr_failed", "The approved PR/MR adapter is unavailable")
        if staged:
            try:
                tip = ctx.workspace.git.remote_task_tip(ctx, task)
            except StageError as error:
                if error.kind not in {"timeout", "git_failed", "push_conflict"}:
                    raise
                raise StageError("pr_failed", "Could not verify the pushed task branch") from error
            if tip != task["commit_sha"]:
                raise StageError("pr_failed", "Task branch no longer references the approved pushed commit")
        result = adapter.create(ctx.task)
        fields = {"pull_request": result, "pr_error": None}
        if staged:
            delivery = dict(ctx.task["delivery"], pull_request=result, mr_status="created")
            delivery.pop("pr_error", None)
            fields["delivery"] = delivery
        ctx.save(**fields)
        return result

    def report(self, ctx, error):
        task = ctx.task
        data = {key: value for key, value in task.items() if key not in {"remote_identity", "artifacts"}}
        if ctx.job["action"] == "pr" and task.get("workflow_mode") == "staged-feature":
            data.update(workflow_pr_outcome(task, error, now()))
        if error:
            data["operation_error"] = {"kind": error.kind, "message": str(error), "result": error.result}
            if ctx.job["action"] not in {"recover", "pr"}:
                data.update(status=error.kind if error.kind in {"cancelled", "recovery_required"} else "failed",
                            failure_kind=error.kind, error_message=str(error), finished_at=now(), exit_code=error.exit_code)
                if error.kind == "baseline_asset_metadata":
                    data.update(lease=False, released_at=data["finished_at"])
        path = ctx.artifact("development-report", ".json", "report")
        path.write_text(json.dumps(self.clean_output(data), ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.save(report=path.name)
