import hashlib
import hmac
import json
import os
import secrets
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from process_runner import StageError
from test_workflow import APPROVER, TOKEN, WorkflowFixture
from test_workspace import git


def envelope(claims, key, issued):
    data = {"claims": claims, "issued_at": issued}
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()
    return {**data, "signature": hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()}


def claims_for(task):
    return {
        "project_id": task["workflow_project_id"], "workflow_id": task["workflow_project_id"] + "-feature",
        "workflow_run_id": task["workflow_run_id"], "accept_attempt": 1,
        "task_id": task["task_id"], "pool_id": task["pool_id"], "slot_id": task["slot_id"],
        "implementation_attempt": task["implementation_attempt"], "base_commit": task["base_commit"],
        "revision": task["revision"], "plan_digest": task["approved_plan_digest"],
        "test_job_id": task["verification"]["test_job_id"], "approval_artifact_id": "artifact-approval",
        "approval_digest": "7" * 64, "test_artifact_id": "artifact-test", "test_digest": "8" * 64,
    }


class ApprovalEnvelopeTests(unittest.TestCase):
    def test_signatures_bind_all_claims_and_have_bounded_lifetime(self):
        from workflow_approval import sign_approval, verify_approval

        key = secrets.token_hex(32)
        claims = claims_for({"workflow_project_id": "Test", "workflow_run_id": "run-test", "task_id": str(uuid4()),
                             "pool_id": "pool", "slot_id": "slot", "base_commit": "a" * 40, "revision": "b" * 40,
                             "approved_plan_digest": "c" * 64, "verification": {"test_job_id": str(uuid4())},
                             "implementation_attempt": 0})
        proof = sign_approval(claims, key, 1000)
        self.assertEqual(proof, envelope(claims, key, 1000))
        for now in (995, 1000, 1030):
            self.assertEqual(verify_approval(proof, key, now), claims)
        for invalid, now in ((proof, 994), (proof, 1031), ({**proof, "issued_at": 1001}, 1000),
                             ({**proof, "signature": "invalid"}, 1000), ({**proof, "extra": ""}, 1000)):
            with self.assertRaises(ValueError):
                verify_approval(invalid, key, now)
        for fields in ({"accept_attempt": True}, {"implementation_attempt": -1}, {"test_digest": "bad"},
                       {"approval_artifact_id": ""}, {"test_job_id": "wrong"}, {"extra": "unknown"}):
            with self.assertRaises(ValueError):
                verify_approval(envelope({**claims, **fields}, key, 1000), key, 1000)


