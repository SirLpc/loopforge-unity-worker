import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from support import test_directory
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
