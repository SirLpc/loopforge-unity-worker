import os
import sys
import threading
import time
import unittest
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

from development import DevelopmentWorker
from support import test_directory
from test_workspace import git, repository
from test_workflow import APPROVER, TOKEN, FixtureRunner
from worker import create_app
from worker_config import load_service_settings


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        remote, self.first, logs = repository(self.root)
        self.second = self.root / "second"
        other_remote = self.root / "other-remote"
        other = self.root / "other"
        git(self.root, "clone", str(remote), str(self.second))
        git(self.root, "clone", "--bare", str(remote), str(other_remote))
        git(self.root, "clone", str(other_remote), str(other))
        origins = [git(self.first, "remote", "get-url", *args, "origin") for args in ((), ("--push",))]
        alternate = [git(other, "remote", "get-url", *args, "origin") for args in ((), ("--push",))]
        self.enterContext(patch.dict(os.environ, {"UNITY_WORKER_TOKEN": TOKEN, "UNITY_WORKER_APPROVAL_TOKEN": APPROVER}))
        self.config = load_service_settings(config={
            "worker_id": "fixture", "logs": str(logs), "unity_exe": sys.executable, "unity_version": "2022.3.62f3",
            "default_pool": "game", "legacy_slot": "worker-01",
            "pools": {
                "game": {"remote_identity": sha256("\0".join(origins).encode()).hexdigest(), "slots": [
                    {"id": f"worker-0{index}", "git_root": str(repo), "project": str(repo)}
                    for index, repo in enumerate((self.first, self.second), 1)
                ]},
                "tools": {"remote_identity": sha256("\0".join(alternate).encode()).hexdigest(), "slots": [
                    {"id": "tools-01", "git_root": str(other), "project": str(other)},
                ]},
            },
        })
        self.enterContext(patch("unity_executor.unity_product_version", return_value="2022.3.62f3"))
        # Directory contents have real Git coverage in test_pool_config and the full pipeline below.
        self.enterContext(patch("development.check_slot", return_value=None))

    def client(self, **kwargs):
        return TestClient(create_app(self.config, **kwargs), headers={"Authorization": f"Bearer {TOKEN}"})

    def create(self, client, *, request_id=None, **extra):
        response = client.post("/api/v1/tasks", json={"request_id": request_id or str(uuid4()),
                               "requirement": "Update greeting", "plan": "Edit hello.txt", "acceptance_criteria": ["Updated"], **extra})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["task_id"]

    def action(self, client, task_id, action, *, request_id=None, operator=False, **extra):
        headers = {"Authorization": f"Bearer {APPROVER}"} if operator else None
        response = client.post(f"/api/v1/tasks/{task_id}/{action}", headers=headers,
                               json={"request_id": request_id or str(uuid4()), **extra})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def task(self, client, task_id):
        return client.get(f"/api/v1/tasks/{task_id}").json()

    def until(self, query, predicate):
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            observed = query()
            if predicate(observed):
                return observed
            time.sleep(0.04)
        self.fail(f"Timed out waiting for state. Last response {observed}")

    def job(self, client, job_id):
        return client.get(f"/api/v1/jobs/{job_id}").json()

    def wait(self, client, job):
        return self.until(lambda: self.job(client, job["job_id"]), lambda value: value["status"] not in {"queued", "running"})

    def test_two_slots_run_and_third_starts_in_fifo_order(self):
        started = {"worker-01": threading.Event(), "worker-02": threading.Event()}
        allow = threading.Event()
        self.addCleanup(allow.set)
        paths = {}

        def prepare(_worker, ctx):
            slot_id = ctx.task["slot_id"]
            paths[slot_id] = ctx.settings.checkout
            if slot_id in started:
                started[slot_id].set()
                allow.wait(12)
            return {}

        with patch.object(DevelopmentWorker, "stage_prepare", prepare), self.client() as client:
            self.assertEqual([p["id"] for p in client.get("/api/v1/pools").json()["pools"]], ["game", "tools"])
            tasks = [self.create(client) for _ in range(4)]
            jobs = [self.action(client, task, "prepare", queue_wait_seconds=90) for task in tasks]
            try:
                self.assertTrue(all(event.wait(10) for event in started.values()))
                self.assertEqual(paths, {"worker-01": self.first, "worker-02": self.second})
                self.assertEqual([self.job(client, job["job_id"])["status"] for job in jobs[2:]], ["queued", "queued"])
                self.assertIsNone(self.task(client, tasks[2])["slot_id"])
                self.assertEqual(client.get("/api/v1/pools").json()["pools"][0]["queued_count"], 2)
            finally:
                allow.set()
            self.assertEqual(self.wait(client, jobs[0])["status"], "succeeded")
            self.action(client, tasks[0], "cancel")
            self.assertEqual(self.wait(client, self.action(client, tasks[0], "release"))["status"], "succeeded")
            self.assertEqual(self.wait(client, jobs[2])["status"], "succeeded")
            self.assertEqual(self.task(client, tasks[2])["slot_id"], "worker-01")
            self.assertEqual(self.job(client, jobs[3]["job_id"])["status"], "queued")
            other = self.create(client, pool_id="tools")
            self.assertEqual(self.wait(client, self.action(client, other, "prepare"))["status"], "succeeded")
            self.assertEqual(self.task(client, other)["slot_id"], "tools-01")

    def test_duplicate_request_and_cancel_queued_task(self):
        first = self.config.pools["game"].slots[0]
        self.config = replace(self.config, pools={**self.config.pools, "game": replace(self.config.pools["game"], slots=(first,))})
        with patch.object(DevelopmentWorker, "stage_prepare", return_value={}), self.client() as client:
            create_id = str(uuid4())
            task = self.create(client, request_id=create_id)
            self.assertEqual(self.create(client, request_id=create_id, pool_id="game"), task)
            self.assertEqual(self.wait(client, self.action(client, task, "prepare"))["status"], "succeeded")
            second = self.create(client)
            request_id = str(uuid4())
            jobs = [self.action(client, second, "prepare", request_id=request_id, queue_wait_seconds=90) for _ in range(2)]
            self.assertEqual(jobs[0]["job_id"], jobs[1]["job_id"])
            self.action(client, second, "cancel")
            self.assertEqual(self.job(client, jobs[0]["job_id"])["status"], "cancelled")
            self.assertIsNone(self.task(client, second)["slot_id"])
            self.assertFalse(self.task(client, second)["lease"])

    def test_deadline_and_restart_preserve_queue_and_binding(self):
        first = self.config.pools["game"].slots[0]
        self.config = replace(self.config, pools={**self.config.pools, "game": replace(self.config.pools["game"], slots=(first,))})
        with patch.object(DevelopmentWorker, "stage_prepare", return_value={}):
            with self.client() as client:
                owner = self.create(client)
                self.wait(client, self.action(client, owner, "prepare"))
                expired = self.create(client)
                expiry_job = self.action(client, expired, "prepare", queue_wait_seconds=1)
                self.assertEqual(self.wait(client, expiry_job)["failure_kind"], "queue_timeout")
                pending = self.create(client)
                job = self.action(client, pending, "prepare", queue_wait_seconds=120)
                self.assertEqual(job["status"], "queued")
            with self.client() as client:
                self.assertEqual(self.job(client, job["job_id"])["status"], "queued")
                self.assertEqual(self.task(client, owner)["slot_id"], "worker-01")
                self.action(client, owner, "cancel")
                self.assertEqual(self.wait(client, self.action(client, owner, "release"))["status"], "succeeded")
                self.assertEqual(self.wait(client, job)["status"], "succeeded")
                self.assertEqual(self.task(client, pending)["slot_id"], "worker-01")

    def test_spawn_failure_does_not_recover_other_running_jobs(self):
        entered = threading.Event()
        release = threading.Event()

        def prepare(_worker, _ctx):
            entered.set()
            release.wait(12)
            return {}

        original = threading.Thread.start

        def fail_selected_start(thread):
            if thread.name == "unity-prepare":
                raise RuntimeError("Cannot start thread")
            return original(thread)

        with patch.object(DevelopmentWorker, "stage_prepare", prepare), self.client() as client:
            first, second = self.create(client), self.create(client)
            old = self.action(client, first, "prepare")
            try:
                self.assertTrue(entered.wait(5))
                with patch.object(threading.Thread, "start", fail_selected_start):
                    failed = self.action(client, second, "prepare")
                self.assertEqual(failed["status"], "recovery_required")
                self.assertEqual(self.job(client, old["job_id"])["status"], "running")
                self.assertEqual(self.task(client, second)["slot_id"], "worker-02")
            finally:
                release.set()
            self.assertEqual(self.wait(client, old)["status"], "succeeded")

    def prepared_tasks(self, client):
        tasks = [self.create(client), self.create(client)]
        with patch.object(DevelopmentWorker, "stage_prepare", return_value={}), \
                patch.object(DevelopmentWorker, "stage_implement", return_value={}):
            jobs = [self.action(client, task, "prepare") for task in tasks]
            for task, job in zip(tasks, jobs):
                self.assertEqual(self.wait(client, job)["status"], "succeeded")
                self.assertEqual(self.wait(client, self.action(client, task, "implement"))["status"], "succeeded")
        return tasks

    def concurrent_compile(self, parallel):
        self.config = replace(self.config, max_parallel_unity=parallel)
        delegate = FixtureRunner(self.root)
        release = threading.Event()
        active = set()
        observed = set()
        lock = threading.Lock()

        original = delegate.run

        def observe(argv, cwd, prefix, **kwargs):
            if "-projectPath" not in argv:
                return original(argv, cwd, prefix, **kwargs)
            path = argv[argv.index("-projectPath") + 1]
            with lock:
                active.add(path)
                observed.add(path)
            try:
                release.wait(12)
                return original(argv, cwd, prefix, **kwargs)
            finally:
                with lock:
                    active.remove(path)

        delegate.run = observe
        with patch.object(DevelopmentWorker, "stage_compile", lambda _, ctx: ctx.workspace.unity.run(ctx, "compile")), \
                self.client(runner=delegate) as client:
            tasks = self.prepared_tasks(client)
            jobs = [self.action(client, task, "compile") for task in tasks]
            try:
                self.until(lambda: len(active), lambda count: count >= parallel)
                if parallel == 1:
                    self.assertTrue(all(self.job(client, job["job_id"])["status"] == "running" for job in jobs))
                    self.assertFalse(release.wait(0.4))
                self.assertEqual(len(active), parallel)
            finally:
                release.set()
            self.assertTrue(all(self.wait(client, job)["status"] == "succeeded" for job in jobs))
            self.assertEqual(observed, {str(self.first), str(self.second)})

    def test_two_tasks_pass_git_and_codex_with_one_editor_permit(self):
        self.concurrent_compile(1)

    def test_editor_limit_two_allows_two_project_paths(self):
        self.concurrent_compile(2)

    def test_third_checkout_waits_for_editor_and_active_cancellation_admits_it(self):
        from process_runner import StageError
        from task_store import Store

        third = self.root / "extra-checkout"
        git(self.root, "clone", str(self.root / "remote"), str(third))
        pool = self.config.pools["game"]
        ids = ("map-north", "shader-lab", "nightly-x7")
        paths = tuple(map(str, (self.first, self.second, third)))
        slots = (
            replace(pool.slots[0], id=ids[0]),
            replace(pool.slots[1], id=ids[1]),
            replace(pool.slots[1], id=ids[2], settings=replace(pool.slots[1].settings, project=third, git_root=third)),
        )
        self.config = replace(self.config, legacy_slot=ids[0], max_parallel_unity=2,
                              pools={**self.config.pools, "game": replace(pool, slots=slots)})
        started = {path: threading.Event() for path in paths}
        finish = threading.Event()
        waiting = threading.Event()
        active = set()
        lock = threading.Lock()
        delegate = FixtureRunner(self.root)
        original_run = delegate.run
        original_take = Store.take_unity_permit

        def run(argv, cwd, prefix, **kwargs):
            if "-projectPath" not in argv:
                return original_run(argv, cwd, prefix, **kwargs)
            path = argv[argv.index("-projectPath") + 1]
            with lock:
                active.add(path)
                started[path].set()
            try:
                while not finish.wait(0.05):
                    if kwargs["cancel"].is_set():
                        raise StageError("cancelled", "Fixture Unity invocation cancelled")
                return original_run(argv, cwd, prefix, **kwargs)
            finally:
                with lock:
                    active.remove(path)

        def take(db, job_id, limit):
            acquired = original_take(db, job_id, limit)
            if not acquired:
                waiting.set()
            return acquired

        delegate.run = run
        with patch.object(DevelopmentWorker, "stage_prepare", return_value={}), \
                patch.object(DevelopmentWorker, "stage_implement", return_value={}), \
                patch.object(DevelopmentWorker, "stage_compile", lambda _, ctx: ctx.workspace.unity.run(ctx, "compile")), \
                patch.object(Store, "take_unity_permit", staticmethod(take)), self.client(runner=delegate) as client:
            try:
                first, second = self.prepared_tasks(client)
                first_job = self.action(client, first, "compile")
                second_job = self.action(client, second, "compile")
                self.assertTrue(started[paths[0]].wait(10))
                self.assertTrue(started[paths[1]].wait(10))

                extra = self.create(client)
                for stage in ("prepare", "implement"):
                    self.assertEqual(self.wait(client, self.action(client, extra, stage))["status"], "succeeded")
                self.assertEqual([self.task(client, task)["slot_id"] for task in (first, second, extra)], list(ids))
                extra_job = self.action(client, extra, "compile")
                self.assertTrue(waiting.wait(10))
                with lock:
                    self.assertEqual(active, set(paths[:2]))
                self.assertEqual(self.job(client, extra_job["job_id"])["status"], "running")

                self.action(client, first, "cancel")
                self.assertEqual(self.wait(client, first_job)["status"], "cancelled")
                self.assertTrue(started[paths[2]].wait(10))
                with lock:
                    self.assertEqual(active, set(paths[1:]))
                self.assertEqual(self.job(client, second_job["job_id"])["status"], "running")
                self.assertTrue(self.task(client, first)["lease"])
            finally:
                finish.set()
            for job in (second_job, extra_job):
                self.assertEqual(self.wait(client, job)["status"], "succeeded")

    def test_editor_permit_survives_unverified_exit_and_restart(self):
        from process_runner import StageError

        delegate = FixtureRunner(self.root)
        entered = threading.Event()
        original = delegate.run
        fail_once = True

        def run(argv, cwd, prefix, **kwargs):
            nonlocal fail_once
            if "-projectPath" in argv:
                if fail_once:
                    fail_once = False
                    raise StageError("recovery_required", "Editor process status unknown")
                entered.set()
            return original(argv, cwd, prefix, **kwargs)

        delegate.run = run
        with patch.object(DevelopmentWorker, "stage_compile", lambda _, ctx: ctx.workspace.unity.run(ctx, "compile")):
            with self.client(runner=delegate) as client:
                first, second = self.prepared_tasks(client)
                crashed = self.wait(client, self.action(client, first, "compile"))
                self.assertEqual(crashed["status"], "recovery_required")
            with self.client(runner=delegate) as client:
                cancelled = self.action(client, second, "compile")
                self.assertFalse(entered.wait(0.4))
                self.action(client, second, "cancel")
                self.assertEqual(self.wait(client, cancelled)["status"], "cancelled")
                self.assertFalse(entered.is_set())
                recovered = self.action(client, first, "recover", operator=True, confirmed_stopped=True, note="checked")
                self.assertEqual(self.wait(client, recovered)["status"], "succeeded")
                third = self.create(client, pool_id="tools")
                with patch.object(DevelopmentWorker, "stage_prepare", return_value={}), \
                        patch.object(DevelopmentWorker, "stage_implement", return_value={}):
                    for action in ("prepare", "implement"):
                        self.assertEqual(self.wait(client, self.action(client, third, action))["status"], "succeeded")
                self.assertEqual(self.wait(client, self.action(client, third, "compile"))["status"], "succeeded")
                self.assertTrue(entered.is_set())

    def test_two_isolated_tasks_approve_push_and_release(self):
        from pathlib import Path
        from types import SimpleNamespace

        overlap = threading.Barrier(2, timeout=35)
        real = FixtureRunner(self.root)
        project_paths = set()
        for repo in (self.first, self.second):
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")

        def run(argv, cwd, prefix, **kwargs):
            if "-projectPath" in argv:
                project_paths.add(Path(argv[argv.index("-projectPath") + 1]))
            return real.run(argv, cwd, prefix, **kwargs)

        def execute(ctx, _prompt, *, review=False):
            if review:
                return {"verdict": "pass", "summary": "Reviewed", "issues": [], "session_id": "review-" + ctx.task["task_id"]}
            overlap.wait()
            path = ctx.settings.checkout / "hello.txt"
            path.write_text(ctx.task["task_id"] + "\n", encoding="utf-8")
            return {"success": True, "summary": "Changed the greeting", "session_id": "implement-" + ctx.task["task_id"]}

        with self.client(agent=SimpleNamespace(execute=execute), runner=SimpleNamespace(run=run)) as client:
            tasks = [self.create(client), self.create(client)]
            jobs = [self.action(client, task, "run", queue_wait_seconds=180) for task in tasks]
            self.assertTrue(all(self.wait(client, job)["status"] == "succeeded" for job in jobs))
            records = [self.task(client, task) for task in tasks]
            self.assertEqual([task["slot_id"] for task in records], ["worker-01", "worker-02"])
            self.assertNotEqual(records[0]["revision"], records[1]["revision"])
            self.assertEqual(project_paths, {self.first, self.second})
            for task in records:
                task_id, revision = task["task_id"], task["revision"]
                self.action(client, task_id, "approve", operator=True, revision=revision, approver="Fixture")
                delivered = self.wait(client, self.action(client, task_id, "commit", revision=revision))
                self.assertEqual(delivered["status"], "succeeded", delivered)
                self.assertTrue(delivered["stage_result"]["pushed"])
                self.assertEqual(git(self.root / "remote", "show", task["task_branch"] + ":hello.txt"), task_id)
                self.assertEqual(self.wait(client, self.action(client, task_id, "release"))["status"], "succeeded")
                self.assertFalse(self.task(client, task_id)["lease"])
            self.assertEqual([slot["status"] for slot in client.get("/api/v1/pools").json()["pools"][0]["slots"]],
                             ["idle", "idle"])

    def test_legacy_smoke_waits_behind_queue_head(self):
        from pool_scheduler import PoolScheduler

        first = self.config.pools["game"].slots[0]
        self.config = replace(self.config, pools={**self.config.pools, "game": replace(self.config.pools["game"], slots=(first,))})
        with patch.object(PoolScheduler, "dispatch"), patch.object(DevelopmentWorker, "stage_prepare", return_value={}), \
                self.client(runner=FixtureRunner(self.root)) as client:
            owner, waiting = self.create(client), self.create(client)
            self.assertEqual(self.wait(client, self.action(client, owner, "prepare"))["status"], "succeeded")
            head = self.action(client, waiting, "prepare", queue_wait_seconds=120)
            self.action(client, owner, "cancel")
            self.assertEqual(self.wait(client, self.action(client, owner, "release"))["status"], "succeeded")
            self.assertEqual(client.post("/jobs", json={"job_id": str(uuid4())}).status_code, 409)
            self.assertEqual(self.job(client, head["job_id"])["status"], "queued")

    def test_effective_default_pr_target_is_bound_while_task_pending(self):
        from pull_requests import HostedPullRequests

        initial = HostedPullRequests("gitlab", "https://example.invalid/api/v4", "group/first", "configuration-key")
        redirected = HostedPullRequests("gitlab", "https://example.invalid/api/v4", "group/second", "configuration-key")
        with self.client(pr_adapter=initial) as client:
            self.create(client)
        with self.assertRaisesRegex(RuntimeError, "execution settings"), self.client(pr_adapter=redirected):
            pass

    def test_invalid_delivery_configuration_leaves_state_unmodified(self):
        selected = self.config.pools["game"]
        invalid = {"provider": "gitlab", "api_url": "http://example.invalid", "repository": "group/repo", "token_env": "TEST_TOKEN"}
        self.config = replace(self.config, pools={**self.config.pools, "game": replace(selected, pr=invalid)})
        self.assertFalse((self.config.logs / "worker.sqlite3").exists())
        with self.assertRaises(ValueError), self.client():
            pass
        self.assertFalse((self.config.logs / "worker.sqlite3").exists())

    def test_legacy_unknown_editor_holds_capacity_for_second_slot(self):
        marker = self.config.logs / "active.lock"
        marker.touch()
        delegate = FixtureRunner(self.root)
        entered = threading.Event()
        original = delegate.run

        def observe(argv, cwd, prefix, **kwargs):
            if "-projectPath" in argv:
                entered.set()
            return original(argv, cwd, prefix, **kwargs)

        delegate.run = observe
        with patch.object(DevelopmentWorker, "stage_prepare", return_value={}), \
                patch.object(DevelopmentWorker, "stage_implement", return_value={}), \
                patch.object(DevelopmentWorker, "stage_compile", lambda _, ctx: ctx.workspace.unity.run(ctx, "compile")), \
                self.client(runner=delegate) as client:
            task = self.create(client)
            for action in ("prepare", "implement"):
                self.assertEqual(self.wait(client, self.action(client, task, action))["status"], "succeeded")
            self.assertEqual(self.task(client, task)["slot_id"], "worker-02")
            job = self.action(client, task, "compile")
            self.assertFalse(entered.wait(0.4))
            marker.unlink()
            self.assertEqual(self.wait(client, job)["status"], "succeeded")
            self.assertTrue(entered.is_set())
