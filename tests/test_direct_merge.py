import subprocess
import unittest
from unittest.mock import patch

from process_runner import StageError
from support import test_directory
import test_workspace


git = test_workspace.git
TASK_BRANCH = "ai/task-" + "1" * 32
SCRIPT = "Assets/物品 代码.cs"
META = b"guid: " + b"a" * 32 + b"\n"
COMMON = b"class Item { int count = 0; }\n"
SOURCE = b"class Item { int count = 1; }\n"
TARGET = b"class Item { int count = 2; }\n"


def write(checkout, path, data):
    file = checkout / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)


class DirectMergeTests(unittest.TestCase):
    def repository(self, root, *, path=None, common=COMMON, seed=None):
        workspace, ctx, remote, checkout = test_workspace.WorkspaceTests().workspace(root)
        if path is not None and common is not None:
            write(checkout, path, common)
            if path.startswith("Assets/") and not path.endswith(".meta"):
                write(checkout, path + ".meta", META)
            git(checkout, "add", "--all")
            git(checkout, "commit", "-m", "Track the original file")
            git(checkout, "push", "origin", "main")
        if seed:
            seed(checkout)
            self.publish(checkout, "main")

        author = root / "author"
        git(root, "clone", str(remote), str(author))
        task = workspace.prepare(ctx, TASK_BRANCH)
        return workspace, ctx, remote, checkout, author, task

    def publish(self, checkout, branch, *, staged=False):
        if not staged:
            git(checkout, "add", "--all")
        git(checkout, "commit", "-m", "Independent change")
        git(checkout, "push", "origin", branch)
        return git(checkout, "rev-parse", "HEAD")

    def assert_remote_refs(self, remote, base, source):
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), base)
        self.assertEqual(git(remote, "rev-parse", f"refs/heads/{TASK_BRANCH}"), source)

    def test_clean_candidate_preserves_both_parents_and_source_ref(self):
        with test_directory() as root:
            workspace, ctx, remote, checkout, author, task = self.repository(root)
            write(checkout, "hello.txt", b"approved source\n")
            source = self.publish(checkout, TASK_BRANCH)
            write(author, "main-only.txt", b"new main content\n")
            base = self.publish(author, "main")

            from direct_merge import DirectMergeGit
            merge = DirectMergeGit(workspace)
            self.assertEqual(merge.read_target(ctx, task), base)
            candidate = merge.begin(ctx, task, source, base)
            self.assertEqual(candidate.conflict_paths, ())
            self.assertEqual(git(checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                             [candidate.commit_sha, base, source])
            self.assertEqual(candidate.tree_sha, git(checkout, "rev-parse", "HEAD^{tree}"))
            self.assertEqual(git(checkout, "branch", "--show-current"), "")
            self.assertEqual((checkout / "hello.txt").read_bytes(), b"approved source\n")
            self.assertEqual((checkout / "main-only.txt").read_bytes(), b"new main content\n")
            self.assertTrue(merge.contains(ctx, source, candidate.commit_sha))
            self.assertFalse(merge.contains(ctx, source, base))
            merge.assert_clean_candidate(ctx, candidate)
            self.assert_remote_refs(remote, base, source)
            self.assertEqual(git(checkout, "rev-parse", TASK_BRANCH), source)

            write(checkout, "manual-note.txt", b"keep operator evidence")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual((checkout / "manual-note.txt").read_bytes(), b"keep operator evidence")
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), candidate.commit_sha)
            (checkout / "manual-note.txt").unlink()

            marker = checkout / ".git/MERGE_AUTOSTASH"
            marker.write_text(source + "\n")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertTrue(marker.is_file())
            marker.unlink()

            DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), source)
            self.assertFalse((checkout / "main-only.txt").exists())
            self.assertEqual((checkout / "Library/cache").read_text(), "keep")
            write(checkout, "hello.txt", b"post-delivery edits")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual((checkout / "hello.txt").read_bytes(), b"post-delivery edits")
            git(checkout, "restore", "--", "hello.txt")
            git(checkout, "switch", "--detach", candidate.commit_sha)
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), candidate.commit_sha)
            self.assert_remote_refs(remote, base, source)

    def test_reads_main_from_push_endpoint_when_fetch_remote_differs(self):
        with test_directory() as root:
            workspace, ctx, remote, checkout, author, task = self.repository(root)
            mirror = root / "old-mirror"
            git(root, "clone", "--bare", str(remote), str(mirror))
            git(checkout, "remote", "set-url", "origin", str(mirror))
            git(checkout, "remote", "set-url", "--push", "origin", str(remote))
            task["remote_identity"] = workspace.remote_identity(ctx)
            write(author, "main-only.txt", b"new target content\n")
            base = self.publish(author, "main")

            from direct_merge import DirectMergeGit
            merge = DirectMergeGit(workspace)
            self.assertNotEqual(git(mirror, "rev-parse", "main"), base)
            self.assertEqual(merge.read_target(ctx, task), base)
            self.assertEqual(git(checkout, "cat-file", "-t", base), "commit")
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), task["base_commit"])

    @unittest.skipUnless(subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0, "Git LFS")
    def test_candidate_hydrates_lfs_and_rejects_pointer_replacement(self):
        with test_directory() as root:
            workspace, ctx, remote, checkout, author, task = self.repository(root)
            for repository in (checkout, author):
                git(repository, "lfs", "install", "--local", "--skip-repo")
            mirror = root / "old-mirror"
            git(root, "clone", "--bare", str(remote), str(mirror))
            git(checkout, "remote", "set-url", "origin", str(mirror))
            git(checkout, "remote", "set-url", "--push", "origin", str(remote))
            task["remote_identity"] = workspace.remote_identity(ctx)
            git(author, "lfs", "track", "payload.asset")
            payload = b"binary Unity dependency\0" * 30
            write(author, "payload.asset", payload)
            base = self.publish(author, "main")
            write(checkout, "hello.txt", b"approved source\n")
            source = self.publish(checkout, TASK_BRANCH)

            from direct_merge import DirectMergeGit
            merge = DirectMergeGit(workspace)
            self.assertEqual(merge.read_target(ctx, task), base)
            candidate = merge.begin(ctx, task, source, base)
            self.assertEqual((checkout / "payload.asset").read_bytes(), payload)
            (checkout / "payload.asset").unlink()
            workspace.git(ctx, "checkout-index", "--force", "--index", "--", "payload.asset")
            pointer = git(checkout, "show", "HEAD:payload.asset") + "\n"
            self.assertEqual((checkout / "payload.asset").read_bytes(), pointer.encode("ascii"))
            with self.assertRaisesRegex(StageError, "unhydrated Git LFS"):
                merge.assert_clean_candidate(ctx, candidate)
            self.assert_remote_refs(remote, base, source)

    def test_review_restore_retries_owned_submodule_hydration_after_switch(self):
        with test_directory() as root:
            module_remote, module_author = root / "module-remote", root / "module-author"
            git(root, "init", "--bare", "--initial-branch=main", str(module_remote))
            git(root, "clone", str(module_remote), str(module_author))
            write(module_author, "version.txt", b"original module\n")
            old_module = self.publish(module_author, "main")

            def seed(checkout):
                git(checkout, "-c", "protocol.file.allow=always", "submodule", "add",
                    str(module_remote), "Packages/Fixture")
                git(checkout / "Packages/Fixture", "config", "protocol.file.allow", "always")

            workspace, ctx, remote, checkout, author, task = self.repository(root, seed=seed)
            write(module_author, "version.txt", b"advanced module\n")
            new_module = self.publish(module_author, "main")
            git(author, "update-index", "--cacheinfo", f"160000,{new_module},Packages/Fixture")
            base = self.publish(author, "main", staged=True)
            write(checkout, "hello.txt", b"approved source\n")
            source = self.publish(checkout, TASK_BRANCH)

            from direct_merge import DirectMergeGit
            merge = DirectMergeGit(workspace)
            candidate = merge.begin(ctx, task, source, merge.read_target(ctx, task))
            merge.assert_clean_candidate(ctx, candidate)
            checked_out = checkout / "Packages/Fixture"
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), new_module)
            self.assertEqual((checked_out / "version.txt").read_bytes(), b"advanced module\n")
            git(checkout, "switch", "--no-recurse-submodules", TASK_BRANCH)
            with self.assertRaises(StageError):
                merge.restore(ctx, task, source)
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), new_module)
            git(checkout, "switch", "--detach", "--no-recurse-submodules", candidate.commit_sha)

            with patch.object(workspace, "hydrate_checkout", side_effect=StageError("git_failed", "connection lost")):
                with self.assertRaisesRegex(StageError, "connection lost"):
                    merge.restore(ctx, task, source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), new_module)

            write(checked_out, "version.txt", b"operator changes\n")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual((checked_out / "version.txt").read_bytes(), b"operator changes\n")
            git(checked_out, "restore", "--", "version.txt")
            git(checked_out, "commit", "--allow-empty", "-m", "Operator dependency commit")
            external = git(checked_out, "rev-parse", "HEAD")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), external)
            git(checked_out, "switch", "--detach", new_module)

            DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), old_module)
            self.assertEqual((checked_out / "version.txt").read_bytes(), b"original module\n")
            self.assert_remote_refs(remote, base, source)

    def review_conflict(self, root, *, seed=None):
        from direct_merge import DirectMergeGit

        workspace, ctx, remote, checkout, author, task = self.repository(root, path=SCRIPT, seed=seed)
        write(checkout, SCRIPT, SOURCE)
        source = self.publish(checkout, TASK_BRANCH)
        write(author, SCRIPT, TARGET)
        base = self.publish(author, "main")
        merge = DirectMergeGit(workspace)
        candidate = merge.begin(ctx, task, source, merge.read_target(ctx, task))
        self.assertEqual(candidate.conflict_paths, (SCRIPT,))
        return merge, ctx, candidate, remote, checkout, base, source

    def test_review_resolution_rejects_raw_changes_hidden_by_git_normalization(self):
        def seed(checkout):
            write(checkout, ".gitattributes", b"hello.txt text eol=lf\n")
            write(checkout, "hello.txt", b"old\n")
            git(checkout, "config", "core.autocrlf", "false")

        with test_directory() as root:
            merge, ctx, candidate, remote, checkout, base, source = self.review_conflict(root, seed=seed)
            self.assertEqual((checkout / "hello.txt").read_bytes(), b"old\n")
            write(checkout, "hello.txt", b"old\r\n")
            # A clean conversion refreshes index stat data without changing the staged blob.
            git(checkout, "add", "--", "hello.txt")
            self.assertNotIn("hello.txt", git(checkout, "status", "--porcelain"))
            write(checkout, SCRIPT, b"class Item { int both = 3; }\n")
            with self.assertRaises(StageError) as rejected:
                merge.resolve(ctx, candidate)
            self.assertEqual(rejected.exception.kind, "merge_resolution_scope")
            self.assertEqual((checkout / "hello.txt").read_bytes(), b"old\r\n")
            self.assert_remote_refs(remote, base, source)

    def test_review_resolution_rejects_markers_after_bom_or_bare_cr(self):
        for prefix in (b"\xef\xbb\xbf", b"class Item {}\r"):
            with self.subTest(prefix=prefix), test_directory() as root:
                merge, ctx, candidate, remote, checkout, base, source = self.review_conflict(root)
                write(checkout, SCRIPT, prefix + b"<<<<<<< leftover marker\n")
                with self.assertRaises(StageError) as rejected:
                    merge.resolve(ctx, candidate)
                self.assertEqual(rejected.exception.kind, "merge_resolution_scope")
                self.assertTrue((checkout / ".git/MERGE_HEAD").is_file())
                self.assert_remote_refs(remote, base, source)

    def test_content_conflict_accepts_exact_csharp_paths(self):
        with test_directory() as root:
            workspace, ctx, remote, checkout, author, task = self.repository(root, path=SCRIPT)
            write(checkout, SCRIPT, SOURCE)
            write(checkout, "source.txt", b"source addition\n")
            source = self.publish(checkout, TASK_BRANCH)
            write(author, SCRIPT, TARGET)
            write(author, "main.txt", b"main addition\n")
            base = self.publish(author, "main")

            from direct_merge import DirectMergeGit
            merge = DirectMergeGit(workspace)
            candidate = merge.begin(ctx, task, source, merge.read_target(ctx, task))
            self.assertEqual(candidate.conflict_paths, ("Assets/物品 代码.cs",))
            self.assertIsNone(candidate.commit_sha)

            write(checkout, SCRIPT, b"class Item { int both = 3; }\n")
            candidate = merge.resolve(ctx, candidate)
            merge.assert_clean_candidate(ctx, candidate)
            self.assertEqual(git(checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                             [candidate.commit_sha, base, source])
            self.assertEqual((checkout / SCRIPT).read_bytes(), b"class Item { int both = 3; }\n")
            self.assertEqual((checkout / "source.txt").read_bytes(), b"source addition\n")
            self.assertEqual((checkout / "main.txt").read_bytes(), b"main addition\n")
            self.assert_remote_refs(remote, base, source)

    def test_rejects_complex_and_out_of_scope_changes(self):
        lfs = (b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"1" * 64 + b"\nsize 10\n")
        cases = (
            ("scene", "Assets/Level.unity", COMMON),
            ("prefab", "Assets/Item.prefab", COMMON),
            ("meta", "Assets/Item.cs.meta", META),
            ("rename", SCRIPT, COMMON),
            ("add/add", SCRIPT, None),
            ("modify/delete", SCRIPT, COMMON),
            ("symlink", SCRIPT, COMMON),
            ("binary", SCRIPT, b"bin\0ary\n"),
            ("LFS pointer", SCRIPT, lfs),
            ("unrelated change", SCRIPT, COMMON),
            ("staged change", SCRIPT, COMMON),
            ("markers", SCRIPT, COMMON),
        )
        for kind, path, common in cases:
            with self.subTest(kind=kind), test_directory() as root:
                workspace, ctx, remote, checkout, author, task = self.repository(root, path=path, common=common)
                if kind == "rename":
                    git(checkout, "mv", path, "Assets/Renamed.cs")
                    git(checkout, "mv", path + ".meta", "Assets/Renamed.cs.meta")
                    git(author, "mv", path, "Assets/Other.cs")
                    git(author, "mv", path + ".meta", "Assets/Other.cs.meta")
                elif kind == "modify/delete":
                    (checkout / path).unlink()
                    (checkout / (path + ".meta")).unlink()
                    write(author, path, TARGET)
                elif kind == "symlink":
                    # The fixture records a linked Git file even on Windows without symlink permissions.
                    for parent, destination in ((checkout, b"hello.txt"), (author, b".gitignore")):
                        git(parent, "config", "core.symlinks", "false")
                        write(parent, path, destination)
                        blob = git(parent, "hash-object", "-w", "--", path)
                        git(parent, "update-index", "--cacheinfo", f"120000,{blob},{path}")
                elif kind == "binary":
                    write(checkout, path, b"task\0binary")
                    write(author, path, b"main\0binary")
                elif kind == "LFS pointer":
                    write(checkout, path, lfs.replace(b"1" * 64, b"2" * 64))
                    write(author, path, lfs.replace(b"1" * 64, b"3" * 64))
                else:
                    write(checkout, path, SOURCE)
                    write(author, path, TARGET)
                source = self.publish(checkout, TASK_BRANCH, staged=(kind == "symlink"))
                base = self.publish(author, "main", staged=(kind == "symlink"))

                from direct_merge import DirectMergeGit
                merge = DirectMergeGit(workspace)
                self.assertEqual(merge.read_target(ctx, task), base)
                if kind in {"unrelated change", "staged change", "markers"}:
                    candidate = merge.begin(ctx, task, source, base)
                    if kind == "markers":
                        write(checkout, path, b">>>>>>> leftover marker\n")
                    else:
                        write(checkout, path, b"class Item { int both = 3; }\n")
                        write(checkout, "hello.txt", b"outside the resolution scope")
                        if kind == "staged change":
                            git(checkout, "add", "--", "hello.txt")
                    with self.assertRaises(StageError) as rejected:
                        merge.resolve(ctx, candidate)
                    self.assertEqual(rejected.exception.kind, "merge_resolution_scope")
                else:
                    with self.assertRaises(StageError) as rejected:
                        merge.begin(ctx, task, source, base)
                    self.assertEqual(rejected.exception.kind, "merge_conflict")
                self.assert_remote_refs(remote, base, source)
                self.assertEqual(git(checkout, "rev-parse", TASK_BRANCH), source)
                if kind in {"unrelated change", "staged change", "markers"}:
                    self.assertTrue((checkout / ".git/MERGE_HEAD").is_file())
                    with self.assertRaises(StageError):
                        DirectMergeGit(workspace).restore(ctx, task, source)
                    self.assertTrue((checkout / ".git/MERGE_HEAD").is_file())
                    if kind != "markers":
                        self.assertEqual((checkout / "hello.txt").read_bytes(), b"outside the resolution scope")
