import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json

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

    def test_reconstruct_clipped_rejection_from_event_and_current_gate_context(self):
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
        self.run["events"] = [{"runId": self.run_id, "nodeId": "accept", "type": "gate.rejected",
                              "message": "打回到 implement：" + reason}]
        raw = ("# 人工补充上下文\n\n" + note + "\n").encode()
        self.bodies["extra-context"] = raw
        self.run["artifacts"].append({"id": "extra-context", "runId": self.run_id, "nodeId": "accept", "attempt": 3,
                                      "name": "human-context", "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(self.context()["feedback"], full)
        self.run["events"].append({"runId": self.run_id, "nodeId": "implement", "type": "node.retry_requested",
                                   "message": "从指定节点重跑：Retry transport"})
        nodes["implement"]["feedback"] = "Retry transport"
        self.assertEqual(self.context()["feedback"], full)
        self.run["events"].pop()
        nodes["implement"]["feedback"] = clipped
        self.run["events"][0]["message"] = "打回到 implement：different"
        result = self.context()
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
