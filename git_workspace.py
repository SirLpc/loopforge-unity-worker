"""Operate on a single resident Git checkout. Preserve unknown work and caches."""

import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from process_runner import StageError
from unity_executor import assert_unity_closed
from worker_config import PROJECT_VERSION, system_env


CACHES = {"library", "temp", "logs", "obj", "usersettings", ".vs", ".idea", "memorycaptures"}
OUTPUTS = {"build", "builds", "artifacts", "testresults", "recordings"}
PROTECTED = {"main", "master", "develop", "development", "trunk"}
GIT_CONTROLS = {".git", ".gitmodules", ".lfsconfig", ".gitattributes"}
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".codex", ".azure", "secrets"}
SECRET_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx"}
BUILD_SUFFIXES = {".apk", ".aab", ".ipa", ".exe", ".appx", ".msix"}
SECRET_PATTERN = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})")
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GUID = re.compile(rb"^guid: [a-fA-F0-9]{32}[ \t]*\r?$", re.M)


def has_hidden_index_entries(path):
    with path.open("rb") as stream:
        pending = b""
        while data := stream.read(1024 * 1024):
            rows = (pending + data).split(b"\0")
            pending = rows.pop()
            if any(row[:1] == b"S" or row[:1].islower() for row in rows):
                return True
    return False


def outside_cache(path: Path, target: Path) -> Path:
    """Resolve existing ancestors strictly, including for stores that Git has not created yet."""
    path = Path(os.path.normpath(path))
    if not path.is_absolute():
        raise StageError("unsafe_cache", "Could not determine the Git storage location")
    for ancestor in (path, *path.parents):
        try:
            ancestor.lstat()
        except FileNotFoundError:
            continue
        existing = ancestor.resolve(strict=True)
        # Windows drive, UNC and extended-length aliases can spell the same directory differently.
        if any(parent.samefile(target) for parent in (existing, *existing.parents)) or (
                ancestor == path and any(parent.samefile(existing) for parent in target.parents)):
            raise StageError("unsafe_cache", "The cache overlaps Git or Git LFS storage")
        return existing / path.relative_to(ancestor)
    raise StageError("unsafe_cache", "Could not determine the Git storage location")


@dataclass(frozen=True)
class Snapshot:
    tree: str
    changes: list


