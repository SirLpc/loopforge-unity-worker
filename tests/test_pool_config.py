import os
import sys
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from support import test_directory
from test_workspace import git, repository
import worker_config


class PoolConfigTests(unittest.TestCase):
    def pool_fixture(self, root):
        remote, first, logs = repository(root)
        second = root / "worker-02"
        git(root, "clone", str(remote), str(second))
        urls = [git(first, "remote", "get-url", *args, "origin") for args in ((), ("--push",))]
        digest = sha256("\0".join(urls).encode()).hexdigest()
        config = {
            "worker_id": "service",
            "logs": str(logs),
            "default_pool": "game",
            "legacy_slot": "worker-01",
            "pools": {
                "game": {
                    "unity_exe": sys.executable,
                    "unity_version": "2022.3.62f3",
                    "remote_identity": digest,
                    "slots": [
                        {"id": "worker-01", "git_root": str(first), "project": str(first)},
                        {"id": "worker-02", "git_root": str(second), "project": str(second)},
                    ],
                },
            },
        }
        return config, first, second

    def config_loader(self):
        self.assertTrue(hasattr(worker_config, "load_service_settings"), "Missing named pool loader")
        return worker_config.load_service_settings

    def setUp(self):
        env = {"UNITY_WORKER_TOKEN": "test-" * 8}
        self.enterContext(patch.dict(os.environ, env))

    def test_loads_two_named_pools_and_legacy_defaults(self):
        load = self.config_loader()
        with test_directory() as root:
            config, first, _ = self.pool_fixture(root)
            tools = root / "tools"
            git(root, "clone", str(root / "remote"), str(tools))
            config["pools"]["tools"] = {**config["pools"]["game"], "slots": [
                {"id": "tools-01", "git_root": str(tools), "project": str(tools)},
            ]}
            service = load(config=config)
            self.assertEqual([slot.id for slot in service.pools["game"].slots], ["worker-01", "worker-02"])
            self.assertEqual([slot.id for slot in service.pools["tools"].slots], ["tools-01"])
            self.assertEqual(service.max_parallel_unity, 1)
            legacy = load(config={"project": str(first), "logs": config["logs"], "unity_exe": sys.executable,
                                  "worker_id": "legacy-install"})
            self.assertEqual(legacy.default_pool, "legacy-install")
            self.assertEqual(legacy.legacy_slot, "worker-01")
            self.assertEqual(legacy.slots["worker-01"].settings.project, first)

    def test_direct_merge_requires_explicit_boolean_pool_opt_in(self):
        with test_directory() as root:
            config, _, _ = self.pool_fixture(root)
            definition = config["pools"]["game"]
            load = self.config_loader()
            self.assertIs(getattr(load(config=config).pools["game"], "direct_merge", None), False)
            for setting in (False, True):
                definition["direct_merge"] = setting
                self.assertIs(load(config=config).pools["game"].direct_merge, setting)
            for setting in ("true", 1, None):
                definition["direct_merge"] = setting
                with self.assertRaises(ValueError):
                    load(config=config)

    def test_pool_status_exposes_effective_target_branch_and_repository_fingerprint(self):
        from development import DevelopmentWorker
        with test_directory() as root:
            config, _, _ = self.pool_fixture(root)
            config["base_branch"] = "release/next"
            config["pools"]["game"]["direct_merge"] = True
            worker = DevelopmentWorker(self.config_loader()(config=config))
            self.addCleanup(worker.close)
            pool, = worker.pools()
            self.assertEqual(pool.get("base_branch"), "release/next")
            self.assertEqual(pool.get("remote_identity"), config["pools"]["game"]["remote_identity"])

    def test_rejects_overlapping_checkouts_and_codex_home_inside_a_checkout(self):
        load = self.config_loader()
        with test_directory() as root:
            config, first, _ = self.pool_fixture(root)
            for override in ({"id": "worker-01"},
                             {"git_root": str(first), "project": str(first)},
                             {"git_root": str(first / "nested"), "project": str(first / "nested")}):
                with self.subTest(override=override), self.assertRaises(ValueError):
                    edited = deepcopy(config)
                    edited["pools"]["game"]["slots"][1].update(override)
                    load(config=edited)
            config.update(codex_exe=sys.executable, codex_home=str(first / "codex"))
            with self.assertRaisesRegex(ValueError, "Codex home"):
                load(config=config)

    def test_all_slots_and_pools_share_one_worker_codex_home(self):
        with test_directory() as root:
            config, _, _ = self.pool_fixture(root)
            tools = root / "tools"
            git(root, "clone", str(root / "remote"), str(tools))
            config["pools"]["tools"] = {**config["pools"]["game"], "slots": [
                {"id": "tools-01", "git_root": str(tools), "project": str(tools)},
            ]}
            login = root / "worker-codex-home"
            login.mkdir()
            config.update(codex_exe=sys.executable, codex_home=str(login))
            service = self.config_loader()(config=config)
            self.assertEqual(set(service.slots), {"worker-01", "worker-02", "tools-01"})
            self.assertEqual({slot.settings.codex_home for slot in service.slots.values()}, {login})
            config.pop("codex_home")
            with patch.dict(os.environ, {"UNITY_CODEX_HOME": str(login)}):
                self.assertEqual({slot.settings.codex_home for slot in self.config_loader()(config=config).slots.values()}, {login})

    def test_marks_only_bad_slot_unavailable(self):
        load = self.config_loader()
        with test_directory() as root:
            config, first, second = self.pool_fixture(root)
            service = load(config=config)
            git(second, "remote", "set-url", "origin", str(root / "elsewhere"))
            with patch("unity_executor.unity_product_version", return_value="2022.3.62f3"):
                self.assertIsNone(worker_config.check_slot(service.slots["worker-01"], leased=False))
                self.assertEqual(worker_config.check_slot(service.slots["worker-02"], leased=False), "remote_mismatch")
                lock = first / "Temp/UnityLockfile"
                lock.parent.mkdir()
                lock.touch()
                self.assertEqual(worker_config.check_slot(service.slots["worker-01"], leased=False), "workspace_in_use")
                self.assertIsNone(worker_config.check_slot(service.slots["worker-01"], leased=True))

    def test_nested_cache_can_never_link_to_another_slot(self):
        import subprocess

        with test_directory() as root:
            config, first, second = self.pool_fixture(root)
            foreign = second / "Library"
            foreign.mkdir()
            link = first / "Library/RedirectedCache"
            if os.name == "nt":
                subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(foreign)],
                               check=True, capture_output=True)
            else:
                link.symlink_to(foreign, target_is_directory=True)
            try:
                service = self.config_loader()(config=config)
                with patch("unity_executor.unity_product_version", return_value="2022.3.62f3"):
                    self.assertEqual(worker_config.check_slot(service.slots["worker-01"], leased=False), "workspace_link")
                    self.assertIsNone(worker_config.check_slot(service.slots["worker-02"], leased=False))
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()
