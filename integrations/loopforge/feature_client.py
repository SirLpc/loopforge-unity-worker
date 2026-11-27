"""Staged feature actions for the standalone LoopForge Runner client."""

import argparse
import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

if "SOP_ARGS" in globals():
    api = sys.modules[__name__]
elif __package__:
    from . import worker_client as api
else:
    import worker_client as api


OUTPUTS = {
    "clarify": ("unity-scope", "requirement"), "design": ("unity-planning", "design"),
    "implement": ("unity-implementation", "implementation"), "review": ("unity-review", "review"),
    "test": ("unity-verification", "test-report"), "mr": ("unity-delivery", "mr-body"),
    "deliver": (None, "delivery"),
}
SCOPE = ("workflow_project_id", "workflow_id", "workflow_run_id")
ASSIGNMENT = ("task_id", "pool_id", "slot_id", "base_commit")


def require(condition, message):
    if not condition:
        raise api.ClientError(message)


def review_feedback(review):
    findings = [f"{issue['file']}:{issue['line']} — {issue['description']} {issue['suggestion']}"
                for issue in review["issues"]]
    return "\n".join([review["summary"], *findings])[:4000]


def mounted_evidence(ref):
    require(re.fullmatch(r"unity-[a-z]+", ref["name"]), "Invalid upstream artifact name")
    for path in Path(".agentflow/inputs").glob("**/" + ref["name"] + ".json"):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() == ref["sha256"]:
            value = json.loads(raw)
            require(isinstance(value, dict), "Invalid mounted feature evidence")
            return value
    raise api.ClientError("No mounted artifact matches the current platform digest")


def write_report(node, sections):
    title = {"clarify": "需求定型", "design": "方案设计", "implement": "开发实现", "review": "独立评审",
             "test": "测试验证", "mr": "发起 MR", "deliver": "交付汇总"}[node]
    lines = ["# " + title, ""]
    for label, value in sections.items():
        lines.extend(["## " + label, ""])
        if isinstance(value, str):
            lines.append(value)
        else:
            lines.extend(["```json", json.dumps(value, ensure_ascii=False, indent=2), "```"])
        lines.append("")
    path = Path(OUTPUTS[node][1] + ".md")
    tmp = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