class WorkflowDeliveryTests(WorkflowFixture):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": ""}))
        super().setUp()
        self.key = secrets.token_hex(32)

    def delivery_client(self, pr_adapter=None):
        with patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": self.key}):
            return self.staged_client(pr_adapter=pr_adapter)

    def test_staged_operation_lookup_is_task_scoped(self):
        with self.delivery_client() as client:
            tasks = []
            for mode in ("staged-feature", "staged-feature", None):
                request = {"request_id": str(uuid4()), "planning_profile": "feature", "requirement": "Find receipts",
                           "workflow_run_id": "run-44"}
                if mode:
                    request.update(workflow_mode=mode, workflow_project_id="garden")
                job = client.post("/api/v1/planning-tasks", json=request).json()
                tasks.append((job, request["request_id"]))
            (job, request_id), (other, _), (legacy, legacy_request) = tasks
            url = lambda owner, request: f"/api/v1/tasks/{owner['task_id']}/operations/{request}"
            response = client.get(url(job, request_id))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["job_id"], job["job_id"])
            self.assertIsNone(client.get(url(other, request_id)).json()["job"])
            self.assertEqual(client.get(url(legacy, legacy_request)).status_code, 404)
            # The predecessor can change between the Runner's GET and POST.
            route = f"/api/v1/tasks/{job['task_id']}/clarify?after_job={other['job_id']}"
            late_id = str(uuid4())
            self.assertEqual(client.post(route, json={"request_id": late_id}).status_code, 409)
            self.assertIsNone(client.get(url(job, late_id)).json()["job"])

    def test_key_is_private_and_independent(self):
        from codex_executor import CodexExecutor
        from worker_config import Settings, system_env
        from pull_requests import HostedPullRequests

        self.enterContext(patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": self.key}))
        cfg = Settings.load()
        self.assertTrue(self.key in cfg.secrets)
        self.assertFalse(self.key in repr(cfg))
        self.assertFalse(self.key in system_env().values())
        self.assertFalse(self.key in CodexExecutor(cfg).environment().values())
        adapter = {"provider": "gitlab", "api_url": "https://example.test/api", "repository": "code/game",
                   "token_env": "UNITY_WORKER_WORKFLOW_APPROVAL_KEY"}
        with self.assertRaises(ValueError):
            HostedPullRequests.from_pool_config(adapter)
        for shared in (TOKEN, APPROVER, "too-short"):
            with patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": shared}), self.assertRaises(ValueError):
                Settings.load()
        with patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": ""}), self.staged_client() as client:
            self.assertNotIn("staged-feature", client.get("/api/v1/workers").json()["workers"][0].get("capabilities", []))
            response = self.deliver(client, {"task_id": str(uuid4())}, {})
            self.assertEqual(response.status_code, 403)

    def verified_task(self, client):
        task = self.accepted_staged_plan(client)
        self.assertEqual(self.stage(client, task, "run")["status"], "succeeded")
        return self.task(client, task)

    def deliver(self, client, task, proof, request_id=None):
        return client.post(f"/api/v1/tasks/{task['task_id']}/workflow-deliver",
                           json={"approval": proof, "request_id": request_id or str(uuid4())})

    def test_delivery_requires_current_loopforge_approval(self):
        with self.delivery_client() as client:
            self.assertIn("staged-feature", client.get("/api/v1/workers").json()["workers"][0].get("capabilities", []))
            task = self.verified_task(client)
            claims, now = claims_for(task), int(time.time())
            signed = lambda fields, at=now: envelope({**claims, **fields}, self.key, at)
            for field, value in (("project_id", "Other"), ("workflow_id", "Other-feature"), ("workflow_run_id", "run-other"),
                                 ("task_id", str(uuid4())), ("test_job_id", str(uuid4())), ("slot_id", "other"),
                                 ("pool_id", "other"), ("base_commit", "f" * 40), ("revision", "d" * 40),
                                 ("implementation_attempt", 1), ("plan_digest", "e" * 64)):
                with self.subTest(field=field):
                    self.assertEqual(self.deliver(client, task, signed({field: value})).status_code, 409)
            for invalid in (envelope(claims, secrets.token_hex(32), now), signed({}, now - 31)):
                self.assertEqual(self.deliver(client, task, invalid).status_code, 403)
            for action, body in (("approve", {"revision": task["revision"], "approver": "Tester"}),
                                 ("commit", {"revision": task["revision"]}), ("pr", {})):
                response = client.post(f"/api/v1/tasks/{task['task_id']}/{action}",
                                       headers={"Authorization": f"Bearer {APPROVER}"},
                                       json={"request_id": str(uuid4()), **body})
                self.assertEqual(response.status_code, 409)
            self.assertEqual(self.task(client, task["task_id"])["job_id"], claims["test_job_id"])
            self.assertIsNone(self.task(client, task["task_id"])["approval"])
            request_id = str(uuid4())
            with patch.object(client.app.state.worker.store, "save_job", side_effect=RuntimeError("fixture database failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture database failure"):
                    self.deliver(client, task, signed({}), request_id)
            self.assertIsNone(self.task(client, task["task_id"])["approval"])
            job = self.deliver(client, task, signed({}), request_id)
            self.assertEqual(job.status_code, 202, job.text)
            result = self.wait(client, job.json()["job_id"])
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(result["stage_result"]["mr_status"], "pending")
            self.assertEqual(git(self.remote, "rev-parse", task["task_branch"] + "^{tree}"), task["revision"])
            self.assertEqual(git(self.remote, "rev-parse", "main"), task["base_commit"])
            replay = self.deliver(client, task, signed({}, now + 1), request_id)
            self.assertEqual(replay.json()["job_id"], job.json()["job_id"])
            old = self.deliver(client, task, signed({}, now - 60), request_id)
            self.assertEqual(old.json()["job_id"], job.json()["job_id"])
            bad = self.deliver(client, task, envelope(claims, secrets.token_hex(32), now), request_id)
            self.assertEqual(bad.status_code, 403)
            self.assertEqual(self.deliver(client, task, signed({"accept_attempt": 2}), request_id).status_code, 409)

    def test_post_test_drift_blocks_push(self):
        with self.delivery_client() as client:
            task = self.verified_task(client)
            (self.project / "hello.txt").write_text("unreviewed change\n")
            response = self.deliver(client, task, envelope(claims_for(task), self.key, int(time.time())))
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(self.wait(client, response.json()["job_id"])["failure_kind"], "workspace_changed")
            self.assertIsNone(self.task(client, task["task_id"])["verification"])
            self.assertEqual(git(self.remote, "branch", "--list"), "* main")

    def test_mr_retry_reuses_pushed_commit(self):
        class Adapter:
            def __init__(self):
                self.commits = []

            def create(self, task):
                self.commits.append(task["commit_sha"])
                if len(self.commits) == 1:
                    raise StageError("pr_failed", "fixture API offline")
                return {"provider": "gitlab", "id": 4, "url": "https://example.test/mr/4", "state": "opened"}

        adapter = Adapter()
        push_remote = self.root / "push-remote"
        git(self.root, "clone", "--bare", str(self.remote), str(push_remote))
        git(self.project, "remote", "set-url", "--push", "origin", str(push_remote))
        with self.delivery_client(pr_adapter=adapter) as client:
            tested = self.verified_task(client)
            claims = claims_for(tested)

            def sign(fields):
                return envelope({**claims, **fields}, self.key, int(time.time()))
            initial_id = str(uuid4())
            from git_workspace import GitWorkspace
            git_text = GitWorkspace.text

            def offline(workspace, ctx, *args, **kwargs):
                if args[0] == "ls-remote":
                    raise StageError("git_failed", "fixture remote temporarily unavailable")
                return git_text(workspace, ctx, *args, **kwargs)

            with patch.object(GitWorkspace, "text", offline):
                initial = self.deliver(client, tested, sign({}), initial_id)
                self.assertEqual(initial.status_code, 202, initial.text)
                failed = self.wait(client, initial.json()["job_id"])
            self.assertEqual((failed["action"], failed["failure_kind"]), ("commit", "pr_failed"))
            pushed = self.task(client, tested["task_id"])
            self.assertTrue(pushed["pushed"] and pushed["lease"])
            self.assertEqual(pushed["delivery"]["task_branch"], tested["task_branch"])
            self.assertEqual(self.deliver(client, tested, sign({}), initial_id).json()["job_id"], failed["job_id"])
            self.assertEqual(self.deliver(client, tested, sign({"approval_digest": "1" * 64})).status_code, 409)
            self.assertEqual(client.post(f"/api/v1/tasks/{tested['task_id']}/pr",
                                         json={"request_id": str(uuid4())}).status_code, 409)
            offline_mr = self.deliver(client, tested, sign({}))
            self.assertEqual(offline_mr.status_code, 202, offline_mr.text)
            adapter_failure = self.wait(client, offline_mr.json()["job_id"])
            self.assertEqual((adapter_failure["action"], adapter_failure["failure_kind"]), ("pr", "pr_failed"))
            self.assertIn("fixture API offline", self.task(client, tested["task_id"])["delivery"]["pr_error"])
            branch_ref = "refs/heads/" + tested["task_branch"]
            git(push_remote, "update-ref", branch_ref, tested["base_commit"])
            drift = self.deliver(client, tested, sign({}))
            self.assertEqual(drift.status_code, 202, drift.text)
            self.assertEqual(self.wait(client, drift.json()["job_id"])["failure_kind"], "pr_failed")
            git(push_remote, "update-ref", branch_ref, pushed["commit_sha"])
            retry_id = str(uuid4())
            with patch("git_workspace.GitWorkspace.push", side_effect=AssertionError("PR retry pushed again")):
                response = self.deliver(client, tested, sign({}), retry_id)
                self.assertEqual(response.status_code, 202, response.text)
                result = self.wait(client, response.json()["job_id"])
                self.assertEqual((result["action"], result["status"]), ("pr", "succeeded"), result)
            retried = self.task(client, tested["task_id"])
            self.assertEqual(retried["commit_sha"], pushed["commit_sha"])
            self.assertEqual(retried["delivery"]["pull_request"]["id"], 4)
            self.assertEqual(retried["status"], "completed")
            report = client.get(f"/api/v1/tasks/{tested['task_id']}/artifacts/{retried['report']}").json()
            self.assertEqual((report["status"], report["failure_kind"]), ("completed", None))
            self.assertEqual(report["stage_result"]["commit_sha"], pushed["commit_sha"])
            self.assertEqual(self.deliver(client, tested, sign({}), retry_id).json()["job_id"], result["job_id"])
            self.assertEqual(self.deliver(client, tested, sign({})).status_code, 409)
            self.assertEqual(adapter.commits, [pushed["commit_sha"], pushed["commit_sha"]])

    def current_delivery_job(self, client, task, request_id=None):
        proof = envelope(claims_for(task), self.key, int(time.time()))
        response = self.deliver(client, task, proof, request_id)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(client, response.json()["job_id"])

    def test_reconcile_branch_delivery_after_report_failure(self):
        with self.delivery_client() as client:
            task = self.verified_task(client)
            original_id = str(uuid4())
            with patch.object(client.app.state.worker, "report", side_effect=OSError("fixture disk offline")):
                original = self.current_delivery_job(client, task, original_id)
            self.assertEqual(original["failure_kind"], "report_io_failed")
            pushed = self.task(client, task["task_id"])
            self.assertTrue(pushed["pushed"])
            self.assertEqual(self.current_delivery_job(client, task, original_id)["job_id"], original["job_id"])
            with patch("git_workspace.GitWorkspace.push", side_effect=AssertionError("branch was already pushed")):
                recovered = self.current_delivery_job(client, task)
            self.assertEqual((recovered["action"], recovered["status"]), ("commit", "succeeded"), recovered)
            self.assertEqual(recovered["stage_result"], pushed["delivery"])
            finished = self.task(client, task["task_id"])
            report = client.get(f"/api/v1/tasks/{task['task_id']}/artifacts/{finished['report']}").json()
            self.assertEqual((report["status"], report["delivery"]["mr_status"]), ("completed", "pending"))

    def test_reconcile_persisted_mr_after_worker_recovery(self):
        class Adapter:
            def create(self, task):
                return {"provider": "gitlab", "id": 12, "state": "opened", "url": "https://example.test/mr/12"}

        adapter = Adapter()
        with self.delivery_client(adapter) as client:
            task = self.verified_task(client)
            worker = client.app.state.worker

            def interrupted(job, *args):
                worker.store.recover_interrupted(job["job_id"])

            request_id = str(uuid4())
            route = f"/api/v1/tasks/{task['task_id']}/operations/{request_id}"
            with patch.object(worker, "finish", side_effect=interrupted):
                first = self.current_delivery_job(client, task, request_id)
            self.assertIsNone(client.get(route).json()["recovered_by"])
            self.assertEqual(first["status"], "recovery_required")
            pushed = self.task(client, task["task_id"])
            self.assertEqual(pushed["delivery"]["pull_request"]["id"], 12)
            self.recover(client, task["task_id"])
            recovery_id = self.task(client, task["task_id"])["job_id"]
            self.assertEqual(client.get(route).json()["recovered_by"], recovery_id)
            with patch.object(adapter, "create", side_effect=AssertionError("MR was already recorded")), \
                    patch("git_workspace.GitWorkspace.push", side_effect=AssertionError("branch was already pushed")):
                with patch.object(worker, "report", side_effect=OSError("fixture report directory offline")):
                    lost_report = self.current_delivery_job(client, task)
                self.assertEqual(lost_report["failure_kind"], "report_io_failed", lost_report)
                self.assertEqual(self.task(client, task["task_id"])["delivery"], pushed["delivery"])
                reconciled = self.current_delivery_job(client, task)
            self.assertEqual((reconciled["action"], reconciled["status"]), ("pr", "succeeded"), reconciled)
            self.assertEqual(reconciled["stage_result"], pushed["pull_request"])
            finished = self.task(client, task["task_id"])
            report = client.get(f"/api/v1/tasks/{task['task_id']}/artifacts/{finished['report']}").json()
            self.assertEqual((report["status"], report["delivery"]), ("completed", pushed["delivery"]))

    def test_reconcile_lost_push_acknowledgement_without_recreating_branch(self):
        from git_workspace import GitWorkspace

        push_remote = self.root / "push-remote"
        git(self.root, "clone", "--bare", str(self.remote), str(push_remote))
        git(self.project, "remote", "set-url", "--push", "origin", str(push_remote))
        with self.delivery_client() as client:
            task = self.verified_task(client)
            ref = "refs/heads/" + task["task_branch"]
            real_git = GitWorkspace.git
            states_at_push = []

            def lose_ack(workspace, ctx, *args, **kwargs):
                if args[0] == "push":
                    states_at_push.append(client.app.state.worker.task(task["task_id"]).get("push_state"))
                    real_git(workspace, ctx, *args, **kwargs)
                    raise StageError("git_failed", "fixture connection lost after remote update")
                return real_git(workspace, ctx, *args, **kwargs)

            with patch.object(GitWorkspace, "git", lose_ack):
                original = self.current_delivery_job(client, task)
            self.assertEqual(original["failure_kind"], "git_failed")
            interrupted = self.task(client, task["task_id"])
            self.assertFalse(interrupted["pushed"])
            commit = interrupted["commit_sha"]
            self.assertEqual(git(push_remote, "rev-parse", ref), commit)
            self.assertEqual(states_at_push, ["in_flight"])
            git(push_remote, "update-ref", "-d", ref, commit)
            with patch.object(GitWorkspace, "push", side_effect=AssertionError("uncertain push was repeated")):
                missing = self.current_delivery_job(client, task)
                self.assertEqual(missing["failure_kind"], "push_ambiguous", missing)
                self.assertEqual(git(push_remote, "branch", "--list", task["task_branch"]), "")
                git(push_remote, "update-ref", ref, task["base_commit"])
                changed = self.current_delivery_job(client, task)
                self.assertEqual(changed["failure_kind"], "push_conflict", changed)
                self.assertEqual(git(push_remote, "rev-parse", ref), task["base_commit"])
                # An interrupted staged task saved by the earlier Worker has no push journal.
                store = client.app.state.worker.store
                with store.connect(write=True) as db:
                    previous = store.task(db, task["task_id"])
                    previous.pop("push_state")
                    previous["failure_kind"] = "recovered"
                    store.save_task(db, previous)
                git(push_remote, "update-ref", ref, commit)
                recovered = self.current_delivery_job(client, task)
            self.assertEqual((recovered["action"], recovered["status"]), ("commit", "succeeded"), recovered)
            self.assertEqual(recovered["stage_result"]["commit_sha"], commit)
            self.assertTrue(self.task(client, task["task_id"])["pushed"])
