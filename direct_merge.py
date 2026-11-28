"""Build and verify a disposable merge candidate in the resident Unity checkout."""

import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from hashlib import file_digest, sha256
from pathlib import PurePosixPath
from uuid import uuid4

from git_workspace import SHA, GitWorkspace
from process_runner import StageError


CONFLICT_MARKERS = re.compile(
    rb"(?:\A(?:\xef\xbb\xbf)?|[\r\n]|\xc2\x85|\xe2\x80[\xa8\xa9])(?:<{7,}|={7,}|>{7,}|\|{7,})"
)
LFS_POINTER = re.compile(rb"^(?:\xef\xbb\xbf)?version https?://(?:git-lfs.github.com/spec/v1|hawser.github.com/spec/v1)(?:\r?\n|$)")
MAX_CONFLICT_BYTES = 8_000_000


@dataclass(frozen=True)
class IndexEntry:
    path: str
    mode: str
    blob: str
    stage: int


@dataclass(frozen=True)
class MergeCandidate:
    source_sha: str
    base_sha: str
    conflict_paths: tuple[str, ...] = ()
    commit_sha: str | None = None
    tree_sha: str | None = None
    index_entries: tuple[IndexEntry, ...] = field(default=(), repr=False, compare=False)
    outside_fingerprints: tuple[tuple[str, bytes], ...] = field(default=(), repr=False, compare=False)
    task_branch: str | None = field(default=None, repr=False, compare=False)
    remote_identity: str | None = field(default=None, repr=False, compare=False)
    resolution_id: str | None = field(default=None, repr=False, compare=False)

    def _resolution_reflog(self):
        record = [self.resolution_id, self.base_sha, self.source_sha, self.commit_sha, self.tree_sha, self.conflict_paths]
        return "unity-worker resolved " + sha256(json.dumps(record).encode("ascii")).hexdigest()


