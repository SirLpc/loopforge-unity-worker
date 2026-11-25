import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from worker import create_app
from support import test_directory


TOKEN = "legacy-token-" + "x" * 32


class MVPTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(test_directory())
        self.project = root / "project"
        self.logs = root / "logs"
        (self.project / "ProjectSettings").mkdir(parents=True)
        (self.project / "ProjectSettings/ProjectVersion.txt").write_text(
            "m_EditorVersion: 2022.3.62f3\n"
        )
        (self.project / "Library").mkdir()
        self.env = {
            "UNITY_WORKER_TOKEN": TOKEN,
            "UNITY_EXE": sys.executable,
            "UNITY_PROJECT": str(self.project),
            "UNITY_WORKER_LOGS": str(self.logs),
        }
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def test_startup_contract_and_exclusivity(self):
        started = threading.Event()
        finish = threading.Event()

        def fake_unity(args, *_paths, **kwargs):
            self.assertIn("-batchmode", args)
            self.assertIn("-quit", args)
            self.assertEqual(Path(args[args.index("-projectPath") + 1]), self.project)
            Path(args[args.index("-logFile") + 1]).write_text("Unity smoke evidence\n" + TOKEN)
            started.set()
            finish.wait(5)
            return SimpleNamespace(exit_code=0)

        job = str(uuid4())
        with patch.dict(os.environ, self.env):
            with TestClient(create_app(runner=SimpleNamespace(run=fake_unity))) as app:
                self.assertEqual(app.get("/health").status_code, 401)
                self.assertEqual(app.get("/health", headers=self.headers).json(), {
                    "ok": True, "busy_or_blocked": False, "library_exists": True,
                    "operation": "unity_startup_import_smoke",
                })
                try:
                    self.assertEqual(app.post("/jobs", headers=self.headers, json={"job_id": job}).status_code, 202)
                    self.assertTrue(started.wait(5))
                    self.assertEqual(app.post("/jobs", headers=self.headers, json={"job_id": job}).json()["status"], "running")
                    self.assertEqual(app.post("/jobs", headers=self.headers, json={"job_id": str(uuid4())}).status_code, 409)
                    self.assertTrue(app.get("/health", headers=self.headers).json()["busy_or_blocked"])
                finally:
                    finish.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = app.get(f"/jobs/{job}", headers=self.headers).json()
                    if result["status"] != "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("Unity smoke evidence", result["log_tail"])
                self.assertNotIn(TOKEN, result["log_tail"])
                self.assertFalse(app.get("/health", headers=self.headers).json()["busy_or_blocked"])
            with TestClient(create_app()) as restarted:
                self.assertEqual(restarted.get(f"/jobs/{job}", headers=self.headers).json()["status"], "succeeded")

    def test_preupgrade_marker_blocks_legacy_submission(self):
        self.logs.mkdir()
        (self.logs / "active.lock").write_text(str(uuid4()))
        with patch.dict(os.environ, self.env), TestClient(create_app()) as app:
            self.assertTrue(app.get("/health", headers=self.headers).json()["busy_or_blocked"])
            self.assertEqual(app.post("/jobs", headers=self.headers, json={"job_id": str(uuid4())}).status_code, 409)
            self.assertTrue((self.logs / "active.lock").exists())


if __name__ == "__main__":
    unittest.main()
