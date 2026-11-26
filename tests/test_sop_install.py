import ast
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.loopforge import install_sops


class InstallSopsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "worker_id": "worker-host",
            "default_pool": "other",
            "pools": {"game": {"slots": [{"id": "primary"}, {"id": "reserve", "enabled": False}]}},
        }
        self.responses = {
            "/api/v1/workers": {"workers": [{"worker_id": "worker-host", "status": "leased"}]},
            "/api/v1/pools": {"pools": [{"id": "game", "queued_count": 0, "slots": [
                {"id": "primary", "status": "idle"}, {"id": "reserve", "status": "draining"},
            ]}]},
        }

    def probe(self, method, path):
        self.assertEqual(method, "GET")
        return self.responses[path]

    def test_explicit_pool_probes_that_pool_even_when_default_pool_is_busy(self):
        result = install_sops.preflight_worker(self.probe, self.settings, "game")
        self.assertEqual(result["pool"]["id"], "game")
        self.assertEqual(result["worker"]["worker_id"], "worker-host")

    def test_preflight_rejects_wrong_worker_and_unavailable_enabled_slot(self):
        with self.assertRaisesRegex(RuntimeError, "slot.*primary"):
            self.responses["/api/v1/pools"]["pools"][0]["slots"][0]["status"] = "unavailable"
            install_sops.preflight_worker(self.probe, self.settings, "game")
        self.responses["/api/v1/workers"]["workers"][0]["worker_id"] = "wrong-host"
        with self.assertRaisesRegex(RuntimeError, "worker-host"):
            install_sops.preflight_worker(self.probe, self.settings, "game")

    def test_preflight_cli_uses_explicit_probe_url_without_loopforge_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "worker.toml"
            config.write_text('worker_id = "worker-host"\ndefault_pool = "game"\n'
                              '[[pools.game.slots]]\nid = "primary"\n'
                              '[[pools.game.slots]]\nid = "reserve"\nenabled = false\n', encoding="utf-8")
            result_path = root / "reports" / "worker-preflight.json"
            with patch.dict(os.environ, {"UNITY_WORKER_TOKEN": "sample-token"}, clear=True), \
                 patch.object(install_sops, "call_factory", return_value=self.probe) as factory:
                install_sops.main(["--preflight", "--config", str(config), "--output-dir", str(result_path.parent),
                                   "--worker-url", "https://worker.example.test/", "--probe-url", "http://127.0.0.1:8765"])
                factory.assert_called_once_with("sample-token", "http://127.0.0.1:8765")
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["pool"]["id"], "game")

    def test_generated_sops_use_explicit_project_and_sandbox(self):
        target = install_sops.Target("garden", "custom-sandbox", "https://worker.example.test")
        child = install_sops.execution_workflow(target, "worker-credential", "game")
        plan = install_sops.windows_workflow(target, install_sops.STARTERS[0], "worker-credential", "plan-key", "game")
        self.assertEqual(child["id"], "garden-worker-execution")
        self.assertEqual(plan["id"], "garden-feature")
        self.assertEqual(plan["projectId"], "garden")
        self.assertEqual(plan["sandboxProfileId"], "custom-sandbox")
        self.assertEqual(plan["projectCredentialIds"], ["worker-credential", "plan-key"])
        command = shlex.split(plan["nodes"][0]["script"])[-1]
        arguments = ast.literal_eval(command.splitlines()[0].removeprefix("SOP_ARGS = "))
        self.assertEqual(arguments[arguments.index("--pool") + 1], "game")
        self.assertIn("approve-delivery.ps1", next(node["taskPrompt"] for node in plan["nodes"]
                                                   if node["id"] == "delivery-approval"))

    def test_draft_install_creates_workflows_and_credentials_for_selected_project(self):
        project = {"id": "garden", "name": "Garden", "workflowId": "existing"}
        workflows = {}
        credentials = []

        def api(method, path, value=None, *, missing=False):
            if method == "GET" and path == "/api/projects/garden":
                return project
            if path == "/api/projects/garden/credentials":
                if method == "GET":
                    return {"canManage": True, "credentials": credentials}
                self.assertEqual(method, "POST")
                credential = {"id": f"credential-{len(credentials) + 1}", **value}
                credentials.append(credential)
                return credential
            if method == "GET" and path.startswith(("/api/workflows/", "/api/agents/")) and missing:
                return None
            if method == "PUT" and path == "/api/agents":
                self.assertEqual(value["projectId"], "garden")
                return value
            if method == "PUT" and path == "/api/workflows":
                self.assertEqual(value["status"], "draft")
                workflows[value["id"]] = value
                return value
            self.fail(f"Unexpected draft install call: {method} {path}")

        environment = {
            "UNITY_FORMAL_STUDIO_TOKEN": "studio-token",
            "UNITY_WORKER_TOKEN": "worker-token",
            "UNITY_PLANNING_CONTEXT_TOKEN": "context-token".ljust(32, "x"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "worker.toml"
            config.write_text('default_pool = "game"\n[[pools.game.slots]]\nid = "primary"\n', encoding="utf-8")
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(install_sops, "call_factory", return_value=api) as factory:
                install_sops.main(["--windows-planning", "--config", str(config), "--output-dir", str(root),
                                   "--project-id", "garden", "--api-url", "https://studio.example.test",
                                   "--worker-url", "https://worker.example.test"])
                factory.assert_called_once_with("studio-token", "https://studio.example.test")
            report = json.loads((root / "garden-install.json").read_text(encoding="utf-8"))
            self.assertEqual(report["default"], "existing")

        self.assertEqual(set(workflows), {"garden-worker-execution", "garden-feature", "garden-ui",
                                          "garden-product", "garden-issue-fix"})
        self.assertEqual(workflows["garden-worker-execution"]["projectCredentialIds"], ["credential-1"])
        self.assertEqual(workflows["garden-feature"]["projectCredentialIds"], ["credential-1", "credential-2"])
        self.assertEqual(workflows["garden-product"]["projectCredentialIds"], [])
        self.assertIn({"name": "UNITY_WORKER_URL", "value": "https://worker.example.test"}, credentials[0]["fields"])

    def test_draft_install_preserves_an_existing_credential(self):
        target = install_sops.Target("garden", "sandbox", "https://worker.example.test")

        def api(method, path, value=None):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/projects/garden/credentials")
            return {"canManage": True, "credentials": [{"name": "Unity Worker 正式执行",
                                                       "kind": "custom", "id": "live-key"}]}

        self.assertEqual(install_sops.worker_credential(target, api, "new-token"), "live-key")

    def test_publish_updates_the_sandbox_and_graph_of_existing_owned_sops(self):
        target = install_sops.Target("garden", "updated-sandbox", "https://worker.example.test")
        draft = install_sops.definition(target, "garden-feature", "Feature", "backend",
                                        [{"id": "new-node", "type": "approval"}])
        live = {**draft, "sandboxProfileId": "previous-sandbox", "nodes": [], "status": "published",
                "revision": 7, "tags": [install_sops.TAG, "user-tag", install_sops.WINDOWS_PLANNING_TAG]}
        calls = []

        def api(method, path, value=None, *, missing=False):
            calls.append(method)
            if method == "GET":
                return live
            if method == "PUT":
                self.assertEqual(value["revision"], 7)
                self.assertEqual(value["sandboxProfileId"], "updated-sandbox")
                self.assertEqual(value["nodes"][0]["id"], "new-node")
                self.assertEqual(value["tags"], [install_sops.TAG, "user-tag"])
                self.assertEqual(value["status"], "published")
                return value
            return {**draft, "status": "published"}

        self.assertEqual(install_sops.save_workflow(target, api, draft), live)
        self.assertEqual(calls, ["GET"])
        install_sops.save_workflow(target, api, draft, publish=True)
        self.assertEqual(calls, ["GET", "GET", "PUT", "POST"])