class DirectMergeGit:
    def __init__(self, workspace: GitWorkspace):
        self.workspace = workspace

    def _push_url(self, ctx, task):
        self.workspace.assert_task_push_target(ctx, task)
        identity, url = self.workspace.remote_binding(ctx)
        if identity != task["remote_identity"]:
            raise StageError("workspace_changed", "Configured Git remote changed after preparation")
        return url

    def read_target(self, ctx, task: dict) -> str:
        workspace = self.workspace
        workspace.validate_checkout_identity(ctx)
        url = self._push_url(ctx, task)
        ref = "refs/heads/" + workspace.cfg.base_branch
        workspace.text(ctx, "check-ref-format", ref)
        line = workspace.text(ctx, "ls-remote", "--refs", "--", url, ref)
        if not line:
            raise StageError("merge_target_missing", "The configured target branch no longer exists")
        commit, _, advertised = line.partition("\t")
        if advertised != ref or not SHA.fullmatch(commit):
            raise StageError("git_failed", "Push target returned an unexpected mainline reference")
        # The target may advance between the advertisement and the fetch.
        workspace.git(ctx, "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "--", url, ref)
        fetched = workspace.text(ctx, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if fetched != commit and not self.contains(ctx, commit, fetched):
            raise StageError("merge_target_rewritten", "The advertised target was rewritten during fetch")
        return fetched

    def contains(self, ctx, ancestor: str, descendant: str) -> bool:
        self._require_sha(ancestor)
        self._require_sha(descendant)
        code, _ = self.workspace.git(ctx, "merge-base", "--is-ancestor", ancestor, descendant, allowed=(0, 1))
        return code == 0

    def begin(self, ctx, task: dict, source_sha: str, base_sha: str) -> MergeCandidate:
        workspace = self.workspace
        self._require_sha(source_sha)
        self._require_sha(base_sha)
        url = self._push_url(ctx, task)
        workspace.checkout_is(ctx, task, source_sha)
        self._assert_no_merge_markers()
        self._assert_checkout_contents(ctx, source_sha)
        self.assert_remote_source(ctx, task, source_sha)
        if self.contains(ctx, source_sha, base_sha):
            raise StageError("workspace_changed", "The approved commit is already in the target history")
        if task.get("approved_revision") and workspace.text(ctx, "rev-parse", "HEAD^{tree}") != task["approved_revision"]:
            raise StageError("workspace_changed", "The source tree differs from approval")

        workspace.validate_unity_revision(ctx, base_sha)
        lfs = workspace.validate_lfs_filters(ctx, base_sha)
        pin = self._pin_ref(task)
        workspace.git(ctx, "update-ref", pin, base_sha, "0" * len(base_sha))
        workspace.git(ctx, "switch", "--detach", "--no-overwrite-ignore", "--no-recurse-submodules", base_sha)
        workspace.hydrate_checkout(ctx, lfs=lfs, lfs_remote=workspace.cfg.git_remote, lfs_url=url)
        self._assert_detached(ctx, base_sha)
        self._assert_checkout_contents(ctx, base_sha)
        code, _ = workspace.git(
            ctx, "-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false", "-c", "merge.conflictStyle=merge",
            "merge", "--no-commit", "--no-ff", "--no-edit", "--no-autostash", "-s", "ort", "-Xfind-renames",
            source_sha, allowed=(0, 1),
        )
        candidate = MergeCandidate(source_sha, base_sha)
        self._assert_merge_state(ctx, candidate)
        if code:
            entries = self._index_entries(ctx)
            paths = tuple(sorted({entry.path for entry in entries if entry.stage}))
            if not paths:
                raise StageError("merge_conflict", "Git did not produce a resolvable content conflict")
            candidate = replace(candidate, conflict_paths=paths, index_entries=entries,
                                task_branch=task["task_branch"], remote_identity=task["remote_identity"])
            self._classify_conflicts(ctx, candidate)
            workspace.assert_submodules_clean(ctx)
            workspace.hydrate_submodules(ctx)
            self._assert_working_scope(ctx, conflicted=paths)
            return replace(candidate, outside_fingerprints=self._worktree_fingerprints(ctx, entries))
        return self._finish(ctx, task, candidate, url)

    def resolve(self, ctx, candidate: MergeCandidate) -> MergeCandidate:
        workspace = self.workspace
        task = self._task_for_candidate(ctx, candidate)
        url = self._push_url(ctx, task)
        self._assert_merge_state(ctx, candidate)
        workspace.assert_submodules_clean(ctx)
        entries = candidate.index_entries
        paths = candidate.conflict_paths
        if not paths or self._index_entries(ctx) != entries:
            raise StageError("merge_resolution_scope", "The merge index changed outside Worker staging")
        self._assert_working_scope(ctx, conflicted=paths)
        actual_fingerprints = dict(self._worktree_fingerprints(ctx, entries))
        raw_edits = [path for path, original in candidate.outside_fingerprints
                     if actual_fingerprints.pop(path, None) != original]
        if raw_edits or actual_fingerprints:
            raise StageError("merge_resolution_scope", "Tracked files outside the conflict paths changed on disk",
                             result={"dirty_paths": (raw_edits + list(actual_fingerprints))[:30]})
        for path in paths:
            workspace.check_path(path)
            local = workspace.root / path
            try:
                info = local.lstat()
            except FileNotFoundError:
                raise self._resolution_error(path) from None
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFLICT_BYTES:
                raise self._resolution_error(path)
            with local.open("rb") as stream:
                self._check_resolution_text(path, stream.read(MAX_CONFLICT_BYTES + 1))
            workspace.check_secrets(path)
        for path in paths:
            workspace.stage_paths(ctx, [path])

        actual = self._index_entries(ctx)
        original_outside = tuple(entry for entry in entries if not entry.stage)
        staged_outside = tuple(entry for entry in actual if entry.path not in paths)
        if staged_outside != original_outside or any(entry.stage for entry in actual):
            raise StageError("merge_resolution_scope", "Staging changed the automatic merge outside the conflict paths")
        expected_modes = {entry.path: entry.mode for entry in entries if entry.stage == 1}
        resolved = {entry.path: entry for entry in actual if entry.path in paths}
        if {path: entry.mode for path, entry in resolved.items()} != expected_modes:
            raise StageError("merge_resolution_scope", "A resolution removed a conflict file or changed its Git mode")
        for path, entry in resolved.items():
            self._check_resolution_text(path, self._blob(ctx, entry.blob))
        return self._finish(ctx, task, candidate, url)

    def assert_clean_candidate(self, ctx, candidate: MergeCandidate) -> None:
        if not candidate.commit_sha or not candidate.tree_sha:
            raise StageError("workspace_changed", "The merge candidate has no complete commit and tree")
        self._require_sha(candidate.commit_sha)
        self._require_sha(candidate.tree_sha)
        self.workspace.validate(ctx)
        self._assert_no_merge_markers()
        self._assert_detached(ctx, candidate.commit_sha)
        self._assert_candidate_identity(ctx, candidate)
        self._assert_checkout_contents(ctx, candidate.commit_sha)

    def push(self, ctx, task: dict, candidate: MergeCandidate) -> None:
        workspace = self.workspace
        claims = (task.get("approval") or {}).get("claims") or {}
        if (claims.get("delivery_mode") != "direct-merge" or claims.get("commit_sha") != candidate.source_sha
                or claims.get("revision") != task.get("approved_revision")):
            raise StageError("workspace_changed", "The candidate differs from the direct merge approval")
        self.assert_clean_candidate(ctx, candidate)
        if (self.pin(ctx, task) != candidate.commit_sha
                or workspace.text(ctx, "rev-parse", "--verify", "refs/heads/" + task["task_branch"]) != candidate.source_sha
                or not self.contains(ctx, candidate.base_sha, candidate.commit_sha)
                or not self.contains(ctx, candidate.source_sha, candidate.commit_sha)):
            raise StageError("workspace_changed", "The pinned candidate does not preserve the target and approved task")
        url = self._push_url(ctx, task)
        ref = "refs/heads/" + workspace.cfg.base_branch
        if workspace.validate_lfs_filters(ctx, candidate.commit_sha):
            data = f"{ref} {candidate.commit_sha} {ref} {candidate.base_sha}\n".encode("utf-8")
            workspace.git(ctx, "lfs", "pre-push", workspace.cfg.git_remote, url, data=data)
        target = self.read_target(ctx, task)
        if target != candidate.base_sha:
            raise StageError("merge_target_moved", "The target changed after verification", result={"target_sha": target})
        self.assert_remote_source(ctx, task, candidate.source_sha)
        try:
            # A literal endpoint avoids configured mirror refspecs; the exact lease also
            # rejects an intervening update that could itself fast-forward to the candidate.
            workspace.git(ctx, "push", "--porcelain", f"--force-with-lease={ref}:{candidate.base_sha}",
                          "--no-follow-tags", "--recurse-submodules=no", "--", url, f"{candidate.commit_sha}:{ref}")
        except StageError as error:
            if error.kind not in {"git_failed", "timeout"}:
                raise
            raise StageError("push_failed", str(error), error.exit_code, error.result) from error

    def assert_remote_source(self, ctx, task, source):
        if self.workspace.remote_task_tip(ctx, task) != source:
            raise StageError("push_conflict", "Remote task branch no longer points to the approved commit")

    def pin(self, ctx, task):
        return self.workspace.text(ctx, "rev-parse", "--verify", "--quiet", self._pin_ref(task), allowed=(0, 1))

    def restore(self, ctx, task: dict, source_sha: str) -> None:
        workspace = self.workspace
        self._require_sha(source_sha)
        workspace.validate(ctx)
        self._assert_no_merge_markers()
        url = self._push_url(ctx, task)
        pin = self._pin_ref(task)
        owned = workspace.text(ctx, "rev-parse", "--verify", "--quiet", pin, allowed=(0, 1))
        restore_ref = "refs/unity-worker/direct-merge-restore/" + pin.rsplit("/", 1)[1]
        record_sha = workspace.text(ctx, "rev-parse", "--verify", "--quiet", restore_ref, allowed=(0, 1))
        branch = self._current_branch(ctx)
        task_ref = "refs/heads/" + task["task_branch"]
        source_ref = workspace.text(ctx, "rev-parse", "--verify", task_ref)
        head = workspace.text(ctx, "rev-parse", "HEAD")
        if source_ref != source_sha or (branch and (branch != task_ref or head != source_sha)):
            raise StageError("workspace_changed", "The original task branch changed outside the Worker")
        if not branch:
            if head != owned:
                raise StageError("workspace_changed", "The detached checkout does not match the Worker merge operation")
            workspace.assert_clean(ctx)
            workspace.validate_unity_revision(ctx, source_sha)
            workspace.validate_lfs_filters(ctx, source_sha)
            record_sha = self._record_restore_checkout(ctx, restore_ref, record_sha, source_sha, owned)
        reflog = f"unity-worker restore {owned} {task_ref} {record_sha}"
        if not branch:
            workspace.git(ctx, "-c", "core.logAllRefUpdates=true", "switch", "--no-overwrite-ignore",
                          "--no-recurse-submodules", task["task_branch"], reflog_action=reflog)

        workspace.checkout_is(ctx, task, source_sha)
        if (owned and record_sha
                and workspace.text(ctx, "reflog", "-1", "--format=%H%x09%gs", "HEAD") == f"{source_sha}\t{reflog}"):
            record = json.loads(workspace.git(ctx, "cat-file", "blob", record_sha)[1])
            if record["source"] != source_sha or record["candidate"] != owned:
                raise StageError("dirty_workspace", "The restore checkout record does not match the task and candidate")
            self._assert_restore_scope(ctx, owned, record["modules"])
            workspace.validate_unity_revision(ctx, source_sha)
            lfs = workspace.validate_lfs_filters(ctx, source_sha)
            workspace.hydrate_checkout(ctx, lfs=lfs, lfs_remote=workspace.cfg.git_remote, lfs_url=url)
        self._assert_checkout_contents(ctx, source_sha)
        if owned:
            commands = f"delete {pin} {owned}\n"
            if record_sha:
                commands += f"delete {restore_ref} {record_sha}\n"
            workspace.git(ctx, "update-ref", "--stdin", data=commands.encode("ascii"))

    def _record_restore_checkout(self, ctx, ref, recorded, source, owned):
        workspace = self.workspace
        record = json.dumps({"source": source, "candidate": owned, "modules": self._module_fingerprints(ctx)},
                            sort_keys=True).encode("ascii")
        record_sha = workspace.text(ctx, "hash-object", "-w", "--stdin", data=record)
        if recorded and recorded != record_sha:
            raise StageError("dirty_workspace", "The pre-restore checkout differs from its recorded state")
        if not recorded:
            workspace.git(ctx, "update-ref", ref, record_sha, "0" * len(record_sha))
        return record_sha

    def _assert_restore_scope(self, ctx, owned, expected_modules):
        workspace = self.workspace
        rows = workspace.status(ctx)
        workspace.assert_submodules_clean(ctx, ignore_submodule_heads=True)
        moving = self._moving_submodules(ctx, owned) if (workspace.root / ".gitmodules").is_file() else {}
        moving_roots = set(moving.values())
        dirty = [path for state, path in rows if state != " M" or path not in moving_roots]
        if dirty:
            raise StageError("dirty_workspace", "The restored task branch has local changes",
                             result={"dirty_paths": dirty[:30]})
        if moving:
            actual_modules = self._module_fingerprints(ctx, moving=moving)
            relevant_modules = {name: digest for name, digest in expected_modules.items()
                                if any(PurePosixPath(name).is_relative_to(parent) for parent in moving)}
            if not set(map(str, moving)).issubset(actual_modules) or actual_modules != relevant_modules:
                raise StageError("dirty_workspace", "A moving submodule differs from its recorded worktree bytes",
                                 result={"dirty_paths": sorted(str(path) for path in moving)[:30]})

    def _moving_submodules(self, ctx, owned):
        workspace = self.workspace
        _, output = workspace.git(
            ctx, "submodule", "foreach", "--quiet", "--recursive",
            'actual=$(git rev-parse HEAD) && printf "%s\\000%s\\000%s\\000" "$displaypath" "$sha1" "$actual"',
        )
        paths = output.decode("utf-8").split("\0")
        checkout_links = {PurePosixPath(): self._gitlinks(ctx, workspace.root, owned)}
        roots = {}
        moving = {}
        for raw_path, expected, actual in zip(paths[0:-1:3], paths[1::3], paths[2::3]):
            path = PurePosixPath(raw_path)
            parent = next(parent for parent in path.parents if parent in checkout_links)
            previous = checkout_links[parent].get(path.relative_to(parent).as_posix())
            roots[path] = roots.get(parent, raw_path)
            if actual != expected:
                if actual != previous:
                    raise StageError("dirty_workspace", "A submodule checkout changed outside the merge operation",
                                     result={"dirty_paths": [raw_path]})
                moving[path] = roots[path]
            checkout_links[path] = self._gitlinks(ctx, workspace.root / raw_path, previous) if previous else {}
        return moving

    def _gitlinks(self, ctx, checkout, revision):
        _, output = self.workspace.git(ctx, "-C", str(checkout), "ls-tree", "-r", "-z", revision, limit=None)
        links = {}
        for raw in output.split(b"\0"):
            if raw.startswith(b"160000 commit "):
                entry, path = raw.decode("utf-8").split("\t", 1)
                links[path] = entry.rsplit(" ", 1)[1]
        return links

    @staticmethod
    def _require_sha(sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise StageError("workspace_changed", "The merge requires a complete Git object ID")

    @staticmethod
    def _pin_ref(task):
        branch = task["task_branch"]
        if not re.fullmatch(r"ai/task-[0-9a-f]{32}", branch):
            raise StageError("unsafe_change", "The merge requires a generated task branch")
        return "refs/unity-worker/direct-merge/" + branch.rsplit("/", 1)[1]

    def _current_branch(self, ctx):
        return self.workspace.text(ctx, "symbolic-ref", "--quiet", "HEAD", allowed=(0, 1))

    def _assert_detached(self, ctx, sha):
        if self._current_branch(ctx) or self.workspace.text(ctx, "rev-parse", "HEAD") != sha:
            raise StageError("workspace_changed", "The detached merge checkout changed outside the Worker")

    def _assert_no_merge_markers(self, *, in_progress=False):
        names = ("MERGE_AUTOSTASH",) if in_progress else ("MERGE_AUTOSTASH", "MERGE_MODE", "MERGE_MSG", "AUTO_MERGE")
        for name in names:
            marker = self.workspace.root / ".git" / name
            if marker.is_symlink() or marker.exists():
                raise StageError("dirty_workspace", f"Unrecognized Git operation marker ({name}). Inspect it manually.")

    def _assert_merge_state(self, ctx, candidate):
        workspace = self.workspace
        self._assert_detached(ctx, candidate.base_sha)
        workspace.validate(ctx, allow_merge=True)
        self._assert_no_merge_markers(in_progress=True)
        marker = workspace.root / ".git/MERGE_HEAD"
        if (marker.is_symlink() or not marker.is_file()
                or marker.read_bytes().splitlines() != [candidate.source_sha.encode("ascii")]):
            raise StageError("workspace_changed", "The in-progress Git merge changed outside the Worker")

    def _index_entries(self, ctx):
        output = self.workspace.git(ctx, "ls-files", "--stage", "-z", limit=None)[1]
        return tuple(self._parse_index_entries(output))

    @staticmethod
    def _parse_index_entries(output):
        for row in output.decode("utf-8").split("\0"):
            if row:
                header, path = row.split("\t", 1)
                mode, blob, stage = header.split()
                yield IndexEntry(path, mode, blob, int(stage))

    def _submodule_indexes(self, ctx):
        workspace = self.workspace
        if not (workspace.root / ".gitmodules").is_file():
            return
        _, output = workspace.git(
            ctx, "submodule", "foreach", "--quiet", "--recursive",
            'printf "%s\\000" "$displaypath" && git ls-files --stage -z && printf "\\000"',
            limit=None,
        )
        for module in output.split(b"\0\0"):
            if module:
                directory, _, index = module.partition(b"\0")
                yield directory.decode("utf-8"), self._parse_index_entries(index)

    def _module_fingerprints(self, ctx, *, moving=None):
        hashes = {}
        for directory, entries in self._submodule_indexes(ctx):
            path = PurePosixPath(directory)
            if moving is not None and not any(path.is_relative_to(parent) for parent in moving):
                continue
            digest = sha256()
            for name, fingerprint in self._tracked_fingerprints(entries, prefix=directory + "/"):
                digest.update(name.encode("utf-8") + b"\0" + fingerprint)
            hashes[directory] = digest.hexdigest()
        return hashes

    def _worktree_fingerprints(self, ctx, entries):
        fingerprints = list(self._tracked_fingerprints(entries))
        for directory, index in self._submodule_indexes(ctx):
            fingerprints.extend(self._tracked_fingerprints(index, prefix=directory + "/"))
        return tuple(fingerprints)

    def _tracked_fingerprints(self, entries, *, prefix=""):
        workspace = self.workspace
        paths = (prefix + entry.path for entry in entries if entry.stage == 0 and entry.mode != "160000")
        checked_directories = set()
        for path in paths:
            local = workspace.root / path
            try:
                if local.parent not in checked_directories:
                    if local.parent.resolve(strict=True) != local.parent:
                        raise OSError("linked directory")
                    checked_directories.add(local.parent)
                mode = local.lstat().st_mode
                if stat.S_ISLNK(mode):
                    digest = b"L" + sha256(os.fsencode(os.readlink(local))).digest()
                elif stat.S_ISREG(mode):
                    with local.open("rb") as stream:
                        digest = b"F" + file_digest(stream, "sha256").digest()
                else:
                    raise OSError("unsupported tracked file type")
            except (OSError, RuntimeError) as error:
                raise StageError("merge_resolution_scope", "Cannot verify a tracked file outside the conflict paths",
                                 result={"dirty_paths": [path]}) from error
            yield path, digest

    def _blob(self, ctx, sha):
        return self.workspace.git(ctx, "cat-file", "blob", sha, limit=MAX_CONFLICT_BYTES)[1]

    @staticmethod
    def _is_text(blob):
        if re.search(rb"[\x00-\x08\x0b\x0e-\x1f\x7f]", blob) or LFS_POINTER.match(blob):
            return False
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def _classify_conflicts(self, ctx, candidate):
        workspace = self.workspace
        paths = candidate.conflict_paths
        revisions = workspace.text(ctx, "merge-base", "--all", candidate.base_sha, candidate.source_sha).splitlines()
        if len(revisions) != 1:
            raise self._conflict_error(paths)
        ancestor = revisions[0]
        for revision in (candidate.base_sha, candidate.source_sha):
            _, output = workspace.git(ctx, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                                      "--find-renames", "--diff-filter=R", ancestor, revision)
            fields = output.decode("utf-8").split("\0")
            renamed = set(fields[1::3]) | set(fields[2::3])
            if renamed.intersection(paths):
                raise self._conflict_error(paths)
        stages = {}
        for entry in candidate.index_entries:
            if entry.stage:
                stages.setdefault(entry.path, {})[entry.stage] = entry
        for path in paths:
            relative = PurePosixPath(path)
            if workspace.unity_prefix not in relative.parents or relative.suffix.lower() != ".cs":
                raise self._conflict_error(paths)
            workspace.check_path(path)
            indexed = stages[path]
            if (set(indexed) != {1, 2, 3} or indexed[1].mode not in {"100644", "100755"}
                    or len({entry.mode for entry in indexed.values()}) != 1
                    or len({entry.blob for entry in indexed.values()}) != 3):
                raise self._conflict_error(paths)

            attrs = workspace.git(ctx, "check-attr", "-z", "diff", "merge", "filter", "conflict-marker-size",
                                  "--", path)[1].decode("utf-8").split("\0")
            settings = dict(zip(attrs[1::3], attrs[2::3]))
            if (settings.get("diff") == "unset" or settings.get("merge") not in {"set", "text", "unspecified"}
                    or settings.get("filter") == "lfs" or settings.get("conflict-marker-size") not in {"7", "unspecified"}):
                raise self._conflict_error(paths)

            for stage, revision in enumerate((ancestor, candidate.base_sha, candidate.source_sha), 1):
                entry = indexed[stage]
                tree = workspace.git(ctx, "--literal-pathspecs", "ls-tree", "-z", revision, "--", path)[1]
                expected = f"{entry.mode} blob {entry.blob}\t{path}\0".encode("utf-8")
                if tree != expected or not self._is_text(self._blob(ctx, entry.blob)):
                    raise self._conflict_error(paths)

    @staticmethod
    def _conflict_error(paths):
        return StageError("merge_conflict", "The merge contains conflicts requiring manual resolution",
                          result={"conflict_paths": list(paths)})

    @staticmethod
    def _resolution_error(path):
        return StageError("merge_resolution_scope", f"The resolution is not clean C# text: {path}",
                          result={"conflict_paths": [path]})

    def _check_resolution_text(self, path, blob):
        if len(blob) > MAX_CONFLICT_BYTES or not self._is_text(blob) or CONFLICT_MARKERS.search(blob):
            raise self._resolution_error(path)

    def _assert_working_scope(self, ctx, *, conflicted=()):
        unexpected = []
        for state, path in self.workspace.status(ctx):
            if state == "UU" and path in conflicted:
                continue
            if state[0] not in " MADT" or state[1] != " ":
                unexpected.append(path)
        if unexpected:
            raise StageError("merge_resolution_scope", "The merge checkout has edits outside the expected index",
                             result={"dirty_paths": unexpected[:30]})

    def _task_for_candidate(self, ctx, candidate):
        # The transient conflict candidate belongs to the task that set the base pin.
        if not candidate.task_branch or not candidate.remote_identity:
            raise StageError("workspace_changed", "The in-progress candidate requires its Worker task")
        task = {"task_branch": candidate.task_branch, "remote_identity": candidate.remote_identity}
        if self.workspace.text(ctx, "rev-parse", "--verify", self._pin_ref(task)) != candidate.base_sha:
            raise StageError("workspace_changed", "The in-progress merge does not match the Worker operation")
        return task

    def _finish(self, ctx, task, candidate, url):
        workspace = self.workspace
        self._assert_merge_state(ctx, candidate)
        tree = workspace.text(ctx, "write-tree")
        workspace.validate_unity_revision(ctx, tree)
        lfs = workspace.validate_lfs_filters(ctx, tree)
        workspace.assert_submodules_clean(ctx)
        workspace.hydrate_submodules(ctx)
        self._assert_working_scope(ctx)
        if workspace.text(ctx, "write-tree") != tree:
            raise StageError("workspace_changed", "The automatic merge tree changed during candidate preparation")
        signing = workspace.text(ctx, "config", "--bool", "--get", "commit.gpgSign", allowed=(0, 1))
        options = ("-S",) if signing == "true" else ()
        sha = workspace.text(ctx, "commit-tree", tree, "-p", candidate.base_sha, "-p", candidate.source_sha,
                             *options, "-F", "-", data=b"Merge approved Unity task\n")
        candidate = replace(candidate, commit_sha=sha, tree_sha=tree, index_entries=(), outside_fingerprints=(),
                            task_branch=None, remote_identity=None)
        self._assert_candidate_identity(ctx, candidate)
        record = ("--create-reflog", "-m", candidate._resolution_reflog()) if candidate.resolution_id else ()
        workspace.git(ctx, "update-ref", *record, self._pin_ref(task), sha, candidate.base_sha)
        workspace.git(ctx, "update-ref", "--no-deref", "HEAD", sha, candidate.base_sha)
        workspace.git(ctx, "merge", "--quit")
        if lfs:
            workspace.hydrate_lfs(ctx, workspace.cfg.git_remote, lfs_url=url)
        self.assert_clean_candidate(ctx, candidate)
        return candidate

    def adopt_resolution(self, ctx, task, journal):
        """Recover the exact solved pin when its Git transition preceded the task save."""
        pin = self.pin(ctx, task)
        paths = journal["conflict_paths"]
        evidence = {"conflict_paths": paths, "base_sha": journal["base_sha"], "pinned_sha": pin or None}
        if not journal.get("resolution_id") or not pin or pin == journal["base_sha"]:
            raise StageError("merge_resolution_scope", "A solver was admitted but its completed pin is unavailable",
                             result=evidence)
        try:
            tree = self.workspace.text(ctx, "rev-parse", "--verify", pin + "^{tree}")
            candidate = MergeCandidate(journal["source_sha"], journal["base_sha"], tuple(paths), pin, tree,
                                       resolution_id=journal["resolution_id"])
            self.assert_clean_candidate(ctx, candidate)
            entry = self.workspace.text(ctx, "reflog", "-1", "--format=%H%x09%gs", self._pin_ref(task))
            if entry != f"{pin}\t{candidate._resolution_reflog()}" or self.pin(ctx, task) != pin:
                raise StageError("merge_resolution_scope", "The pin does not record this solver's completed candidate")
            self._assert_detached(ctx, pin)
            return candidate
        except StageError as error:
            error.result.update(evidence)
            raise

    def _assert_candidate_identity(self, ctx, candidate):
        workspace = self.workspace
        parents = workspace.text(ctx, "rev-list", "--parents", "-n", "1", candidate.commit_sha).split()
        if (parents != [candidate.commit_sha, candidate.base_sha, candidate.source_sha]
                or workspace.text(ctx, "rev-parse", candidate.commit_sha + "^{tree}") != candidate.tree_sha):
            raise StageError("workspace_changed", "The merge candidate tree or parent commits changed")

    def _assert_checkout_contents(self, ctx, revision):
        workspace = self.workspace
        workspace.validate_unity_revision(ctx, revision)
        workspace.assert_clean(ctx)
        workspace.assert_submodules_current(ctx)
        lfs_list = ("lfs", "ls-files", "--json", "--include=", "--exclude=")
        if workspace.validate_lfs_filters(ctx, revision):
            self._assert_lfs_entries(workspace.git(ctx, *lfs_list)[1])
        if (workspace.root / ".gitmodules").is_file():
            _, outputs = workspace.git(
                ctx, "submodule", "foreach", "--quiet", "--recursive",
                "if git grep -I -q -F filter=lfs HEAD -- .gitattributes ':(glob)**/.gitattributes'; "
                "then git lfs ls-files --json --include= --exclude= && printf '\\000'; "
                'else test "$?" -eq 1; fi',
            )
            for output in outputs.split(b"\0"):
                if output:
                    self._assert_lfs_entries(output)

    @staticmethod
    def _assert_lfs_entries(output):
        if any(not entry["checkout"] for entry in json.loads(output)["files"]):
            raise StageError("workspace_changed", "The checkout contains unhydrated Git LFS dependencies")


def _save_journal(ctx, journal, **changes):
    updated = {**journal, **changes}
    ctx.save(merge_journal=updated)
    return updated


def _approved_evidence(task, claims):
    evidence = task.get("verification") or {}
    if (evidence.get("revision") != claims["revision"] or evidence.get("test_job_id") != claims["test_job_id"]
            or evidence.get("checkout") != {"branch": task["task_branch"], "commit_sha": claims["commit_sha"]}):
        raise StageError("workspace_changed", "The approved verification evidence changed")
    return {"mode": "reused", **evidence}


def _restore_operation(ctx, git, journal):
    pin = git.pin(ctx, ctx.task)
    known = journal.get("merge_sha")
    if pin and known and pin != known:
        raise StageError("workspace_changed", "The merge pin differs from the verified candidate")
    if pin and not known and pin != journal["base_sha"]:
        # begin() can finish the candidate before its verification evidence is saved.
        parents = git.workspace.text(ctx, "rev-list", "--parents", "-n", "1", pin).split()
        if parents != [pin, journal["base_sha"], journal["source_sha"]]:
            raise StageError("workspace_changed", "The merge pin differs from the recorded operation")
    git.restore(ctx, ctx.task, journal["source_sha"])


def _candidate_from(journal):
    return MergeCandidate(journal["source_sha"], journal["base_sha"], tuple(journal["conflict_paths"]),
                          journal["merge_sha"], journal["tree_sha"])


def recover_merge_checkout(ctx, *, released=False):
    """Validate the interrupted checkout before clearing the process permit or returning its lease."""
    task, workspace = ctx.task, ctx.workspace.git
    git = DirectMergeGit(workspace)
    claims = task["approval"]["claims"]
    source = claims["commit_sha"]
    workspace.validate(ctx)
    git._assert_no_merge_markers()
    journal = task.get("merge_journal")
    pin = git.pin(ctx, task)
    if not journal:
        if released or pin:
            raise StageError("workspace_changed", "The merge operation has no matching journal")
        # A process can be interrupted during the initial mixed reset.
        workspace.adopt_checkpoint(ctx, task, source)
        return
    if (journal["approval"] != claims or journal["source_sha"] != source
            or journal["target_branch"] != workspace.cfg.base_branch or journal["task_branch"] != task["task_branch"]
            or journal["remote_identity"] != workspace.remote_identity(ctx)
            or workspace.text(ctx, "rev-parse", "--verify", "refs/heads/" + task["task_branch"]) != source):
        raise StageError("workspace_changed", "The merge operation no longer matches its task and assignment")
    if released or task.get("current_stage") == "delivered":
        if (journal["status"] != "confirmed" or not task.get("delivery")
                or task["delivery"] != journal["receipt"] or pin):
            raise StageError("workspace_changed", "A confirmed receipt and restored checkout are required")
        workspace.checkout_is(ctx, task, source)
        git._assert_checkout_contents(ctx, source)
    elif journal.get("conflict_paths") and not journal.get("merge_sha"):
        git.adopt_resolution(ctx, task, journal)
    elif journal["status"] == "confirmed" or journal.get("rebuild_target"):
        _restore_operation(ctx, git, journal)
    elif journal.get("merge_sha"):
        if pin != journal["merge_sha"]:
            raise StageError("workspace_changed", "The recorded merge candidate lost its pin")
        git.assert_clean_candidate(ctx, _candidate_from(journal))
    elif git._current_branch(ctx):
        if pin and pin != journal["base_sha"]:
            raise StageError("workspace_changed", "Unexpected pin before merge checkout")
        workspace.checkout_is(ctx, task, source)
        git._assert_checkout_contents(ctx, source)
    else:
        git._assert_detached(ctx, pin)
        if pin == journal["base_sha"]:
            git._assert_checkout_contents(ctx, pin)
            return
        # A clean merge may have been pinned before its candidate record reached SQLite.
        # Independently reproduce the automatic tree before accepting this exact pin.
        tree = workspace.text(ctx, "merge-tree", "--write-tree", journal["base_sha"], source)
        candidate = MergeCandidate(source, journal["base_sha"], (), pin, tree)
        git.assert_clean_candidate(ctx, candidate)
        _save_journal(ctx, journal, merge_sha=pin, tree_sha=tree, conflict_paths=[])


def _verify_for_push(ctx, git, journal, evidence, solve, verify):
    task = ctx.task
    if journal.get("merge_sha"):
        candidate = _candidate_from(journal)
        if git.pin(ctx, task) != candidate.commit_sha:
            raise StageError("workspace_changed", "The merge pin differs from the candidate awaiting verification")
    else:
        _restore_operation(ctx, git, journal)
        candidate = git.begin(ctx, task, journal["source_sha"], journal["base_sha"])
        if candidate.conflict_paths:
            journal = _save_journal(ctx, journal, conflict_paths=list(candidate.conflict_paths),
                                    resolution_id=uuid4().hex)
            solve(ctx, candidate)
            candidate = git.resolve(ctx, replace(candidate, resolution_id=journal["resolution_id"]))
        journal = _save_journal(ctx, journal, merge_sha=candidate.commit_sha,
                                tree_sha=candidate.tree_sha, conflict_paths=list(candidate.conflict_paths))

    git.assert_clean_candidate(ctx, candidate)
    if not candidate.conflict_paths and candidate.tree_sha == journal["approval"]["revision"]:
        checked = evidence
    else:
        try:
            checked = verify(ctx, candidate)
            git.assert_clean_candidate(ctx, candidate)
        except StageError as error:
            _save_journal(ctx, journal, verification_failure={"kind": error.kind, "message": str(error),
                                                             "result": error.result})
            raise
        journal = dict(journal)
        journal.pop("verification_failure", None)
    if not isinstance(checked, dict) or not checked:
        raise StageError("verification_failed", "Candidate verification returned no evidence")
    return candidate, _save_journal(ctx, journal, status="in_flight", verification=checked)


def _confirmed(ctx, git, journal, target, landed, verification, paths):
    task = ctx.task
    receipt = {
        "mode": "direct-merge", "status": "merged", "task_branch": task["task_branch"],
        "source_sha": journal["source_sha"], "target_branch": journal["target_branch"],
        "base_sha": journal["base_sha"], "merge_sha": landed, "target_sha": target,
        "conflict_paths": list(paths), "verification": verification,
    }
    # Keep the candidate fields until restore completes, including when a third
    # party independently lands the approved source via a different commit.
    _save_journal(ctx, journal, status="confirmed", receipt=receipt, target_sha=target)
    _restore_operation(ctx, git, journal)
    return receipt


def land_approved(ctx, *, solve, verify) -> dict:
    """Land the task authorized by the Worker, or reconcile a prior transport attempt."""
    task, workspace = ctx.task, ctx.workspace.git
    git = DirectMergeGit(workspace)
    claims = (task.get("approval") or {}).get("claims") or {}
    source = claims.get("commit_sha")
    git._require_sha(source)
    git._require_sha(claims.get("revision"))
    if claims.get("delivery_mode") != "direct-merge" or claims["revision"] != task.get("approved_revision"):
        raise StageError("workspace_changed", "The task has no matching direct merge approval")
    evidence = _approved_evidence(task, claims)
    binding = {"approval": claims, "source_sha": source, "target_branch": workspace.cfg.base_branch,
               "task_branch": task["task_branch"], "remote_identity": task["remote_identity"]}
    journal = task.get("merge_journal")
    if journal is not None and (any(journal.get(k) != v for k, v in binding.items())
                                or journal.get("status") not in {"building", "in_flight", "confirmed"}):
        raise StageError("workspace_changed", "The merge journal belongs to a different approval or target")

    target = git.read_target(ctx, task)
    if journal is None:
        workspace.checkout_is(ctx, task, source)
        if git.pin(ctx, task):
            raise StageError("workspace_changed", "The task already has a merge pin without its operation record")
        if not git.contains(ctx, task["base_commit"], target):
            raise StageError("merge_target_rewritten", "The target no longer contains the task's original base")
        git.assert_remote_source(ctx, task, source)
        journal = _save_journal(ctx, binding, base_sha=target, status="building", rebuilds=0)

    while True:
        base, status = journal["base_sha"], journal["status"]
        if journal.get("conflict_paths") and not journal.get("merge_sha"):
            candidate = git.adopt_resolution(ctx, task, journal)
            journal = _save_journal(ctx, journal, merge_sha=candidate.commit_sha, tree_sha=candidate.tree_sha)
        if status == "confirmed":
            receipt = journal["receipt"]
            if not git.contains(ctx, receipt["merge_sha"], target):
                raise StageError("merge_target_rewritten", "The target lost the confirmed merge")
            return _confirmed(ctx, git, journal, target, receipt["merge_sha"], receipt["verification"],
                              receipt["conflict_paths"])

        if status == "in_flight":
            candidate = _candidate_from(journal)
            git._assert_candidate_identity(ctx, candidate)
            if git.contains(ctx, candidate.commit_sha, target):
                return _confirmed(ctx, git, journal, target, candidate.commit_sha,
                                  journal["verification"], candidate.conflict_paths)
            if journal.get("push_acknowledged"):
                raise StageError("push_ambiguous", "The target no longer contains the acknowledged merge")

        if not git.contains(ctx, base, target):
            raise StageError("merge_target_rewritten", "The target history changed after merge preparation")
        git.assert_remote_source(ctx, task, source)
        if git.contains(ctx, source, target):
            return _confirmed(ctx, git, journal, target, source, evidence, ())

        pending = journal.get("rebuild_target")
        if (pending and target != pending) or (target != base and journal["rebuilds"]):
            raise StageError("merge_target_moved", "The target advanced again after the single rebuild")
        if target != base and not pending:
            # Reserve this exact retry base before releasing the previous pin.
            journal = _save_journal(ctx, journal, rebuild_target=target)
        if target != base:
            _restore_operation(ctx, git, journal)
            journal = _save_journal(ctx, binding, base_sha=target, status="building", rebuilds=1)
            status = "building"

        if status == "building":
            candidate, journal = _verify_for_push(ctx, git, journal, evidence, solve, verify)

        try:
            git.push(ctx, task, candidate)
        except StageError as error:
            if error.kind == "merge_target_moved":
                target = error.result["target_sha"]
            elif error.kind == "push_failed":
                target = git.read_target(ctx, task)
                if target == journal["base_sha"]:
                    raise
            else:
                raise
        else:
            journal = _save_journal(ctx, journal, push_acknowledged=True)
            target = git.read_target(ctx, task)
