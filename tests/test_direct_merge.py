import subprocess
import unittest
from dataclasses import replace
from types import SimpleNamespace
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
            self.assertEqual((checkout / "hello.txt").read_text(), "approved source\n")
            self.assertEqual((checkout / "main-only.txt").read_text(), "new main content\n")
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
            write(module_author, ".gitattributes", b"version.txt text eol=lf\n")
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

            write(checked_out, "version.txt", b"advanced module\r\n")
            git(checked_out, "add", "--", "version.txt")
            self.assertEqual(git(checked_out, "status", "--porcelain"), "")
            with self.assertRaises(StageError):
                DirectMergeGit(workspace).restore(ctx, task, source)
            self.assertEqual((checked_out / "version.txt").read_bytes(), b"advanced module\r\n")
            self.assertEqual(git(checked_out, "rev-parse", "HEAD"), new_module)
            write(checked_out, "version.txt", b"advanced module\n")
            git(checked_out, "add", "--", "version.txt")

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
            self.assertEqual((checkout / "source.txt").read_text(), "source addition\n")
            self.assertEqual((checkout / "main.txt").read_text(), "main addition\n")
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

    def approved(self, root, *, independent=True):
        workspace, ctx, remote, checkout, author, task = self.repository(root)
        write(checkout, "source.txt", b"approved source")
        source = self.publish(checkout, TASK_BRANCH)
        if independent:
            write(author, "main.txt", b"external contribution")
            self.publish(author, "main")
        revision = git(checkout, "rev-parse", "HEAD^{tree}")
        claims = {"delivery_mode": "direct-merge", "commit_sha": source, "revision": revision,
                  "test_job_id": "test-job", "approval_digest": "a" * 64}
        task.update(approved_revision=revision, approval={"claims": claims},
                    verification={"test_job_id": "test-job", "revision": revision,
                                  "checkout": {"branch": TASK_BRANCH, "commit_sha": source}})
        ctx.task, ctx.workspace, ctx.save = task, SimpleNamespace(git=workspace), task.update
        return ctx, remote, checkout, author, source

    def land(self, ctx, verify=None):
        from direct_merge import land_approved

        def unexpected(*_):
            self.fail("Unexpected conflict or additional verification")
        return land_approved(ctx, solve=unexpected, verify=verify or unexpected)

    def test_retries_once_when_target_advances_before_push(self):
        from direct_merge import can_abandon_merge
        for persistent in (False, True):
            with self.subTest(persistent=persistent), test_directory() as root:
                ctx, remote, checkout, author, source = self.approved(root)
                verified, external = [], []
                def verify(ctx, candidate):
                    self.assertEqual(git(checkout, "rev-parse", "HEAD"), candidate.commit_sha)
                    verified.append(candidate.commit_sha)
                    if not external or persistent:
                        write(author, "race.txt", str(len(verified)).encode())
                        external.append(self.publish(author, "main"))
                    return {"checked": candidate.commit_sha}
                if persistent:
                    with self.assertRaises(StageError):
                        self.land(ctx, verify)
                    self.assert_remote_refs(remote, external[-1], source)
                    self.assertTrue(can_abandon_merge(ctx.task, terminal_status="failed"))
                    with self.assertRaises(StageError):
                        self.land(ctx, verify)
                else:
                    receipt = self.land(ctx, verify)
                    self.assert_remote_refs(remote, receipt["merge_sha"], source)
                    self.assertEqual(receipt["base_sha"], external[-1])
                    self.assertEqual(receipt["target_sha"], verified[-1])
                    self.assertEqual(receipt["verification"], {"checked": verified[-1]})
                    self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)
                self.assertEqual(len(verified), 2)
                self.assertNotEqual(*verified)

    def test_interrupted_final_checks_resume_verified_pin_without_reverification(self):
        from direct_merge import DirectMergeGit
        with test_directory() as root:
            ctx, remote, _, _, source = self.approved(root)
            base = git(remote, "rev-parse", "main")
            check_source = DirectMergeGit.assert_remote_source

            def interrupt_final_check(merge, ctx, task, source):
                if (task.get("merge_journal") or {}).get("verification"):
                    raise InterruptedError("interrupted during final pretransport checks")
                check_source(merge, ctx, task, source)

            with patch.object(DirectMergeGit, "assert_remote_source", interrupt_final_check):
                with self.assertRaises(InterruptedError):
                    self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            self.assert_remote_refs(remote, base, source)
            journal = ctx.task["merge_journal"]
            self.assertEqual(journal["status"], "verified")
            receipt = self.land(ctx)
            self.assertEqual(receipt["merge_sha"], journal["merge_sha"])
            self.assert_remote_refs(remote, receipt["merge_sha"], source)

    def test_independently_landed_verified_pin_still_checks_source_before_delivery(self):
        with test_directory() as root:
            ctx, remote, checkout, author, source = self.approved(root)
            def verify(ctx, candidate):
                # An independent writer has access to the same candidate. The Worker has not sent a push.
                git(author, "fetch", str(checkout), candidate.commit_sha)
                git(author, "push", "origin", candidate.commit_sha + ":refs/heads/main")
                git(author, "fetch", "origin", TASK_BRANCH)
                git(author, "switch", "-c", TASK_BRANCH, "FETCH_HEAD")
                write(author, "new.txt", b"unapproved source")
                self.publish(author, TASK_BRANCH)
                return {"checked": candidate.commit_sha}

            with self.assertRaises(StageError) as rejected:
                self.land(ctx, verify)
            self.assertEqual(rejected.exception.kind, "push_conflict")
            self.assertEqual(ctx.task["merge_journal"]["status"], "verified")
            self.assertNotEqual(git(remote, "rev-parse", TASK_BRANCH), source)
            self.assertEqual(git(remote, "rev-parse", "main"), ctx.task["merge_journal"]["merge_sha"])

    def test_reconciles_lost_push_ack_after_a_further_main_commit(self):
        with test_directory() as root:
            ctx, remote, checkout, author, source = self.approved(root)
            run = ctx.run
            def lose_ack(argv, *args, **kwargs):
                if "push" in argv:
                    self.assertEqual(ctx.task["merge_journal"]["status"], "in_flight")
                result = run(argv, *args, **kwargs)
                if "push" in argv:
                    self.assertEqual(result.exit_code, 0)
                    raise InterruptedError("worker stopped after receive-pack")
                return result
            ctx.run = lose_ack
            with self.assertRaises(InterruptedError):
                self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            persisted_candidate = ctx.task["merge_journal"]["merge_sha"]
            self.assert_remote_refs(remote, persisted_candidate, source)
            ctx.run = run
            git(author, "pull", "--ff-only", "origin", "main")
            write(author, "later.txt", b"later main commit")
            later = self.publish(author, "main")
            git(author, "switch", "-C", TASK_BRANCH, "origin/main")
            write(author, "next-task.txt", b"source changed after merge")
            moved_source = self.publish(author, TASK_BRANCH)
            receipt = self.land(ctx)
            self.assertEqual(receipt["merge_sha"], persisted_candidate)
            self.assertEqual(receipt["target_sha"], later)
            self.assertEqual(receipt["source_sha"], source)
            self.assertEqual(receipt["status"], "merged")
            self.assertEqual(receipt["verification"], {"checked": persisted_candidate})
            self.assert_remote_refs(remote, later, moved_source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)

    def test_blocks_source_drift_and_rejects_non_fast_forward(self):
        from direct_merge import DirectMergeGit
        with test_directory() as root:
            ctx, remote, _, author, source = self.approved(root)
            merge = DirectMergeGit(ctx.workspace.git)
            base = merge.read_target(ctx, ctx.task)
            candidate = merge.begin(ctx, ctx.task, source, base)
            with self.assertRaises(StageError):
                merge.push(ctx, ctx.task, replace(candidate, commit_sha=source))
            self.assert_remote_refs(remote, base, source)
            git(author, "fetch", "origin", TASK_BRANCH)
            git(author, "switch", "-c", TASK_BRANCH, "FETCH_HEAD")
            write(author, "later.txt", b"unapproved source change")
            moved = self.publish(author, TASK_BRANCH)
            with self.assertRaises(StageError):
                merge.push(ctx, ctx.task, candidate)
            self.assert_remote_refs(remote, base, moved)

    def test_exact_target_lease_rejects_last_moment_advance_and_pushes_one_ref(self):
        from direct_merge import DirectMergeGit
        with test_directory() as root:
            ctx, remote, checkout, author, interim = self.approved(root, independent=False)
            write(checkout, "last.txt", b"second approved change")
            source = self.publish(checkout, TASK_BRANCH)
            ctx.task["approval"]["claims"]["commit_sha"] = source
            ctx.task["approved_revision"] = git(checkout, "rev-parse", "HEAD^{tree}")
            ctx.task["approval"]["claims"]["revision"] = ctx.task["approved_revision"]
            merge = DirectMergeGit(ctx.workspace.git)
            base = merge.read_target(ctx, ctx.task)
            candidate = merge.begin(ctx, ctx.task, source, base)
            run = ctx.run
            advanced = []
            def advance_before_transport(argv, *args, **kwargs):
                if "push" in argv:
                    # The interim commit is an ancestor of the candidate. A plain fast-forward
                    # push would succeed here despite having verified a different base.
                    git(author, "fetch", "origin", TASK_BRANCH)
                    git(author, "merge", "--ff-only", interim)
                    git(author, "push", "origin", "main")
                    advanced.append(interim)
                return run(argv, *args, **kwargs)
            ctx.run = advance_before_transport
            with self.assertRaises(StageError):
                merge.push(ctx, ctx.task, candidate)
            self.assert_remote_refs(remote, advanced[0], source)
            ctx.run = run
            merge.restore(ctx, ctx.task, source)
            candidate = merge.begin(ctx, ctx.task, source, merge.read_target(ctx, ctx.task))
            git(checkout, "tag", "-a", "private-tag", "-m", "not for delivery")
            git(checkout, "config", "push.followTags", "true")
            git(checkout, "config", "remote.origin.mirror", "true")
            merge.push(ctx, ctx.task, candidate)
            self.assertEqual(set(git(remote, "for-each-ref", "--format=%(refname)").splitlines()),
                             {"refs/heads/main", "refs/heads/" + TASK_BRANCH})
            self.assert_remote_refs(remote, candidate.commit_sha, source)

    def test_already_landed_reuses_history(self):
        with test_directory() as root:
            ctx, remote, checkout, author, source = self.approved(root, independent=False)
            git(author, "fetch", "origin", TASK_BRANCH)
            git(author, "merge", "--ff-only", "FETCH_HEAD")
            write(author, "after.txt", b"external addition after source inclusion")
            target = self.publish(author, "main")
            receipt = self.land(ctx)
            self.assertEqual(receipt["merge_sha"], source)
            self.assertEqual(receipt["target_sha"], target)
            self.assertEqual(receipt["status"], "merged")
            self.assert_remote_refs(remote, target, source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)

    def test_same_tree_reuses_approved_test_evidence(self):
        with test_directory() as root:
            ctx, remote, checkout, _, source = self.approved(root, independent=False)
            receipt = self.land(ctx)
            self.assertEqual(receipt["verification"]["test_job_id"], "test-job")
            self.assertEqual(receipt["verification"]["revision"], ctx.task["approved_revision"])
            self.assertEqual(receipt["mode"], "direct-merge")
            self.assertEqual(receipt["task_branch"], TASK_BRANCH)
            self.assertEqual(receipt["target_branch"], "main")
            self.assertEqual(receipt["conflict_paths"], [])
            self.assertEqual(git(checkout, "rev-parse", receipt["merge_sha"] + "^{tree}"),
                             ctx.task["approved_revision"])
            self.assert_remote_refs(remote, receipt["merge_sha"], source)

    def test_failed_push_reuses_verified_candidate_after_restart(self):
        with test_directory() as root:
            ctx, remote, _, _, source = self.approved(root)
            base = git(remote, "rev-parse", "main")
            hook = remote / "hooks/pre-receive"
            hook.write_bytes(b"#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            with self.assertRaises(StageError):
                self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            journal = ctx.task["merge_journal"]
            self.assert_remote_refs(remote, base, source)
            self.assertEqual(journal["status"], "in_flight")
            hook.unlink()
            receipt = self.land(ctx)
            self.assertEqual(receipt["merge_sha"], journal["merge_sha"])
            self.assert_remote_refs(remote, journal["merge_sha"], source)

    def test_deleted_rewritten_or_unreachable_target_preserves_candidate(self):
        for fault in ("deleted", "rewritten", "unreachable"):
            with self.subTest(fault=fault), test_directory() as root:
                ctx, remote, checkout, _, source = self.approved(root)
                def verify(ctx, candidate):
                    if fault == "deleted":
                        git(remote, "update-ref", "-d", "refs/heads/main")
                    elif fault == "rewritten":
                        git(remote, "update-ref", "refs/heads/main", ctx.task["base_commit"])
                    else:
                        remote.rename(root / "offline")
                    return {"checked": candidate.commit_sha}
                with self.assertRaises(StageError):
                    self.land(ctx, verify)
                journal = ctx.task["merge_journal"]
                self.assertEqual(git(checkout, "rev-parse", "HEAD"), journal["merge_sha"])
                actual = root / "offline" if fault == "unreachable" else remote
                self.assertEqual(git(actual, "rev-parse", TASK_BRANCH), source)
                targets = git(actual, "for-each-ref", "--format=%(objectname)", "refs/heads/main")
                self.assertEqual(targets, {"deleted": "", "rewritten": ctx.task["base_commit"],
                                           "unreachable": journal["base_sha"]}[fault])

    def test_pre_push_restart_restores_only_clean_worker_checkout(self):
        with test_directory() as root:
            ctx, remote, checkout, _, source = self.approved(root)
            base = git(remote, "rev-parse", "main")
            def interrupt_verify(*_):
                raise InterruptedError("worker stopped before verification")
            with self.assertRaises(InterruptedError):
                self.land(ctx, interrupt_verify)
            self.assertEqual(ctx.task["merge_journal"]["status"], "building")
            head = git(checkout, "rev-parse", "HEAD")
            marker = checkout / ".git/MERGE_HEAD"
            marker.write_text(source + "\n")
            with self.assertRaises(StageError):
                self.land(ctx)
            self.assertTrue(marker.is_file())
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), head)
            self.assert_remote_refs(remote, base, source)
            marker.unlink()
            write(checkout, "local.txt", b"keep pending work")
            with self.assertRaises(StageError):
                self.land(ctx)
            self.assertEqual((checkout / "local.txt").read_bytes(), b"keep pending work")
            (checkout / "local.txt").unlink()
            pin = "refs/unity-worker/direct-merge/" + TASK_BRANCH.split("/")[1]
            unknown = ctx.task["base_commit"]
            git(checkout, "switch", "--detach", unknown)
            git(checkout, "update-ref", pin, unknown, head)
            with self.assertRaises(StageError):
                self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), unknown)
            self.assert_remote_refs(remote, base, source)
            git(checkout, "switch", "--detach", head)
            git(checkout, "update-ref", pin, head, unknown)
            receipt = self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            self.assert_remote_refs(remote, receipt["merge_sha"], source)

    def test_target_rewind_between_advertisement_and_fetch_blocks_delivery(self):
        with test_directory() as root:
            ctx, remote, checkout, _, source = self.approved(root)
            run = ctx.run
            # Expose both objects locally, as on a resident slot that fetched last week.
            git(checkout, "fetch", "origin", "main")
            original = ctx.task["base_commit"]
            def rewind_before_fetch(argv, *args, **kwargs):
                if "fetch" in argv:
                    ctx.run = run
                    git(remote, "update-ref", "refs/heads/main", original)
                return run(argv, *args, **kwargs)
            ctx.run = rewind_before_fetch
            with self.assertRaises(StageError):
                self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            self.assert_remote_refs(remote, original, source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)

    def test_restart_during_rebuild_keeps_the_single_retry_budget(self):
        with test_directory() as root:
            ctx, remote, checkout, author, source = self.approved(root)
            def verify(ctx, candidate):
                write(author, "race.txt", b"first target advance")
                self.publish(author, "main")
                return {"checked": candidate.commit_sha}
            def interrupt_rebuild(**changes):
                row = changes["merge_journal"]
                if row["status"] == "building" and row["rebuilds"] == 1:
                    raise InterruptedError("stop while restarting on the new base")
                ctx.task.update(changes)
            ctx.save = interrupt_rebuild
            with self.assertRaises(InterruptedError):
                self.land(ctx, verify)
            write(author, "race.txt", b"second target advance while stopped")
            second = self.publish(author, "main")
            ctx.save = ctx.task.update
            with self.assertRaises(StageError):
                self.land(ctx, lambda _, c: {"checked": c.commit_sha})
            self.assert_remote_refs(remote, second, source)
            self.assertEqual(git(checkout, "branch", "--show-current"), TASK_BRANCH)

    @unittest.skipUnless(subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0, "Git LFS")
    def test_target_lfs_pre_push_uses_the_candidate_and_destination_ref(self):
        from direct_merge import DirectMergeGit
        with test_directory() as root:
            ctx, remote, checkout, _, _ = self.approved(root)
            git(checkout, "lfs", "install", "--local", "--skip-repo")
            git(checkout, "lfs", "track", "payload.dat")
            write(checkout, "payload.dat", b"compiled dependency\0" * 40)
            source = self.publish(checkout, TASK_BRANCH)
            task = ctx.task
            revision = git(checkout, "rev-parse", "HEAD^{tree}")
            task.update(approved_revision=revision)
            task["approval"]["claims"].update(revision=revision, commit_sha=source)
            merge = DirectMergeGit(ctx.workspace.git)
            base = merge.read_target(ctx, task)
            candidate = merge.begin(ctx, task, source, base)
            run = ctx.run
            supplied = []
            def transport(argv, *args, **kwargs):
                if "pre-push" in argv:
                    supplied.append(kwargs["data"].decode().split())
                    raise StageError("git_failed", "target-scoped LFS write denied by fixture")
                return run(argv, *args, **kwargs)
            ctx.run = transport
            with self.assertRaises(StageError):
                merge.push(ctx, task, candidate)
            self.assert_remote_refs(remote, base, source)
            self.assertEqual(supplied, [["refs/heads/main", candidate.commit_sha, "refs/heads/main", base]])
            ctx.run = run
            merge.push(ctx, task, candidate)
            self.assert_remote_refs(remote, candidate.commit_sha, source)

    def approved_conflict(self, root):
        workspace, ctx, remote, checkout, author, task = self.repository(root, path=SCRIPT)
        write(checkout, SCRIPT, SOURCE)
        source = self.publish(checkout, TASK_BRANCH)
        write(author, SCRIPT, TARGET)
        base = self.publish(author, "main")
        revision = git(checkout, "rev-parse", "HEAD^{tree}")
        task.update(approved_revision=revision, approval={"claims": {"delivery_mode": "direct-merge",
                    "commit_sha": source, "revision": revision, "test_job_id": "test-job"}},
                    verification={"test_job_id": "test-job", "revision": revision,
                                  "checkout": {"branch": TASK_BRANCH, "commit_sha": source}})
        ctx.task, ctx.workspace, ctx.save = task, SimpleNamespace(git=workspace), task.update
        return ctx, remote, checkout, source, base, revision

    def test_conflict_equal_to_source_still_verifies_and_resumes_the_same_solution(self):
        from direct_merge import land_approved
        with test_directory() as root:
            ctx, remote, checkout, source, base, revision = self.approved_conflict(root)
            def solve(_, candidate):
                self.assertEqual(candidate.conflict_paths, (SCRIPT,))
                write(checkout, SCRIPT, SOURCE)
            candidates = []
            def verify(_, candidate):
                candidates.append(candidate.commit_sha)
                self.assertEqual(git(checkout, "rev-parse", "HEAD^{tree}"), revision)
                if len(candidates) == 1:
                    raise InterruptedError("worker stopped during candidate verification")
                return {"candidate_check": candidate.commit_sha}
            with self.assertRaises(InterruptedError):
                land_approved(ctx, solve=solve, verify=verify)
            self.assert_remote_refs(remote, base, source)
            receipt = self.land(ctx, verify)
            self.assertEqual(candidates, [receipt["merge_sha"], receipt["merge_sha"]])
            self.assertEqual(receipt["conflict_paths"], [SCRIPT])
            self.assertEqual(receipt["verification"], {"candidate_check": receipt["merge_sha"]})
            self.assert_remote_refs(remote, receipt["merge_sha"], source)

    def test_recovers_solver_commit_if_candidate_journal_save_was_interrupted(self):
        from direct_merge import land_approved
        for fault in ("intact", "replaced", "restored"):
            with self.subTest(fault=fault), test_directory() as root:
                ctx, remote, checkout, source, base, _ = self.approved_conflict(root)
                solved, finished = [], []
                def solve(_, candidate):
                    solved.append(candidate.conflict_paths)
                    write(checkout, SCRIPT, SOURCE)
                def interrupt_save(**changes):
                    row = changes["merge_journal"]
                    if row.get("merge_sha"):
                        finished.append(row["merge_sha"])
                        raise InterruptedError("solver finished, candidate journal was not saved")
                    ctx.task.update(changes)
                ctx.save = interrupt_save
                with self.assertRaises(InterruptedError):
                    land_approved(ctx, solve=solve, verify=None)
                first, = finished
                self.assertEqual(git(checkout, "rev-parse", "HEAD"), first)
                self.assertEqual(ctx.task["merge_journal"]["conflict_paths"], [SCRIPT])
                self.assertNotIn("merge_sha", ctx.task["merge_journal"])
                self.assert_remote_refs(remote, base, source)
                ctx.save = ctx.task.update
                pin = "refs/unity-worker/direct-merge/" + TASK_BRANCH.split("/")[1]
                if fault == "intact":
                    receipt = land_approved(ctx, solve=solve, verify=lambda _, c: {"checked": c.commit_sha})
                    self.assertEqual(receipt["merge_sha"], first)
                    self.assertEqual(receipt["verification"], {"checked": first})
                    self.assert_remote_refs(remote, first, source)
                else:
                    if fault == "replaced":
                        # Preserve the exact parents, tree and a clean detached checkout,
                        # but replace the private ref with an unrelated commit.
                        head = git(checkout, "commit-tree", first + "^{tree}", "-p", base, "-p", source,
                                   "-m", "Independent resolution")
                        git(checkout, "switch", "--detach", head)
                        label = git(checkout, "reflog", "-1", "--format=%gs", pin)
                        git(checkout, "update-ref", "-m", label, pin, head, first)
                    else:
                        head = source
                        git(checkout, "switch", TASK_BRANCH)
                        git(checkout, "update-ref", "-d", pin, first)
                    with self.assertRaises(StageError) as failure:
                        land_approved(ctx, solve=solve, verify=lambda _, c: {"checked": c.commit_sha})
                    self.assertEqual(failure.exception.result["conflict_paths"], [SCRIPT])
                    self.assertEqual(git(checkout, "rev-parse", "HEAD"), head)
                    self.assert_remote_refs(remote, base, source)
                self.assertEqual(solved, [(SCRIPT,)])
