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
        **({"commit_sha": task["verification"]["checkout"]["commit_sha"]}
           if "checkout" in task["verification"] else {}),
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

    def test_compiled_rounds_extend_one_branch_and_deliver_the_tested_commit(self):
        from git_workspace import GitWorkspace

        with self.delivery_client() as client:
            task_id = self.accepted_staged_plan(client, publish_checkpoints=True)
            for stage in ("implement", "compile"):
                result = self.stage(client, task_id, stage)
                self.assertEqual(result["status"], "succeeded", result)
            first = self.task(client, task_id)
            branch = first["task_branch"]
            first_sha = git(self.remote, "rev-parse", branch)
            self.assertEqual(first["compile_report"]["checkout"], {"branch": branch, "commit_sha": first_sha})
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), first["base_commit"])
            for stage in ("review", "test"):
                result = self.stage(client, task_id, stage)
                self.assertEqual(result["status"], "succeeded", result)
            first = self.task(client, task_id)
            self.assertEqual(first["verification"]["checkout"], first["compile_report"]["checkout"])
            first_claims = claims_for(first)

            execute = self.agent.execute

            def revise(ctx, prompt, *, review=False):
                result = execute(ctx, prompt, review=review)
                if not review:
                    (self.project / "hello.txt").write_text("revised greeting\n")
                return result

            with patch.object(self.agent, "execute", side_effect=revise):
                revised = self.stage(client, task_id, "rework", workflow_run_id=first["workflow_run_id"],
                                     implementation_attempt=1, feedback="Revise the greeting")
            self.assertEqual(revised["status"], "succeeded", revised)
            for stage in ("compile", "review", "test"):
                result = self.stage(client, task_id, stage)
                self.assertEqual(result["status"], "succeeded", result)
            task = self.task(client, task_id)
            sha = git(self.remote, "rev-parse", branch)
            self.assertNotEqual(sha, first_sha)
            self.assertEqual(git(self.remote, "rev-parse", sha + "^"), first_sha)
            self.assertEqual(git(self.remote, "rev-parse", sha + "^{tree}"), task["revision"])
            self.assertEqual(task["verification"]["checkout"], {"branch": branch, "commit_sha": sha})
            self.assertIsNone(task["commit_sha"])
            stale = envelope(first_claims, self.key, int(time.time()))
            self.assertEqual(self.deliver(client, task, stale).status_code, 409)

            claims = claims_for(task)
            self.assertEqual(self.deliver(client, task, envelope({**claims, "commit_sha": first_sha},
                                                                self.key, int(time.time()))).status_code, 409)
            run_git = GitWorkspace.git

            def interrupt_reset(workspace, ctx, *args, **kwargs):
                if args[:2] == ("reset", "--mixed"):
                    run_git(workspace, ctx, "read-tree", sha)
                    raise StageError("git_failed", "Interrupted after writing the index")
                return run_git(workspace, ctx, *args, **kwargs)

            with patch.object(GitWorkspace, "git", interrupt_reset):
                response = self.deliver(client, task, envelope(claims, self.key, int(time.time())))
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(self.wait(client, response.json()["job_id"])["failure_kind"], "git_failed")
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), task["base_commit"])

            response = self.deliver(client, task, envelope(claims, self.key, int(time.time())))
            self.assertEqual(response.status_code, 202, response.text)
            result = self.wait(client, response.json()["job_id"])
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(result["stage_result"]["commit_sha"], sha)
            self.assertEqual(git(self.remote, "rev-parse", branch), sha)
            self.assertEqual(git(self.remote, "rev-parse", "main"), task["base_commit"])
            result = self.stage(client, task_id, "release")
            self.assertEqual(result["status"], "succeeded", result)

    def test_checkpoint_retry_reuses_the_sha_after_push_reply_is_lost(self):
        from git_workspace import GitWorkspace

        with self.delivery_client() as client:
            task_id = self.accepted_staged_plan(client, publish_checkpoints=True)
            result = self.stage(client, task_id, "implement")
            self.assertEqual(result["status"], "succeeded", result)
            run_git = GitWorkspace.git

            def lose_push_reply(workspace, ctx, *args, **kwargs):
                result = run_git(workspace, ctx, *args, **kwargs)
                if args[0] == "push":
                    raise StageError("git_failed", "Lost push reply")
                return result

            with patch.object(GitWorkspace, "git", lose_push_reply):
                result = self.stage(client, task_id, "compile")
            self.assertEqual(result["failure_kind"], "git_failed", result)
            branch = self.task(client, task_id)["task_branch"]
            sha = git(self.remote, "rev-parse", branch)
            result = self.stage(client, task_id, "compile")
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(result["stage_result"]["checkout"], {"branch": branch, "commit_sha": sha})
            self.assertEqual(git(self.remote, "rev-parse", branch), sha)

    def test_checkpoint_does_not_overwrite_a_concurrently_created_task_branch(self):
        from git_workspace import GitWorkspace

        with self.delivery_client() as client:
            task_id = self.accepted_staged_plan(client, publish_checkpoints=True)
            result = self.stage(client, task_id, "implement")
            self.assertEqual(result["status"], "succeeded", result)
            task = self.task(client, task_id)
            branch = task["task_branch"]
            run_git = GitWorkspace.git

            def race_push(workspace, ctx, *args, **kwargs):
                if args[0] == "push":
                    git(self.remote, "update-ref", "refs/heads/" + branch, task["base_commit"])
                return run_git(workspace, ctx, *args, **kwargs)

            with patch.object(GitWorkspace, "git", race_push):
                result = self.stage(client, task_id, "compile")
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(git(self.remote, "rev-parse", branch), task["base_commit"])

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

    def test_recovered_release_completes_original_delivery_and_returns_lease(self):
        with self.delivery_client() as client:
            task = self.verified_task(client)
            delivery = self.current_delivery_job(client, task)
            self.assertEqual(delivery["status"], "succeeded", delivery)
            task_id = task["task_id"]
            worker = client.app.state.worker
            with patch.object(worker, "finish", side_effect=lambda job, *_: worker.store.recover_interrupted(job["job_id"])):
                request_id = str(uuid4())
                interrupted = self.stage(client, task_id, "release", request_id=request_id)
            self.assertEqual(interrupted["status"], "recovery_required", interrupted)
            self.recover(client, task_id)
            current = self.task(client, task_id)
            lookup = client.get(f"/api/v1/tasks/{task_id}/operations/{request_id}").json()
            self.assertEqual(lookup["recovered_by"], current["job_id"])
            self.assertTrue(current["lease"])
            self.assertEqual(current["failure_kind"], "recovered")
            resumed = self.stage(client, task_id, "release")
            self.assertEqual(resumed["stage_result"], {"released": True})
            current = self.task(client, task_id)
            self.assertEqual(current["status"], "completed")
            self.assertFalse(current["lease"])
            self.assertEqual(current["verification"], task["verification"])
            self.assertEqual(current["delivery"], delivery["stage_result"])

    def test_reconcile_persisted_mr_after_worker_recovery(self):
        class Adapter:
            def create(self, task):
                return {"provider": "gitlab", "id": 12, "state": "opened", "url": "https://example.test/mr/12"}

        adapter = Adapter()
        with self.delivery_client(adapter) as client:
            task = self.verified_task(client)
            worker = client.app.state.worker

            def interrupted(job, *args):
                cancel = worker.action(task["task_id"], "cancel", str(uuid4()), {})
                self.assertEqual(cancel["status"], "succeeded")
                self.assertEqual(worker.task(task["task_id"])["job_id"], job["job_id"])
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

    def direct_client(self):
        from dataclasses import replace
        from fastapi.testclient import TestClient
        from test_planning import PlanningAgent
        from worker import create_app
        from worker_config import Settings, legacy_service

        with patch.dict(os.environ, {"UNITY_WORKER_WORKFLOW_APPROVAL_KEY": self.key}):
            settings = replace(Settings.load(), test_assemblies={"editmode": ("Tests",), "playmode": ("PlayTests",)},
                               test_modes=("editmode", "playmode"))
        cfg = legacy_service(settings)
        pool = cfg.pools[cfg.default_pool]
        cfg.pools[pool.id] = replace(pool, direct_merge=True)
        cfg = replace(cfg, implicit=False)
        self.agent = PlanningAgent(self.project)
        original_plan = self.agent.plan

        def scoped_plan(ctx, briefing):
            plan = original_plan(ctx, briefing)
            plan["test_selection"]["playmode"] = [{"assembly": "PlayTests", "fixtures": ["Game.UnitTests"]}]
            return plan

        self.agent.plan = scoped_plan
        return TestClient(create_app(cfg, runner=self.runner, agent=self.agent),
                          headers={"Authorization": f"Bearer {TOKEN}"})

    def direct_task(self, client, *, conflict=False, mode=True):
        if conflict:
            source = self.project / "Example.cs"
            source.write_text("class Base {}\n")
            git(self.project, "add", "Example.cs")
            git(self.project, "commit", "-m", "original class")
            git(self.project, "push", "origin", "main")
        execute = self.agent.execute

        def edit(ctx, prompt, *, review=False):
            result = execute(ctx, prompt, review=review)
            if not review:
                (self.project / "hello.txt").write_text(ctx.task["task_id"] + "\n")
                if conflict:
                    source.write_text("class Feature {}\n")
            return result

        options = {"delivery_mode": "direct-merge"} if mode else {}
        task_id = self.accepted_staged_plan(client, publish_checkpoints=True, **options)
        with patch.object(self.agent, "execute", side_effect=edit):
            result = self.stage(client, task_id, "run")
            self.assertEqual(result["status"], "succeeded", result)
        return self.task(client, task_id)

    def advance_target(self, *, conflict=False):
        other = self.root / "other"
        git(self.root, "clone", str(self.remote), str(other))
        path = "Example.cs" if conflict else "concurrent.txt"
        (other / path).write_text("class Main {}\n")
        git(other, "add", path)
        git(other, "commit", "-m", "concurrent target edit")
        git(other, "push", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def direct_proof(self, task):
        return envelope({**claims_for(task), "delivery_mode": "direct-merge"}, self.key, int(time.time()))

    def merge_job(self, client, task, request_id=None):
        response = self.deliver(client, task, self.direct_proof(task), request_id)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(client, response.json()["job_id"])

    def test_direct_delivery_requires_signed_mode_and_pool_opt_in(self):
        from dataclasses import replace

        with self.direct_client() as client:
            self.assertIn("staged-feature-direct-merge", client.get("/api/v1/workers").json()["workers"][0]["capabilities"])
            task = self.direct_task(client)
            task_id = task["task_id"]
            claims, now = claims_for(task), int(time.time())
            proof = self.direct_proof(task)
            self.assertEqual(self.deliver(client, task, envelope(claims, self.key, now)).status_code, 409)
            self.assertEqual(self.deliver(client, task, {**proof, "signature": "0" * 64}).status_code, 403)
            missing_sha = {key: value for key, value in proof["claims"].items() if key != "commit_sha"}
            self.assertEqual(self.deliver(client, task, envelope(missing_sha, self.key, now)).status_code, 403)
            for action in ("commit", "merge", "pr"):
                body = {"revision": task["revision"]} if action == "commit" else {}
                response = client.post(f"/api/v1/tasks/{task_id}/{action}", json={"request_id": str(uuid4()), **body})
                self.assertIn(response.status_code, (409, 422))
            pools = client.app.state.worker.cfg.pools
            pool = pools[task["pool_id"]]
            self.assertIs(client.get("/api/v1/pools").json()["pools"][0]["direct_merge"], True)
            with patch.dict(pools, {pool.id: replace(pool, direct_merge=False)}):
                self.assertEqual(self.deliver(client, task, proof).status_code, 409)
            self.assertEqual(git(self.remote, "rev-parse", "main"), task["base_commit"])
            request_id = str(uuid4())
            job = self.merge_job(client, task, request_id)
            self.assertEqual((job["action"], job["status"]), ("merge", "succeeded"), job)
            self.assertEqual(self.merge_job(client, task, request_id)["job_id"], job["job_id"])
            self.assertEqual(self.deliver(client, task, envelope(claims, self.key, now), request_id).status_code, 409)
            self.stage(client, task_id, "release")
            legacy = self.direct_task(client, mode=False)
            self.assertEqual(self.deliver(client, legacy, self.direct_proof(legacy)).status_code, 409)

    def check_candidate_failure(self, fault, expected):
        with self.direct_client() as client:
            task = self.direct_task(client)
            base = self.advance_target()
            if fault in ("broken_compile", "broken_tests"):
                (self.root / fault).touch()
            workspace = client.app.state.worker.workspaces[task["slot_id"]]
            run = workspace.unity.run
            calls = []

            def unity(ctx, mode, **kwargs):
                self.assertEqual(git(self.project, "branch", "--show-current"), "")
                calls.append(mode)
                result = run(ctx, mode, **kwargs)
                if fault == "drift":
                    (self.project / "unexpected.meta").write_text("unapproved\n")
                return result

            with patch.object(workspace.unity, "run", side_effect=unity):
                job = self.merge_job(client, task)
            self.assertEqual(job["failure_kind"], expected, job)
            journal = self.task(client, task["task_id"])["merge_journal"]
            failure = journal.get("verification_failure") or {}
            self.assertEqual(failure.get("kind"), expected, journal)
            self.assertEqual(failure["result"]["step"], "editmode" if fault == "broken_tests" else "compile")
            self.assertEqual(calls, ["compile", "editmode"] if fault == "broken_tests" else ["compile"])
            self.assertEqual(git(self.remote, "rev-parse", "main"), base)
            for key in ("verification", "test_report", "compile_report", "review_result"):
                self.assertEqual(self.task(client, task["task_id"])[key], task[key])
            release = self.stage(client, task["task_id"], "release")
            self.assertEqual(release["status"], "failed", release)
            self.assertTrue(self.task(client, task["task_id"])["lease"])
            for action in ("run", "fix"):
                response = client.post(f"/api/v1/tasks/{task['task_id']}/{action}", json={"request_id": str(uuid4())})
                self.assertEqual(response.status_code, 409)
            self.assertEqual(self.deliver(client, task, self.direct_proof(task)).status_code, 409)

    def test_direct_delivery_blocks_unity_workspace_drift(self):
        self.check_candidate_failure("drift", "dirty_workspace")

    def test_direct_delivery_preserves_compile_failure(self):
        self.check_candidate_failure("broken_compile", "compile_failed")

    def test_direct_delivery_preserves_test_failure(self):
        self.check_candidate_failure("broken_tests", "tests_failed")

    def test_direct_delivery_retries_journal_and_releases_original_checkout(self):
        with self.direct_client() as client:
            task = self.direct_task(client, conflict=True)
            task_id = task["task_id"]
            base = self.advance_target(conflict=True)
            worker = client.app.state.worker
            calls, reviews, solves = [], [], []
            workspace = worker.workspaces[task["slot_id"]]
            unity = workspace.unity.run

            def verify(ctx, mode, **kwargs):
                calls.append((mode, kwargs.get("scope"), git(self.project, "rev-parse", "HEAD")))
                self.assertEqual(git(self.project, "branch", "--show-current"), "")
                return unity(ctx, mode, **kwargs)

            def agent(ctx, prompt, *, review=False):
                if review:
                    reviews.append(prompt)
                    return {"verdict": "pass", "issues": [], "summary": "both changes kept", "session_id": "independent"}
                solves.append(prompt)
                (self.project / "Example.cs").write_text("class Main {}\nclass Feature {}\n")
                return {"success": True, "session_id": "solver"}

            save = worker.save

            def stop_at_candidate(job, **updates):
                if updates.get("merge_journal", {}).get("merge_sha"):
                    raise StageError("recovery_required", "Interrupted before the candidate SHA reached SQLite")
                return save(job, **updates)

            with patch.object(self.agent, "execute", side_effect=agent), patch.object(workspace.unity, "run", side_effect=verify):
                with patch.object(worker, "save", side_effect=stop_at_candidate):
                    interrupted = self.merge_job(client, task)
                self.assertEqual(interrupted["status"], "recovery_required", interrupted)
                candidate = git(self.project, "rev-parse", "HEAD")
                self.assertNotEqual(candidate, base)
                self.assertNotIn("merge_sha", self.task(client, task_id)["merge_journal"])
                (self.project / "unexpected.meta").touch()
                recovered = client.post(f"/api/v1/tasks/{task_id}/recover", headers={"Authorization": f"Bearer {APPROVER}"},
                                        json={"request_id": str(uuid4()), "confirmed_stopped": True, "note": "stopped"})
                self.assertEqual(self.wait(client, recovered.json()["job_id"])["status"], "failed")
                (self.project / "unexpected.meta").unlink()
                self.recover(client, task_id)
                self.assertEqual(git(self.project, "rev-parse", "HEAD"), candidate)
                merged = self.merge_job(client, task)
            self.assertEqual(merged["status"], "succeeded", merged)
            self.assertEqual((len(solves), len(reviews)), (1, 1))
            for evidence in ("class Main", "class Feature", "Example.cs"):
                self.assertIn(evidence, reviews[0])
            self.assertEqual(calls, [("compile", None, candidate),
                                     ("editmode", {"assembly": "Tests", "fixtures": ["Game.UnitTests"]}, candidate),
                                     ("playmode", {"assembly": "PlayTests", "fixtures": ["Game.UnitTests"]}, candidate)])
            current = self.task(client, task_id)
            self.assertEqual(git(self.remote, "rev-parse", "main"), current["delivery"]["merge_sha"])
            self.assertEqual(git(self.project, "branch", "--show-current"), task["task_branch"])
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), task["verification"]["checkout"]["commit_sha"])
            for key in ("test_report", "compile_report", "review_result", "verification"):
                self.assertEqual(current[key], task[key])
            with patch.object(worker, "finish", side_effect=lambda job, *_: worker.store.recover_interrupted(job["job_id"])):
                self.assertEqual(self.stage(client, task_id, "release")["status"], "recovery_required")
            self.recover(client, task_id)
            self.assertEqual(self.stage(client, task_id, "release")["status"], "succeeded")
            self.assertFalse(self.task(client, task_id)["lease"])
            self.assertEqual(self.task(client, task_id)["status"], "completed")

    def test_direct_delivery_recovers_remote_push_and_failed_receipt_write(self):
        from direct_merge import DirectMergeGit

        with self.direct_client() as client:
            task = self.direct_task(client)
            worker = client.app.state.worker
            real_push = DirectMergeGit.push

            def interrupted_push(git_merge, ctx, record, candidate):
                real_push(git_merge, ctx, record, candidate)
                raise StageError("recovery_required", "Worker stopped before persisting acknowledgement")

            with patch.object(DirectMergeGit, "push", interrupted_push):
                original = self.merge_job(client, task)
            self.assertEqual(original["status"], "recovery_required", original)
            journal = self.task(client, task["task_id"])["merge_journal"]
            self.assertEqual(journal["status"], "in_flight")
            self.assertEqual(git(self.remote, "rev-parse", "main"), journal["merge_sha"])
            self.recover(client, task["task_id"])
            release = client.post(f"/api/v1/tasks/{task['task_id']}/release", json={"request_id": str(uuid4())})
            self.assertEqual(release.status_code, 409)
            save = worker.save

            def lose_receipt(job, **change):
                if "delivery" in change:
                    raise OSError("Disk full after checkout restoration")
                return save(job, **change)

            with patch.object(DirectMergeGit, "push", side_effect=AssertionError("duplicate target push")):
                with patch.object(worker, "save", side_effect=lose_receipt):
                    failed = self.merge_job(client, task)
                self.assertEqual(failed["status"], "failed", failed)
                self.assertEqual(self.task(client, task["task_id"])["merge_journal"]["status"], "confirmed")
                response = client.post(f"/api/v1/tasks/{task['task_id']}/release", json={"request_id": str(uuid4())})
                self.assertEqual(response.status_code, 409)
                reconciled = self.merge_job(client, task)
            self.assertEqual(reconciled["status"], "succeeded", reconciled)
            self.assertEqual(reconciled["stage_result"]["merge_sha"], journal["merge_sha"])
            git(self.project, "switch", "--detach", journal["merge_sha"])
            wrong_checkout = self.stage(client, task["task_id"], "release")
            self.assertEqual(wrong_checkout["failure_kind"], "workspace_changed")
            self.assertTrue(self.task(client, task["task_id"])["lease"])
            git(self.project, "switch", task["task_branch"])
            self.assertEqual(self.stage(client, task["task_id"], "release")["status"], "succeeded")

    def test_direct_delivery_recovers_the_exact_unrecorded_clean_merge(self):
        with self.direct_client() as client:
            task = self.direct_task(client)
            self.advance_target()
            worker = client.app.state.worker
            save = worker.save

            def interrupt(job, **changes):
                if changes.get("merge_journal", {}).get("merge_sha"):
                    raise StageError("recovery_required", "Clean candidate pin persisted before task update")
                return save(job, **changes)

            with patch.object(worker, "save", side_effect=interrupt):
                interrupted = self.merge_job(client, task)
            self.assertEqual(interrupted["status"], "recovery_required", interrupted)
            candidate = git(self.project, "rev-parse", "HEAD")
            self.recover(client, task["task_id"])
            recovered = self.task(client, task["task_id"])
            self.assertEqual(recovered["merge_journal"]["merge_sha"], candidate)
            with patch("direct_merge.DirectMergeGit.begin", side_effect=AssertionError("candidate was rebuilt")):
                delivered = self.merge_job(client, task)
            self.assertEqual(delivered["status"], "succeeded", delivered)
            self.assertEqual(git(self.remote, "rev-parse", "main"), candidate)

    def test_operator_restored_unlanded_conflict_can_release_failed_task(self):
        with self.direct_client() as client:
            task = self.direct_task(client)
            task_id = task["task_id"]
            source = task["verification"]["checkout"]["commit_sha"]
            other = self.root / "other"
            git(self.root, "clone", str(self.remote), str(other))
            (other / "hello.txt").write_text("concurrent greeting\n")
            git(other, "commit", "-am", "independent main greeting")
            git(other, "push", "origin", "main")
            base = git(self.remote, "rev-parse", "main")
            failed = self.merge_job(client, task)
            self.assertEqual(failed["failure_kind"], "merge_conflict", failed)
            self.assertTrue((self.project / ".git/MERGE_HEAD").is_file())
            release = self.stage(client, task_id, "release")
            self.assertEqual(release["status"], "failed", release)
            self.assertTrue(self.task(client, task_id)["lease"])
            self.assertTrue((self.project / ".git/MERGE_HEAD").is_file())
            # The operator archives the conflict and restores the source independently.
            (self.root / "archived-hello.txt").write_bytes((self.project / "hello.txt").read_bytes())
            git(self.project, "merge", "--abort")
            git(self.project, "switch", task["task_branch"])
            self.assertEqual(git(self.project, "rev-parse", "HEAD"), source)
            (self.project / "extra.meta").touch()
            dirty = self.stage(client, task_id, "release")
            self.assertEqual(dirty["failure_kind"], "dirty_workspace")
            self.assertTrue((self.project / "extra.meta").exists())
            (self.project / "extra.meta").rename(self.root / "archived-extra.meta")
            pin_ref = "refs/unity-worker/direct-merge/" + task["task_branch"].split("/")[-1]
            git(self.project, "update-ref", pin_ref, source, base)
            wrong_pin = self.stage(client, task_id, "release")
            self.assertEqual(wrong_pin["failure_kind"], "workspace_changed")
            self.assertEqual(git(self.project, "rev-parse", pin_ref), source)
            git(self.project, "update-ref", pin_ref, base, source)
            release = self.stage(client, task_id, "release")
            self.assertEqual(release["stage_result"], {"released": True}, release)
            current = self.task(client, task_id)
            self.assertFalse(current["lease"])
            self.assertEqual((current["status"], current["failure_kind"]), ("failed", "merge_conflict"))
            self.assertFalse(current.get("delivery"))
            self.assertFalse(current["pushed"])
            self.assertEqual(git(self.remote, "rev-parse", "main"), base)

    def test_confirmed_merge_retries_restore_after_operator_archives_dirty_file(self):
        from direct_merge import DirectMergeGit

        with self.direct_client() as client:
            task = self.direct_task(client)
            task_id = task["task_id"]
            restore = DirectMergeGit.restore
            dirty = self.project / "after-push.meta"

            def dirty_on_restore(git_merge, ctx, record, source):
                if record["merge_journal"]["status"] == "confirmed":
                    dirty.write_text("operator evidence\n")
                return restore(git_merge, ctx, record, source)

            with patch.object(DirectMergeGit, "restore", dirty_on_restore):
                failed = self.merge_job(client, task)
            self.assertEqual(failed["failure_kind"], "dirty_workspace", failed)
            journal = self.task(client, task_id)["merge_journal"]
            self.assertEqual(journal["status"], "confirmed")
            self.assertEqual(git(self.remote, "rev-parse", "main"), journal["receipt"]["merge_sha"])
            release = client.post(f"/api/v1/tasks/{task_id}/release", json={"request_id": str(uuid4())})
            self.assertEqual(release.status_code, 409)
            with patch.object(DirectMergeGit, "push", side_effect=AssertionError("unexpected second push")):
                again = self.merge_job(client, task)
                self.assertEqual(again["failure_kind"], "dirty_workspace", again)
                self.assertTrue(dirty.exists())
                dirty.rename(self.root / "archived-after-push.meta")
                merged = self.merge_job(client, task)
            self.assertEqual(merged["status"], "succeeded", merged)
            self.assertEqual(merged["stage_result"], journal["receipt"])
            self.assertEqual(git(self.project, "branch", "--show-current"), task["task_branch"])
            self.assertEqual(self.stage(client, task_id, "release")["status"], "succeeded")

    def check_interrupted_abandonment(self, *, solved):
        with self.direct_client() as client:
            task = self.direct_task(client, conflict=True)
            task_id = task["task_id"]
            base = self.advance_target(conflict=True)
            worker = client.app.state.worker

            def solver(ctx, prompt, *, review=False):
                if not solved:
                    raise StageError("agent_failed", "Cannot resolve the conflict", result={"session_id": "solver"})
                (self.project / "Example.cs").write_text("class Main {}\nclass Feature {}\n")
                return {"success": True, "session_id": "solver"}

            if solved:
                (self.root / "broken_compile").touch()
            with patch.object(self.agent, "execute", side_effect=solver):
                failed = self.merge_job(client, task)
            expected = "compile_failed" if solved else "agent_failed"
            self.assertEqual(failed["failure_kind"], expected, failed)
            current = self.task(client, task_id)
            fields = ("status", "current_stage", "finished_at", "failure_kind", "error_message", "stage_result", "exit_code")
            outcome = {key: current[key] for key in fields}
            journal = current["merge_journal"]
            self.assertEqual(journal["conflict_paths"], ["Example.cs"])
            self.assertEqual(bool(journal.get("merge_sha")), solved)
            if not solved:
                (self.root / "archived-conflict.txt").write_bytes((self.project / "Example.cs").read_bytes())
                git(self.project, "merge", "--abort")
            git(self.project, "switch", task["task_branch"])
            pin_ref = "refs/unity-worker/direct-merge/" + task["task_branch"].split("/")[-1]
            pin = git(self.project, "rev-parse", pin_ref)
            self.assertEqual(pin, journal.get("merge_sha", base))

            # Crash once immediately after admission, then once after the operator archives the pin.
            def never_started(job, payload):
                worker.store.recover_interrupted(job["job_id"])
                return worker.job(job["job_id"])

            with patch.object(worker, "spawn", side_effect=never_started):
                interrupted = self.stage(client, task_id, "release")
            self.assertEqual(interrupted["status"], "recovery_required", interrupted)
            dirty = self.project / "operator.meta"
            dirty.touch()
            blocked = client.post(f"/api/v1/tasks/{task_id}/recover", headers={"Authorization": f"Bearer {APPROVER}"},
                                  json={"request_id": str(uuid4()), "confirmed_stopped": True, "note": "stopped"})
            self.assertEqual(self.wait(client, blocked.json()["job_id"])["status"], "failed")
            self.assertTrue(dirty.exists())
            dirty.rename(self.root / "archived-operator.meta")
            self.recover(client, task_id)
            current = self.task(client, task_id)
            self.assertTrue(current["lease"])
            self.assertEqual({key: current[key] for key in fields}, outcome)
            git(self.project, "update-ref", "refs/archive/test-abandonment", pin)
            git(self.project, "update-ref", "-d", pin_ref, pin)
            with patch.object(worker, "finish", side_effect=lambda job, *_: worker.store.recover_interrupted(job["job_id"])):
                interrupted = self.stage(client, task_id, "release")
            self.assertEqual(interrupted["status"], "recovery_required", interrupted)
            self.recover(client, task_id)
            self.assertEqual(self.stage(client, task_id, "release")["status"], "succeeded")
            current = self.task(client, task_id)
            self.assertFalse(current["lease"])
            self.assertEqual({key: current[key] for key in fields}, outcome)
            self.assertFalse(current.get("delivery"))
            self.assertEqual(git(self.project, "rev-parse", "refs/archive/test-abandonment"), pin)
            self.assertEqual(git(self.remote, "rev-parse", "main"), base)

    def test_interrupted_abandonment_after_unsolved_conflict(self):
        self.check_interrupted_abandonment(solved=False)

    def test_interrupted_abandonment_after_candidate_compile_failure(self):
        self.check_interrupted_abandonment(solved=True)
