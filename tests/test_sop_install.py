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

    def test_staged_feature_has_reference_order_and_one_gate(self):
        target = install_sops.Target("garden", "custom-sandbox", "https://worker.example.test")
        workflow = install_sops.staged_feature_workflow(target, "worker-key", "context-key", "game")
        expected = [
            ("clarify", "需求定型", ["requirement.md", "unity-scope.json"]),
            ("design", "方案设计", ["design.md", "unity-planning.json"]),
            ("implement", "开发实现", ["implementation.md", "unity-implementation.json"]),
            ("review", "独立评审", ["review.md", "unity-review.json"]),
            ("test", "测试验证", ["test-report.md", "unity-verification.json"]),
            ("accept", "人工验收", []),
            ("mr", "发起 MR", ["mr-body.md", "unity-delivery.json"]),
            ("deliver", "交付汇总", ["delivery.md"]),
        ]
        nodes = workflow["nodes"]
        self.assertEqual(workflow["id"], "garden-feature")
        self.assertEqual(workflow["sandboxProfileId"], "custom-sandbox")
        self.assertEqual(workflow["projectCredentialIds"], ["worker-key", "context-key"])
        self.assertEqual([n["id"] for n in nodes], [item[0] for item in expected])
        self.assertEqual([(n["id"], n.get("rejectTo")) for n in nodes if n["type"] == "approval"],
                         [("accept", "implement")])
        self.assertEqual([(e["source"], e["target"], e["when"]) for e in workflow["edges"]],
                         [(a[0], b[0], "success") for a, b in zip(expected, expected[1:])])
        for node, (node_id, title, outputs) in zip(nodes, expected):
            with self.subTest(node=node_id):
                self.assertEqual(node["name"], title)
                self.assertEqual(node["maxAttempts"], 1 if node_id == "accept" else 2)
                self.assertEqual(node["maxReworks"], (10 if node_id in {"review", "test", "accept"} else 0))
                if node_id in {"review", "test"}:
                    self.assertEqual(node["onFailure"], "fail")
                    self.assertNotIn("rejectTo", node)
                self.assertEqual(node.get("artifactOutputs", []), [
                    {"name": Path(path).stem, "path": path, "required": True,
                     "mediaType": "application/json" if path.endswith(".json") else "text/markdown"}
                    for path in outputs
                ])
                if node_id == "accept":
                    continue
                self.assertEqual(node["type"], "script")
                command = shlex.split(node["script"])
                self.assertEqual(command[:-1], ["exec", "/usr/bin/python3", "-I", "-c"])
                compile(command[-1], node_id, "exec")
                arguments = ast.literal_eval(command[-1].splitlines()[0].removeprefix("SOP_ARGS = "))
                self.assertEqual(arguments[0], "feature-delivery" if node_id == "deliver" else f"feature-{node_id}")
                self.assertEqual(arguments[arguments.index("--pool") + 1], "game")
                if node_id == "clarify":
                    self.assertEqual(int(arguments[arguments.index("--queue-wait-seconds") + 1]), 5400)

    def test_direct_merge_graph_has_one_approval_and_preserves_existing_bindings(self):
        target = install_sops.Target("garden", "custom-sandbox", "https://worker.example.test")
        workflow = install_sops.staged_feature_workflow(target, "worker-key", "context-key", "game", direct_merge=True)
        self.assertEqual((workflow["id"], workflow["sandboxProfileId"], workflow["projectCredentialIds"]),
                         ("garden-feature", "custom-sandbox", ["worker-key", "context-key"]))
        nodes = workflow["nodes"]
        self.assertEqual([n["id"] for n in nodes],
                         ["clarify", "design", "implement", "review", "test", "accept", "merge", "deliver"])
        self.assertEqual([(n["id"], n["rejectTo"]) for n in nodes if n["type"] == "approval"], [("accept", "implement")])
        self.assertIn("直接合入", nodes[5]["taskPrompt"])
        self.assertEqual([n["maxReworks"] for n in nodes[3:6]], [10, 10, 10])
        self.assertEqual([(e["source"], e["target"]) for e in workflow["edges"][-2:]],
                         [("accept", "merge"), ("merge", "deliver")])
        self.assertEqual([a["path"] for a in nodes[6]["artifactOutputs"]], ["merge-report.md", "unity-delivery.json"])
        args = ast.literal_eval(shlex.split(nodes[6]["script"])[-1].splitlines()[0].removeprefix("SOP_ARGS = "))
        self.assertEqual(args[0], "feature-merge")

    def test_direct_merge_preflight_requires_worker_capability_and_selected_pool(self):
        worker = self.responses["/api/v1/workers"]["workers"][0]
        pool = self.responses["/api/v1/pools"]["pools"][0]
        worker["capabilities"] = ["staged-feature-checkpoints"]
        settings = self.settings["pools"]["game"]
        for local, remote, cap, expected in [(True, True, False, "capability"), (True, False, True, "game"),
                                              (False, True, True, "game"), (True, True, True, None)]:
            settings["direct_merge"], pool["direct_merge"] = local, remote
            if cap and "staged-feature-direct-merge" not in worker["capabilities"]:
                worker["capabilities"].append("staged-feature-direct-merge")
            with self.subTest(local=local, remote=remote, cap=cap):
                if expected:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        install_sops.preflight_worker(self.probe, self.settings, "game", staged_feature=True, direct_merge=True)
                else:
                    self.assertEqual(install_sops.preflight_worker(self.probe, self.settings, "game",
                                     staged_feature=True, direct_merge=True)["pool"]["id"], "game")

    def test_direct_merge_publish_requires_preview_revision(self):
        target = install_sops.Target("garden", "old", "https://worker.example.test")
        with self.assertRaisesRegex(ValueError, "expected-revision"):
            install_sops.install_staged_feature(target, self.fail, "game", Path("."), publish=True, direct_merge=True)

    def test_direct_merge_publishes_the_previewed_workflow_with_live_node_settings(self):
        self._check_staged_install("published-direct")

    def test_staged_publish_preserves_project_bindings_on_conflict(self):
        for outcome in ("preview", "stale-preview", "put-conflict", "changed-before-publish", "changed-on-publish",
                        "no-capability", "wrong-credentials", "published", "published-draft"):
            with self.subTest(outcome=outcome):
                self._check_staged_install(outcome)

    def _check_staged_install(self, outcome):
        feature_path = "/api/workflows/garden-feature"
        project = {"id": "garden", "workflowId": "garden-feature", "sopPlaybooks": [
            {"id": "feature", "default": True, "bindings": [{"workflowId": "garden-feature", "order": 1}]},
            {"id": "ui", "bindings": [{"workflowId": "garden-ui", "order": 1}]},
        ]}
        original_project = json.dumps(project)
        original = {
            "id": "garden-feature", "name": "现有功能流程", "projectId": "garden",
            "status": "published", "revision": 6, "publishedRevision": 6, "version": 3,
            "tags": ["unity-worker-formal", "user-tag"], "sandboxProfileId": "existing-sandbox",
            "projectCredentialIds": ["context-key", "worker-key"], "description": "现有介绍",
            "nodes": [{"id": "previous"}], "edges": [],
        }
        if outcome == "published-draft":
            original.update(status="draft", revision=1, version=1)
            original.pop("publishedRevision")
        if outcome == "published-direct":
            original.update(revision=20, publishedRevision=20, version=11)
            original["nodes"] = [{"id": "review", "type": "script", "maxReworks": 10, "maxAttempts": 4,
                                  "position": {"x": 43, "y": 99}, "notifications": [{"type": "completed"}]}]
        live = original
        writes = []
        workflow_writes = []
        credentials = [
            {"id": "worker-key", "kind": "custom", "variables": ["UNITY_WORKER_URL", "UNITY_WORKER_TOKEN"]},
            {"id": "context-key", "kind": "custom", "variables": ["UNITY_PLANNING_CONTEXT_TOKEN"]},
        ]
        if outcome == "wrong-credentials":
            credentials[0]["variables"] = ["UNITY_WORKER_TOKEN"]
        self.responses["/api/v1/workers"]["workers"][0]["capabilities"] = (
            ["staged-feature"] if outcome == "no-capability" else ["staged-feature", "staged-feature-checkpoints"])
        self.responses["/api/v1/pools"]["pools"][0]["slots"][0]["status"] = "leased"
        if outcome == "published-direct":
            self.responses["/api/v1/workers"]["workers"][0]["capabilities"].append("staged-feature-direct-merge")
            self.responses["/api/v1/pools"]["pools"][0]["direct_merge"] = True

        def api(method, path, value=None, *, missing=False):
            nonlocal live
            if method == "GET":
                return {"/api/projects/garden": project,
                        "/api/projects/garden/credentials": {"canManage": True, "credentials": credentials},
                        feature_path: live}[path]
            writes.append((method, path))
            if (method, path) == ("PUT", "/api/workflows"):
                workflow_writes.append(value["id"])
                if outcome == "published-direct":
                    self.assertEqual([n["id"] for n in value["nodes"]][-3:], ["accept", "merge", "deliver"])
                    review = next(n for n in value["nodes"] if n["id"] == "review")
                    for key, old in original["nodes"][0].items():
                        self.assertEqual(review[key], old)
                self.assertEqual({k: v for k, v in value.items() if k not in {"nodes", "edges"}}, {
                    k: v for k, v in original.items() if k not in {"nodes", "edges"}
                })
                if outcome == "put-conflict":
                    raise RuntimeError("PUT /api/workflows: HTTP 409")
                saved = {**value, "revision": value["revision"] + 1}
                if outcome == "published-draft":
                    saved["nodes"] = [{k: v for k, v in node.items() if k != "maxReworks" or v}
                                      for node in value["nodes"]]
                live = {**saved, "revision": 8} if outcome == "changed-before-publish" else saved
                return saved
            if (method, path) == ("POST", feature_path + "/publish"):
                revision = live["revision"] + 1
                live = {**live, "status": "published", "revision": revision,
                        "publishedRevision": revision, "version": live["version"] + 1}
                if outcome == "changed-on-publish":
                    live = {**live, "nodes": [{"id": "someone-else"}]}
                return live
            self.fail(f"Unexpected staged install write {method} {path}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "worker.toml"
            config.write_text('worker_id = "worker-host"\ndefault_pool = "game"\n'
                              '[[pools.game.slots]]\nid = "primary"\n'
                              '[[pools.game.slots]]\nid = "reserve"\nenabled = false\n', encoding="utf-8")
            args = ["--staged-feature", "--config", str(config), "--output-dir", str(root),
                    "--project-id", "garden", "--api-url", "https://studio.example.test",
                    "--worker-url", "https://worker.example.test"]
            if outcome == "published-direct":
                config.write_text(config.read_text().replace("[[pools.game.slots]]",
                                  "[pools.game]\ndirect_merge = true\n[[pools.game.slots]]", 1))
                args.extend(("--direct-merge", "--expected-revision", "20"))
            if outcome != "preview":
                args.append("--publish")
            if outcome == "stale-preview":
                args.extend(("--expected-revision", "5"))
            env = {"UNITY_FORMAL_STUDIO_TOKEN": "studio-token", "UNITY_WORKER_TOKEN": "worker-token"}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(install_sops, "call_factory", side_effect=[self.probe, api]):
                errors = {"stale-preview": "expected 5, found 6", "put-conflict": "HTTP 409",
                          "changed-before-publish": "changed",
                          "changed-on-publish": "published.*match", "no-capability": "staged-feature",
                          "wrong-credentials": "credential"}
                if outcome in errors:
                    with self.assertRaisesRegex(RuntimeError, errors[outcome]):
                        install_sops.main(args)
                else:
                    install_sops.main(args)
                    backup = json.loads((root / "garden-feature-before.json").read_text(encoding="utf-8"))
                    self.assertEqual(backup, original)

        self.assertEqual(json.dumps(project), original_project)
        if outcome in {"preview", "stale-preview", "no-capability", "wrong-credentials"}:
            self.assertEqual(writes, [])
        else:
            self.assertEqual(workflow_writes, ["garden-feature"])
            expected = [("PUT", "/api/workflows")]
            if outcome in {"published", "published-draft", "published-direct", "changed-on-publish"}:
                expected.append(("POST", feature_path + "/publish"))
            self.assertEqual(writes, expected)
