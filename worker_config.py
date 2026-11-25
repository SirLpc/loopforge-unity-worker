"""Worker-only settings. The HTTP API never selects a tool, path or remote."""

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace


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
    expected_remote_identity: str | None = None
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
    def load(cls, *, config=None, environ=None, defer_paths=False):
        config = config if config is not None else config_document()
        environ = os.environ if environ is None else environ

        def get(name, variable, default=None):
            return environ.get(variable, config.get(name, default))

        def positive(name, variable, default):
            value = int(get(name, variable, default))
            if value <= 0:
                raise ValueError(f"{variable} must be positive")
            return value

        token = environ["UNITY_WORKER_TOKEN"]
        approval = environ.get("UNITY_WORKER_APPROVAL_TOKEN", "")
        if len(token) < 32 or (approval and (len(approval) < 32 or approval == token)):
            raise ValueError("Worker and approval tokens must be distinct and at least 32 characters")
        if any(not value.isascii() or any(c.isspace() for c in value) for value in (token, approval) if value):
            raise ValueError("Bearer tokens must use ASCII without whitespace")
        project = Path(get("project", "UNITY_PROJECT")).resolve(strict=not defer_paths)
        checkout = Path(get("git_root", "UNITY_GIT_ROOT", project)).resolve(strict=not defer_paths)
        if not project.is_relative_to(checkout) or (not defer_paths and not checkout.is_dir()):
            raise ValueError("Unity project must be inside its Git checkout")
        logs = Path(get("logs", "UNITY_WORKER_LOGS", Path(__file__).parent / "logs")).resolve()
        service = Path(__file__).resolve().parent
        if logs.is_relative_to(checkout) or service.is_relative_to(checkout):
            raise ValueError("Service and data directories must be outside the Git checkout")
        version = get("unity_version", "UNITY_VERSION")
        if not defer_paths and version is None:
            version = read_project_version(project)
        if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
            raise ValueError("Configure a Unity Editor version")
        if not defer_paths and version != read_project_version(project):
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
            codex_home = Path(codex_home).resolve(strict=not defer_paths)
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
            git_root=checkout, expected_remote_identity=config.get("remote_identity"), allowed_tracked_outputs=allowed_outputs, ignored_cache_paths=ignored_cache_paths,
            test_assemblies={mode: tuple(names) for mode, names in assemblies.items()},
        )


def config_document():
    if filename := os.environ.get("UNITY_WORKER_CONFIG"):
        with open(filename, "rb") as source:
            return tomllib.load(source)
    return {}


@dataclass(frozen=True)
class SlotSettings:
    id: str
    pool_id: str
    enabled: bool
    settings: Settings


@dataclass(frozen=True)
class PoolSettings:
    id: str
    remote_identity: str | None
    slots: tuple[SlotSettings, ...]
    pr: dict[str, str] | None = None

    @property
    def policy(self) -> Settings:
        return self.slots[0].settings


@dataclass(frozen=True)
class ServiceSettings:
    default_pool: str
    legacy_slot: str
    pools: dict[str, PoolSettings]
    max_parallel_unity: int = 1
    implicit: bool = False

    @property
    def slots(self) -> dict[str, SlotSettings]:
        return {slot.id: slot for pool in self.pools.values() for slot in pool.slots}

    @property
    def legacy(self) -> Settings:
        return self.slots[self.legacy_slot].settings

    @property
    def worker_id(self) -> str:
        return self.legacy.worker_id

    @property
    def logs(self) -> Path:
        return self.legacy.logs

    @property
    def secrets(self) -> tuple[str, ...]:
        pool_secrets = (os.environ.get(pool.pr["token_env"]) for pool in self.pools.values() if pool.pr)
        return (*self.legacy.secrets, *(secret for secret in pool_secrets if secret))


def legacy_service(settings: Settings, max_parallel_unity: int = 1) -> ServiceSettings:
    pool_id, slot_id = settings.worker_id, "worker-01"
    slot = SlotSettings(slot_id, pool_id, True, settings)
    return ServiceSettings(pool_id, slot_id, {pool_id: PoolSettings(pool_id, None, (slot,))},
                           max_parallel_unity, implicit=True)


def overlaps(left: Path, right: Path) -> bool:
    if left.is_relative_to(right) or right.is_relative_to(left):
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


def pool_pr(data):
    if data is None:
        return None
    if (not isinstance(data, dict) or set(data) != {"provider", "api_url", "repository", "token_env"}
            or any(not isinstance(value, str) or not value for value in data.values())
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data["token_env"])):
        raise ValueError("Provide provider, api_url, repository and token_env for pool PR/MR delivery")
    return data


