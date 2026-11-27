import os
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from process_runner import StageError
from support import test_directory
from test_mvp import TOKEN
from test_workspace import git, repository
from worker import create_app


APPROVER = "human-approval-" + "z" * 32


class ApiBaseline(unittest.TestCase):
    def test_legacy_job_id_cannot_replace_a_development_task(self):
        with test_directory() as root:
            project = root / "project"
            (project / "ProjectSettings").mkdir(parents=True)
            (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
            env = {"UNITY_EXE": sys.executable, "UNITY_PROJECT": str(project),
                   "UNITY_WORKER_LOGS": str(root / "logs"), "UNITY_WORKER_TOKEN": TOKEN}
            with patch.dict(os.environ, env), TestClient(create_app(), headers={"Authorization": f"Bearer {TOKEN}"}) as client:
                created = client.post("/api/v1/tasks", json={"request_id": str(uuid4()), "requirement": "r",
                                                            "plan": "p", "acceptance_criteria": ["a"]}).json()
                for identifier in (created["task_id"], created["job_id"]):
                    self.assertEqual(client.post("/jobs", json={"job_id": identifier}).status_code, 409)
                self.assertEqual(client.get(f"/api/v1/tasks/{created['task_id']}").json()["task_type"], "development")

    def test_workers_route_keeps_mvp_health(self):
        with test_directory() as root:
            project = root / "project"
            (project / "ProjectSettings").mkdir(parents=True)
            (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
            env = {"UNITY_EXE": sys.executable, "UNITY_PROJECT": str(project),
                   "UNITY_WORKER_LOGS": str(root / "logs"), "UNITY_WORKER_TOKEN": TOKEN}
            with patch.dict(os.environ, env), TestClient(create_app()) as client:
                headers = {"Authorization": f"Bearer {TOKEN}"}
                self.assertEqual(client.get("/health", headers=headers).status_code, 200)
                self.assertEqual(client.get("/api/v1/workers", headers=headers).json()["workers"][0]["status"], "idle")

    def test_interrupted_job_cannot_bypass_recovery_from_a_stable_checkpoint(self):
        from development import DevelopmentWorker
        from worker_config import Settings

        for stage, status, action in (("compiled", "compiling", "test"), ("approved", "awaiting_approval", "commit")):
            with self.subTest(stage=stage), test_directory() as root:
                project = root / "project"
                (project / "ProjectSettings").mkdir(parents=True)
                (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
                env = {"UNITY_EXE": sys.executable, "UNITY_PROJECT": str(project),
                       "UNITY_WORKER_LOGS": str(root / "logs"), "UNITY_WORKER_TOKEN": TOKEN}
                with patch.dict(os.environ, env):
                    cfg = Settings.load()
                    prior = DevelopmentWorker(cfg)
                    task_id = str(uuid4())
                    task = prior.new_task(task_id, {}, "development")
                    job = prior.new_job(task_id, str(uuid4()), action, {})
                    task.update(lease=True, slot_id=prior.cfg.legacy_slot, status=status, current_stage=stage, job_id=job["job_id"],
                                approved_revision="a" * 40 if action == "commit" else None)
                    with prior.store.connect(write=True) as db:
                        prior.store.save_task(db, task)
                        prior.store.save_job(db, job)
                    with TestClient(create_app(cfg), headers={"Authorization": f"Bearer {TOKEN}"}) as client:
                        self.assertEqual(client.get(f"/api/v1/tasks/{task_id}").json()["status"], "recovery_required")
                        payload = {"request_id": str(uuid4())}
                        if action == "commit":
                            payload["revision"] = "a" * 40
                        self.assertEqual(client.post(f"/api/v1/tasks/{task_id}/{action}",
                                                     json=payload).status_code, 409)
                        self.assertEqual(client.post(f"/api/v1/tasks/{task_id}/release",
                                                     json={"request_id": str(uuid4())}).status_code, 409)

    def test_commit_rejects_a_stale_sop_revision(self):
        from development import DevelopmentWorker
        from worker_config import Settings

        with test_directory() as root:
            project = root / "project"
            (project / "ProjectSettings").mkdir(parents=True)
            (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
            env = {"UNITY_EXE": sys.executable, "UNITY_PROJECT": str(project),
                   "UNITY_WORKER_LOGS": str(root / "logs"), "UNITY_WORKER_TOKEN": TOKEN}
            with patch.dict(os.environ, env):
                cfg = Settings.load()
                worker = DevelopmentWorker(cfg)
                task_id = str(uuid4())
                task = worker.new_task(task_id, {}, "development")
                task.update(lease=True, slot_id=worker.cfg.legacy_slot, status="awaiting_approval", current_stage="approved", approved_revision="a" * 40)
                with worker.store.connect(write=True) as db:
                    worker.store.save_task(db, task)
                with TestClient(create_app(cfg), headers={"Authorization": f"Bearer {TOKEN}"}) as client:
                    response = client.post(f"/api/v1/tasks/{task_id}/commit",
                                           json={"request_id": str(uuid4()), "revision": "b" * 40})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(client.get(f"/api/v1/tasks/{task_id}").json()["current_stage"], "approved")


class FixtureRunner:
    def __init__(self, root):
        from process_runner import ProcessRunner

        self.root = root
        self.real = ProcessRunner()
        self.started = threading.Event()

    def run(self, argv, cwd, prefix, **kwargs):
        if argv[0] == sys.executable:
            if "exec" in argv and "--help" not in argv:
                self.started.set()
            argv = [sys.executable, str(Path(__file__).with_name("fake_tool.py")), str(self.root), *argv[1:]]
        return self.real.run(argv, cwd, prefix, **kwargs)


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        self.remote, self.project, self.logs = repository(self.root)
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        env = {"UNITY_EXE": sys.executable, "UNITY_PROJECT": str(self.project), "UNITY_WORKER_LOGS": str(self.logs),
               "UNITY_WORKER_TOKEN": TOKEN, "UNITY_WORKER_APPROVAL_TOKEN": APPROVER,
               "UNITY_CODEX_EXE": sys.executable, "UNITY_CODEX_HOME": str(codex_home), "UNITY_MAX_FIXES": "1"}
        self.enterContext(patch.dict(os.environ, env))
        self.runner = FixtureRunner(self.root)
        self.enterContext(patch("unity_executor.unity_product_version", return_value="2022.3.62f3"))

    def client(self):
        return TestClient(create_app(runner=self.runner), headers={"Authorization": f"Bearer {TOKEN}"})

    def create(self, client, request_id=None, **extra):
        body = {"request_id": request_id or str(uuid4()), "workflow_run_id": "loopforge-test-1",
                "requirement": "Change the greeting", "plan": "Edit only hello.txt",
                "acceptance_criteria": ["Greeting is updated"], **extra}
        response = client.post("/api/v1/tasks", json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["task_id"]

    def start(self, client, task, action, request_id=None, **body):
        if action == "commit" and "revision" not in body:
            body["revision"] = self.task(client, task)["revision"]
        response = client.post(f"/api/v1/tasks/{task}/{action}", json={"request_id": request_id or str(uuid4()), **body})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["job_id"]

    def wait(self, client, job):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/jobs/{job}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["status"] != "running":
                return result
            time.sleep(0.06)
        self.fail("Job still running")

    def task(self, client, task):
        return client.get(f"/api/v1/tasks/{task}").json()

    def stage(self, client, task, action, **kwargs):
        return self.wait(client, self.start(client, task, action, **kwargs))

    def approve(self, client, task):
        payload = {"request_id": str(uuid4()), "revision": self.task(client, task)["revision"], "approver": "Tester"}
        path = f"/api/v1/tasks/{task}/approve"
        self.assertEqual(client.post(path, json=payload).status_code, 403)
        self.assertEqual(client.post(path, json=payload, headers={"Authorization": f"Bearer {APPROVER}"}).status_code, 202)

    def staged_client(self, pr_adapter=None):
        from test_planning import PlanningAgent
        from worker_config import Settings

        self.agent = PlanningAgent(self.project)
        cfg = replace(Settings.load(), test_assemblies={"editmode": ("Tests",), "playmode": ("PlayTests",)})
        return TestClient(create_app(cfg, runner=self.runner, agent=self.agent, pr_adapter=pr_adapter),
                          headers={"Authorization": f"Bearer {TOKEN}"})

    def accepted_staged_plan(self, client):
        response = client.post("/api/v1/planning-tasks", json={
            "request_id": str(uuid4()), "workflow_run_id": "run-feature-41", "workflow_mode": "staged-feature",
            "workflow_project_id": "Game-1", "planning_profile": "feature", "requirement": "Change the greeting",
        })
        self.assertEqual(response.status_code, 202, response.text)
        task = response.json()["task_id"]
        for action in ("clarify", "plan"):
            job = self.stage(client, task, action)
            self.assertEqual(job["status"], "succeeded", job)
        planned = self.task(client, task)
        job = self.stage(client, task, "accept-plan", slot_id=planned["slot_id"], base_commit=planned["base_commit"],
                         plan_digest=planned["planning"]["plan_digest"])
        self.assertEqual(job["status"], "succeeded", job)
        return task

    def recover(self, client, task):
        response = client.post(f"/api/v1/tasks/{task}/recover", headers={"Authorization": f"Bearer {APPROVER}"},
                               json={"request_id": str(uuid4()), "confirmed_stopped": True, "note": "Process exited"})
        self.assertEqual(response.status_code, 202, response.text)
        job = self.wait(client, response.json()["job_id"])
        self.assertEqual(job["status"], "succeeded", job)


class WorkflowTests(WorkflowFixture):
    def test_staged_implement_retries_transient_failure_only_on_the_frozen_clean_checkout(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            workspace = client.app.state.worker.workspaces[self.task(client, task)["slot_id"]]
            for kind in ("git_failed", "timeout"):
                with patch.object(workspace.git, "clean", side_effect=StageError(kind, "Transient Git failure")):
                    failed = self.stage(client, task, "implement")
                self.assertEqual(failed["failure_kind"], kind, failed)
            self.assertEqual(self.stage(client, task, "implement")["status"], "succeeded")

    def test_staged_implement_retry_preserves_changes_left_after_timeout(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            with patch.object(self.agent, "execute", side_effect=StageError("timeout", "Stopped before completion")):
                self.assertEqual(self.stage(client, task, "implement")["failure_kind"], "timeout")
            greeting = self.project / "hello.txt"
            greeting.write_text("unconfirmed work")
            with patch.object(self.agent, "execute", side_effect=AssertionError("Must preserve the workspace")):
                blocked = self.stage(client, task, "implement")
            self.assertEqual(blocked["failure_kind"], "dirty_workspace", blocked)
            self.assertEqual(greeting.read_text(), "unconfirmed work")

    def test_staged_standalone_compile_review_retry_report_io_and_interruption(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            self.assertEqual(self.stage(client, task, "implement")["status"], "succeeded")
        for stage, checkpoint in (("compile", "compiled"), ("review", "reviewed")):
            with self.staged_client() as client:
                failed = self.stage_with_report_write_failure(client, task, stage)
                self.assertEqual(failed["failure_kind"], "report_io_failed", failed)
                self.assertEqual(self.task(client, task)["current_stage"], stage)
                reached_finish = threading.Event()
                with patch.object(client.app.state.worker, "finish", side_effect=lambda *_: reached_finish.set()):
                    interrupted = self.start(client, task, stage)
                    self.assertTrue(reached_finish.wait(45))
                self.assertEqual(self.task(client, task)["current_stage"], stage)
            with self.staged_client() as client:
                self.assertEqual(client.get(f"/api/v1/jobs/{interrupted}").json()["status"], "recovery_required")
                self.recover(client, task)
                completed = self.stage(client, task, stage)
                self.assertEqual(completed["status"], "succeeded", completed)
                self.assertEqual(self.task(client, task)["current_stage"], checkpoint)

    def test_staged_recovery_resumes_reserved_rework_without_spending_another_correction(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            rework = {"workflow_run_id": "run-feature-41", "implementation_attempt": 1,
                      "feedback": " Keep the welcome message. "}
            request_id = str(uuid4())
            with patch.object(self.agent, "execute", side_effect=StageError("timeout", "Codex timed out")):
                first = self.stage(client, task, "rework", request_id=request_id, **rework)
            self.assertEqual(first["failure_kind"], "timeout", first)
            self.assertEqual(self.start(client, task, "rework", request_id=request_id, **rework), first["job_id"])
            for change in ({"feedback": "Replace the feedback"}, {"implementation_attempt": 2}, {"workflow_run_id": "run-other"}):
                response = client.post(f"/api/v1/tasks/{task}/rework",
                                       json={"request_id": str(uuid4()), **rework, **change})
                self.assertEqual(response.status_code, 409, response.text)

            with patch.object(self.agent, "execute", side_effect=StageError("recovery_required", "Unknown process exit")):
                interrupted = self.stage(client, task, "rework", **rework)
            self.assertEqual(interrupted["status"], "recovery_required", interrupted)
            self.assertNotEqual(interrupted["job_id"], first["job_id"])
            response = client.post(f"/api/v1/tasks/{task}/rework", json={"request_id": str(uuid4()), **rework})
            self.assertEqual(response.status_code, 409, response.text)
            self.recover(client, task)

            resumed = self.stage(client, task, "rework", **rework)
            self.assertEqual(resumed["status"], "succeeded", resumed)
            self.assertNotEqual(resumed["job_id"], interrupted["job_id"])
            current = self.task(client, task)
            self.assertEqual(current["current_stage"], "implemented")
            self.assertEqual(current["implementation_attempt"], 1)
            self.assertEqual(current["retry_count"], 1)
            self.assertIsNone(current["verification"])
            self.assertEqual(self.agent.implementation_briefs[-1]["previous_failure"]["feedback"], rework["feedback"])

    def test_staged_recovery_retries_test_interrupted_before_finalization(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            for stage in ("implement", "compile", "review"):
                job = self.stage(client, task, stage)
                self.assertEqual(job["status"], "succeeded", job)
            finalizing = threading.Event()
            worker = client.app.state.worker
            with patch.object(worker, "finish", side_effect=lambda *_: finalizing.set()):
                interrupted = self.start(client, task, "test")
                self.assertTrue(finalizing.wait(45), "Test did not reach finalization")
                self.assertIsNone(self.task(client, task)["verification"])

        with self.staged_client() as client:
            abandoned = client.get(f"/api/v1/jobs/{interrupted}").json()
            self.assertEqual(abandoned["status"], "recovery_required", abandoned)
            self.recover(client, task)
            retried = self.stage(client, task, "test")
            self.assertEqual(retried["status"], "succeeded", retried)
            self.assertEqual(self.task(client, task)["verification"]["test_job_id"], retried["job_id"])

    def assert_cancelled_staged_report(self, client, task, action, **payload):
        reporting, allow_report = threading.Event(), threading.Event()
        worker = client.app.state.worker
        report = worker.report

        def pause_report(ctx, error):
            reporting.set()
            if not allow_report.wait(15):
                raise AssertionError("Report test did not release the Worker")
            return report(ctx, error)

        with patch.object(worker, "report", side_effect=pause_report):
            try:
                job_id = self.start(client, task, action, **payload)
                self.assertTrue(reporting.wait(55), "Job did not reach report creation")
                self.assertEqual(self.stage(client, task, "cancel")["status"], "succeeded")
            finally:
                allow_report.set()
            finished = self.wait(client, job_id)
        self.assertEqual(finished["status"], "cancelled", finished)
        current = self.task(client, task)
        self.assertEqual(current["status"], "cancelled")
        self.assertIsNone(current["verification"])
        return current

    def test_staged_cancel_during_test_report_does_not_publish_verification(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            current = self.assert_cancelled_staged_report(client, task, "run")
            self.assertEqual(current["current_stage"], "test")

    def test_staged_cancel_during_rework_report_does_not_advance_checkpoint(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            current = self.assert_cancelled_staged_report(client, task, "rework", workflow_run_id="run-feature-41",
                                                          implementation_attempt=1, feedback="Keep it brief")
            self.assertEqual(current["current_stage"], "fix")

    def stage_with_report_write_failure(self, client, task, action, *, error_type=OSError, **payload):
        write_text = Path.write_text

        def write_file(path, *args, **kwargs):
            if path.name.endswith("-development-report.json"):
                raise error_type("Simulated report write failure")
            return write_text(path, *args, **kwargs)

        with patch.object(Path, "write_text", new=write_file):
            return self.stage(client, task, action, **payload)

    def test_staged_report_io_failure_can_retry_test(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            for stage in ("implement", "compile", "review"):
                job = self.stage(client, task, stage)
                self.assertEqual(job["status"], "succeeded", job)
            reviewed = self.task(client, task)
            failed = self.stage_with_report_write_failure(client, task, "test")
            self.assertEqual(failed["status"], "failed", failed)
            incomplete = self.task(client, task)
            self.assertEqual(incomplete["current_stage"], "test")
            self.assertIsNone(incomplete["verification"])

            completed = self.stage(client, task, "test")
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(failed["failure_kind"], "report_io_failed", failed)
            self.assertNotEqual(completed["job_id"], failed["job_id"])
            current = self.task(client, task)
            self.assertEqual(current["review_result"], reviewed["review_result"])
            self.assertEqual(current["verification"], {"revision": reviewed["revision"], "test_job_id": completed["job_id"],
                                                       "implementation_attempt": 0})
            self.assertEqual(current["retry_count"], 0)

            payload = {"workflow_run_id": "run-feature-41", "implementation_attempt": 1, "feedback": "Keep the greeting"}
            unexpected = self.stage_with_report_write_failure(client, task, "rework", error_type=RuntimeError, **payload)
            self.assertEqual(unexpected["failure_kind"], "worker_error", unexpected)
            response = client.post(f"/api/v1/tasks/{task}/rework", json={"request_id": str(uuid4()), **payload})
            self.assertEqual(response.status_code, 409, response.text)

    def test_staged_report_io_failure_can_resume_rework(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            for stage in ("implement", "compile", "review", "test"):
                job = self.stage(client, task, stage)
                self.assertEqual(job["status"], "succeeded", job)
            payload = {"workflow_run_id": "run-feature-41", "implementation_attempt": 1,
                       "feedback": "  Keep the welcome.  "}
            request_id = str(uuid4())
            failed = self.stage_with_report_write_failure(client, task, "rework", request_id=request_id, **payload)
            self.assertEqual(failed["status"], "failed", failed)
            incomplete = self.task(client, task)
            self.assertEqual(incomplete["current_stage"], "fix")
            self.assertIsNone(incomplete["verification"])
            self.assertEqual(self.start(client, task, "rework", request_id=request_id, **payload), failed["job_id"])

            resumed = self.stage(client, task, "rework", **payload)
            self.assertEqual(resumed["status"], "succeeded", resumed)
            self.assertEqual(failed["failure_kind"], "report_io_failed", failed)
            self.assertNotEqual(resumed["job_id"], failed["job_id"])
            current = self.task(client, task)
            self.assertEqual(current["current_stage"], "implemented")
            self.assertEqual(current["implementation_attempt"], 1)
            self.assertEqual(current["retry_count"], 1)
            self.assertEqual(self.agent.implementation_briefs[-1]["previous_failure"]["feedback"], payload["feedback"])
            self.assertIsNone(current["verification"])

    def test_staged_review_precedes_tests_and_rework_invalidates_evidence(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)

            def action(name, request_id=None, **payload):
                return client.post(f"/api/v1/tasks/{task}/{name}", json={
                    "request_id": request_id or str(uuid4()), **payload})

            rework = {"workflow_run_id": "run-feature-41", "implementation_attempt": 1,
                      "feedback": "  Keep the friendly greeting.\nMention the welcome screen.  "}
            for stage in ("implement", "compile"):
                job = self.stage(client, task, stage)
                self.assertEqual(job["status"], "succeeded", job)
            compiled = self.task(client, task)
            self.assertEqual(action("test").status_code, 409)
            self.assertEqual(action("rework", **rework).status_code, 409)
            reviewed_job = self.stage(client, task, "review")
            self.assertEqual(reviewed_job["status"], "succeeded", reviewed_job)
            reviewed = self.task(client, task)
            self.assertEqual(reviewed["review_result"]["revision"], compiled["compile_report"]["revision"])
            self.assertIsNone(reviewed["test_report"])
            self.assertIsNone(reviewed["verification"])

            tested_job = self.stage(client, task, "test")
            self.assertEqual(tested_job["status"], "succeeded", tested_job)
            tested = self.task(client, task)
            self.assertEqual(tested["verification"], {"revision": tested["revision"], "test_job_id": tested_job["job_id"],
                                                      "implementation_attempt": 0})
            self.assertEqual(tested["test_report"]["editmode"]["revision"], reviewed["review_result"]["revision"])
            self.assertIsNone(tested["approved_revision"])
            for wrong in ({"workflow_run_id": "run-other-41"}, {"implementation_attempt": 2}):
                self.assertEqual(action("rework", **{**rework, **wrong}).status_code, 409)

            started, allow_fix = threading.Event(), threading.Event()
            execute = self.agent.execute

            def pause_fix(ctx, prompt, *, review=False):
                started.set()
                if not allow_fix.wait(10):
                    raise AssertionError("Rework test did not release the agent")
                return execute(ctx, prompt, review=review)

            request_id = str(uuid4())
            with patch.object(self.agent, "execute", side_effect=pause_fix):
                try:
                    response = action("rework", request_id=request_id, **rework)
                    self.assertEqual(response.status_code, 202, response.text)
                    job_id = response.json()["job_id"]
                    self.assertTrue(started.wait(10), "Rework did not reach the implementation agent")
                    reworked = self.task(client, task)
                    for key in ("verification", "test_report", "review_result", "approved_revision", "approval"):
                        self.assertIsNone(reworked[key], key)
                    for key in ("slot_id", "base_commit", "task_branch", "approved_plan_digest", "plan"):
                        self.assertEqual(reworked[key], tested[key], key)
                    self.assertEqual(reworked["implementation_attempt"], 1)
                    self.assertEqual(action("test").status_code, 409)
                    self.assertEqual(action("rework", **{**rework, "implementation_attempt": 2}).status_code, 409)
                    repeated = action("rework", request_id=request_id, **rework)
                    self.assertEqual(repeated.json()["job_id"], job_id)
                    self.assertEqual(action("rework", request_id=request_id, **{**rework, "feedback": "Other"}).status_code, 409)
                finally:
                    allow_fix.set()
                finished = self.wait(client, job_id)
            self.assertEqual(finished["status"], "succeeded", finished)
            self.assertEqual(self.agent.implementation_briefs[-1]["previous_failure"]["feedback"], rework["feedback"])
            self.assertEqual(action("rework", **rework).status_code, 409)
            run_job = self.stage(client, task, "run")
            self.assertEqual(run_job["status"], "succeeded", run_job)
            current = self.task(client, task)
            self.assertEqual(current["verification"], {"revision": current["revision"], "test_job_id": run_job["job_id"],
                                                       "implementation_attempt": 1})
            self.assertEqual(current["retry_count"], 1)
            self.assertEqual(action("rework", **{**rework, "implementation_attempt": 2}).status_code, 409)

    def test_staged_correction_records_the_actual_fix_and_recompile_predecessors(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            edit = self.stage(client, task, "implement")
            self.assertEqual(edit["status"], "succeeded")
            worker = client.app.state.worker
            with patch.object(worker, "stage_compile", side_effect=StageError("compile_failed", "Invalid script")):
                broken = self.stage(client, task, "compile")
            self.assertEqual(broken["failure_kind"], "compile_failed")
            self.assertEqual(self.stage(client, task, "fix")["status"], "succeeded")
            compiled = self.stage(client, task, "compile")
            self.assertEqual(compiled["status"], "succeeded")
            current = self.task(client, task)
            correction = {"fix": broken["job_id"], "edit_job_id": edit["job_id"], "implementation_attempt": 0,
                          "plan_digest": current["approved_plan_digest"], "compile_job_id": compiled["job_id"]}
            path = f"/api/v1/tasks/{task}?include_implementation_correction=true"
            self.assertEqual(client.get(path).json().get("implementation_correction"), correction)
            (self.project / "hello.txt").write_text("greeting changed after compile")
            drift = self.stage(client, task, "review")
            self.assertEqual(drift["failure_kind"], "workspace_changed", drift)
            recompiled = self.stage(client, task, "compile")
            self.assertEqual(recompiled["status"], "succeeded")
            self.assertEqual(client.get(path).json()["implementation_correction"],
                             {**correction, "recompile": drift["job_id"], "compile_job_id": recompiled["job_id"]})

    def test_staged_compile_failure_preserves_bounded_implementation_corrections(self):
        (self.root / "broken_compile").touch()
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            failed = self.stage(client, task, "run")
            self.assertEqual(failed["failure_kind"], "compile_failed", failed)
            current = self.task(client, task)
            self.assertEqual(current["retry_count"], 1)
            self.assertEqual(current.get("implementation_attempt"), 0)
            self.assertIsNone(current["verification"])
            self.assertIsNone(current["review_result"])

    def test_staged_compile_keeps_compiler_evidence_when_git_check_hits_a_lock(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            self.assertEqual(self.stage(client, task, "implement")["status"], "succeeded")
            (self.root / "broken_compile").touch()
            lock = self.project / "Temp/UnityLockfile"
            lock.parent.mkdir(exist_ok=True)
            run = self.runner.run

            def block_revision_check(argv, cwd, prefix, **kwargs):
                result = run(argv, cwd, prefix, **kwargs)
                if "-batchmode" in argv:
                    lock.write_bytes(b"not a disposable batch lock")
                return result

            with patch.object(self.runner, "run", side_effect=block_revision_check):
                blocked = self.stage(client, task, "compile")
            self.assertEqual(blocked["failure_kind"], "workspace_in_use", blocked)
            self.assertEqual(lock.read_bytes(), b"not a disposable batch lock")
            evidence = blocked["stage_result"]["unity_failure"]
            self.assertEqual(evidence["failure_kind"], "compile_failed")
            self.assertTrue(evidence["result"]["log"].endswith("-compile.log"))
            self.assertIsNone(self.task(client, task)["compile_report"])

    def test_staged_unity_retries_need_review_again_when_the_tree_changes(self):
        with self.staged_client() as client:
            task = self.accepted_staged_plan(client)
            for stage in ("implement", "compile", "review"):
                job = self.stage(client, task, stage)
                self.assertEqual(job["status"], "succeeded", job)
            reviewed = self.task(client, task)

            (self.root / "license_fail").touch()
            failed = self.stage(client, task, "test")
            self.assertEqual(failed["failure_kind"], "license_failed", failed)
            blocked = self.task(client, task)
            self.assertEqual(blocked["retry_count"], 0)
            self.assertEqual(blocked["review_result"], reviewed["review_result"])
            self.assertIsNone(blocked["verification"])

            run = self.runner.run

            def update_during_unity(argv, cwd, prefix, **kwargs):
                result = run(argv, cwd, prefix, **kwargs)
                if "-runTests" in argv:
                    (self.project / "hello.txt").write_text("Unity rewrote the greeting\n")
                return result

            with patch.object(self.runner, "run", side_effect=update_during_unity):
                changed = self.stage(client, task, "test")
            self.assertEqual(changed["failure_kind"], "unity_changed", changed)
            self.assertIsNone(self.task(client, task)["verification"])
            (self.root / "license_fail").unlink()

            self.assertEqual(self.stage(client, task, "compile")["status"], "succeeded")
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/test", json={"request_id": str(uuid4())}).status_code, 409)
            self.assertEqual(self.stage(client, task, "review")["status"], "succeeded")
            tested = self.stage(client, task, "test")
            self.assertEqual(tested["status"], "succeeded", tested)
            current = self.task(client, task)
            self.assertNotEqual(current["revision"], reviewed["revision"])
            self.assertEqual(current["verification"]["revision"], current["review_result"]["revision"])
            self.assertEqual(current["retry_count"], 0)

    def test_lease_duplicate_requests_and_stages(self):
        with self.client() as client:
            req = str(uuid4())
            task = self.create(client, req)
            self.assertEqual(self.create(client, req), task)
            other = self.create(client)
            prepare = str(uuid4())
            job = self.start(client, task, "prepare", request_id=prepare)
            self.assertEqual(self.start(client, task, "prepare", request_id=prepare), job)
            self.assertEqual(client.post(f"/api/v1/tasks/{other}/prepare", json={"request_id": str(uuid4())}).status_code, 409)
            self.assertEqual(self.wait(client, job)["status"], "succeeded")
            self.assertEqual(self.task(client, task)["current_stage"], "prepared")
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/test", json={"request_id": str(uuid4())}).status_code, 409)
            self.assertEqual(self.stage(client, task, "implement")["status"], "succeeded")
            self.assertEqual(self.stage(client, task, "compile")["status"], "succeeded")
            self.assertEqual(self.task(client, task)["current_stage"], "compiled")
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/review", json={"request_id": str(uuid4())}).status_code, 409)
            self.assertEqual((self.project / "Library/cache").read_text(), "keep")

    def test_review_rework_approval_restart_and_delivery(self):
        (self.root / "review_once").touch()
        with self.client() as client:
            task = self.create(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            current = self.task(client, task)
            self.assertEqual(current["retry_count"], 1)
            self.assertEqual(current["status"], "awaiting_approval")
            self.assertNotEqual(current["agent_session_id"], current["review_result"]["session_id"])
            artifacts = client.get(f"/api/v1/tasks/{task}/artifacts").json()["artifacts"]
            report = next(entry for entry in reversed(artifacts) if entry["kind"] == "report")
            self.assertEqual(client.get(report["path"]).json()["status"], "awaiting_approval")
            self.assertFalse(any(TOKEN in entry["path"] for entry in artifacts))
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/commit",
                                         json={"request_id": str(uuid4()), "revision": current["revision"]}).status_code, 409)
            self.assertEqual(git(self.remote, "rev-parse", "main"), current["base_commit"])
        with self.client() as client:
            self.assertEqual(self.task(client, task)["status"], "awaiting_approval")
            self.assertTrue(client.get("/health").json()["busy_or_blocked"])
            self.approve(client, task)
            self.assertEqual(self.stage(client, task, "commit")["status"], "succeeded")
            completed = self.task(client, task)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(git(self.remote, "rev-parse", completed["task_branch"]), completed["commit_sha"])
            self.assertEqual(self.stage(client, task, "release")["status"], "succeeded")
            self.assertFalse(client.get("/health").json()["busy_or_blocked"])
            self.assertEqual((self.project / "Library/cache").read_text(), "keep")

    def test_nested_scoped_workflow_pushes_only_the_approved_tree(self):
        unity = self.project / "client"
        unity.mkdir()
        for name in ("ProjectSettings", "Packages", "Library", ".gitignore"):
            (self.project / name).rename(unity / name)
        git(self.project, "add", "--all")
        git(self.project, "commit", "-m", "Nested Unity project")
        git(self.project, "push", "origin", "main")
        config = self.root / "fixture.toml"
        config.write_text('[test_assemblies]\neditmode = ["Game.Tests"]\n', encoding="utf-8")
        env = {"UNITY_PROJECT": str(unity), "UNITY_GIT_ROOT": str(self.project),
               "UNITY_WORKER_CONFIG": str(config)}
        with patch.dict(os.environ, env), self.client() as client:
            req = {"request_id": str(uuid4()), "requirement": "Greeting", "plan": "Edit hello.txt",
                   "acceptance_criteria": ["Greeting is updated"]}
            self.assertEqual(client.post("/api/v1/tasks", json=req).status_code, 422)
            req["test_selection"] = {"editmode": [{"assembly": "Game.Tests", "fixtures": [".*"]}]}
            self.assertEqual(client.post("/api/v1/tasks", json=req).status_code, 422)
            scope = {"editmode": [{"assembly": "Game.Tests", "fixtures": ["Game.UnitTests"]}]}
            task = self.create(client, test_selection=scope)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            reviewed = self.task(client, task)
            self.assertEqual(reviewed["test_report"]["editmode"]["revision"], reviewed["revision"])
            self.assertEqual(reviewed["test_report"]["editmode"]["runs"][0]["scope"], scope["editmode"][0])
            self.assertEqual(reviewed["test_report"]["playmode"]["status"], "not_run")
            self.approve(client, task)
            self.assertEqual(self.stage(client, task, "commit")["status"], "succeeded")
            self.assertEqual(self.stage(client, task, "release")["status"], "succeeded")
            completed = self.task(client, task)
            self.assertEqual(git(self.remote, "rev-parse", completed["task_branch"] + "^{tree}"), reviewed["revision"])
            self.assertEqual(git(self.remote, "rev-parse", "main"), reviewed["base_commit"])
            self.assertEqual((unity / "Library/cache").read_text(), "keep")

    def test_compile_failure_stops_at_retry_limit(self):
        (self.root / "broken_compile").touch()
        with self.client() as client:
            task = self.create(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "failed")
            info = self.task(client, task)
            self.assertEqual(info["failure_kind"], "compile_failed")
            self.assertEqual(info["retry_count"], 1)
            self.assertIsNone(info["review_result"])
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/fix", json={"request_id": str(uuid4())}).status_code, 409)
            self.assertTrue(client.get("/health").json()["busy_or_blocked"])

    def test_approval_does_not_cover_new_changes(self):
        with self.client() as client:
            task = self.create(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            self.approve(client, task)
            (self.project / "hello.txt").write_text("changed after approval")
            self.assertEqual(self.stage(client, task, "commit")["status"], "failed")
            info = self.task(client, task)
            self.assertIsNone(info["approved_revision"])
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), info["base_commit"])
            self.assertEqual(git(self.remote, "branch", "--list"), "* main")

    def test_test_assertions_block_and_license_errors_do_not_trigger_ai_rework(self):
        for flag, kind, retries in (("broken_tests", "tests_failed", 1), ("license_fail", "license_failed", 0)):
            with self.subTest(flag=flag):
                (self.root / flag).touch()
                with self.client() as client:
                    task = self.create(client)
                    self.assertEqual(self.stage(client, task, "run")["status"], "failed")
                    info = self.task(client, task)
                    self.assertEqual(info["failure_kind"], kind)
                    self.assertEqual(info["retry_count"], retries)
                    (self.project / "hello.txt").write_text("old\n")
                    self.assertEqual(self.stage(client, task, "release")["status"], "succeeded")
                (self.root / flag).unlink()

    def test_cancelled_worker_keeps_lease_and_path_or_shell_inputs_are_rejected(self):
        with self.client() as client:
            task = self.create(client)
            self.assertEqual(self.stage(client, task, "prepare")["status"], "succeeded")
            (self.root / "slow_codex").touch()
            self.runner.started.clear()
            job = self.start(client, task, "implement")
            self.assertTrue(self.runner.started.wait(10))
            self.stage(client, task, "cancel")
            self.assertEqual(self.wait(client, job)["status"], "cancelled")
            self.assertTrue(client.get("/health").json()["busy_or_blocked"])
            self.assertEqual(client.post(f"/api/v1/tasks/{task}/release",
                                         json={"request_id": str(uuid4()), "command": "git push --force main"}).status_code, 422)
            self.assertEqual(client.get(f"/api/v1/tasks/{task}/artifacts/..%5Cworker.sqlite3").status_code, 404)
            self.assertEqual(self.stage(client, task, "release")["status"], "succeeded")

    def test_codex_failure_is_persisted_and_has_no_validation_success(self):
        (self.root / "codex_fail").touch()
        with self.client() as client:
            task = self.create(client)
            result = self.stage(client, task, "run")
            self.assertEqual((result["status"], result["exit_code"]), ("failed", 7))
            info = self.task(client, task)
            self.assertEqual(info["failure_kind"], "agent_failed")
            self.assertIsNone(info["compile_report"])
            self.assertTrue(info["lease"])
            failure_report = client.get(f"/api/v1/tasks/{task}/artifacts/{info['report']}").json()
            self.assertEqual(failure_report["status"], "failed")

    def test_push_retries_the_same_local_commit(self):
        from process_runner import StageError

        with self.client() as client:
            task = self.create(client)
            self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
            self.approve(client, task)
            with patch("git_workspace.GitWorkspace.push", side_effect=StageError("git_failed", "fixture remote offline")):
                self.assertEqual(self.stage(client, task, "commit")["status"], "failed")
            before = self.task(client, task)
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), before["commit_sha"])
            self.assertFalse(before["pushed"])
            self.assertEqual(self.stage(client, task, "commit")["status"], "succeeded")
            after = self.task(client, task)
            self.assertEqual(after["commit_sha"], before["commit_sha"])
            self.assertEqual(git(self.remote, "rev-parse", after["task_branch"]), before["commit_sha"])
