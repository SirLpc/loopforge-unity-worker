import subprocess
import shutil
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from support import test_directory


def git(root, *args):
    cmd = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args]
    return subprocess.run(cmd, cwd=root, check=True, capture_output=True).stdout.decode().strip()


def repository(root):
    remote, project, logs = (root / name for name in ("remote", "project", "logs"))
    for directory in (remote, project, logs):
        directory.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(project, "init", "--initial-branch=main")
    git(project, "config", "user.name", "Fixture")
    git(project, "config", "user.email", "fixture@example.invalid")
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f3\n")
    (project / "Packages").mkdir()
    (project / "Packages/manifest.json").write_text('{"dependencies":{"com.unity.test-framework":"1.1.33"}}\n')
    (project / ".gitignore").write_text("/Library/\n/Temp/\n/Logs/\n/ignored.txt\n")
    (project / "hello.txt").write_text("old\n")
    git(project, "add", "--", "ProjectSettings/ProjectVersion.txt", "Packages/manifest.json", ".gitignore", "hello.txt")
    git(project, "commit", "-m", "base")
    git(project, "remote", "add", "origin", str(remote))
    git(project, "push", "-u", "origin", "main")
    (project / "Library").mkdir()
    (project / "Library/cache").write_text("keep")
    return remote, project, logs


class WorkspaceTests(unittest.TestCase):
    def workspace(self, root):
        from git_workspace import GitWorkspace
        from process_runner import ProcessRunner

        remote, project, logs = repository(root)
        cfg = SimpleNamespace(project=project, git_exe=Path(shutil.which("git")).resolve().as_posix(),
                              git_remote="origin", base_branch="main", git_timeout=10, init_submodules=False,
                              max_diff_bytes=100_000, unity_version="2022.3.62f3", secrets=("fake-sensitive-token",))
        runner = ProcessRunner()

        def run(argv, label, **options):
            return runner.run(argv, project, logs / (label + "-" + uuid4().hex),
                              cancel=threading.Event(), **options)

        ctx = SimpleNamespace(run=run, directory=logs)
        return GitWorkspace(cfg), ctx, remote, project

    def test_fetch_base_and_create_branch_in_place(self):
        with test_directory() as root:
            workspace, ctx, remote, project = self.workspace(root)
            base = git(remote, "rev-parse", "main")
            self.assertEqual(workspace.prepare(ctx, "ai/task-123")["base_commit"], base)
            self.assertEqual(git(project, "branch", "--show-current"), "ai/task-123")
            self.assertEqual((project / "Library/cache").read_text(), "keep")

    def test_unknown_untracked_and_ignored_files_block_and_survive(self):
        from process_runner import StageError

        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            for filename in ("stray.txt", "ignored.txt"):
                with self.subTest(filename=filename):
                    path = project / filename
                    path.write_text("retain this")
                    with self.assertRaises(StageError) as error:
                        workspace.prepare(ctx, "ai/task-123")
                    self.assertEqual(error.exception.kind, "dirty_workspace")
                    self.assertEqual(path.read_text(), "retain this")
                    self.assertEqual(git(project, "branch", "--show-current"), "main")
                    path.unlink()

    def test_snapshot_detects_assets_missing_meta_and_rejects_secret_file(self):
        from process_runner import StageError

        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            task = workspace.prepare(ctx, "ai/task-123")
            (project / "Assets").mkdir()
            (project / "Assets/New.cs").write_text("class New {}")
            with self.assertRaisesRegex(StageError, "meta"):
                workspace.snapshot(ctx, task, check_meta=True)
            (project / "Assets/New.cs.meta").write_text("guid: " + "a" * 32 + "\n")
            diff = workspace.snapshot(ctx, task, check_meta=True)
            self.assertEqual({row["path"] for row in diff.changes}, {"Assets/New.cs", "Assets/New.cs.meta"})
            (project / ".env").write_text("do not expose")
            with self.assertRaisesRegex(StageError, "Sensitive"):
                workspace.snapshot(ctx, task)

    def test_existing_tracked_cache_is_not_removed_by_branch_switch(self):
        from process_runner import StageError

        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            git(project, "add", "-f", "--", "Library/cache")
            git(project, "commit", "-m", "Unexpected tracked cache")
            with self.assertRaisesRegex(StageError, "cache"):
                workspace.prepare(ctx, "ai/task-123")
            self.assertEqual((project / "Library/cache").read_text(), "keep")
            self.assertEqual(git(project, "branch", "--show-current"), "main")

    def test_assume_unchanged_cannot_hide_local_edits(self):
        from process_runner import StageError

        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            git(project, "update-index", "--assume-unchanged", "hello.txt")
            (project / "hello.txt").write_text("hidden local changes")
            self.assertEqual(git(project, "status", "--porcelain"), "")
            with self.assertRaisesRegex(StageError, "assume-unchanged"):
                workspace.prepare(ctx, "ai/task-123")
            self.assertEqual((project / "hello.txt").read_text(), "hidden local changes")

    def test_ignored_submodule_file_blocks_prepare_and_is_preserved(self):
        from process_runner import StageError

        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            module = root / "module"
            module.mkdir()
            git(module, "init", "--initial-branch=main")
            (module / ".gitignore").write_text("private.bin\n")
            git(module, "add", "--", ".gitignore")
            git(module, "commit", "-m", "Submodule")
            git(project, "-c", "protocol.file.allow=always", "submodule", "add", str(module), "Packages/Fixture")
            git(project, "commit", "-m", "Seed initialized submodule")
            git(project, "push", "origin", "main")
            local = project / "Packages/Fixture/private.bin"
            local.write_bytes(b"unknown data")
            self.assertEqual(git(project, "status", "--porcelain", "--ignore-submodules=none"), "")
            with self.assertRaisesRegex(StageError, "submodule"):
                workspace.prepare(ctx, "ai/task-123")
            self.assertEqual(local.read_bytes(), b"unknown data")
            local.unlink()
            workspace.prepare(ctx, "ai/task-123")
            self.assertTrue(git(project, "submodule", "status"))

    @unittest.skipUnless(subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0, "Git LFS")
    def test_prepares_lfs_checkout_in_place(self):
        with test_directory() as root:
            workspace, ctx, _, project = self.workspace(root)
            git(project, "lfs", "install", "--local", "--skip-repo")
            git(project, "lfs", "track", "*.asset")
            payload = b"binary lfs data\x00" * 25
            (project / "Large.asset").write_bytes(payload)
            git(project, "add", "--", ".gitattributes", "Large.asset")
            git(project, "commit", "-m", "Add LFS data")
            git(project, "lfs", "push", "origin", "main")
            git(project, "push", "origin", "main")
            task = workspace.prepare(ctx, "ai/task-123")
            self.assertTrue(task["lfs"])
            self.assertTrue((project / ".git/lfs/objects").is_dir())
            self.assertEqual((project / "Large.asset").read_bytes(), payload)