def load_service_settings(*, config=None) -> ServiceSettings:
    config = config if config is not None else config_document()
    parallel = config.get("max_parallel_unity", 1)
    if type(parallel) is not int or parallel < 1:
        raise ValueError("max_parallel_unity must be a positive integer")
    if "pools" not in config:
        return legacy_service(Settings.load(config=config), parallel)

    configured_pools = config["pools"]
    if not isinstance(configured_pools, dict) or not configured_pools:
        raise ValueError("Configure at least one named pool")
    path_fields = {"git_root", "project", "codex_home"}
    legacy_overrides = {"UNITY_GIT_ROOT", "UNITY_PROJECT"}
    if path_fields & config.keys() or legacy_overrides & os.environ.keys():
        raise ValueError("Configure project, git_root and codex_home per slot in pool mode")
    pool_overrides = {"UNITY_VERSION", "UNITY_GIT_REMOTE", "UNITY_BASE_BRANCH", "UNITY_CODEX_HOME", "UNITY_TEST_MODES"}
    environ = {key: value for key, value in os.environ.items() if key not in pool_overrides}
    common = {key: value for key, value in config.items() if key != "pools"}
    pools = {}
    slots = {}
    checkouts = []
    codex_homes = []
    for pool_id, definition in configured_pools.items():
        if not re.fullmatch(REMOTE_PATTERN, pool_id) or not isinstance(definition, dict):
            raise ValueError("Invalid pool ID or definition")
        remote = definition.get("remote_identity", "")
        if not isinstance(remote, str) or not re.fullmatch(r"[a-f0-9]{64}", remote):
            raise ValueError("Configure the pool's expected Git remote identity")
        entries = definition.get("slots")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Configure at least one slot for each pool")
        policy = {key: value for key, value in definition.items() if key not in ("slots", "pr")}
        if path_fields & policy.keys() or {"logs", "worker_id"} & policy.keys():
            raise ValueError("Pool options must use service state and per-slot paths")
        pool_slots = []
        for entry in entries:
            if (not isinstance(entry, dict) or not {"id", "git_root", "project"} <= entry.keys()
                    or entry.keys() - (path_fields | {"id", "enabled"})):
                raise ValueError("Each slot requires id, git_root and project")
            slot_id, enabled = entry["id"], entry.get("enabled", True)
            if (not isinstance(slot_id, str) or not re.fullmatch(REMOTE_PATTERN, slot_id)
                    or slot_id.casefold() in slots or type(enabled) is not bool):
                raise ValueError("Invalid or duplicate slot ID or enabled flag")
            if any(not isinstance(entry[key], str) or not Path(entry[key]).is_absolute() for key in path_fields & entry.keys()):
                raise ValueError("Slot paths must be absolute")
            settings = Settings.load(config={**common, **policy, **entry}, environ=environ, defer_paths=True)
            if any(overlaps(settings.checkout, target) for target in (*checkouts, settings.logs)):
                raise ValueError("Slot checkouts, service and state must not overlap")
            if settings.codex_exe and not settings.codex_home:
                raise ValueError("Configure a dedicated Codex home for each slot")
            if settings.codex_home:
                if any(overlaps(settings.codex_home, target) for target in (*codex_homes, settings.logs)):
                    raise ValueError("Each slot requires a separate Codex home outside service state")
                codex_homes.append(settings.codex_home)
            checkouts.append(settings.checkout)
            slot = SlotSettings(slot_id, pool_id, enabled, settings)
            slots[slot_id.casefold()] = slot
            pool_slots.append(slot)
        templates = {slot.settings.project.relative_to(slot.settings.checkout) for slot in pool_slots}
        if len(templates) != 1:
            raise ValueError("Slots in a pool must use the same relative Unity project path")
        pools[pool_id] = PoolSettings(pool_id, remote, tuple(pool_slots), pool_pr(definition.get("pr")))
    if any(overlaps(home, checkout) for home in codex_homes for checkout in checkouts):
        raise ValueError("Codex homes must stay outside all slot checkouts")
    default_pool, default_slot = config.get("default_pool"), config.get("legacy_slot")
    if not isinstance(default_pool, str) or default_pool not in pools or not any(
            slot.id == default_slot for slot in pools[default_pool].slots):
        raise ValueError("Set legacy_slot to a slot in default_pool")
    return ServiceSettings(default_pool, default_slot, pools, parallel)


def check_slot(slot: SlotSettings, *, leased: bool) -> str | None:
    from git_workspace import GitWorkspace
    from process_runner import ProcessRunner, StageError
    from unity_executor import UnityExecutor

    cfg = slot.settings
    try:
        if (not cfg.project.is_dir() or not cfg.checkout.is_dir()
                or cfg.checkout.resolve(strict=True) != cfg.checkout or cfg.project.resolve(strict=True) != cfg.project):
            return "workspace_missing"
        protected = [cfg.checkout / ".git", *(cfg.checkout / name for name in cfg.ignored_cache_paths)]
        protected += [cfg.project / name for name in ("Assets", "Packages", "ProjectSettings", "Library", "Temp", "Logs", "Obj", "UserSettings")]
        if any(path.resolve() != path for path in protected):
            return "workspace_link"
        if cfg.codex_home is not None and not cfg.codex_home.is_dir():
            return "codex_home_missing"
        UnityExecutor(cfg).validate()
        workspace = GitWorkspace(cfg)
        runner = ProcessRunner()
        with TemporaryDirectory(prefix="slot-probe-", dir=cfg.logs) as temporary:
            directory = Path(temporary)
            def run(argv, label, **kwargs):
                return runner.run(argv, prefix=directory / label, **kwargs)
            ctx = SimpleNamespace(run=run, directory=directory)
            workspace.validate_checkout_identity(ctx)
            remote = workspace.remote_identity(ctx)
            if cfg.expected_remote_identity is not None and remote != cfg.expected_remote_identity:
                return "remote_mismatch"
            if not leased:
                workspace.clean(ctx)
    except StageError as error:
        return error.kind
    except (ValueError, OSError):
        return "workspace_unavailable"
    return None
