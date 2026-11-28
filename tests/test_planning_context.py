import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json

from integrations.loopforge.install_sops import Target, staged_feature_workflow
from integrations.loopforge.planning_context import (
    ContextError, run_context, sandbox_identity, signed_context, write_context,
)
from integrations.loopforge.worker_client import read_planning_context

PROJECT = "garden-worker"


class PlanningContextTests(unittest.TestCase):
    def run_state(self):
        return {"id": "run-890", "workflowId": PROJECT + "-ui", "status": "running", "input": "Exact input\n",
                "executionContext": {"projectId": PROJECT},
                "workflowSnapshot": {"projectId": PROJECT, "nodes": [
                    {"id": "worker-plan", "type": "script"}, {"id": "plan-approval", "type": "approval"},
                    {"id": "handoff", "type": "script"},
                ]}, "nodes": {"worker-plan": {"status": "running", "attempt": 2, "reworkCount": 1,
                                             "feedback": "Use a stronger check"}}}

    def test_context_contains_the_real_run_input_and_rework(self):
        state = self.run_state()
        result = run_context(state, "run-890", "plan", PROJECT)
        self.assertEqual(result, {"workflow_run_id": "run-890", "planning_profile": "ui", "requirement": "Exact input",
                                  "feedback": "Use a stronger check", "rework": 1, "attempt": 2})
        for changes in ({"workflowId": PROJECT + "-product"}, {"pauseRequested": True}, {"cancelRequested": True},
                        {"executionContext": {"projectId": "alternate-project"}}, {"id": "run-891"}):
            with self.subTest(changes=changes), self.assertRaises(ContextError):
                run_context({**state, **changes}, "run-890", "plan", PROJECT)

    def test_handoff_requires_current_approval_and_plan_hash(self):
        state = self.run_state()
        state["nodes"]["worker-plan"]["status"] = "succeeded"
        state["nodes"]["plan-approval"] = {"status": "succeeded", "attempt": 3}
        state["nodes"]["handoff"] = {"status": "running"}
        state["artifacts"] = [
            {"nodeId": "worker-plan", "attempt": 1, "name": "unity-planning", "sha256": "a" * 64},
            {"nodeId": "worker-plan", "attempt": 2, "name": "unity-planning", "sha256": "b" * 64},
            {"nodeId": "plan-approval", "attempt": 2, "name": "decision"},
        ]
        with self.assertRaises(ContextError):
            run_context(state, "run-890", "approve", PROJECT)
        state["artifacts"].append({"nodeId": "plan-approval", "attempt": 3, "name": "decision"})
        self.assertEqual(run_context(state, "run-890", "approve", PROJECT)["plan_sha256"], "b" * 64)

    def test_container_identity_requires_its_actual_project_runner_mount(self):
        hostname = "012345abcdef"
        workspace = Path("/operator/runner/workspaces")
        metadata = {"Id": hostname + "c" * 52, "Name": "/agentflow-rt-run-run-890-" + "a" * 12,
                    "Config": {"Hostname": hostname}, "State": {"Running": True}, "Mounts": [
                        {"Type": "bind", "Source": str(workspace / "runs" / "run-890" / "workspace"),
                         "Destination": "/workspace"}]}
        with patch("integrations.loopforge.planning_context.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps([metadata]))):
            self.assertEqual(sandbox_identity(hostname, workspace), ("run-890", "run-890"))
            for name, root in (("--invalid-arg", workspace), (hostname, workspace / "alternate")):
                with self.subTest(name=name, root=root), self.assertRaises(ContextError):
                    sandbox_identity(name, root)

    @unittest.skipUnless(sys.platform == "linux", "The Runner context relay requires Linux")
    def test_local_context_writes_atomically_to_the_real_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "runs" / "run-890" / "workspace"
            workspace.mkdir(parents=True)
            unrelated = root / "unrelated"
            unrelated.write_text("unchanged", encoding="utf-8")
            target = workspace / ".unity-worker-context.json"
            target.symlink_to(unrelated)
            record = signed_context("s" * 32, {"012345abcdef": {"operation": "plan"}}, 100)
            write_context(root, "run-890", record)
            self.assertFalse(target.is_symlink())
            self.assertEqual(json.loads(target.read_text()), record)
            with patch("integrations.loopforge.worker_client.time.time", return_value=101):
                self.assertEqual(read_planning_context(target, "plan", "s" * 32, "012345abcdef", 100)["operation"],
                                 "plan")
            self.assertEqual(unrelated.read_text(), "unchanged")
            workspace = root / "runs" / "run-891" / "workspace"
            workspace.parent.mkdir()
            workspace.symlink_to(target.parent, target_is_directory=True)
            with self.assertRaises(OSError):
                write_context(root, "run-891", {"replaced": True})
            self.assertEqual(json.loads(target.read_text()), record)

class FeatureContextTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        from uuid import uuid4
        from integrations.loopforge import planning_context

        self.relay = planning_context
        self.key = "relay-test-key-" * 3
        self.run_id = "run-890"
        self.task_id, self.test_job_id = str(uuid4()), str(uuid4())
        order = ("clarify", "design", "implement", "review", "test", "accept", "mr", "deliver")
        self.run = {
            "id": self.run_id, "workflowId": PROJECT + "-feature", "status": "running", "input": "Add a garden",
            "executionContext": {"projectId": PROJECT}, "workflowSnapshot": {
                "id": PROJECT + "-feature", "projectId": PROJECT,
                "nodes": [{"id": n, "type": "approval" if n == "accept" else "script"} for n in order],
                "edges": [{"source": a, "target": b, "when": "success"} for a, b in zip(order, order[1:])]},
            "nodes": {n: {"nodeId": n, "attempt": 3, "status": "succeeded", "reworkCount": int(n == "implement")}
                      for n in order}, "artifacts": []}
        self.run["nodes"]["mr"]["status"] = "running"
        self.run["nodes"]["deliver"]["status"] = "pending"
        self.bodies = {}
        parent = None
        for node, name in zip(order, ("unity-scope", "unity-planning", "unity-implementation", "unity-review",
                                      "unity-verification", "decision")):
            identifier = "artifact-94-" + node
            record = dict(node_id=node, node_attempt=3, workflow_project_id=PROJECT, workflow_id=PROJECT + "-feature",
                          workflow_run_id=self.run_id, task_id=self.task_id, pool_id="garden", slot_id="slot-2",
                          base_commit="b" * 40, parent_artifact_id=parent, job_id=str(uuid4()))
            if node != "clarify":
                record["plan_digest"] = "e" * 64
            if node in {"implement", "review", "test"}:
                record.update(revision="f" * 40, implementation_attempt=1)
            if node == "review":
                record["review_result"] = {"verdict": "pass", "revision": record["revision"]}
            if node == "test":
                record["job_id"] = self.test_job_id
                record["verification"] = {"revision": record["revision"], "test_job_id": self.test_job_id,
                                          "implementation_attempt": 1}
            body = (b"# Approval\n\nApproved by a human gate.\n\nReviewed tests\n" if node == "accept"
                    else json.dumps(record).encode())
            self.run["artifacts"].append(dict(id=identifier, runId=self.run_id, nodeId=node, attempt=3,
                                              name=name, sha256=hashlib.sha256(body).hexdigest()))
            self.bodies[identifier] = body
            parent = identifier

    def context(self):
        return self.relay.feature_context(self.run, self.run_id, PROJECT, self.bodies.__getitem__, self.key, 1000)

    def test_feature_signs_only_current_approval_and_test_artifact(self):
        from workflow_approval import verify_approval
        claims = verify_approval(self.context()["approval"], self.key, 1001)
        self.assertEqual((claims["accept_attempt"], claims["implementation_attempt"]), (3, 1))
        self.assertEqual((claims["test_job_id"], claims["test_artifact_id"], claims["approval_artifact_id"]),
                         (self.test_job_id, "artifact-94-test", "artifact-94-accept"))
        for field, replacement in (("attempt", 2), ("runId", "run-891"), ("sha256", "a" * 64)):
            with self.subTest(field=field), patch.dict(self.run["artifacts"][4], {field: replacement}):
                with self.assertRaises(ContextError):
                    self.context()
        with patch.dict(self.run["nodes"]["mr"], status="pending"), self.assertRaises(ContextError):
            self.context()

    def test_feature_approval_binds_the_checkout_in_the_current_test_artifact(self):
        import hashlib
        from workflow_approval import verify_approval

        checkout = {"branch": "ai/task-" + self.task_id.replace("-", ""), "commit_sha": "d" * 40}
        for node, key in (("implement", "compile_report"), ("review", "review_result"), ("test", "verification")):
            meta = next(a for a in self.run["artifacts"] if a["nodeId"] == node)
            record = json.loads(self.bodies[meta["id"]])
            record.setdefault(key, {})["checkout"] = checkout
            body = json.dumps(record).encode()
            self.bodies[meta["id"]] = body
            meta["sha256"] = hashlib.sha256(body).hexdigest()
        claims = verify_approval(self.context()["approval"], self.key, 1001)
        self.assertEqual(claims["test_digest"], meta["sha256"])
        self.assertEqual(claims["commit_sha"], checkout["commit_sha"])

        record["verification"]["checkout"] = {**checkout, "commit_sha": "c" * 40}
        body = json.dumps(record).encode()
        self.bodies[meta["id"]] = body
        meta["sha256"] = hashlib.sha256(body).hexdigest()
        with self.assertRaisesRegex(ContextError, "successful test job"):
            self.context()

    def test_direct_merge_signing_is_bound_to_the_frozen_graph(self):
        import hashlib
        from workflow_approval import verify_approval

        wf = self.run["workflowSnapshot"]
        self.assertNotIn("delivery_mode", self.context()["approval"]["claims"])
        self.run["nodes"]["merge"] = self.run["nodes"].pop("mr")
        wf["nodes"][-2]["id"] = "merge"
        wf["edges"][-2]["target"] = "merge"
        with self.assertRaises(ContextError):
            self.context()
        wf["edges"][-1]["source"] = "merge"
        with self.assertRaises(ContextError):
            self.context()  # Old evidence has no tested commit.
        checkout = {"branch": "ai/task-" + self.task_id.replace("-", ""), "commit_sha": "a" * 40}
        for node, key in (("implement", "compile_report"), ("review", "review_result"), ("test", "verification")):
            meta = next(a for a in self.run["artifacts"] if a["nodeId"] == node)
            record = json.loads(self.bodies[meta["id"]])
            record.setdefault(key, {})["checkout"] = checkout
            self.bodies[meta["id"]] = json.dumps(record).encode()
            meta["sha256"] = hashlib.sha256(self.bodies[meta["id"]]).hexdigest()
        ctx = self.context()
        self.assertEqual(ctx["operation"], "feature-merge")
        claims = verify_approval(ctx["approval"], self.key, 1001)
        self.assertEqual((claims["delivery_mode"], claims["commit_sha"], claims["test_digest"]),
                         ("direct-merge", checkout["commit_sha"], meta["sha256"]))
        self.assertEqual((claims["accept_attempt"], claims["test_job_id"]), (3, self.test_job_id))
        with patch.dict(wf["nodes"][-2], type="approval"), self.assertRaises(ContextError):
            self.context()
        decision = self.run["artifacts"][-1]
        manual = b"# Manual pass\n"
        with (patch.dict(self.bodies, {decision["id"]: manual}),
              patch.dict(decision, sha256=hashlib.sha256(manual).hexdigest()), self.assertRaises(ContextError)):
            self.context()
        with patch.dict(self.run["nodes"]["merge"], status="pending"), patch.dict(self.run["nodes"]["clarify"], status="running"):
            self.assertEqual(self.context()["delivery_mode"], "direct-merge")
        merged = {**record, "node_id": "merge", "job_id": "worker-merge", "parent_artifact_id": meta["id"],
                  "approval_artifact_id": decision["id"]}
        merge_body = json.dumps(merged).encode()
        self.bodies["merged"] = merge_body
        self.run["artifacts"].append({"id": "merged", "nodeId": "merge", "runId": self.run_id, "attempt": 3,
                                      "name": "unity-delivery", "sha256": hashlib.sha256(merge_body).hexdigest()})
        with patch.dict(self.run["nodes"]["merge"], status="succeeded"), patch.dict(self.run["nodes"]["deliver"], status="running"):
            summary = self.context()
            self.assertEqual((summary["upstream"]["id"], summary["approved_claims"]), ("merged", claims))
            self.assertNotIn("approval", summary)

    def test_feature_rejects_manual_pass_and_cross_assignment_or_old_round(self):
        import hashlib
        for node, values in (("accept", None), ("review", {"task_id": "ee"}), ("test", {"node_attempt": 2}),
                             ("test", {"implementation_attempt": 0}), ("design", {"parent_artifact_id": "old"})):
            meta = next(a for a in self.run["artifacts"] if a["nodeId"] == node)
            original = self.bodies[meta["id"]]
            body = (json.dumps({**json.loads(original), **values}).encode() if values else
                    b"# Manual pass\n\n# Approval\n\nApproved by a human gate.\n")
            with (self.subTest(node=node, values=values), patch.dict(self.bodies, {meta["id"]: body}),
                  patch.dict(meta, sha256=hashlib.sha256(body).hexdigest()), self.assertRaises(ContextError)):
                self.context()

    def test_reconstruct_clipped_rejection_from_scoped_telemetry_and_current_gate_context(self):
        import hashlib
        nodes = self.run["nodes"]
        nodes["mr"]["status"] = "pending"
        nodes["implement"]["status"] = "running"
        nodes["accept"]["status"] = "pending"
        reason = "修改树木" * 100
        note = "结尾需要再次核对。"
        full = reason + "\n\n人工补充上下文：\n" + note
        # Go JSON encoding replaces each byte of the trailing incomplete rune.
        clipped = "修改树木" * 66 + "修改" + "\ufffd\ufffd…"
        nodes["implement"]["feedback"] = clipped
        from urllib.parse import parse_qs, urlsplit
        nodes["accept"]["startedAt"] = "2026-11-26T13:00:00Z"
        nodes["implement"]["startedAt"] = "2026-11-26T13:10:00Z"
        self.run["telemetrySequence"] = 150
        self.run["events"] = []
        reject = {"runId": self.run_id, "nodeId": "accept", "name": "gate.rejected", "sequence": 65,
                  "category": "verification", "phase": "finish", "level": "info", "source": {}, "context": {},
                  "timestamp": "2026-11-26T13:05:00Z", "message": "打回到 implement：" + reason}
        retry = {**reject, "nodeId": "implement", "name": "node.retry_requested", "sequence": 68,
                 "timestamp": "2026-11-26T13:06:00Z", "message": "从指定节点重跑：Retry transport"}
        records = {"accept": [reject] + [{**reject, "name": "runner.log", "sequence": i} for i in range(80, 130)],
                   "implement": [{**retry, "sequence": 60}]}

        def read(path):
            url = urlsplit(path)
            if "/artifacts/" in url.path:
                return self.bodies[url.path.rsplit("/", 1)[1]]
            if url.path == "/api/runs/" + self.run_id:
                return self.run
            self.assertEqual(url.path, "/api/runs/" + self.run_id + "/telemetry")
            query = parse_qs(url.query)
            self.assertEqual(query["expanded"], ["true"])
            node, = query["nodeId"]
            before = int(query.get("before", ["151"])[0])
            limit = int(query["limit"][0])
            events = [e for e in records[node] if e["sequence"] < before]
            page = events[-limit:]
            return {"runId": self.run_id, "sequence": 150, "events": page, "hasMore": len(events) > limit,
                    "nextBefore": page[0]["sequence"] if page else 0, "nextAfter": page[-1]["sequence"] if page else 0}

        def published():
            with (patch.object(self.relay, "list_sandboxes", return_value=["012345abcdef"]),
                  patch.object(self.relay, "sandbox_identity", return_value=(self.run_id, self.run_id)),
                  patch.object(self.relay, "write_context") as write):
                self.relay.publish_workspace_contexts(Path("/local"), "s" * 32, read, PROJECT, self.key)
            return write.call_args.args[2]["payload"]["sandboxes"]["012345abcdef"]
        raw = ("# 人工补充上下文\n\n" + note + "\n").encode()
        self.bodies["extra-context"] = raw
        self.run["artifacts"].append({"id": "extra-context", "runId": self.run_id, "nodeId": "accept", "attempt": 3,
                                      "name": "human-context", "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(published()["feedback"], full)
        records["implement"].append(retry)
        nodes["implement"]["feedback"] = "Retry transport"
        self.assertEqual(published()["feedback"], full)
        records["implement"].pop()
        nodes["implement"]["feedback"] = clipped
        with patch.dict(reject, timestamp="2026-11-26T12:55:00Z"):
            self.assertTrue(published()["feedback_error"])
        reject["message"] = "打回到 implement：different"
        result = published()
        self.assertTrue(result["feedback_error"])
        self.assertFalse(result["feedback"])

    def test_malformed_review_blocks_only_its_run(self):
        import hashlib
        meta = self.run["artifacts"][3]
        body = json.loads(self.bodies[meta["id"]])
        for invalid in ("pass", ["pass"]):
            raw = json.dumps({**body, "review_result": invalid}).encode()
            with (self.subTest(invalid=invalid), patch.dict(self.bodies, {meta["id"]: raw}),
                  patch.dict(meta, sha256=hashlib.sha256(raw).hexdigest()), self.assertRaises(ContextError)):
                self.context()

    def test_auto_rework_uses_the_frozen_acceptance_target_for_current_review_only(self):
        self.check_auto_rework("review", "review_changes", {"verdict": "request_changes", "summary": "Dispose SerializedObject."},
                               "独立评审要求修改：Dispose SerializedObject.")

    def test_default_review_can_send_back_work_after_review_and_test_reworks(self):
        self.check_auto_rework("review", "review_changes", {"verdict": "request_changes", "summary": "Cover visible drag."},
                               "独立评审要求修改：Cover visible drag.", completed_reworks=3)

    def test_auto_rework_requests_fix_for_named_tests_on_the_current_revision(self):
        self.check_auto_rework("test", "tests_failed", {"mode": "playmode", "failed_tests": [
            {"name": "Game.DragTests.Drag", "message": "Expected rotation."}]},
            "测试验证未通过：Game.DragTests.Drag")

    def check_auto_rework(self, stage, failure, result, reason, *, completed_reworks=1):
        import hashlib
        from uuid import uuid4

        workflow = self.run["workflowSnapshot"]
        definitions = {node["id"]: node for node in workflow["nodes"]}
        target = "configured-repair"
        definitions["implement"]["id"] = target
        definitions["accept"]["rejectTo"] = target
        installed = staged_feature_workflow(Target(PROJECT, "sandbox", "https://worker.example.test"),
                                            "worker-key", "context-key", "garden")
        installed_check = next(node for node in installed["nodes"] if node["id"] == stage)
        definitions[stage].update(maxReworks=installed_check["maxReworks"], onFailure=installed_check["onFailure"])
        nodes = self.run["nodes"]
        nodes[target] = {**nodes.pop("implement"), "nodeId": target, "reworkCount": completed_reworks}
        nodes[stage]["status"] = "failed"
        self.run["status"] = "failed"
        artifact = self.run["artifacts"][2]
        evidence = json.loads(self.bodies[artifact["id"]])
        evidence.update(node_id=target, implementation_attempt=completed_reworks)
        raw = json.dumps(evidence).encode()
        self.bodies[artifact["id"]] = raw
        artifact.update(nodeId=target, sha256=hashlib.sha256(raw).hexdigest())

        job_id = str(uuid4())
        worker_task = {**evidence, "job_id": job_id, "rework_job_id": job_id, "workflow_mode": "staged-feature", "lease": True,
                       "status": "failed", "current_stage": stage, "failure_kind": failure}
        if stage == "review":
            result["revision"] = evidence["revision"]
            revision_evidence = result
        else:
            revision_evidence = {"status": failure, "revision": evidence["revision"]}
            worker_task["test_report"] = {result["mode"]: revision_evidence}
        job = {"task_id": self.task_id, "job_id": job_id, "action": stage, "status": "failed",
               "failure_kind": failure, "stage_result": result}
        task = {"run_id": self.run_id, "task_id": self.task_id, "job_id": job_id, "status": "failed",
                "job_status": "failed", "job_action": stage, "lease": True, "cancel_requested": False}
        path = "/api/runs/" + self.run_id
        writes = []

        def platform_call(request, payload=None):
            if request == path + "/reject":
                writes.append(payload)
                return {}
            return {path: self.run, path + "/wait?timeoutSec=0": {},
                    path + "/artifacts/" + artifact["id"]: raw}[request]

        def worker_call(request):
            return {"/api/v1/workflow-tasks?project_id=" + PROJECT: {"tasks": [task]},
                    "/api/v1/tasks/" + self.task_id + "?include_implementation_correction=true": worker_task,
                    "/api/v1/jobs/" + job_id: job}[request]

        self.relay.reconcile_worker_tasks(platform_call, worker_call, PROJECT)
        self.assertEqual(writes, [{"nodeId": stage, "targetId": target, "reason": reason,
                                  "expected": {"status": "failed", "attempt": 3, "reworkCount": 0}}])
        for config, changes in (
            (job, {"failure_kind": "timeout"}),
            (job, {"failure_kind": "test_execution_failed"}),
            (revision_evidence, {"revision": "d" * 40}),
            (worker_task, {"job_id": str(uuid4())}),
            (worker_task, {"rework_job_id": None}),
            (definitions[stage], {"maxReworks": 0}),
            (definitions[stage], {"maxReworks": completed_reworks}),
            (definitions["accept"], {"rejectTo": "design"}),
            (self.run, {"status": "running"}),
        ):
            with self.subTest(changes=changes), patch.dict(config, changes):
                self.relay.reconcile_worker_tasks(platform_call, worker_call, PROJECT)
                self.assertEqual(len(writes), 1)
        if stage == "test":
            with patch.dict(result, failed_tests=[{"name": "", "message": "Missing case identity"}]):
                self.relay.reconcile_worker_tasks(platform_call, worker_call, PROJECT)
                self.assertEqual(len(writes), 1)
