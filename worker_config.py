"""Worker-only settings. The HTTP API never selects a tool, path or remote."""

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


PROJECT_VERSION = "ProjectSettings/ProjectVersion.txt"
VERSION_PATTERN = r"\d+\.\d+\.\d+[abfp]\d+(?:c\d+)?"
REMOTE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
BRANCH_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}"
ASSEMBLY_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}"
FIXTURE_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*(?:[.+][A-Za-z_][A-Za-z0-9_]*)*"


def validate_test_selection(selection, allowed):
    if selection is None and not allowed:
        return
    if not allowed or not isinstance(selection, dict) or not selection.get("editmode"):
        raise ValueError("Provide approved EditMode test assemblies and fixture names")
    for mode, scopes in selection.items():
        if mode not in allowed or not isinstance(scopes, list) or not 1 <= len(scopes) <= 8:
            raise ValueError("Test mode is unavailable or has an invalid scope count")
        seen = set()
        for scope in scopes:
            if not isinstance(scope, dict) or set(scope) != {"assembly", "fixtures"}:
                raise ValueError("Each test scope requires an assembly and fixture names")
            assembly, fixtures = scope["assembly"], scope["fixtures"]
            if not isinstance(assembly, str) or assembly not in allowed[mode] or assembly in seen:
                raise ValueError("Test assembly is not allowed or is repeated")
            seen.add(assembly)
            if not isinstance(fixtures, list) or not 1 <= len(fixtures) <= 20:
                raise ValueError("Choose between one and twenty test fixtures per assembly")
            if any(not isinstance(name, str) or len(name) > 220 or not re.fullmatch(FIXTURE_PATTERN, name)
                   or name.rsplit(".", 1)[-1].casefold() == "shippedassettests" for name in fixtures):
                raise ValueError("Test fixtures must use approved literal class names")
            if len(set(fixtures)) != len(fixtures):
                raise ValueError("Test fixtures must be distinct")



def read_project_version(project):
    text = (project / PROJECT_VERSION).read_text(encoding="utf-8-sig")
    match = re.search(rf"^m_EditorVersion:\s*({VERSION_PATTERN})[ \t]*$", text, re.MULTILINE)
    if not match:
        raise ValueError("ProjectVersion.txt lacks a supported m_EditorVersion")
    return match[1]


def executable(value, *, required=True):
    if not value and not required:
        return None
    resolved = shutil.which(str(value))
    path = Path(resolved or str(value)).resolve(strict=True)
    if not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise ValueError("Configure a native executable path (.exe on Windows)")
    return str(path)


def system_env():
    names = {
        "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES", "COMPUTERNAME", "COMSPEC",
        "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
        "OS", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES",
        "PROGRAMFILES(X86)", "PUBLIC", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN",
        "USERNAME", "USERPROFILE", "WINDIR",
    }
    return {name: value for name, value in os.environ.items() if name.upper() in names}


