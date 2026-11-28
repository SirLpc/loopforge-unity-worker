"""Build and verify a disposable merge candidate in the resident Unity checkout."""

import json
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from git_workspace import SHA, GitWorkspace
from process_runner import StageError


CONFLICT_MARKERS = re.compile(rb"^(?:<{7,}|={7,}|>{7,}|\|{7,})", re.MULTILINE)
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
    task_branch: str | None = field(default=None, repr=False, compare=False)
    remote_identity: str | None = field(default=None, repr=False, compare=False)


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
        return workspace.text(ctx, "rev-parse", "--verify", "FETCH_HEAD^{commit}")

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
        if workspace.remote_task_tip(ctx, task) != source_sha:
            raise StageError("push_conflict", "Remote task branch no longer points to the approved commit")
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
            return candidate
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

    def restore(self, ctx, task: dict, source_sha: str) -> None:
        workspace = self.workspace
        self._require_sha(source_sha)
        workspace.clean(ctx)
        self._assert_no_merge_markers()
        url = self._push_url(ctx, task)
        pin = self._pin_ref(task)
        owned = workspace.text(ctx, "rev-parse", "--verify", "--quiet", pin, allowed=(0, 1))
        branch = self._current_branch(ctx)
        task_ref = "refs/heads/" + task["task_branch"]
        source_ref = workspace.text(ctx, "rev-parse", "--verify", task_ref)
        head = workspace.text(ctx, "rev-parse", "HEAD")
        if source_ref != source_sha or (branch and (branch != task_ref or head != source_sha)):
            raise StageError("workspace_changed", "The original task branch changed outside the Worker")
        if not branch:
            if head != owned:
                raise StageError("workspace_changed", "The detached checkout does not match the Worker merge operation")
            workspace.validate_unity_revision(ctx, source_sha)
            lfs = workspace.validate_lfs_filters(ctx, source_sha)
            workspace.git(ctx, "switch", "--no-overwrite-ignore", "--no-recurse-submodules", task["task_branch"])
            workspace.hydrate_checkout(ctx, lfs=lfs, lfs_remote=workspace.cfg.git_remote, lfs_url=url)
        workspace.checkout_is(ctx, task, source_sha)
        self._assert_checkout_contents(ctx, source_sha)
        if owned:
            workspace.git(ctx, "update-ref", "-d", pin, owned)

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
        entries = []
        _, output = self.workspace.git(ctx, "ls-files", "--stage", "-z", limit=None)
        for row in output.decode("utf-8").split("\0"):
            if row:
                header, path = row.split("\t", 1)
                mode, blob, stage = header.split()
                entries.append(IndexEntry(path, mode, blob, int(stage)))
        return tuple(entries)

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
        candidate = replace(candidate, commit_sha=sha, tree_sha=tree, index_entries=(),
                            task_branch=None, remote_identity=None)
        self._assert_candidate_identity(ctx, candidate)
        workspace.git(ctx, "update-ref", self._pin_ref(task), sha, candidate.base_sha)
        workspace.git(ctx, "update-ref", "--no-deref", "HEAD", sha, candidate.base_sha)
        workspace.git(ctx, "merge", "--quit")
        if lfs:
            workspace.hydrate_lfs(ctx, workspace.cfg.git_remote, lfs_url=url)
        self.assert_clean_candidate(ctx, candidate)
        return candidate

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
