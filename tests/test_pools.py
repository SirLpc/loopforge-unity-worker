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