class GitWorkspace:
    def __init__(self, settings):
        self.cfg = settings
        self.project = settings.project
        self.root = getattr(settings, "git_root", None) or self.project
        self.unity_prefix = PurePosixPath(self.project.relative_to(self.root).as_posix())
        self.asset_prefix = self.unity_prefix / "Assets"
        self.allowed_tracked_outputs = getattr(settings, "allowed_tracked_outputs", ())
        self.ignored_cache_paths = tuple(PurePosixPath(path.lower())
                                         for path in getattr(settings, "ignored_cache_paths", ()))

    def git(self, ctx, *args, allowed=(0,), data=None, index=None, limit=8_000_000, read=None):
        env = {
            **system_env(), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
            "GIT_LFS_SKIP_SMUDGE": "1", "GIT_LITERAL_PATHSPECS": "0",
        }
        if index:
            env["GIT_INDEX_FILE"] = str(index)
        # Git LFS may install hooks. Keep them outside the checkout and isolate
        # each command from hooks installed by earlier calls.
        with TemporaryDirectory(prefix="git-hooks-", dir=ctx.directory) as hooks:
            result = ctx.run(
                [self.cfg.git_exe, "-c", f"core.hooksPath={Path(hooks).as_posix()}", "-c", "core.fsmonitor=false",
                 "-c", "core.ignoreStat=false", "-c", "protocol.ext.allow=never", *args],
                "git", cwd=self.root, env=env, data=data, timeout=self.cfg.git_timeout, max_output=limit,
            )
        try:
            if result.exit_code not in allowed:
                with result.stderr.open("rb") as error:
                    tail = error.read(4096).decode("utf-8", errors="replace").strip()
                tail = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", tail, flags=re.I)
                for secret in self.cfg.secrets:
                    tail = tail.replace(secret, "[redacted]")
                raise StageError("git_failed", f"Git {args[0]} failed: {tail}", result.exit_code)
            if limit is not None and result.stdout.stat().st_size > limit:
                raise StageError("output_limit", "Git output exceeds the configured limit")
            return result.exit_code, read(result.stdout) if read else result.stdout.read_bytes()
        finally:
            result.stdout.unlink(missing_ok=True)
            result.stderr.unlink(missing_ok=True)

    def text(self, ctx, *args, **kwargs):
        return self.git(ctx, *args, **kwargs)[1].decode("utf-8", errors="strict").strip()

    def validate_checkout_identity(self, ctx):
        root = self.text(ctx, "rev-parse", "--show-toplevel")
        if type(self.root)(root).resolve() != self.root:
            raise StageError("invalid_repository", "Configured git_root must be the Git checkout root")
        git_dir = self.text(ctx, "rev-parse", "--absolute-git-dir")
        if type(self.root)(git_dir).resolve() != self.root / ".git":
            raise StageError("invalid_repository", "Use a standalone Git checkout with its own .git directory")

    def assert_disposable_cache(self, ctx, target: Path) -> None:
        self.validate(ctx)
        for parent in target.parents:
            if parent == self.root:
                break
            try:
                (parent / ".git").lstat()
            except FileNotFoundError:
                continue
            raise StageError("unsafe_cache", "A repository inside the checkout owns this cache")
        owner = self.text(ctx, "-C", str(target), "rev-parse", "--show-toplevel")
        if Path(owner).resolve(strict=True) != self.root:
            raise StageError("unsafe_cache", "The cache is outside the configured Git worktree")
        self.assert_storage_outside_cache(ctx, target)
        relative = target.relative_to(self.root).as_posix()
        cache = Path(relative)

        def contains_tracked_path(output):
            with output.open("rb") as stream:
                pending = b""
                while chunk := stream.read(1024 * 1024):
                    rows = (pending + chunk).split(b"\0")
                    pending = rows.pop()
                    for row in rows:
                        path = Path(os.fsdecode(row))
                        if path.is_relative_to(cache) or cache.is_relative_to(path):
                            return True
                if pending:
                    raise StageError("unsafe_cache", "Incomplete Git path listing")
            return False

        for command in (("ls-files", "--cached", "-z"), ("ls-tree", "-r", "--name-only", "-z", "HEAD")):
            if self.git(ctx, *command, limit=None, read=contains_tracked_path)[1]:
                raise StageError("unsafe_cache", "The Git index or HEAD tracks a path overlapping this cache")
        ignored, _ = self.git(ctx, "check-ignore", "--quiet", "--no-index", "--", relative, allowed=(0, 1))
        if ignored or self.git(ctx, "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "-z",
                               "--", relative)[1]:
            raise StageError("unsafe_cache", "The cache contains a path that Git does not ignore")

    def assert_storage_outside_cache(self, ctx, target):
        git_dir = self.root / ".git"
        common = self.text(ctx, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common).resolve(strict=True) != git_dir:
            raise StageError("unsafe_cache", "Cache cleanup requires a standalone Git directory")
        _, raw = self.git(ctx, "config", "--null", "--get", "lfs.storage", allowed=(0, 1))
        configured = Path(os.fsdecode(raw.removesuffix(b"\0") or b"lfs"))
        if configured.anchor and not configured.is_absolute():
            raise StageError("unsafe_cache", "Could not determine the Git LFS storage location")
        lfs = outside_cache(git_dir / configured, target)
        outside_cache(lfs / "objects", target)

        pending = [git_dir / "objects"]
        seen = set()
        while pending:
            objects = outside_cache(pending.pop(), target)
            if objects in seen:
                continue
            seen.add(objects)
            alternate_file = outside_cache(objects / "info/alternates", target)
            try:
                with alternate_file.open("rb") as stream:
                    for raw_path in stream:
                        value = raw_path.removesuffix(b"\n")
                        if not value or value.startswith(b"#"):
                            continue
                        if value.startswith(b'"'):
                            raise StageError("unsafe_cache", "Could not verify a quoted Git alternate object path")
                        pending.append(objects / os.fsdecode(value))
            except FileNotFoundError:
                continue

    def validate(self, ctx):
        assert_unity_closed(self.project)
        self.validate_checkout_identity(ctx)
        for path in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply",
                     "sequencer", "BISECT_START", "index.lock", "shallow.lock", "config.lock"):
            if (self.root / ".git" / path).exists():
                raise StageError("dirty_workspace", f"Unfinished Git operation ({path}). Resolve it manually.")
        if self.text(ctx, "config", "--bool", "--get", "core.sparseCheckout", allowed=(0, 1)) == "true":
            raise StageError("unsupported", "Sparse checkout is not supported for a resident Unity project")
        _, hidden = self.git(ctx, "ls-files", "-v", "-z", limit=None, read=has_hidden_index_entries)
        if hidden:
            raise StageError("dirty_workspace", "Git index has assume-unchanged or skip-worktree entries. Inspect them manually.")

    def status(self, ctx):
        raw = self.git(ctx, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all",
                       "--ignored=matching", "--ignore-submodules=none")[1]
        rows = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            state, path = entry[:2].decode("ascii"), entry[3:].decode("utf-8")
            if state == "!!" and self.cache(path):
                continue
            rows.append((state, path))
        return rows

    def scoped_parts(self, path):
        value = PurePosixPath(path.lower())
        yield value.parts
        prefix = PurePosixPath(self.unity_prefix.as_posix().lower())
        if prefix.parts and value.is_relative_to(prefix):
            yield value.relative_to(prefix).parts

    def cache(self, path):
        if any(PurePosixPath(path.lower()).is_relative_to(cache) for cache in self.ignored_cache_paths):
            return True
        return any(parts and (parts[0] in CACHES or
                             (len(parts) == 1 and parts[0].endswith((".sln", ".slnx", ".csproj", ".user"))))
                   for parts in self.scoped_parts(path))

    def output(self, path):
        return any(parts and parts[0] in OUTPUTS for parts in self.scoped_parts(path))

    def clean(self, ctx):
        self.validate(ctx)
        self.assert_clean(ctx)

    def assert_clean(self, ctx):
        rows = self.status(ctx)
        if rows:
            raise StageError("dirty_workspace",
                             "Workspace contains local changes. Save or archive them before proceeding.",
                             result={"dirty_paths": [path for _, path in rows[:30]]})
        self.assert_submodules_clean(ctx)

    def assert_submodules_clean(self, ctx):
        if not (self.root / ".gitmodules").is_file():
            return
        scopes = ("submodule", "foreach", "--quiet", "--recursive")
        status = (
            'for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply sequencer BISECT_START index.lock; '
            'do if test -e "$(git rev-parse --git-path "$marker")"; then printf "operation in progress"; exit 0; fi; done; '
            "git -c core.fsmonitor=false -c core.ignoreStat=false status --porcelain=v1 -z "
            "--no-renames --untracked-files=all --ignored=matching --ignore-submodules=none"
        )
        _, changes = self.git(ctx, *scopes, status)
        _, hidden = self.git(ctx, *scopes, "git ls-files -v -z", limit=None, read=has_hidden_index_entries)
        if changes or hidden:
            raise StageError("dirty_workspace", "An initialized submodule has local files, index flags or an unfinished operation.")

    def assert_safe_tree(self, ctx, revision):
        for path in getattr(self.cfg, "ignored_cache_paths", ()):
            if self.git(ctx, "--literal-pathspecs", "ls-tree", "-r", "-z", revision, "--", path)[1]:
                raise StageError("invalid_repository", "The Git tree tracks a configured dependency cache")
        for prefix in dict.fromkeys((PurePosixPath(), self.unity_prefix)):
            tree = f"{revision}:{prefix.as_posix()}" if prefix.parts else revision
            names = self.git(ctx, "ls-tree", "--name-only", "-z", tree)[1].decode("utf-8").split("\0")
            for name in names:
                path = (prefix / name).as_posix()
                if not name or not (self.cache(path) or self.output(path)):
                    continue
                tracked = self.git(ctx, "ls-tree", "-r", "-z", revision, "--", path)[1].decode("utf-8")
                for entry in tracked.rstrip("\0").split("\0"):
                    mode, _, tracked_path = entry.partition("\t")
                    if tracked_path not in self.allowed_tracked_outputs or not mode.startswith(("100644 ", "100755 ")):
                        raise StageError("invalid_repository", "The Git tree tracks a cache or build directory")

    def remote_identity(self, ctx):
        identity, _ = self.remote_binding(ctx)
        return identity

    def remote_binding(self, ctx):
        remote = self.cfg.git_remote
        urls = [self.text(ctx, "remote", "get-url", *options, "--all", "--", remote).splitlines()
                for options in ((), ("--push",))]
        if any(len(items) != 1 for items in urls):
            raise StageError("unsupported", "Configure one fetch URL and one push URL for the Worker remote")
        for (url,) in urls:
            if re.match(r"https?://", url, re.I):
                parsed = urlsplit(url)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise StageError("credentials", "Use a Git credential helper instead of credentials in the remote URL")
            if "::" in url and "://" not in url:
                raise StageError("unsupported", "Custom Git remote helpers are unsupported")
        identity = sha256("\0".join(items[0] for items in urls).encode()).hexdigest()
        return identity, urls[1][0]

    def uses_lfs(self, ctx, revision):
        code, _ = self.git(ctx, "grep", "-I", "-l", "-F", "filter=lfs", revision,
                           "--", ".gitattributes", ":(glob)**/.gitattributes", allowed=(0, 1))
        return code == 0

    def prepare(self, ctx, branch):
        self.clean(ctx)
        self.assert_safe_tree(ctx, "HEAD")
        cfg = self.cfg
        remote_identity = self.remote_identity(ctx)
        expected_remote = getattr(cfg, "expected_remote_identity", None)
        if expected_remote is not None and remote_identity != expected_remote:
            raise StageError("remote_mismatch", "The workspace Git remote differs from its pool")
        self.text(ctx, "check-ref-format", "--branch", cfg.base_branch)
        self.git(ctx, "fetch", "--no-tags", "--no-recurse-submodules", "--", cfg.git_remote,
                 f"refs/heads/{cfg.base_branch}")
        commit = self.text(ctx, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        self.assert_safe_tree(ctx, commit)
        version = self.text(ctx, "show", f"{commit}:{self.unity_prefix / PROJECT_VERSION}", limit=32_768)
        if f"m_EditorVersion: {cfg.unity_version}" not in version.splitlines():
            raise StageError("unsupported", "The target branch requires a different Unity Editor version")
        lfs = self.uses_lfs(ctx, commit)
        if lfs:
            self.git(ctx, "lfs", "version")
            if not self.text(ctx, "config", "--get", "filter.lfs.process", allowed=(0, 1)):
                raise StageError("unsupported", "Install Git LFS filters in the dedicated checkout")
        current = self.text(ctx, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
        if current == branch:
            if self.text(ctx, "rev-parse", "HEAD") != commit:
                raise StageError("workspace_changed", "Partially prepared task branch no longer matches the fetched base")
        else:
            self.git(ctx, "switch", "--no-overwrite-ignore", "--no-recurse-submodules", "-c", branch, commit)
        if (self.root / ".gitmodules").is_file():
            if cfg.init_submodules:
                self.git(ctx, "submodule", "sync", "--recursive")
                self.git(ctx, "submodule", "update", "--init", "--recursive", "--checkout")
            else:
                self.git(ctx, "submodule", "update", "--recursive", "--checkout")
            submodules = self.git(ctx, "submodule", "status", "--recursive")[1].decode("utf-8")
            if any(line.startswith(("-", "+", "U")) for line in submodules.splitlines()):
                raise StageError("unsupported", "Submodule checkout incomplete. Initialize the configured submodules.")
            self.git(ctx, "submodule", "foreach", "--quiet", "--recursive",
                     "if git grep -I -q -F filter=lfs HEAD -- .gitattributes ':(glob)**/.gitattributes'; "
                     'then test -n "$(git config --get filter.lfs.process)" && git lfs pull; '
                     'else test "$?" -eq 1; fi')
        if lfs:
            self.git(ctx, "lfs", "pull", cfg.git_remote)
        self.assert_clean(ctx)
        return {"base_commit": commit, "task_branch": branch, "remote_identity": remote_identity, "lfs": lfs}

    def checkout_is(self, ctx, task, commit):
        self.validate(ctx)
        branch = self.text(ctx, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
        head = self.text(ctx, "rev-parse", "HEAD")
        if branch != task["task_branch"] or head != commit:
            raise StageError("workspace_changed", "The task branch or its HEAD changed outside the Worker")

    def snapshot(self, ctx, task, *, check_meta=False):
        self.checkout_is(ctx, task, task["base_commit"])
        self.assert_submodules_clean(ctx)
        rows = self.status(ctx)
        for state, path in rows:
            if state == "!!":
                raise StageError("dirty_workspace", "Unrecognized ignored file in the task workspace")
            if state[0] not in (" ", "?"):
                raise StageError("workspace_changed", "The Git index contains staged or unmerged changes")
            self.check_path(path)
            if (self.root / path).is_dir():
                raise StageError("unsafe_change", "Changes inside submodules need manual delivery")
        index = ctx.directory / f"index-{uuid4().hex}"
        try:
            self.git(ctx, "read-tree", task["base_commit"], index=index)
            self.stage_paths(ctx, [path for _, path in rows], index=index)
            tree = self.text(ctx, "write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)
            index.with_suffix(".lock").unlink(missing_ok=True)
        raw = self.git(ctx, "diff-tree", "--raw", "-r", "--no-renames", "--no-commit-id", "-z",
                       task["base_commit"], tree)[1]
        fields = raw.decode("utf-8").split("\0")
        changes = []
        for header, path in zip(fields[:-1:2], fields[1:-1:2]):
            before, after, _, _, status = header[1:].split()
            if {before, after} & {"120000", "160000"}:
                raise StageError("unsafe_change", "Symlink and submodule changes need manual delivery")
            if {before, after} - {"000000", "100644", "100755"}:
                raise StageError("unsafe_change", "Unsupported Git file mode")
            self.check_path(path)
            if status != "D":
                self.check_secrets(path)
            changes.append({"path": path, "status": status})
        if check_meta:
            self.check_meta(changes)
        return Snapshot(tree, changes)

    def check_path(self, path):
        parts = PurePosixPath(path).parts
        if (not parts or any(part in ("", ".", "..", "/") or part.rstrip(" .") != part for part in parts)
                or ":" in path or "\\" in path or path.startswith("/") or "\n" in path or "\r" in path):
            raise StageError("unsafe_change", "Unsupported path in Git changes")
        relative = PurePosixPath(path.lower())
        name, suffix = relative.name, relative.suffix
        if (set(relative.parts) & SENSITIVE_DIRS or name in {".env", ".netrc", ".npmrc", ".pypirc", "credentials.json"}
                or (name.startswith(".env.") and name not in {".env.example", ".env.sample"})
                or suffix in SECRET_SUFFIXES):
            raise StageError("unsafe_change", "Sensitive file in Git changes")
        if self.cache(path) or self.output(path) or suffix in BUILD_SUFFIXES or path in self.allowed_tracked_outputs:
            raise StageError("unsafe_change", "Cache or build output in Git changes")
        if set(relative.parts) & GIT_CONTROLS:
            raise StageError("unsafe_change", "Git transport, attribute and submodule changes need manual delivery")
        local = self.root.joinpath(*parts)
        if local.is_symlink() or local.resolve() != local:
            raise StageError("unsafe_change", "Linked files and directories need manual delivery")

    def check_secrets(self, path):
        chunks = tuple(secret.encode() for secret in self.cfg.secrets)
        # shortcut: bounded signatures for common secrets, extend with an organization secret scanner if required.
        with (self.root / path).open("rb") as file:
            previous = b""
            while data := file.read(512 * 1024):
                data = previous + data
                if any(secret in data for secret in chunks) or SECRET_PATTERN.search(data):
                    raise StageError("unsafe_change", "Possible credential in a changed file")
                previous = data[-max(256, *(len(secret) for secret in chunks), 0):]

    def check_meta(self, changes):
        for entry in changes:
            path, status = entry["path"], entry["status"]
            relative = PurePosixPath(path)
            if self.asset_prefix not in relative.parents or any(part.startswith(".") or part.endswith("~")
                                                               for part in relative.parts):
                continue
            local = self.root / path
            if path.endswith(".meta"):
                target = local.with_name(local.name[:-5])
                if ((status != "D" and (not target.exists() or not GUID.search(local.read_bytes())))
                        or (status == "D" and target.exists())):
                    raise StageError("asset_metadata", f"Invalid Unity meta pairing for {path}")
            elif status == "D":
                if local.with_name(local.name + ".meta").exists():
                    raise StageError("asset_metadata", f"Orphan Unity meta for {path}")
            else:
                meta = local.with_name(local.name + ".meta")
                if not meta.is_file() or not GUID.search(meta.read_bytes()):
                    raise StageError("asset_metadata", f"Missing or invalid Unity meta for {path}")
                for parent in local.parents:
                    if parent == self.project / "Assets":
                        break
                    folder_meta = parent.with_name(parent.name + ".meta")
                    if not folder_meta.is_file() or not GUID.search(folder_meta.read_bytes()):
                        raise StageError("asset_metadata", f"Missing or invalid folder meta for {path}")

    def diff(self, ctx, task, snapshot):
        return self.git(ctx, "diff", "--no-ext-diff", "--no-textconv", "--full-index", "--no-renames",
                        task["base_commit"], snapshot.tree, "--", limit=self.cfg.max_diff_bytes)[1]

    def stage_paths(self, ctx, paths, *, index=None):
        if paths:
            data = ("\0".join(paths) + "\0").encode("utf-8")
            self.git(ctx, "--literal-pathspecs", "add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul",
                     data=data, index=index)

    def commit(self, ctx, task, snapshot, message):
        self.stage_paths(ctx, [entry["path"] for entry in snapshot.changes])
        if self.text(ctx, "write-tree") != snapshot.tree:
            raise StageError("workspace_changed", "The staged tree no longer matches the approved revision")
        self.git(ctx, "commit", "-F", "-", data=(message + "\n").encode("utf-8"))
        commit = self.text(ctx, "rev-parse", "HEAD")
        if (not SHA.fullmatch(commit) or self.text(ctx, "rev-parse", "HEAD^{tree}") != snapshot.tree
                or self.text(ctx, "rev-parse", "HEAD^") != task["base_commit"]):
            raise StageError("workspace_changed", "Local commit failed verification")
        return commit

    def push(self, ctx, task):
        cfg, branch = self.cfg, task["task_branch"]
        if (not re.fullmatch(r"ai/task-[0-9a-f]{32}", branch)
                or branch.casefold() in PROTECTED | {cfg.base_branch.casefold()}):
            raise StageError("unsafe_change", "Only the generated task branch can be pushed")
        commit = task["commit_sha"]
        self.checkout_is(ctx, task, commit)
        self.assert_clean(ctx)
        if self.text(ctx, "rev-parse", "HEAD^{tree}") != task["approved_revision"]:
            raise StageError("workspace_changed", "Committed tree no longer matches approval")
        if self.remote_identity(ctx) != task["remote_identity"]:
            raise StageError("workspace_changed", "Configured Git remote changed after preparation")
        if task["lfs"]:
            self.git(ctx, "lfs", "push", cfg.git_remote, branch)
        ref = f"refs/heads/{branch}"
        self.git(ctx, "push", "--porcelain", "--no-force", "--no-follow-tags", "--recurse-submodules=no",
                 "--", cfg.git_remote, f"{commit}:{ref}")
