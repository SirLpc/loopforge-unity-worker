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

    def test_polls_queued_to_running_with_separate_deadlines(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from integrations.loopforge import worker_client

        task = {"task_id": "task", "current_stage": "created", "artifacts": []}
        job = "job"
        clock = [0]

        def request(path, payload=None):
            self.assertIsNone(payload)
            if path.endswith("/" + job):
                status = "queued" if clock[0] < 6 else "running" if clock[0] < 9 else "succeeded"
                return {"job_id": job, "status": status}
            return task

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(worker_client.time, "monotonic", lambda: clock[0]), patch.object(worker_client.time, "sleep", sleep):
            actual_task, actual_job = worker_client.wait_job(SimpleNamespace(request=request), "/tasks/task", job, 4, False,
                                                            queue_wait_seconds=12)
        self.assertEqual(actual_task, task)
        self.assertEqual(actual_job["status"], "succeeded")
        self.assertEqual(clock[0], 9)

    def test_delivery_rejects_mismatched_pool_or_slot(self):
        from integrations.loopforge.worker_client import ClientError, check_delivery

        task = {"pool_id": "game", "slot_id": "worker-02", "revision": "r1", "approved_revision": "r1"}
        for other in ({"pool_id": "tools"}, {"slot_id": "worker-01"}):
            with self.subTest(other=other), self.assertRaises(ClientError):
                check_delivery(task, {**other, "stage_result": {"revision": "r1"}})
        check_delivery(task, {"stage_result": {"revision": "r1"}})

    def run_existing_plan(self, status, failure_kind=None):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge import worker_client

        with test_directory() as root:
            task_id = str(uuid4())
            job_id = str(uuid4())
            plan_path = root / "plan.json"
            request_id = str(uuid4())
            plan_path.write_text(json.dumps({"request_id": request_id, "requirement": "r", "plan": "p",
                                            "acceptance_criteria": ["x"], "pool_id": "untrusted"}), encoding="utf-8")
            stage = "reviewed" if status == "succeeded" else "created"
            task = {"task_id": task_id, "job_id": job_id, "current_stage": stage,
                    "pool_id": "game", "slot_id": "worker-02", "base_commit": "aa", "artifacts": []}
            job = {"job_id": job_id, "action": "run", "status": status, "failure_kind": failure_kind}
            received = []

            def request(path, payload=None):
                received.append((path, payload))
                if path == "/api/v1/tasks":
                    return {"task_id": task_id}
                if path in (f"/api/v1/tasks/{task_id}/run",):
                    self.fail("Existing worker job was submitted again")
                return job if "/jobs/" in path else task

            output = root / "task.json"
            with patch.object(worker_client, "Client", return_value=SimpleNamespace(request=request)):
                code = worker_client.main(["develop", "--input", str(plan_path), "--output", str(output),
                                           "--pool", "game", "--queue-wait-seconds", "5400"])
            artifact = json.loads(output.read_bytes())
            return code, received, artifact, task_id, job_id

    def test_reconnects_to_preupgrade_job_without_posting_again(self):
        code, calls, artifact, task_id, job_id = self.run_existing_plan("succeeded")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][1]["pool_id"], "game")
        self.assertEqual((artifact["task_id"], artifact["job_id"]), (task_id, job_id))
        self.assertEqual((artifact["slot_id"], artifact["base_commit"]), ("worker-02", "aa"))

    def test_queue_timeout_requires_new_attempt(self):
        code, _calls, artifact, _task_id, job_id = self.run_existing_plan("failed", "queue_timeout")
        self.assertEqual(code, 1)
        self.assertEqual(artifact["job_id"], job_id)