class FeatureStage:
    def __init__(self, client, args, context):
        self.client = client
        self.args = args
        self.ctx = context
        self.node = context["node_id"]
        self.attempt = context["node_attempt"]
        self.ref = context.get("upstream")
        self.source = mounted_evidence(self.ref) if self.ref else {}
        expected = "feature-" + ("delivery" if self.node == "deliver" else self.node)
        require(expected == args.action and (self.node == "clarify") == (self.ref is None),
                "Signed context belongs to a different stage")
        if self.source:
            require(all(self.source.get(k) == context[k] for k in SCOPE) and self.source["pool_id"] == args.pool,
                    "Upstream evidence belongs to a different workflow or pool")
            task_id = self.source["task_id"]
        else:
            payload = {"workflow_mode": "staged-feature", "planning_profile": "feature", "pool_id": args.pool,
                       "publish_checkpoints": True,
                       "workflow_project_id": context["workflow_project_id"], "workflow_run_id": context["workflow_run_id"],
                       "requirement": context["requirement"],
                       "request_id": api.operation_id(context["workflow_run_id"], "feature-create", 0)}
            created = client.request("/api/v1/planning-tasks", payload)
            self.create_job = created["job_id"]
            task_id = created["task_id"]
        self.task_id = str(UUID(task_id))
        self.path = "/api/v1/tasks/" + self.task_id
        query = "?include_implementation_correction=true" if self.node == "implement" else ""
        self.task = client.request(self.path + query)
        self.check_task()
        self.scope = ":".join((self.task_id, self.node, self.ref["id"] if self.ref else "create",
                               str(context["rework"])))
        self.last_job = None

    def check_task(self):
        t = self.task
        require(t.get("task_id") == self.task_id and t.get("workflow_mode") == "staged-feature"
                and t.get("planning_profile") == "feature" and t.get("pool_id") == self.args.pool
                and all(t.get(k) == self.ctx[k] for k in ("workflow_run_id", "workflow_project_id")),
                "Worker assignment does not match this staged feature")
        if self.source:
            require(all(t.get(k) == self.source[k] for k in ASSIGNMENT), "Worker slot or Git baseline changed")

    def state_file(self, operation):
        return Path(".unity-feature-" + api.operation_id(self.scope, operation, 0) + ".json")

    def operation_receipt(self, operation):
        # The first client version used generation zero for the original admission.
        # New admissions use the platform attempt, which survives loss of the Runner workspace.
        for generation in range(self.attempt, -1, -1):
            request_id = api.operation_id(self.scope, operation, generation)
            receipt = self.client.request(self.path + "/operations/" + request_id)
            if receipt["job"]:
                break
        return generation, request_id, receipt

    def job(self, action, payload=None, *, version="", predecessor=None):
        """Use the Worker's admission record to join a phase or retry a failed attempt."""
        operation = action + ":" + version
        path = self.state_file(operation)
        actions = {"commit", "pr"} if action == "workflow-deliver" else {action}
        generation, request_id, receipt = self.operation_receipt(operation)
        job = receipt["job"]
        if job:
            require(job["task_id"] == self.task_id and job["action"] in actions, "Foreign operation receipt")
        self.task = self.client.request(self.path)
        self.check_task()
        current = self.task.get("job_id")
        if job and generation == 0:
            saved = api.read_json(path) if path.is_file() else {}
            if saved.get("request_id") == request_id:
                generation = saved["attempt"]
                require(type(generation) is int and 1 <= generation <= self.attempt, "Invalid legacy operation checkpoint")
            elif not receipt.get("recovered_by"):
                generation = self.attempt
                api.save_json(path, {"attempt": generation, "generation": 0, "request_id": request_id})
        if job and job["status"] in {"queued", "running"}:
            require(job["job_id"] == current, "Another Worker job is active")
        if job and generation < self.attempt:
            recovered = receipt.get("recovered_by")
            # A failed check that requires edits cannot rerun on the same implementation.
            needs_rework = (action, job.get("failure_kind")) in {
                ("review", "review_changes"), ("test", "tests_failed")}
            if (job["status"] == "failed" and current == job["job_id"] and not needs_rework
                    or job["status"] == "recovery_required" and recovered and current == recovered
                    and self.task["status"] == "failed" and self.task.get("failure_kind") == "recovered"):
                job = None
                predecessor = current
        if job is None:
            require(predecessor is None or current == predecessor, "Current Worker job differs from the upstream evidence")
            request_id = api.operation_id(self.scope, operation, self.attempt)
            api.save_json(path, {"attempt": self.attempt, "generation": self.attempt, "request_id": request_id})
            route = self.path + "/" + action + "?after_job=" + str(UUID(current))
            job = self.client.request(route, {**(payload or {}), "request_id": request_id})
            require(job["task_id"] == self.task_id and job["action"] in actions, "Worker admitted a different operation")
        job_id = str(UUID(job["job_id"]))
        try:
            self.task, job = api.wait_job(self.client, self.path, job_id, self.args.wait_seconds, self.args.follow,
                                         queue_wait_seconds=self.args.queue_wait_seconds)
        except (KeyboardInterrupt, api.Interrupted):
            current_task = self.client.request(self.path)
            running = self.client.request("/api/v1/jobs/" + job_id)
            if current_task.get("job_id") == job_id and running["status"] in {"queued", "running"}:
                self.client.request(self.path + "/cancel", {"request_id": api.operation_id(job_id, "cancel", 0)})
            raise api.ClientError("Node stopped while waiting for Worker job " + job_id) from None
        self.check_task()
        require(job["task_id"] == self.task_id and job["job_id"] == job_id and job["action"] in actions,
                "Worker polling returned a different operation")
        self.last_job = job
        if job["status"] == "succeeded":
            return job

        failure = {k: job.get(k) for k in ("job_id", "action", "status", "failure_kind", "error_message", "stage_result")}
        evidence = {"Worker 作业": failure, "Worker 产物": api.handoff(self.task, job)["artifact_reference"]}
        if self.task.get("delivery"):
            evidence["已记录的交付"] = self.task["delivery"]
        write_report(self.node, evidence)
        raise api.ClientError(f"Worker {action} job {job_id}: {job.get('failure_kind') or job['status']}")

    def current(self, stage):
        require(self.task["job_id"] == self.last_job["job_id"] and self.task["current_stage"] == stage,
                "A different Worker job or phase is current")

    def clarify(self):
        options = {"queue_wait_seconds": self.args.queue_wait_seconds} if self.args.queue_wait_seconds else {}
        self.job("clarify", options, predecessor=self.create_job)
        self.current("clarified")
        require(self.task["lease"], "No active clarification lease")
        return {"clarification": self.task["clarification"]}, {"需求": self.ctx["requirement"], "定型结果": self.task["clarification"]}

    def design(self):
        require(self.task["clarification"] == self.source["clarification"] and self.task["lease"],
                "Worker clarification differs from the current scope")
        self.job("plan", {"feedback": ""}, predecessor=self.source["job_id"])
        self.current("plan_ready")
        plan = api.planning_artifact(self.task)
        return plan, {"需求": plan["requirement_summary"], "方案": plan["plan"], "验收标准": plan["acceptance_criteria"],
                      "源码依据": plan["source_evidence"], "基线检查": plan["baseline"], "测试范围": plan["test_selection"]}

    def implement(self):
        plan = self.source
        api.check_planning_assignment(self.task, plan)
        require(self.task["lease"], "Worker no longer holds the planning lease")
        if not self.task.get("approved_plan_digest"):
            self.job("accept-plan", {k: plan[k] for k in ("slot_id", "base_commit", "plan_digest")},
                     predecessor=plan["job_id"])
            self.current("plan_accepted")
        require(self.task["approved_plan_digest"] == plan["plan_digest"], "Worker did not freeze the matched plan")
        worker_round, target = self.task["implementation_attempt"], self.ctx["rework"]
        require(worker_round <= target <= worker_round + 1, "Stale or skipped Worker rework round")
        edit = (self.task.get("implementation_correction") or {}) if worker_round == target else {}
        if edit:
            require(edit["implementation_attempt"] == target and edit["plan_digest"] == plan["plan_digest"],
                    "Correction does not belong to this frozen plan and round")
        failed = None
        failure = self.task.get("failure_kind")
        drift = failure in {"workspace_changed", "unity_changed"}
        if (worker_round == target and self.task["status"] == "failed"
                and (drift or failure in {"compile_failed", "tests_failed", "review_changes", "asset_metadata"})
                and self.task["current_stage"] in {"compile", "review", "test", "commit"}):
            failed = self.client.request("/api/v1/jobs/" + self.task["job_id"])
            require(failed["task_id"] == self.task_id and failed["job_id"] == self.task["job_id"]
                    and failed["action"] == self.task["current_stage"] and failed["status"] == "failed"
                    and failed.get("failure_kind") == failure, "Worker correction has no matching failed phase")
        if target:
            predecessor = (self.task.get("verification") or {}).get("test_job_id", "")
            if worker_round == target:
                feedback = self.task["rework_feedback"]
            elif self.task["status"] == "failed" and self.task["current_stage"] == "review" and failure == "review_changes":
                review = self.task.get("review_result") or {}
                revision = self.task["revision"]
                require(review.get("verdict") == "request_changes" and revision and review.get("revision") == revision,
                        "Worker lacks a change request for the current revision")
                feedback = review_feedback(review)
                predecessor = self.task["job_id"]
            elif self.task["status"] == "failed" and self.task["current_stage"] == "test" and failure == "tests_failed":
                result = self.task.get("stage_result") or {}
                report = (self.task.get("test_report") or {}).get(result.get("mode")) or {}
                cases = [case for case in result.get("failed_tests") or ()
                         if isinstance(case, dict) and isinstance(case.get("name"), str) and case["name"].strip()]
                require(self.task.get("rework_job_id") == self.task["job_id"]
                        and cases and report.get("status") == "tests_failed" and self.task["revision"]
                        and report.get("revision") == self.task["revision"],
                        "Worker lacks named failed tests for the current revision")
                feedback = "\n\n".join("\n".join(filter(None, (case["name"], case.get("message"), case.get("stack"))))
                                       for case in cases)[:4000]
                predecessor = self.task["job_id"]
            else:
                require(not self.ctx.get("feedback_error"), self.ctx.get("feedback_error"))
                feedback = self.ctx["feedback"]
            self.job("rework", {"workflow_run_id": self.ctx["workflow_run_id"], "implementation_attempt": target,
                                "feedback": feedback}, predecessor=predecessor)
        else:
            # A new initial edit follows the accepted plan. Existing edits use their admitted request.
            accepted = self.task["job_id"] if self.task["current_stage"] == "plan_accepted" else ""
            self.job("implement", predecessor=accepted)
        require(self.task["implementation_attempt"] == target, "Worker edited a different round")
        self.compile_edit(edit, failed)
        self.current("compiled")
        revision = self.task["revision"]
        require(revision and self.task["compile_report"]["revision"] == revision, "Compiled tree changed")
        fields = {"plan_digest": plan["plan_digest"], "revision": revision, "implementation_attempt": target,
                  "changed_files": self.task.get("changed_files", []), "compile_report": self.task["compile_report"]}
        return fields, {"修改": fields["changed_files"], "实现结果": self.task.get("implementation_result"),
                        "编译结果": fields["compile_report"]}

    def compile_edit(self, edit, failed):
        # Resolve both operations from deterministic receipts before adopting a correction from Worker history.
        if failed or edit.get("fix") or edit.get("recompile"):
            require(edit.get("edit_job_id") == self.last_job["job_id"], "Correction belongs to a different edit")
        if edit.get("fix"):
            self.job("fix", version=str(UUID(edit["fix"])), predecessor=edit["fix"])
        compile_version = edit.get("recompile") or self.last_job["job_id"]
        if failed:
            generation, _, receipt = self.operation_receipt("compile:" + compile_version)
            prior = receipt["job"]
            require(prior and prior["action"] == "compile" and prior["task_id"] == self.task_id
                    and prior["job_id"] == edit.get("compile_job_id"), "Failed phase belongs to a different compile")
            if failed["action"] == "compile":
                require(prior["job_id"] == failed["job_id"], "Worker compile changed before correction")
            else:
                require(prior["status"] == "succeeded", "Downstream phase lacks a successful scoped compile")
            if failed["action"] != "compile" or generation < self.attempt:
                if failed["failure_kind"] in {"workspace_changed", "unity_changed"}:
                    compile_version = failed["job_id"]
                else:
                    self.job("fix", version=failed["job_id"], predecessor=failed["job_id"])
                    compile_version = self.last_job["job_id"]
        self.job("compile", version=compile_version, predecessor=compile_version)

    def validated_tree(self):
        require(all(self.source.get(k) == self.task.get(k) for k in ("revision", "implementation_attempt"))
                and self.source["implementation_attempt"] == self.ctx["rework"]
                and self.task["approved_plan_digest"] == self.source["plan_digest"] and self.source["revision"],
                "Upstream evidence differs from the current Worker revision, round or frozen plan")
        return {k: self.source[k] for k in ("revision", "implementation_attempt", "plan_digest")}

    def review(self):
        fields = self.validated_tree()
        require(self.task["compile_report"] == self.source["compile_report"], "Worker compile evidence changed")
        self.job("review", predecessor=self.source["job_id"])
        self.current("reviewed")
        self.validated_tree()
        result = self.task["review_result"]
        require(result["revision"] == fields["revision"] and result["verdict"] == "pass"
                and result.get("checkout") == self.source["compile_report"].get("checkout"), "Review did not pass")
        return {**fields, "review_result": result}, {"独立评审": result}

    def test(self):
        fields = self.validated_tree()
        review = self.task["review_result"]
        require(review == self.source["review_result"] and review["verdict"] == "pass"
                and review["revision"] == fields["revision"], "Worker lacks the current passing review")
        self.job("test", predecessor=self.source["job_id"])
        self.current("tested")
        self.validated_tree()
        verification = self.task.get("verification")
        expected = {"revision": fields["revision"], "implementation_attempt": fields["implementation_attempt"],
                    "test_job_id": self.last_job["job_id"]}
        sections = {}
        if self.task.get("publish_checkpoints"):
            checkout = review.get("checkout")
            require(isinstance(checkout, dict) and checkout == self.task["compile_report"].get("checkout"),
                    "Missing checkout for the reviewed revision")
            expected["checkout"] = checkout
            sections["待验收版本"] = f"- 分支 `{checkout['branch']}`\n- 提交 SHA `{checkout['commit_sha']}`"
        require(verification == expected, "Missing successful current test verification")
        report = self.task["test_report"]
        sections.update({"验证记录": verification, "测试结果与范围": report})
        return {**fields, "verification": verification, "test_report": report}, sections

    def checked_tests(self):
        fields = self.validated_tree()
        verification = self.source["verification"]
        require(self.task.get("verification") == verification and self.task["test_report"] == self.source["test_report"],
                "Worker verification or tests differ from the current platform evidence")
        return {**fields, "verification": verification, "test_report": self.source["test_report"]}

    def mr(self):
        fields = self.checked_tests()
        approval = self.ctx["approval"]
        claims = approval["claims"]
        expected = {**{k: self.source[k] for k in (*ASSIGNMENT, "workflow_id", "workflow_run_id", "plan_digest",
                                                   "revision", "implementation_attempt")},
                    "project_id": self.ctx["workflow_project_id"], "test_job_id": self.source["job_id"],
                    "test_artifact_id": self.ref["id"], "test_digest": self.ref["sha256"]}
        checkout = fields["verification"].get("checkout")
        if checkout is not None:
            expected["commit_sha"] = checkout["commit_sha"]
        require(all(claims.get(k) == v for k, v in expected.items()), "Delivery approval differs from the tested assignment")
        previous = self.task.get("approval")
        if previous:
            require(previous.get("claims") == claims, "Worker is delivering a different approval")
        self.job("workflow-deliver", {"approval": approval}, version=claims["approval_artifact_id"],
                 predecessor=self.source["job_id"])
        self.current("delivered")
        self.checked_tests()
        require(self.task["status"] == "completed" and self.task["approval"]["claims"] == claims,
                "Worker did not finish this approved delivery")
        delivery = self.task["delivery"]
        require(delivery["pushed"] and delivery["commit_sha"] and delivery["mr_status"] in {"created", "pending"},
                "Worker lacks a durable delivery receipt")
        if checkout is not None:
            require(checkout == {"branch": delivery["task_branch"], "commit_sha": delivery["commit_sha"]},
                    "Delivered commit differs from the tested checkout")
        return {**fields, "delivery": delivery, "approval_artifact_id": claims["approval_artifact_id"]}, {"分支和 MR": delivery}

    def deliver(self):
        self.checked_tests()
        delivery = self.task["delivery"]
        recovered = (self.task["status"] == "failed" and self.task.get("failure_kind") == "recovered"
                     and self.task["lease"])
        require((self.task["status"] == "completed" or recovered) and self.task["current_stage"] == "delivered"
                and delivery == self.source["delivery"] and delivery["pushed"]
                and self.task["approval"]["claims"]["approval_artifact_id"] == self.source["approval_artifact_id"],
                "Worker delivery changed before release")
        self.job("release", predecessor=self.source["job_id"])
        self.current("delivered")
        self.checked_tests()
        require(self.task["status"] == "completed" and self.task["lease"] is False
                and self.task["delivery"] == delivery
                and self.task["approval"]["claims"]["approval_artifact_id"] == self.source["approval_artifact_id"]
                and self.last_job["stage_result"].get("released") is True,
                "Worker did not confirm release")
        return {}, {"验证记录": self.source["verification"], "测试结果与范围": self.source["test_report"],
                    "分支和 MR": delivery, "槽位归还": {"slot_id": self.task["slot_id"], "released": True,
                                                     "released_at": self.task.get("released_at")}}

    def run(self):
        fields, sections = getattr(self, self.node)()
        record = {**{k: self.ctx[k] for k in (*SCOPE, "node_id", "node_attempt")},
                  **{k: self.task[k] for k in ASSIGNMENT}, **fields, "job_id": self.last_job["job_id"],
                  "parent_artifact_id": self.ref["id"] if self.ref else None}
        references = api.handoff(self.task, self.last_job)["artifact_reference"]
        record["artifact_reference"] = references
        name = OUTPUTS[self.node][0]
        if name:
            api.save_json(name + ".json", record)
        write_report(self.node, {**sections, "Worker 任务": self.task_id, "Worker 作业": record["job_id"],
                                 "Worker 产物": references})
        return 0


def feature_main(argv):
    actions = ["feature-" + ("delivery" if n == "deliver" else n) for n in OUTPUTS]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--wait-seconds", type=int, default=int(os.environ.get("UNITY_WORKER_WAIT_SECONDS", "14400")))
    parser.add_argument("--queue-wait-seconds", type=int)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args(argv)
    if args.wait_seconds <= 0 or (args.queue_wait_seconds is not None and
                                  (args.action != "feature-clarify" or not 1 <= args.queue_wait_seconds <= 86400)):
        parser.error("Select a positive wait and a clarify queue timeout from 1 to 86400")
    def interrupt(signum, frame):
        raise api.Interrupted("Runner stopped the feature stage")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        context = api.planning_context(args.action)
        client = api.Client(os.environ.get("UNITY_WORKER_URL", ""), os.environ.get("UNITY_WORKER_TOKEN", ""))
        return FeatureStage(client, args, context).run()
    except (OSError, KeyError, TypeError, ValueError, api.ClientError) as error:
        print("Unity Worker: " + str(error), file=sys.stderr)
        return 1
