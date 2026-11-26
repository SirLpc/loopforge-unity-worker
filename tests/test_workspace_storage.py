import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from support import test_directory
from workspace_storage import measure_directory, volume_usage


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
            if os.name == "nt":
                subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
                               check=True, capture_output=True)
                remove_link = link.rmdir
            else:
                link.symlink_to(external, target_is_directory=True)
                remove_link = link.unlink
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

    def test_unavailable_volume_uses_null_capacity(self):
        with test_directory() as root, patch("shutil.disk_usage", side_effect=OSError):
            self.assertEqual(volume_usage(root), {"total_bytes": None, "free_bytes": None})
