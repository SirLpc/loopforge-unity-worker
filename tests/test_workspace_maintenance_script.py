import json
import os
import shutil
import subprocess
import threading
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

from support import test_directory


POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/workspace-maintenance.ps1"
TOKEN = "fixture-operator-token-oooooooooooooooo"
STORAGE = "/api/v1/storage"
SLOT = STORAGE + "/slots/game-01"
TASK_ID = "b6fe86dd-4c53-487f-96e5-1881956641d6"
JOB_ID = "9d4cc400-0f37-4aad-9a3d-df34a4a984de"
RELEASE = f"/api/v1/tasks/{TASK_ID}/release"
JOB = f"/api/v1/jobs/{JOB_ID}"
VOLUME = {"free_bytes": 987654321, "total_bytes": 1234567890}
CACHE_PATH = r"D:\Fixture\Unity\Library"
REMNANT = r"D:\Fixture\Unity\.unity-worker-cleanup-example"
PREVIEW = {
    "path": CACHE_PATH, "exists": True, "complete": True, "logical_bytes": 3991,
    "buckets": {"Library": 3991, "other": 0}, "skipped_links": 0, "read_errors": 0,
    "volume": VOLUME,
}
CLEANED = {
    "status": "cleaned", "cache": "Library", "path": CACHE_PATH, "logical_bytes": 3991, "volume": VOLUME,
}
OVERVIEW = {
    "state": {"path": r"D:\Fixture\state", "volume": VOLUME},
    "pools": [{"id": "game", "queued_count": 0, "slots": [{
        "id": "game-01", "status": "unavailable", "task_id": TASK_ID, "current_stage": "review",
        "path": r"D:\Fixture", "volume": VOLUME, "cleanup_remnants": [REMNANT],
    }]}],
}


class ScriptAPI(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), ScriptRequest)
        self.requests = []
        self.responses = deque()


class ScriptRequest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
        status, response = self.server.responses.popleft() if self.server.responses else (500, {})
        if status is None:
            self.close_connection = True
            return
        payload = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = handle_request


