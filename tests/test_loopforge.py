import os
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4


class ScriptTests(unittest.TestCase):
    def test_signed_local_context_rejects_stale_or_foreign_container_records(self):
        import hashlib
        import hmac
        import json
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge import worker_client

        key = "test-signing-key" * 3
        sandbox = "012345abcdef"
        stage = {"operation": "plan", "workflow_run_id": "run-123"}
        payload = {"issued_at": 101.5, "expires_at": 111.5, "sandboxes": {sandbox: stage}}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
        with test_directory() as root, patch.object(worker_client.time, "time", return_value=103):
            file = root / ".unity-worker-context.json"
            worker_client.save_json(file, {"payload": payload, "signature": signature})
            self.assertEqual(worker_client.read_planning_context(file, "plan", key, sandbox, 101), stage)
            for operation, secret, container, start in (("approve", key, sandbox, 101), ("plan", key, sandbox, 102),
                                                         ("plan", "another-secret", sandbox, 101)):
                with self.assertRaises(worker_client.ClientError):
                    worker_client.read_planning_context(file, operation, secret, container, start)
            with self.assertRaises(KeyError):
                worker_client.read_planning_context(file, "plan", key, "012345accdef", 101)
        with test_directory() as root, patch.object(worker_client.time, "time", return_value=113):
            file = root / ".unity-worker-context.json"
            worker_client.save_json(file, {"payload": payload, "signature": signature})
            with self.assertRaises(worker_client.ClientError):
                worker_client.read_planning_context(file, "plan", key, sandbox, 101)

    def test_planning_artifact_binds_persistent_worker_assignment_and_digest(self):
        from integrations.loopforge.worker_client import ClientError, planning_artifact, check_planning_assignment

        task = {"task_id": str(uuid4()), "pool_id": "project-pool", "slot_id": "slot-many",
                "workflow_run_id": "run-12345", "base_commit": "a" * 40,
                "planning": {"plan_digest": "b" * 64, "plan": "Document the baseline",
                             "requirement_summary": "Write a guide", "acceptance_criteria": ["Guide exists"],
                             "test_selection": {"editmode": []}, "source_evidence": [], "baseline": []}}
        plan = planning_artifact(task)
        self.assertEqual(plan["request_id"], planning_artifact(task)["request_id"])
        self.assertEqual(plan["slot_id"], "slot-many")
        self.assertEqual(plan["plan_digest"], "b" * 64)
        self.assertEqual(plan["workflow_run_id"], "run-12345")
        check_planning_assignment(task, plan)
        for key in ("task_id", "pool_id", "slot_id", "base_commit", "workflow_run_id", "plan_digest", "plan"):
            with self.subTest(key=key), self.assertRaises(ClientError):
                check_planning_assignment(task, {**plan, key: "wrong"})

    def test_accept_planning_requires_the_exact_approved_artifact(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import patch, Mock
        from support import test_directory
        from integrations.loopforge import worker_client

        task_id = str(uuid4())
        task = {"task_id": task_id, "pool_id": "arbitrary-pool", "slot_id": "available-sandbox",
                "workflow_run_id": "run-890", "base_commit": "a" * 40,
                "planning": {"plan_digest": "b" * 64, "plan": "Use repository evidence",
                             "requirement_summary": "Plan a task", "acceptance_criteria": ["Done"],
                             "test_selection": {"playmode": []}, "source_evidence": [], "baseline": []}}
        plan = worker_client.planning_artifact(task)
        with test_directory() as root:
            args = SimpleNamespace(input=root / "plan.json", output=root / "accepted.json")
            worker_client.save_json(args.input, plan)
            approved = {"workflow_run_id": "run-890", "plan_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest()}
            with patch.object(worker_client, "planning_context", return_value=approved):
                client = Mock()
                client.request.side_effect = [task, {"status": "succeeded"}]
                self.assertEqual(worker_client.accept_planning(client, args), 0)
                self.assertEqual(worker_client.read_json(args.output), plan)
                call = client.request.call_args.args
                self.assertEqual(call[0], f"/api/v1/tasks/{task_id}/accept-plan")
                self.assertEqual(call[1]["slot_id"], task["slot_id"])
                self.assertEqual(call[1]["base_commit"], task["base_commit"])
                self.assertEqual(call[1]["plan_digest"], "b" * 64)
                client.reset_mock()
                worker_client.save_json(args.input, {**plan, "plan": "Replaced after approval"})
                with self.assertRaises(worker_client.ClientError):
                    worker_client.accept_planning(client, args)
                client.request.assert_not_called()

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
        environment = {**os.environ, "PYTHONIOENCODING": "cp936"}
        rendered = subprocess.check_output([sys.executable, str(script), "render", "develop", "--help"], env=environment)
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

class FeatureScriptTests(unittest.TestCase):
    def test_staged_review_reuses_rejected_job_without_resubmission(self):
        import json
        from contextlib import chdir
        from types import SimpleNamespace
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge import feature_client, worker_client

        task_id, review_id, compile_id = (str(uuid4()) for _ in range(3))
        context = {"workflow_project_id": "garden", "workflow_id": "garden-feature",
                   "workflow_run_id": "run-123", "node_id": "review", "node_attempt": 2, "rework": 0,
                   "upstream": {"id": "compile-artifact"}}
        revision = "b" * 40
        evidence = {**{key: context[key] for key in feature_client.SCOPE},
                    "task_id": task_id, "pool_id": "pool", "slot_id": "slot", "base_commit": "a" * 40,
                    "implementation_attempt": 0, "plan_digest": "c" * 64, "revision": revision,
                    "compile_report": {"revision": revision}, "job_id": compile_id}
        task = {**evidence, "workflow_mode": "staged-feature", "planning_profile": "feature",
                "status": "failed", "current_stage": "review", "job_id": review_id,
                "failure_kind": "review_changes", "approved_plan_digest": evidence["plan_digest"], "artifacts": []}
        job = {"task_id": task_id, "job_id": review_id, "action": "review", "status": "failed",
               "failure_kind": "review_changes", "error_message": "Review did not pass",
               "stage_result": {"verdict": "request_changes", "summary": "Address a concrete defect."}}
        scope = f"{task_id}:review:compile-artifact:0"
        original = worker_client.operation_id(scope, "review:", 1)

        def request(path, payload=None):
            if payload is not None:
                self.fail(f"Rejected review was resubmitted: {path}")
            if "/operations/" in path:
                return {"job": job if path.endswith("/" + original) else None, "recovered_by": None}
            if path == f"/api/v1/jobs/{review_id}":
                return job
            self.assertEqual(path, f"/api/v1/tasks/{task_id}")
            return task

        args = SimpleNamespace(action="feature-review", pool="pool", wait_seconds=3, queue_wait_seconds=None,
                               follow=False)
        with test_directory() as root, chdir(root), patch.object(feature_client, "mounted_evidence", return_value=evidence):
            with self.assertRaisesRegex(worker_client.ClientError, "review_changes"):
                feature_client.FeatureStage(SimpleNamespace(request=request), args, context).review()
            report = Path("review.md").read_text(encoding="utf-8")
            result = json.loads(report.split("```json\n", 1)[1].split("\n```", 1)[0])
            self.assertEqual(result["job_id"], review_id)
            self.assertEqual(result["stage_result"], job["stage_result"])
            self.assertFalse(Path("unity-review.json").exists())

    def test_feature_node_reconnect_uses_existing_job(self):
        import json
        from contextlib import chdir
        from types import SimpleNamespace
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge import worker_client

        task_id, test_id = str(uuid4()), str(uuid4())
        base = {"workflow_project_id": "garden", "workflow_run_id": "run-123", "workflow_id": "garden-feature",
                "task_id": task_id, "slot_id": "one", "pool_id": "pool", "base_commit": "a" * 40,
                "plan_digest": "c" * 64, "revision": "b" * 40, "implementation_attempt": 0}
        verification = {"revision": base["revision"], "test_job_id": test_id, "implementation_attempt": 0}
        report = {"editmode": {"status": "passed", "runs": [{"passed": 8}]},
                  "playmode": {"status": "not_run", "reason": "No approved scope for this mode"}}
        task = {**base, "workflow_mode": "staged-feature", "planning_profile": "feature", "status": "awaiting_approval",
                "current_stage": "tested", "approved_plan_digest": base["plan_digest"], "lease": True,
                "verification": verification, "test_report": report, "artifacts": [], "job_id": test_id}
        claims = {"project_id": "garden", **{k: base[k] for k in base if k != "workflow_project_id"},
                  "test_job_id": test_id, "accept_attempt": 1, "approval_artifact_id": "accept-1",
                  "approval_digest": "a" * 64, "test_artifact_id": "test-1"}
        requests, jobs, side_effects = {}, {}, []
        recovered = {}
        deny_new_operation = [False]
        finishing = []
        finishing_task = {}
        receipt = {"pushed": True, "mr_status": "failed", "commit_sha": "f" * 40, "task_branch": "task-1",
                   "base_branch": "main", "remote": "origin"}
        def request(path, payload=None):
            if payload is None:
                if "/operations/" in path:
                    admitted = requests.get(path.rsplit("/", 1)[-1])
                    return {"job": jobs.get(admitted), "recovered_by": recovered.get(admitted)}
                if "/jobs/" in path:
                    result = jobs[path.rsplit("/", 1)[-1]]
                    if finishing and result["job_id"] == finishing[-1]:
                        finishing.pop()
                        task.update(finishing_task)
                        finishing_task.clear()
                        return {**result, "status": "running"}
                    return result
                return dict(task)
            path, _, check = path.partition("?after_job=")
            action = path.rsplit("/", 1)[-1]
            if action != "planning-tasks":
                self.assertEqual(check, task["job_id"])
            if payload["request_id"] in requests:
                return jobs[requests[payload["request_id"]]]
            self.assertFalse(deny_new_operation[0], "A completed operation was admitted again after reconnect")
            if action == "workflow-deliver":
                self.assertEqual(set(payload), {"request_id", "approval"})
                self.assertEqual(payload["approval"]["claims"], claims)
                action = "pr" if task.get("delivery", {}).get("pushed") else "commit"
            else:
                self.assertEqual(action, "release")
            side_effects.append(action)
            job_id = str(uuid4())
            if action == "commit":
                task.update(delivery=dict(receipt), status="failed", current_stage="commit", failure_kind="pr_failed",
                            pushed=True, approved_revision=base["revision"], approval={"claims": claims})
            if action == "pr":
                receipt.update(mr_status="created", pull_request={"url": "https://example.invalid/mr/3"})
                task.update(delivery=dict(receipt), status="completed", current_stage="delivered", failure_kind=None)
            if action == "release":
                task.update(lease=False, released_at="2026-11-26", status="completed", failure_kind=None)
            task["job_id"] = job_id
            jobs[job_id] = {"task_id": task_id, "job_id": job_id, "action": action,
                            "status": "failed" if action == "commit" else "succeeded",
                            "failure_kind": "pr_failed" if action == "commit" else None,
                            "stage_result": {"released": True} if action == "release" else dict(receipt)}
            requests[payload["request_id"]] = job_id
            return jobs[job_id]

        with test_directory() as root, chdir(root):
            import hashlib
            mounted = Path(".agentflow/inputs/test")
            mounted.mkdir(parents=True)
            src = mounted / "unity-verification.json"
            record = {**base, "node_id": "test", "node_attempt": 1, "job_id": test_id, "verification": verification,
                      "test_report": report, "parent_artifact_id": "review-1"}
            src.write_text(json.dumps(record))
            claims["test_digest"] = hashlib.sha256(src.read_bytes()).hexdigest()
            ctx = {**{k: base[k] for k in ("workflow_project_id", "workflow_id", "workflow_run_id")},
                   "node_id": "mr", "node_attempt": 1, "rework": 0, "feedback": "",
                   "upstream": {"id": "test-1", "name": "unity-verification", "sha256": claims["test_digest"]},
                   "approval": {"claims": claims, "issued_at": 1000, "signature": "s" * 64}}
            with (patch.object(worker_client, "planning_context", return_value=ctx),
                  patch.object(worker_client, "Client", return_value=SimpleNamespace(request=request)),
                  patch.object(worker_client.time, "sleep")):
                args = ["feature-mr", "--pool", "pool"]
                self.assertEqual(worker_client.main(args), 1)
                self.assertEqual(worker_client.main(args), 1)
                interrupted_job = task["job_id"]
                jobs[interrupted_job].update(status="recovery_required", failure_kind="recovery_required")
                task.update(status="recovery_required", failure_kind="recovery_required")
                ctx["node_attempt"] = 2
                self.assertEqual(worker_client.main(args), 1)
                self.assertEqual(side_effects, ["commit"])
                recovery = str(uuid4())
                recovered[interrupted_job] = recovery
                jobs[recovery] = {"job_id": recovery, "task_id": task_id, "action": "recover", "status": "succeeded",
                                  "stage_result": {"recovered": True}}
                task.update(job_id=recovery, status="failed", failure_kind="recovered")
                ctx["node_attempt"] = 3
                self.assertEqual(worker_client.main(args), 0)
                self.assertEqual(worker_client.main(args), 0)
                for checkpoint in Path(".").glob(".unity-feature-*.json"):
                    checkpoint.unlink()
                deny_new_operation[0] = True
                finishing.append(task["job_id"])
                finishing_task.update(status="completed", current_stage="delivered")
                task.update(status="failed", current_stage="commit")
                ctx["node_attempt"] = 4
                self.assertEqual(worker_client.main(args), 0)
                deny_new_operation[0] = False
                self.assertEqual(side_effects, ["commit", "pr"])
                src = Path("unity-delivery.json")
                final = json.loads(src.read_bytes())
                self.assertEqual(final["delivery"]["commit_sha"], "f" * 40)
                (mounted / src.name).write_bytes(src.read_bytes())
                ctx.update(node_id="deliver", node_attempt=1, upstream={"id": "mr-2", "name": "unity-delivery",
                           "sha256": hashlib.sha256(src.read_bytes()).hexdigest()})
                ctx.pop("approval")
                for _ in range(2):
                    self.assertEqual(worker_client.main(["feature-delivery", "--pool", "pool"]), 0)
                for checkpoint in Path(".").glob(".unity-feature-*.json"):
                    checkpoint.unlink()
                deny_new_operation[0] = True
                finishing.append(task["job_id"])
                finishing_task.update(lease=False)
                task["lease"] = True
                ctx["node_attempt"] = 2
                self.assertEqual(worker_client.main(["feature-delivery", "--pool", "pool"]), 0)
                self.assertEqual(side_effects, ["commit", "pr", "release"])
                release = task["job_id"]
                jobs[release].update(status="recovery_required", failure_kind="recovery_required", stage_result={})
                task.update(lease=True, status="recovery_required", failure_kind="recovery_required")
                ctx["node_attempt"] = 3
                self.assertEqual(worker_client.main(["feature-delivery", "--pool", "pool"]), 1)
                recovery = str(uuid4())
                recovered[release] = recovery
                jobs[recovery] = {"job_id": recovery, "task_id": task_id, "action": "recover", "status": "succeeded",
                                  "stage_result": {"recovered": True}}
                task.update(job_id=recovery, status="failed", failure_kind="recovered")
                ctx["upstream"]["id"] = "foreign-mr"
                self.assertEqual(worker_client.main(["feature-delivery", "--pool", "pool"]), 1)
                ctx["upstream"]["id"] = "mr-2"
                deny_new_operation[0] = False
                self.assertEqual(worker_client.main(["feature-delivery", "--pool", "pool"]), 0)
                self.assertEqual(side_effects, ["commit", "pr", "release", "release"])
                self.assertFalse(task["lease"])
                self.assertEqual(task["status"], "completed")
                self.assertIn("8", Path("delivery.md").read_text(encoding="utf-8"))
                self.assertIn("not_run", Path("delivery.md").read_text(encoding="utf-8"))

    def test_feature_rendered_client_starts_standalone(self):
        import tempfile
        script = Path("integrations/loopforge/worker_client.py").resolve()
        rendered = subprocess.check_output([sys.executable, str(script), "render", "feature-review", "--help"])
        with tempfile.TemporaryDirectory() as other:
            result = subprocess.run([sys.executable, "-I", "-"], input=rendered, capture_output=True, cwd=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"feature-review", result.stdout)


    def test_feature_stages_freeze_then_compile_review_test_and_resume_reserved_rework(self):
        import hashlib
        import json
        from contextlib import chdir
        from types import SimpleNamespace
        from unittest.mock import patch
        from support import test_directory
        from integrations.loopforge import worker_client

        task_id = str(uuid4())
        scope = {"goal": "Grow", "scope": "Garden", "acceptance_criteria": ["Trees grow"]}
        planning = {"plan_digest": "a" * 64, "plan": "Use Garden.cs", "requirement_summary": "Grow",
                    "acceptance_criteria": ["Trees grow"], "test_selection": {"editmode": ["Trees"]},
                    "source_evidence": [], "baseline": []}
        task = {"task_id": task_id, "current_stage": "created", "status": "pending", "lease": False,
                "implementation_attempt": 0, "artifacts": [], "revision": None, "approved_plan_digest": None}
        ctx = {"workflow_run_id": "run-90", "workflow_project_id": "garden", "workflow_id": "garden-feature",
               "node_id": "clarify", "node_attempt": 1, "rework": 0, "feedback": "", "requirement": "Grow"}
        jobs, ids, operations, recovered = {}, {}, [], {}
        pre_admission_drop = [True]
        def request(path, payload=None):
            if payload is None:
                if "/operations/" in path:
                    admitted = ids.get(path.rsplit("/", 1)[-1])
                    return {"job": jobs.get(admitted), "recovered_by": recovered.get(admitted)}
                if "/jobs/" in path:
                    job = jobs[path.rsplit("/", 1)[-1]]
                    if job["action"] == "accept-plan":
                        task.update(approved_plan_digest=planning["plan_digest"], current_stage="plan_accepted")
                        job["status"] = "succeeded"
                    return job
                return dict(task)
            path, _, check = path.partition("?after_job=")
            action = path.rsplit("/", 1)[-1]
            if action != "planning-tasks":
                self.assertEqual(check, task["job_id"])
            if payload["request_id"] in ids:
                return jobs[ids[payload["request_id"]]]
            if action == "plan" and pre_admission_drop:
                pre_admission_drop.pop()
                raise worker_client.ClientError("Lost transport before Worker admission")
            operations.append(action)
            job = {"task_id": task_id, "action": action, "job_id": str(uuid4()), "status": "succeeded"}
            if action in {"implement", "rework"}:
                task["implementation_correction"] = {"plan_digest": planning["plan_digest"],
                    "implementation_attempt": payload.get("implementation_attempt", 0), "edit_job_id": job["job_id"]}
            if action == "planning-tasks":
                self.assertEqual({k: payload[k] for k in ("workflow_mode", "workflow_project_id", "workflow_run_id",
                                                         "planning_profile", "pool_id", "requirement")},
                                 {"workflow_mode": "staged-feature", "workflow_project_id": "garden",
                                  "workflow_run_id": "run-90", "planning_profile": "feature", "pool_id": "pool",
                                  "requirement": "Grow"})
                task.update(payload)
            elif action == "clarify":
                task.update(clarification=scope, slot_id="s1", lease=True, base_commit="a" * 40, current_stage="clarified")
            elif action == "plan":
                task.update(planning=planning, current_stage="plan_ready", status="awaiting_plan_approval")
            elif action == "accept-plan":
                job["status"] = "running"
            elif action == "implement":
                self.assertEqual(task["current_stage"], "plan_accepted")
                task.update(current_stage="implemented", revision="b" * 40)
            elif action == "rework":
                self.assertEqual(payload["implementation_attempt"], 1)
                self.assertEqual(payload["feedback"], "Use two trees")
                task.update(implementation_attempt=1, rework_feedback=payload["feedback"], verification=None)
                if operations.count("rework") == 1:
                    job.update(status="failed", failure_kind="timeout")
                    task.update(status="failed", current_stage="fix", rework_pending=True, failure_kind="timeout")
                else:
                    task.update(status="fixing", current_stage="implemented", rework_pending=False, revision="c" * 40)
            elif action == "fix":
                task.update(current_stage="implemented", status="fixing", failure_kind=None, revision="d" * 40,
                            implementation_correction={**task["implementation_correction"], "fix": check})
            elif action == "compile":
                if task.get("failure_kind") in {"workspace_changed", "unity_changed"}:
                    task["implementation_correction"] = {**task.get("implementation_correction", {}),
                        "plan_digest": task["approved_plan_digest"], "implementation_attempt": task["implementation_attempt"],
                        "recompile": check}
                task["implementation_correction"]["compile_job_id"] = job["job_id"]
                task.update(current_stage="compiled", status="compiling", failure_kind=None,
                            compile_report={"revision": task["revision"]})
            elif action == "review":
                task.update(current_stage="reviewed", review_result={"verdict": "pass", "revision": task["revision"]})
            elif action == "test":
                if operations.count("test") == 1:
                    job.update(status="recovery_required", failure_kind="recovery_required")
                task.update(status="awaiting_approval", current_stage="tested", test_report={"editmode": {"status": "passed"}},
                            verification={"revision": task["revision"], "implementation_attempt": 0, "test_job_id": job["job_id"]})
                if job["status"] == "recovery_required":
                    task.update(status="recovery_required", current_stage="test", failure_kind="recovery_required", verification=None)
            else:
                self.fail("Unexpected operation " + action)
            jobs[job["job_id"]] = job
            ids[payload["request_id"]] = job["job_id"]
            task["job_id"] = job["job_id"]
            return dict(job)

        with test_directory() as root, chdir(root), patch.object(worker_client, "planning_context", return_value=ctx), patch.object(
                worker_client, "Client", return_value=SimpleNamespace(request=request)):
            mounted = Path(".agentflow/inputs")
            mounted.mkdir(parents=True)
            upstream = {}
            for node, output in (("clarify", "scope"), ("design", "planning"), ("implement", "implementation"),
                                 ("review", "review"), ("test", "verification")):
                ctx["node_id"] = node
                if node == "design":
                    self.assertEqual(worker_client.main(["feature-design", "--pool", "pool"]), 1)
                if node in ("design", "review", "test"):
                    current = task["job_id"]
                    task["job_id"] = str(uuid4())
                    with self.subTest(concurrent=node):
                        self.assertEqual(worker_client.main(["feature-" + node, "--pool", "pool"]), 1)
                        self.assertNotIn("plan" if node == "design" else node, operations)
                    task["job_id"] = current
                if node == "test":
                    self.assertEqual(worker_client.main(["feature-test", "--pool", "pool"]), 1)
                    stopped = task["job_id"]
                    for checkpoint in Path(".").glob(".unity-feature-*.json"):
                        checkpoint.unlink()
                    ctx["node_attempt"] = 2
                    self.assertEqual(worker_client.main(["feature-test", "--pool", "pool"]), 1)
                    recovery = str(uuid4())
                    recovered[stopped] = recovery
                    jobs[recovery] = {"job_id": recovery, "task_id": task_id, "action": "recover", "status": "succeeded",
                                      "stage_result": {"recovered": True}}
                    task.update(job_id=recovery, status="failed", failure_kind="recovered")
                self.assertEqual(worker_client.main(["feature-" + node, "--pool", "pool"]), 0)
                if node in ("design", "review", "test"):
                    for checkpoint in Path(".").glob(".unity-feature-*.json"):
                        checkpoint.unlink()
                    self.assertEqual(worker_client.main(["feature-" + node, "--pool", "pool"]), 0)
                source = Path("unity-" + output + ".json")
                (mounted / source.name).write_bytes(source.read_bytes())
                upstream[node] = {"id": "artifact-" + node, "name": source.stem,
                                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                ctx["upstream"] = upstream[node]
            self.assertEqual(json.loads(source.read_bytes())["verification"], task["verification"])
            self.assertEqual(operations, ["planning-tasks", "clarify", "plan", "accept-plan", "implement", "compile",
                                          "review", "test", "test"])
            ctx.update(node_id="implement", node_attempt=3, rework=1, feedback="Use two trees", upstream=upstream["design"])
            ctx.update(feedback="", feedback_error="Full rejection unavailable")
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)
            self.assertNotIn("rework", operations)
            ctx.pop("feedback_error")
            ctx["feedback"] = "Use two trees"
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)
            interrupted_job = task["job_id"]
            jobs[interrupted_job].update(status="recovery_required", failure_kind="recovery_required")
            task.update(status="recovery_required", failure_kind="recovery_required")
            ctx.update(node_attempt=4, feedback="Retry after recovering machine")
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)
            recovery = str(uuid4())
            recovered[interrupted_job] = recovery
            jobs[recovery] = {"job_id": recovery, "task_id": task_id, "action": "recover", "status": "succeeded",
                              "stage_result": {"recovered": True}}
            task.update(job_id=recovery, status="failed", failure_kind="recovered")
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 0)
            self.assertEqual(operations[-3:], ["rework", "rework", "compile"])
            data = json.loads(Path("unity-implementation.json").read_bytes())
            self.assertEqual((data["node_attempt"], data["implementation_attempt"], data["revision"]), (4, 1, "c" * 40))
            # A concurrent Worker operation changed the current job before the node could publish.
            task["job_id"] = str(uuid4())
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)

            for attempt, stage, failure, expected in (
                (5, "review", "review_changes", ["fix", "compile"]),
                (7, "test", "workspace_changed", ["compile"]),
                (9, "compile", "unity_changed", ["compile"]),
            ):
                stopped = task["job_id"] if stage == "compile" else str(uuid4())
                jobs[stopped] = {"job_id": stopped, "task_id": task_id, "action": stage, "status": "failed",
                                 "failure_kind": failure, "stage_result": {}}
                task.update(current_stage=stage, status="failed", failure_kind=failure, job_id=stopped,
                            compile_report=None, review_result=None, verification=None)
                ctx.update(node_attempt=attempt)
                before = len(operations)
                self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 0)
                self.assertEqual(operations[before:], expected)
                corrected = task["job_id"]
                for checkpoint in Path(".").glob(".unity-feature-*.json"):
                    checkpoint.unlink()
                Path("unity-implementation.json").unlink()
                ctx["node_attempt"] += 1
                self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 0)
                self.assertEqual(json.loads(Path("unity-implementation.json").read_bytes())["job_id"], corrected)
                self.assertEqual(operations[before:], expected)
                original_ref = ctx["upstream"]
                ctx["upstream"] = {**original_ref, "id": "different-plan-artifact"}
                self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)
                self.assertEqual(operations[before:], expected)
                ctx["upstream"] = original_ref

            # A redelivery of the very attempt whose compile failed must report that failure again.
            jobs[corrected].update(status="failed", failure_kind="unity_changed", stage_result={})
            task.update(current_stage="compile", status="failed", failure_kind="unity_changed")
            ctx["node_attempt"] = attempt
            before = len(operations)
            self.assertEqual(worker_client.main(["feature-implement", "--pool", "pool"]), 1)
            self.assertEqual(len(operations), before)
