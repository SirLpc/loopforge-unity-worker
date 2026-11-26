import os
import shutil
import subprocess
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import test_directory
from git_workspace import GitWorkspace
from process_runner import ProcessRunner, StageError
from test_workspace import git
import workspace_storage
from workspace_storage import measure_directory, volume_usage


def directory_link(link, target):
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
        return link.rmdir
    link.symlink_to(target, target_is_directory=True)
    return link.unlink


class WorkspaceStorageTests(unittest.TestCase):
    def test_nested_project_cache_bucket_excludes_checkout_level_cache(self):
        with test_directory() as root:
            files = {".git/objects/data": b"git", "Unity/Library/assets/data": b"cache",
                     "Library/data": b"outside"}
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_bytes(content)

            result = measure_directory(root, {".git": root / ".git", "Library": root / "Unity/Library"})

        self.assertTrue(result["exists"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["logical_bytes"], 3 + 5 + 7)
        self.assertEqual(result["buckets"], {".git": 3, "Library": 5, "other": 7})

    def test_missing_root_has_no_logical_size(self):
        with test_directory() as root:
            result = measure_directory(root / "missing", {})

        self.assertFalse(result["exists"])
        self.assertIsNone(result["logical_bytes"])
        self.assertEqual(result["read_errors"], 0)

    def test_unreadable_directory_keeps_partial_logical_size(self):
        with test_directory() as root:
            unreadable = root / "blocked"
            unreadable.mkdir()
            (root / "visible").write_bytes(b"abc")
            (unreadable / "invisible").write_bytes(b"blocked")
            scandir = os.scandir

            def fail_for_blocked(path):
                if Path(path) == unreadable:
                    raise PermissionError("fixture read error")
                return scandir(path)

            with patch("os.scandir", side_effect=fail_for_blocked):
                result = measure_directory(root, {})

        self.assertFalse(result["complete"])
        self.assertEqual(result["read_errors"], 1)
        self.assertEqual(result["logical_bytes"], 3)
        self.assertEqual(result["buckets"], {"other": 3})

    def test_directory_link_is_skipped_as_entry_root_and_ancestor(self):
        with test_directory() as root:
            checkout = root / "checkout"
            external = root / "external"
            external.mkdir()
            (external / "subdir").mkdir()
            (external / "subdir/private").write_bytes(b"excluded")
            checkout.mkdir()
            (checkout / "visible").write_bytes(b"abc")
            link = checkout / "Library"
            remove_link = directory_link(link, external)
            try:
                for path, total in ((checkout, 3), (link, 0), (link / "subdir", 0)):
                    with self.subTest(path=path):
                        result = measure_directory(path, {})
                        self.assertFalse(result["complete"])
                        self.assertEqual(result["logical_bytes"], total)
                        self.assertEqual(result["skipped_links"], 1)
                        self.assertEqual(result["read_errors"], 0)
            finally:
                remove_link()

    def test_replaced_directory_contributes_no_bytes(self):
        for phase in ("queued directory", "queued ancestor", "opening directory", "opening replacement"):
            with self.subTest(phase=phase), test_directory() as root, ExitStack() as cleanup:
                checkout = root / "checkout"
                library = checkout / "Library"
                (library / "subdir").mkdir(parents=True)
                (checkout / "visible").write_bytes(b"abc")
                external = root / "external"
                (external / "subdir").mkdir(parents=True)
                (external / "external").write_bytes(b"x" * 200)
                (external / "subdir/external").write_bytes(b"x" * 231)
                replacement_is_link = phase != "opening replacement"
                scandir = os.scandir

                def replace_library():
                    library.rename(root / "original")
                    if replacement_is_link:
                        cleanup.callback(directory_link(library, external))
                    else:
                        external.rename(library)

                @contextmanager
                def concurrent_scan(path):
                    path = Path(path)
                    if path == library and phase.startswith("opening"):
                        replace_library()
                    with scandir(path) as entries:
                        yield entries
                    if (path == checkout and phase == "queued directory") or (
                            path == library and phase == "queued ancestor"):
                        replace_library()

                with patch("os.scandir", side_effect=concurrent_scan):
                    result = measure_directory(checkout, {})

                self.assertEqual(result["logical_bytes"], 3)
                self.assertFalse(result["complete"])
                self.assertEqual(result["skipped_links"], int(replacement_is_link))
                self.assertEqual(result["read_errors"], int(not replacement_is_link))

    def test_unavailable_volume_uses_null_capacity(self):
        with test_directory() as root, patch("shutil.disk_usage", side_effect=OSError):
            self.assertEqual(volume_usage(root), {"total_bytes": None, "free_bytes": None})


def cache_checkout(root):
    checkout = root / "checkout"
    project = checkout / "Unity"
    (project / "Library").mkdir(parents=True)
    (project / "Library/data").write_bytes(b"cache")
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
    (checkout / ".gitignore").write_text("/Unity/Library/\n/Unity/Temp/\n/Unity/Obj/\n")
    git(checkout, "init")
    git(checkout, "add", ".gitignore", "Unity/ProjectSettings")
    git(checkout, "commit", "-m", "baseline")
    return checkout, project


class CacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        self.checkout, self.project = cache_checkout(self.root)
        self.cache = self.project / "Library"
        cfg = SimpleNamespace(project=self.project, git_root=self.checkout, git_exe=shutil.which("git"),
                              git_timeout=10, secrets=())
        self.workspace = GitWorkspace(cfg)
        runner = ProcessRunner()
        self.ctx = SimpleNamespace(directory=self.root, run=lambda argv, label, **kw:
                                   runner.run(argv, prefix=self.root / label, **kw))

    def rejected(self):
        with self.assertRaises(StageError):
            workspace_storage.clean_cache(self.workspace, self.ctx, "Library")
        self.assertEqual((self.cache / "data").read_bytes(), b"cache")

    def test_index_and_head_only_files_survive(self):
        git(self.checkout, "add", "-f", "--", "Unity/Library/data")
        self.rejected()
        git(self.checkout, "commit", "-m", "cache file")
        git(self.checkout, "rm", "--cached", "Unity/Library/data")
        self.rejected()

    @unittest.skipUnless(os.name == "nt", "Windows path case")
    def test_different_case_tracked_path_survives(self):
        git(self.checkout, "update-index", "--add", "--cacheinfo",
            f"100644,{git(self.checkout, 'rev-parse', 'HEAD:.gitignore')},unity/library/data")
        self.rejected()

    def test_gitlink_covering_project_survives(self):
        git(self.checkout, "rm", "-r", "--cached", "Unity")
        git(self.checkout, "update-index", "--add", "--cacheinfo",
            f"160000,{git(self.checkout, 'rev-parse', 'HEAD')},Unity")
        self.rejected()

    def test_unignored_cache_and_embedded_repository_survive(self):
        ignore = self.checkout / ".gitignore"
        rules = ignore.read_text()
        ignore.write_text("/Unity/Library/*.tmp\n")
        self.rejected()
        ignore.write_text(rules)
        git(self.cache, "init", "nested")
        self.rejected()

    def test_embedded_bare_repository_survives(self):
        git(self.cache, "init", "--bare", "repo")
        self.rejected()

    def test_unity_lock_and_uncertain_directory_are_protected(self):
        (self.project / "Temp").mkdir()
        lock = self.project / "Temp/UnityLockfile"
        lock.touch()
        self.rejected()
        lock.unlink()
        scandir = os.scandir
        def fail_cache_scan(path):
            if Path(path) == self.cache:
                raise PermissionError("fixture")
            return scandir(path)
        with patch("os.scandir", side_effect=fail_cache_scan):
            self.rejected()

    def test_uncertain_unity_lock_check_preserves_cache(self):
        (self.project / "Temp").mkdir()
        lock = self.project / "Temp/UnityLockfile"
        lstat = os.lstat
        def denied(path, *args, **kwargs):
            if Path(path) == lock:
                raise PermissionError("fixture")
            return lstat(path, *args, **kwargs)
        with patch("os.lstat", side_effect=denied):
            self.rejected()

    def test_directory_links_at_and_below_cache_or_in_project_path_survive(self):
        external = self.root / "external"
        external.mkdir()
        (external / "data").write_bytes(b"cache")
        for path in (self.cache / "nested", self.cache, self.project):
            with self.subTest(path=path), ExitStack() as cleanup:
                if path.exists():
                    original = self.root / "original"
                    path.rename(original)
                    cleanup.callback(original.rename, path)
                else:
                    original = external
                cleanup.callback(directory_link(path, original))
                self.rejected()

    def test_invalid_git_root_survives(self):
        (self.checkout / ".git").rename(self.root / "saved-git")
        self.rejected()