@unittest.skipUnless(POWERSHELL, "Requires Windows PowerShell 5.1")
class WorkspaceMaintenanceScriptTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Workspace maintenance script is missing")
        self.api = ScriptAPI()
        self.server = threading.Thread(target=self.api.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.server.start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        self.env = {**os.environ, "UNITY_WORKER_APPROVAL_TOKEN": TOKEN,
                    "UNITY_WORKER_URL": f"http://127.0.0.1:{self.api.server_port}"}

    def invoke(self, *args, reply=""):
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
            env=self.env, input=reply, capture_output=True, text=True, timeout=25,
        )
        output = result.stdout + result.stderr
        self.assertNotIn(TOKEN, output)
        return result.returncode, output

    def assert_requests(self, *requests):
        self.assertEqual(self.api.requests, [(*request, f"Bearer {TOKEN}") for request in requests])

    def test_default_status_reads_only_overview_and_prints_capacity_and_remnant(self):
        self.api.responses.append((200, OVERVIEW))
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assert_requests(("GET", STORAGE, None))
        for value in ("game-01", TASK_ID, "review", "987654321", "1234567890", REMNANT, "restart"):
            self.assertIn(value, output)

    def test_measure_slot_escapes_id_and_shows_buckets_and_partial_scan(self):
        self.api.responses.append((200, {**PREVIEW, "complete": False, "skipped_links": 2, "read_errors": 1}))
        code, output = self.invoke("Measure", "-SlotId", "game 01#?")
        self.assertEqual(code, 0, output)
        self.assert_requests(("GET", STORAGE + "/slots/game%2001%23%3F", None))
        self.assertRegex(output, r"Library\s+3991")
        self.assertIn("partial", output.lower())
        self.assertRegex(output.lower(), r"skipped links\s+2")
        self.assertRegex(output.lower(), r"read errors\s+1")

    def test_measure_state_uses_explicit_url_and_state_route(self):
        self.api.responses.append((200, {**PREVIEW, "path": r"D:\Fixture\state", "buckets": {"other": 3991}}))
        url = self.env["UNITY_WORKER_URL"]
        self.env["UNITY_WORKER_URL"] = "http://127.0.0.1:1"
        code, output = self.invoke("Measure", "-State", "-WorkerUrl", url + "/")
        self.assertEqual(code, 0, output)
        self.assert_requests(("GET", STORAGE + "/state", None))
        self.assertRegex(output, r"other\s+3991")

    def test_release_submits_uuid_once_and_polls_to_success(self):
        self.api.responses.extend([(202, {"job_id": JOB_ID, "status": "running"}),
                                   (200, {"job_id": JOB_ID, "status": "running"}),
                                   (200, {"job_id": JOB_ID, "status": "succeeded"})])
        code, output = self.invoke("Release", "-TaskId", TASK_ID)
        self.assertEqual(code, 0, output)
        self.assertIn(JOB_ID, output)
        self.assertIn("succeeded", output.lower())
        submitted = self.api.requests[0][2]
        self.assertEqual(set(submitted), {"request_id"})
        self.assertEqual(UUID(submitted["request_id"]).version, 4)
        self.assert_requests(("POST", RELEASE, submitted), ("GET", JOB, None), ("GET", JOB, None))

    def test_release_failure_reports_worker_detail_without_token(self):
        self.api.responses.extend([(202, {"job_id": JOB_ID, "status": "running"}),
                                   (200, {"job_id": JOB_ID, "status": "failed",
                                          "error_message": f"Workspace has local changes ({TOKEN})"})])
        code, output = self.invoke("Release", "-TaskId", TASK_ID)
        self.assertNotEqual(code, 0, output)
        self.assertIn("Workspace has local changes", output)
        self.assertIn(JOB_ID, output)
        self.assert_requests(("POST", RELEASE, self.api.requests[0][2]), ("GET", JOB, None))

    def test_interrupted_release_poll_offers_job_lookup_without_cancel(self):
        self.api.responses.extend([(202, {"job_id": JOB_ID, "status": "running"}), (None, None)])
        code, output = self.invoke("Release", "-TaskId", TASK_ID)
        self.assertNotEqual(code, 0, output)
        self.assertIn(JOB_ID, output)
        self.assertIn(JOB, output)
        self.assertTrue(all(method == "GET" and path == JOB for method, path, *_ in self.api.requests[1:]))
        self.assertTrue(self.api.requests[1:])
        self.assertEqual(self.api.requests[0][:2], ("POST", RELEASE))

    def test_clean_previews_then_submits_one_confirmed_canonical_cache(self):
        self.api.responses.extend([(200, PREVIEW), (200, CLEANED)])
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "library", reply="YES\n")
        self.assertEqual(code, 0, output)
        self.assert_requests(("GET", SLOT + "?cache=Library", None),
                             ("POST", SLOT + "/clean", {"cache": "Library", "confirmed": True}))
        for value in (CACHE_PATH, "3991", "987654321", "reimport", "cleaned"):
            self.assertIn(value, output.lower() if value.islower() else output)

    def test_declined_clean_only_previews(self):
        self.api.responses.append((200, PREVIEW))
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library", reply="NO\n")
        self.assertNotEqual(code, 0, output)
        self.assertIn("cancel", output.lower())
        self.assert_requests(("GET", SLOT + "?cache=Library", None))

    def test_incomplete_clean_preview_ignores_available_confirmation(self):
        self.api.responses.append((200, {**PREVIEW, "complete": False, "read_errors": 1}))
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library", reply="YES\n")
        self.assertNotEqual(code, 0, output)
        self.assertIn("incomplete", output.lower())
        self.assertNotIn("Type YES", output)
        self.assert_requests(("GET", SLOT + "?cache=Library", None))

    def test_lost_clean_response_reports_unknown_outcome_without_retry(self):
        self.api.responses.extend([(200, PREVIEW), (None, None)])
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library", reply="YES\n")
        self.assertNotEqual(code, 0, output)
        self.assertIn("Status", output)
        self.assertIn("preview", output.lower())
        self.assert_requests(("GET", SLOT + "?cache=Library", None),
                             ("POST", SLOT + "/clean", {"cache": "Library", "confirmed": True}))

    def test_clean_conflict_prints_failure_and_remnant_without_token(self):
        self.api.responses.extend([(200, PREVIEW),
                                   (409, {"detail": f"Inspect remaining cleanup directory ({TOKEN})",
                                          "remnant": REMNANT})])
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library", reply="YES\n")
        self.assertNotEqual(code, 0, output)
        self.assertIn("Inspect remaining cleanup directory", output)
        self.assertIn(REMNANT, output)
        self.assertIn("restart", output)
        self.assert_requests(("GET", SLOT + "?cache=Library", None),
                             ("POST", SLOT + "/clean", {"cache": "Library", "confirmed": True}))

    def test_validation_rejects_ambiguous_target_before_http(self):
        code, output = self.invoke("Measure", "-State", "-SlotId", "game-01")
        self.assertNotEqual(code, 0, output)
        self.assertIn("-State", output)
        self.assertIn("-SlotId", output)
        self.assert_requests()

    def test_clean_rejects_unsupported_whatif_before_http(self):
        self.api.responses.extend([(200, PREVIEW), (200, CLEANED)])
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library", "-WhatIf", reply="YES\n")
        self.assert_requests()
        self.assertNotEqual(code, 0, output)
        self.assertIn("NamedParameterNotFound", output)

    def test_clean_rejects_unexpected_positional_operand_before_http(self):
        self.api.responses.extend([(200, PREVIEW), (200, CLEANED)])
        code, output = self.invoke("Clean", "-SlotId", "game-01", "-Cache", "Library",
                                   self.env["UNITY_WORKER_URL"], reply="YES\n")
        self.assert_requests()
        self.assertNotEqual(code, 0, output)
        self.assertIn("PositionalParameterNotFound", output)

    def test_dpapi_file_overrides_environment_credential(self):
        directory = self.enterContext(test_directory())
        dpapi_file = directory / "UNITY_WORKER_APPROVAL_TOKEN.dpapi"
        subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-Command",
                        "$env:UNITY_WORKER_APPROVAL_TOKEN | ConvertTo-SecureString -AsPlainText -Force | "
                        "ConvertFrom-SecureString | Set-Content -LiteralPath $env:TEST_DPAPI_PATH"],
                       env={**self.env, "TEST_DPAPI_PATH": str(dpapi_file)}, check=True, timeout=15)
        self.env["UNITY_WORKER_APPROVAL_TOKEN"] = "wrong-fixture-token"
        self.api.responses.append((200, OVERVIEW))
        code, output = self.invoke("Status", "-CredentialDirectory", str(directory))
        self.assertEqual(code, 0, output)
        self.assertNotIn("wrong-fixture-token", output)
        self.assert_requests(("GET", STORAGE, None))
