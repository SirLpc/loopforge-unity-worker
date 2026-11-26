import os
import shutil
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from support import test_directory
from test_workspace_storage import cache_checkout
from worker import create_app
from worker_config import Settings, load_service_settings


TOKEN = "fixture-worker-" + "w" * 32
APPROVER = "fixture-operator-" + "o" * 32
ENV = {"UNITY_WORKER_TOKEN": TOKEN, "UNITY_WORKER_APPROVAL_TOKEN": APPROVER}
OPERATOR = {"Authorization": f"Bearer {APPROVER}"}
STORAGE = "/api/v1/storage"
LEGACY_SLOT = f"{STORAGE}/slots/worker-01"


class StorageApiTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        self.checkout = self.root / "checkout"
        self.project = self.checkout / "Unity"
        self.logs = self.root / "logs"
        self.version = self.project / "ProjectSettings/ProjectVersion.txt"
        self.version.parent.mkdir(parents=True)
        self.version.write_bytes(b"m_EditorVersion: 2022.3.62f3\n")
        for directory, content in ((self.checkout / ".git", b"git"), (self.project / "Library", b"cache"),
                                   (self.checkout / "Library", b"outside")):
            directory.mkdir()
            (directory / "data").write_bytes(content)
        self.settings = Settings.load(config={"project": str(self.project), "git_root": str(self.checkout),
                                              "logs": str(self.logs), "unity_exe": sys.executable,
                                              "worker_id": "fixture"}, environ=ENV)

    def client(self):
        return TestClient(create_app(self.settings))

    def test_storage_reads_require_operator_token(self):
        worker = {"Authorization": f"Bearer {TOKEN}"}
        with self.client() as client:
            for path in (STORAGE, f"{STORAGE}/state", LEGACY_SLOT):
                for headers, status in (({}, 401), (worker, 403), (OPERATOR, 200)):
                    with self.subTest(path=path, status=status):
                        response = client.get(path, headers=headers)
                        self.assertEqual(response.status_code, status, response.text)

    def test_legacy_overview_reuses_pool_status_without_scanning(self):
        with self.client() as client:
            pools = client.get("/api/v1/pools", headers=OPERATOR).json()["pools"]
            with patch("development.measure_directory", create=True,
                       side_effect=AssertionError("overview walked files")):
                response = client.get(STORAGE, headers=OPERATOR)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(len(result["pools"]), 1)
        pool = result["pools"][0]
        self.assertEqual(pool["id"], "fixture")
        self.assertEqual(pool["queued_count"], pools[0]["queued_count"])
        self.assertEqual(len(pool["slots"]), 1)
        slot = pool["slots"][0]
        self.assertEqual(slot["id"], "worker-01")
        self.assertEqual({key: slot[key] for key in pools[0]["slots"][0]}, pools[0]["slots"][0])
        self.assertEqual(slot["path"], str(self.checkout))
        self.assertEqual(result["state"]["path"], str(self.logs))
        for volume in (slot["volume"], result["state"]["volume"]):
            self.assertGreater(volume["total_bytes"], 0)
            self.assertGreaterEqual(volume["free_bytes"], 0)

    def test_named_pool_overview_uses_configured_slot_paths_and_redacts_secrets(self):
        tools = self.root / APPROVER
        tools.mkdir()
        with patch.dict(os.environ, ENV), patch("development.check_slot", return_value=None):
            settings = load_service_settings(config={
                "logs": str(self.logs), "unity_exe": sys.executable, "unity_version": "2022.3.62f3",
                "default_pool": "game", "legacy_slot": "game-01",
                "pools": {
                    "game": {"remote_identity": "a" * 64, "slots": [
                        {"id": "game-01", "git_root": str(self.checkout), "project": str(self.project)},
                    ]},
                    "tools": {"remote_identity": "b" * 64, "slots": [
                        {"id": "tools-01", "git_root": str(tools), "project": str(tools), "enabled": False},
                    ]},
                },
            })
            with TestClient(create_app(settings)) as client:
                response = client.get(STORAGE, headers=OPERATOR)
                tools_response = client.get(f"{STORAGE}/slots/tools-01", headers=OPERATOR)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(APPROVER, response.text)
        self.assertEqual(tools_response.status_code, 200, tools_response.text)
        self.assertNotIn(APPROVER, tools_response.text)
        pools = {pool["id"]: pool["slots"] for pool in response.json()["pools"]}
        self.assertEqual({pool: [slot["id"] for slot in slots] for pool, slots in pools.items()},
                         {"game": ["game-01"], "tools": ["tools-01"]})
        self.assertEqual(pools["game"][0]["path"], str(self.checkout))
        self.assertEqual(pools["tools"][0]["path"], str(self.root / "[redacted]"))
        self.assertEqual(pools["tools"][0]["status"], "draining")
        self.assertEqual(tools_response.json()["logical_bytes"], 0)

    def test_state_and_slot_measurements_use_separate_roots(self):
        with self.client() as client:
            (self.logs / "tasks").mkdir()
            (self.logs / "tasks/evidence").write_bytes(b"artifact!?!")
            state_db_bytes = (self.logs / "worker.sqlite3").stat().st_size
            state_response = client.get(f"{STORAGE}/state", headers=OPERATOR)
            slot_response = client.get(LEGACY_SLOT, headers=OPERATOR)

        self.assertEqual(state_response.status_code, 200, state_response.text)
        self.assertEqual(slot_response.status_code, 200, slot_response.text)
        state = state_response.json()
        self.assertEqual(state["path"], str(self.logs))
        self.assertEqual(state["logical_bytes"], state_db_bytes + 11)
        self.assertEqual(state["buckets"], {"other": state_db_bytes + 11})
        slot = slot_response.json()
        self.assertEqual(slot["path"], str(self.checkout))
        self.assertEqual(slot["buckets"], {".git": 3, "Library": 5, "Temp": 0, "Obj": 0,
                                           "other": 7 + self.version.stat().st_size})

    def test_targeted_cache_preview_does_not_scan_an_unrelated_bucket(self):
        scandir = os.scandir

        def fail_for_git(path):
            if Path(path) == self.checkout / ".git":
                raise PermissionError("fixture read error")
            return scandir(path)

        with self.client() as client, patch("os.scandir", side_effect=fail_for_git):
            whole = client.get(LEGACY_SLOT, headers=OPERATOR)
            preview = client.get(LEGACY_SLOT, params={"cache": "Library"}, headers=OPERATOR)

        self.assertEqual(whole.status_code, 200, whole.text)
        self.assertFalse(whole.json()["complete"])
        self.assertEqual(whole.json()["read_errors"], 1)
        self.assertEqual(preview.status_code, 200, preview.text)
        result = preview.json()
        self.assertTrue(result["complete"])
        self.assertEqual(result["path"], str(self.project / "Library"))
        self.assertEqual(result["logical_bytes"], 5)
        self.assertEqual(result["buckets"], {"Library": 5, "other": 0})

    def test_unknown_slot_and_invalid_cache_are_rejected(self):
        with self.client() as client:
            self.assertEqual(client.get(f"{STORAGE}/slots/unknown", headers=OPERATOR).status_code, 404)
            response = client.get(LEGACY_SLOT, headers=OPERATOR, params={"cache": "../Library"})
            self.assertEqual(response.status_code, 422, response.text)


class CleanStorageApiTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        self.checkout, self.project = cache_checkout(self.root)
        self.logs = self.root / "logs"
        self.settings = Settings.load(config={"project": str(self.project), "git_root": str(self.checkout),
                                              "logs": str(self.logs), "unity_exe": sys.executable}, environ=ENV)
        self.body = {"cache": "Library", "confirmed": True}
        self.url = LEGACY_SLOT + "/clean"

    def client(self):
        return TestClient(create_app(self.settings), headers=OPERATOR)

    def use_named_pool(self, *, enabled=True):
        sibling = self.root / "sibling"
        (sibling / "Unity").mkdir(parents=True)
        with patch.dict(os.environ, ENV):
            self.settings = load_service_settings(config={
                "logs": str(self.logs), "unity_exe": sys.executable, "unity_version": "2022.3.62f3",
                "default_pool": "game", "legacy_slot": "worker-01",
                "pools": {"game": {"remote_identity": "a" * 64, "slots": [
                    {"id": "worker-01", "git_root": str(self.checkout), "project": str(self.project),
                     "enabled": enabled},
                    {"id": "worker-02", "git_root": str(sibling), "project": str(sibling / "Unity")},
                ]}},
            })
        self.enterContext(patch("development.check_slot", return_value=None))

    def test_operator_confirmation_and_fixed_cache_are_required(self):
        with TestClient(create_app(self.settings)) as client:
            for headers, code in (({}, 401), ({"Authorization": f"Bearer {TOKEN}"}, 403)):
                self.assertEqual(client.post(self.url, json=self.body, headers=headers).status_code, code)
            for body in ({"cache": "Library"}, {**self.body, "confirmed": False}, {**self.body, "confirmed": 1},
                         {**self.body, "confirmed": "true"}, {**self.body, "cache": "../Assets"},
                         {**self.body, "path": str(self.checkout)}):
                with self.subTest(body=body):
                    self.assertEqual(client.post(self.url, json=body, headers=OPERATOR).status_code, 422)
            self.assertEqual(client.post(STORAGE + "/slots/unknown/clean", json=self.body,
                                         headers=OPERATOR).status_code, 404)
            self.assertEqual(client.post(self.url, params={"path": str(self.checkout)},
                                         json=self.body, headers=OPERATOR).status_code, 422)
            self.assertEqual((self.project / "Library/data").read_bytes(), b"cache")

    def test_confirmed_cleanup_removes_only_selected_cache_and_reports_missing_on_retry(self):
        (self.project / "Temp").mkdir()
        (self.project / "Temp/keep").write_text("another cache")
        with self.client() as client:
            sentinel = self.logs / "evidence"
            sentinel.write_text("state")
            head = self.checkout / ".git/HEAD"
            original_head = head.read_bytes()
            result = client.post(self.url, json=self.body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "cleaned")
            self.assertEqual(result.json()["cache"], "Library")
            self.assertEqual(result.json()["logical_bytes"], 5)
            self.assertEqual(result.json()["path"], str(self.project / "Library"))
            self.assertGreater(result.json()["volume"]["total_bytes"], 0)
            self.assertFalse((self.project / "Library").exists())
            self.assertEqual(client.post(self.url, json=self.body).json()["status"], "missing")
            self.assertEqual((self.project / "Temp/keep").read_text(), "another cache")
            self.assertEqual(sentinel.read_text(), "state")
            self.assertEqual(head.read_bytes(), original_head)

    def test_marker_and_stopping_prevent_cleanup(self):
        with self.client() as client:
            marker = self.logs / "active.lock"
            marker.touch()
            self.assertEqual(client.post(self.url, json=self.body).status_code, 409)
            marker.unlink()
            self.assertTrue(client.app.state.worker.shutdown_when_idle())
            self.assertEqual(client.post(self.url, json=self.body).status_code, 409)
            self.assertTrue((self.project / "Library/data").exists())

    def test_disabled_drained_slot_accepts_cleanup(self):
        self.use_named_pool(enabled=False)
        with self.client() as client:
            self.assertEqual(client.post(self.url, json=self.body).status_code, 200)

    def test_failed_deletion_blocks_slot_until_operator_removes_remnant_and_restarts(self):
        rmtree = shutil.rmtree
        def fail_staging(path, *args, **kwargs):
            if Path(path).name.startswith(".unity-worker-cleanup-"):
                raise PermissionError("fixture")
            return rmtree(path, *args, **kwargs)

        with self.client() as client, patch("shutil.rmtree", side_effect=fail_staging):
            result = client.post(self.url, json=self.body)
            self.assertEqual(result.status_code, 409, result.text)
            remnant = Path(result.json()["remnant"])
            self.assertEqual(remnant.parent, self.project)
            self.assertEqual((remnant / "Library/data").read_bytes(), b"cache")
            self.assertFalse((self.project / "Library").exists())
            slot = client.get(STORAGE).json()["pools"][0]["slots"][0]
            self.assertEqual(slot["status"], "unavailable")
            self.assertEqual(slot.get("cleanup_remnants"), [str(remnant)])
        with self.client() as client:
            self.assertEqual(client.post(self.url, json=self.body).status_code, 409)
            self.assertEqual(client.get(STORAGE).json()["pools"][0]["slots"][0]["cleanup_remnants"], [str(remnant)])
            self.assertNotIn("cleanup_remnants", client.get("/api/v1/pools").json()["pools"][0]["slots"][0])
        rmtree(remnant)
        with self.client() as client:
            self.assertEqual(client.get(STORAGE).json()["pools"][0]["slots"][0]["status"], "idle")

    def test_named_pool_restart_blocks_only_slot_with_remnant(self):
        self.use_named_pool()
        remnant = self.project / ".unity-worker-cleanup-leftover"
        remnant.mkdir()
        with self.client() as client:
            slots = client.get(STORAGE).json()["pools"][0]["slots"]
            self.assertEqual([slot["status"] for slot in slots], ["unavailable", "idle"])
            self.assertEqual(slots[0].get("cleanup_remnants"), [str(remnant)])

    def test_failed_move_with_empty_staging_remnant_blocks_slot(self):
        rmdir = Path.rmdir
        def fail_staging(path):
            if path.name.startswith(".unity-worker-cleanup-"):
                raise PermissionError("fixture")
            return rmdir(path)
        with self.client() as client, patch.object(Path, "rename", side_effect=PermissionError("fixture")), \
                patch.object(Path, "rmdir", fail_staging):
            response = client.post(self.url, json=self.body)
            self.assertEqual(response.status_code, 409, response.text)
            remnant = Path(response.json()["remnant"])
            self.assertEqual(remnant.parent, self.project)
            self.assertEqual(list(remnant.iterdir()), [])
            self.assertEqual((self.project / "Library/data").read_bytes(), b"cache")
            self.assertEqual(client.get(STORAGE).json()["pools"][0]["slots"][0]["status"], "unavailable")

    def hold_deletion(self):
        entered, resume = threading.Event(), threading.Event()
        rmtree = shutil.rmtree

        def hold_staging(path, *args, **kwargs):
            if Path(path).name.startswith(".unity-worker-cleanup-"):
                entered.set()
                if not resume.wait(15):
                    raise TimeoutError("cleanup was not resumed")
            return rmtree(path, *args, **kwargs)

        return entered, resume, patch("shutil.rmtree", side_effect=hold_staging)

    def test_maintenance_excludes_smoke_and_dispatches_queue_after_cleanup(self):
        self.use_named_pool()
        entered, resume, paused_deletion = self.hold_deletion()
        assigned = threading.Event()

        def prepare(ctx):
            if ctx.task["slot_id"] == "worker-01":
                assigned.set()
            return {}

        with self.client() as client, paused_deletion, \
                patch("development.DevelopmentWorker.stage_prepare", side_effect=prepare), ThreadPoolExecutor(1) as pool:
            cleanup = pool.submit(client.post, self.url, json=self.body)
            try:
                self.assertTrue(entered.wait(5), "cleanup failed to reach staged deletion")
                self.assertEqual(client.post(self.url, json=self.body).status_code, 409)
                self.assertEqual(client.post("/jobs", json={"job_id": str(uuid4())}).status_code, 409)
                for path, status in ((STORAGE, "maintenance"), ("/api/v1/pools", "unavailable")):
                    self.assertEqual(client.get(path).json()["pools"][0]["slots"][0]["status"], status)
                for index in range(2):
                    created = client.post("/api/v1/tasks", json={"request_id": str(uuid4()), "requirement": "task",
                                          "plan": "plan", "acceptance_criteria": ["done"]}).json()
                    task = f"/api/v1/tasks/{created['task_id']}"
                    job = client.post(task + "/prepare", json={"request_id": str(uuid4()), "queue_wait_seconds": 30})
                    self.assertEqual(job.status_code, 202, job.text)
                    if index == 0:
                        self.assertEqual(client.get(task).json()["slot_id"], "worker-02")
                        sibling = f"{STORAGE}/slots/worker-02/clean"
                        self.assertEqual(client.post(sibling, json=self.body).status_code, 409)
                    else:
                        self.assertEqual(job.json()["status"], "queued")
            finally:
                resume.set()
            self.assertEqual(cleanup.result().status_code, 200)
            self.assertTrue(assigned.wait(5))
            self.assertEqual(client.get(task).json()["slot_id"], "worker-01")

    def test_shutdown_waits_for_maintenance(self):
        entered, resume, paused_deletion = self.hold_deletion()
        with self.client() as client, paused_deletion, ThreadPoolExecutor(1) as pool:
            cleanup = pool.submit(client.post, self.url, json=self.body)
            try:
                self.assertTrue(entered.wait(5), "cleanup failed to reach staged deletion")
                self.assertFalse(client.app.state.worker.shutdown_when_idle())
            finally:
                resume.set()
            self.assertEqual(cleanup.result().status_code, 200)
            self.assertTrue(client.app.state.worker.shutdown_when_idle())
