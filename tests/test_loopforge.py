import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4


class ScriptTests(unittest.TestCase):
    def test_handoff_selects_the_artifact_for_the_approved_plan(self):
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge.worker_client import input_json, save_json

        with test_directory() as root, patch.object(Path, "cwd", return_value=root):
            first = {"plan_request_id": "plan-1", "task_id": "task-1", "stage_result": {"revision": "r1"}}
            other = {"plan_request_id": "plan-2", "task_id": "task-2", "stage_result": {"revision": "r2"}}
            save_json(root / "first.json", first)
            save_json(root / "other.json", other)
            self.assertEqual(input_json("*.json", plan_request_id="plan-1"), first)

    def test_request_ids_are_stable_with_explicit_retry(self):
        from integrations.loopforge.worker_client import operation_id

        task = str(uuid4())
        self.assertEqual(operation_id(task, "run", 0), operation_id(task, "run", 0))
        self.assertNotEqual(operation_id(task, "run", 0), operation_id(task, "run", 1))
        self.assertNotEqual(operation_id(task, "run", 0), operation_id(task, "release", 0))

    def test_delivery_requires_the_same_approved_revision(self):
        from integrations.loopforge.worker_client import ClientError, check_delivery

        task = {"status": "awaiting_approval", "current_stage": "reviewed", "revision": "r1", "approved_revision": None}
        for approved, reference in ((None, "r1"), ("r1", "r2")):
            task["approved_revision"] = approved
            with self.subTest(approved=approved, reference=reference), self.assertRaises(ClientError):
                check_delivery(task, {"stage_result": {"revision": reference}})
        task["approved_revision"] = "r1"
        check_delivery(task, {"stage_result": {"revision": "r1"}})

    def test_rendered_inline_script_starts_without_file_globals(self):
        script = Path("integrations/loopforge/worker_client.py").resolve()
        rendered = subprocess.check_output([sys.executable, str(script), "render", "develop", "--help"])
        result = subprocess.run([sys.executable, "-"], input=rendered, capture_output=True, check=True)
        self.assertIn(b"--input", result.stdout)

    def test_export_uses_references_and_not_raw_artifacts(self):
        from integrations.loopforge.worker_client import handoff

        task = {
            "task_id": str(uuid4()), "job_id": str(uuid4()), "revision": "r1", "status": "awaiting_approval",
            "current_stage": "reviewed", "changed_files": [{"path": "a", "status": "M"}],
            "review_result": {"verdict": "pass"}, "plan": "private source",
            "artifacts": [{"name": "report.json", "kind": "report"}],
        }
        result = handoff(task, {})
        self.assertNotIn("plan", result)
        self.assertTrue(result["artifact_reference"][0].endswith("/report.json"))
        self.assertEqual(result["stage_result"]["revision"], "r1")
