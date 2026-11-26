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