@dataclass(frozen=True)
class Settings:
    project: Path
    logs: Path
    unity_exe: str
    unity_version: str
    token: str = field(repr=False)
    approval_token: str = field(default="", repr=False)
    worker_id: str = "worker-01"
    unity_timeout: int = 1800
    git_exe: str = "git"
    git_remote: str = "origin"
    base_branch: str = "main"
    git_timeout: int = 600
    init_submodules: bool = False
    codex_exe: str | None = None
    codex_home: Path | None = None
    codex_timeout: int = 1800
    codex_sandbox: str = "mxc"
    max_fixes: int = 3
    test_modes: tuple = ("editmode", "playmode")
    max_diff_bytes: int = 2_000_000

    git_root: Path | None = None
    allowed_tracked_outputs: tuple[str, ...] = ()
    ignored_cache_paths: tuple[str, ...] = ()
    test_assemblies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def checkout(self):
        return self.git_root or self.project

    @property
    def secrets(self):
        return tuple(value for value in (
            self.token, self.approval_token, os.environ.get("UNITY_PR_TOKEN", ""),
            os.environ.get("OPENAI_API_KEY", ""), os.environ.get("CODEX_API_KEY", ""),
        ) if value)

    @classmethod
    def load(cls):
        config = {}
        if filename := os.environ.get("UNITY_WORKER_CONFIG"):
            with open(filename, "rb") as source:
                config = tomllib.load(source)

        def get(name, variable, default=None):
            return os.environ.get(variable, config.get(name, default))

        def positive(name, variable, default):
            value = int(get(name, variable, default))
            if value <= 0:
                raise ValueError(f"{variable} must be positive")
            return value

        token = os.environ["UNITY_WORKER_TOKEN"]
        approval = os.environ.get("UNITY_WORKER_APPROVAL_TOKEN", "")
        if len(token) < 32 or (approval and (len(approval) < 32 or approval == token)):
            raise ValueError("Worker and approval tokens must be distinct and at least 32 characters")
        if any(not value.isascii() or any(c.isspace() for c in value) for value in (token, approval) if value):
            raise ValueError("Bearer tokens must use ASCII without whitespace")
        project = Path(get("project", "UNITY_PROJECT")).resolve(strict=True)
        checkout = Path(get("git_root", "UNITY_GIT_ROOT", project)).resolve(strict=True)
        if not checkout.is_dir() or not project.is_relative_to(checkout):
            raise ValueError("Unity project must be inside its Git checkout")
        logs = Path(get("logs", "UNITY_WORKER_LOGS", Path(__file__).parent / "logs")).resolve()
        service = Path(__file__).resolve().parent
        if logs.is_relative_to(checkout) or service.is_relative_to(checkout):
            raise ValueError("Service and data directories must be outside the Git checkout")
        version = get("unity_version", "UNITY_VERSION", read_project_version(project))
        if version != read_project_version(project):
            raise ValueError("Configured Unity version does not match ProjectVersion.txt")
        remote = get("git_remote", "UNITY_GIT_REMOTE", "origin")
        branch = get("base_branch", "UNITY_BASE_BRANCH", "main")
        worker_id = get("worker_id", "UNITY_WORKER_ID", "worker-01")
        if not re.fullmatch(REMOTE_PATTERN, remote) or not re.fullmatch(REMOTE_PATTERN, worker_id):
            raise ValueError("Invalid configured Git remote name or worker ID")
        if not re.fullmatch(BRANCH_PATTERN, branch) or ".." in branch or branch.lower().startswith("ai/task-"):
            raise ValueError("Invalid configured base branch")
        sandbox = get("codex_sandbox", "UNITY_CODEX_SANDBOX", "mxc")
        if sandbox not in ("mxc", "elevated"):
            raise ValueError("Codex requires the mxc or elevated Windows sandbox")
        max_fixes = int(get("max_fixes", "UNITY_MAX_FIXES", 3))
        if not 0 <= max_fixes <= 20:
            raise ValueError("UNITY_MAX_FIXES must be between 0 and 20")
        modes = get("test_modes", "UNITY_TEST_MODES", ["editmode", "playmode"])
        if isinstance(modes, str):
            modes = modes.split(",")
        if not isinstance(modes, list) or not modes or set(modes) - {"editmode", "playmode"} or len(set(modes)) != len(modes):
            raise ValueError("Select editmode and/or playmode in UNITY_TEST_MODES")
        submodules = get("init_submodules", "UNITY_INIT_SUBMODULES", False)
        if str(submodules).lower() not in ("true", "false"):
            raise ValueError("UNITY_INIT_SUBMODULES must be true or false")
        codex_home = get("codex_home", "UNITY_CODEX_HOME")
        if codex_home:
            codex_home = Path(codex_home).resolve(strict=True)
            if codex_home.is_relative_to(checkout) or checkout.is_relative_to(codex_home):
                raise ValueError("Use a separate worker Codex home outside the Unity project")
        def relative_paths(key):
            paths = config.get(key, [])
            if (not isinstance(paths, list) or any(
                    not isinstance(path, str) or not path or not PurePosixPath(path).parts
                    or PurePosixPath(path).as_posix() != path
                    or any(part in ("..", "/") for part in PurePosixPath(path).parts)
                    or any(char in path for char in ("\\", ":", "\r", "\n"))
                    for path in paths)):
                raise ValueError(f"{key} must contain exact checkout-relative paths")
            return tuple(paths)

        allowed_outputs = relative_paths("allowed_tracked_outputs")
        ignored_cache_paths = relative_paths("ignored_cache_paths")
        assemblies = config.get("test_assemblies", {})
        if (not isinstance(assemblies, dict) or (assemblies and "editmode" not in assemblies)
                or any(mode not in modes or not isinstance(names, list) or not names
                       or any(not isinstance(name, str) or not re.fullmatch(ASSEMBLY_PATTERN, name)
                              or "assetvalidation" in name.casefold() for name in names)
                       for mode, names in assemblies.items())):
            raise ValueError("Configure valid scoped test assemblies by enabled mode")
        configured_git = get("git_exe", "UNITY_GIT_EXE", "git")
        unity_exe = executable(get("unity_exe", "UNITY_EXE"))
        logs.mkdir(parents=True, exist_ok=True)
        return cls(
            project=project, logs=logs, unity_exe=unity_exe, unity_version=version, token=token,
            approval_token=approval, worker_id=worker_id,
            unity_timeout=positive("unity_timeout", "UNITY_TIMEOUT_SECONDS", 1800),
            git_exe=executable(configured_git) if shutil.which(str(configured_git)) or configured_git != "git" else "git",
            git_remote=remote, base_branch=branch, git_timeout=positive("git_timeout", "UNITY_GIT_TIMEOUT", 600),
            init_submodules=str(submodules).lower() == "true",
            codex_exe=executable(get("codex_exe", "UNITY_CODEX_EXE"), required=False),
            codex_home=codex_home, codex_timeout=positive("codex_timeout", "UNITY_CODEX_TIMEOUT", 1800),
            codex_sandbox=sandbox, max_fixes=max_fixes, test_modes=tuple(modes),
            max_diff_bytes=positive("max_diff_bytes", "UNITY_MAX_DIFF_BYTES", 2_000_000),
            git_root=checkout, allowed_tracked_outputs=allowed_outputs, ignored_cache_paths=ignored_cache_paths,
            test_assemblies={mode: tuple(names) for mode, names in assemblies.items()},
        )
