"""Unity startup, compilation evidence and Unity Test Framework report validation."""

import ctypes
import json
import os
import re
import stat
from contextlib import nullcontext, suppress
from xml.etree import ElementTree

from process_runner import StageError
from worker_config import PROJECT_VERSION, VERSION_PATTERN, read_project_version, system_env


ERRORS = {
    "license_failed": re.compile(
        r"no valid unity editor license|no (?:unity )?licen[cs]e found|failed to (?:activate|acquire|update|fetch|connect to).*licen[cs]"
        r"|licen[cs](?:e|ing).*(?:is invalid|has expired|verification failed|is not active|could not be found)"
        r"|failed to connect to local license client|entitlement check failed", re.I,
    ),
    "package_failed": re.compile(
        r"failed to (?:resolve|initialize|start|connect to).*package|error resolving packages"
        r"|an error occurred while resolving packages|package manager.*(?:failed|could not be started)", re.I,
    ),
    "compile_failed": re.compile(
        r"error\s+CS\d{4,}|scripts? (?:have|has) compiler? errors?|script compilation failed|compiler errors in scripts", re.I,
    ),
    "startup_failed": re.compile(
        r"another unity instance is running|it looks like another unity instance|project is already open|fatal error in unity"
        r"|cannot open project|crash!!!|aborting batchmode due to failure", re.I,
    ),
}
VERSION_LINE = re.compile(rf"Initialize engine version:\s*({VERSION_PATTERN})")
IMPORT_EVIDENCE = re.compile(r"Asset Pipeline Refresh|AssetDatabase: script compilation time|Refresh completed", re.I)
EXIT_EVIDENCE = re.compile(r"Exiting batchmode successfully now|Batchmode quit successfully", re.I)


def classify_editor_log(text):
    return next((name for name, pattern in ERRORS.items() if pattern.search(text)), None)


def inspect_log(path):
    errors, version, imported, exited = set(), None, False, False
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                errors.update(name for name, pattern in ERRORS.items() if pattern.search(line))
                if match := VERSION_LINE.search(line):
                    version = match[1]
                imported |= bool(IMPORT_EVIDENCE.search(line))
                exited |= bool(EXIT_EVIDENCE.search(line))
    return next((name for name in ERRORS if name in errors), None), version, imported, exited


def unity_product_version(executable):
    if os.name != "nt":
        return None
    from ctypes import wintypes as w

    version = ctypes.WinDLL("version", use_last_error=True)
    size_of = version.GetFileVersionInfoSizeW
    size_of.argtypes = (w.LPCWSTR, w.LPVOID)
    size_of.restype = w.DWORD
    read = version.GetFileVersionInfoW
    read.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID)
    query = version.VerQueryValueW
    query.argtypes = (w.LPVOID, w.LPCWSTR, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.UINT))
    size = size_of(executable, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not read(executable, 0, size, data):
        return None
    value, length = w.LPVOID(), w.UINT()
    if not query(data, r"\VarFileInfo\Translation", ctypes.byref(value), ctypes.byref(length)):
        return None
    translations = ctypes.cast(value, ctypes.POINTER(w.WORD))
    for index in range(0, length.value // 2 - 1, 2):
        key = "\\StringFileInfo\\%04x%04x\\ProductVersion" % (translations[index], translations[index + 1])
        if query(data, key, ctypes.byref(value), ctypes.byref(length)):
            match = re.match(VERSION_PATTERN, ctypes.wstring_at(value))
            if match:
                return match[0]
    return None


def assert_unity_closed(project):
    if any((project / marker).exists() for marker in ("Temp/UnityLockfile", "Library/UnityLockfile")):
        raise StageError("workspace_in_use", "Unity project lock exists. Close the Editor or verify the stale lock.")


def clear_exited_batch_lock(project):
    """Remove an empty Windows Temp marker after our batch process tree has exited."""
    if os.name != "nt":
        return
    import _winapi
    import msvcrt
    from ctypes import wintypes as w

    from process_runner import win_function

    marker = project / "Temp/UnityLockfile"
    delete_access = 0x10000
    open_reparse_point = 0x200000
    file_disposition_info = 4
    with suppress(OSError):
        if marker.parent.resolve(strict=True) != marker.parent:
            return
        # Zero sharing excludes active editors. Validate and unlink through the
        # same handle so a different editor cannot take the lock between them.
        handle = _winapi.CreateFile(str(marker), _winapi.GENERIC_READ | delete_access, 0, 0,
                                   _winapi.OPEN_EXISTING, open_reparse_point, 0)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except OSError:
            _winapi.CloseHandle(handle)
            raise
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size or info.st_nlink != 1
                    or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return
            set_info = win_function("SetFileInformationByHandle", w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
            delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile
            if not set_info(handle, file_disposition_info, ctypes.byref(delete), ctypes.sizeof(delete)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            os.close(fd)


def has_test_framework(project):
    name = "com.unity.test-framework"
    packages = project / "Packages"
    if (packages / name / "package.json").is_file():
        return True
    for filename in ("manifest.json", "packages-lock.json"):
        try:
            manifest = json.loads((packages / filename).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("dependencies"), dict):
            if name in manifest["dependencies"]:
                return True
    return False


def fixture_pattern(name):
    return rf"^{re.escape(name)}(?:\([^\r\n]*\))?\.[A-Za-z_][A-Za-z0-9_]*(?:\([^\r\n]*\))?$"


def check_test_scope(root, scope):
    expected = scope["assembly"] + ".dll"
    expected_fixtures = set(scope["fixtures"])
    executed = set()
    covered = set()
    for suite in root.iter("test-suite"):
        if suite.get("type") != "Assembly" or not list(suite.iter("test-case")):
            continue
        actual = suite.get("name", "").replace(chr(92), "/").rsplit("/", 1)[-1]
        if actual != expected:
            raise StageError("test_execution_failed", "Unity executed tests from an unrequested assembly")
        for case in suite.iter("test-case"):
            fixture = case.get("classname", "").split("(", 1)[0]
            fullname = case.get("fullname", "")
            categories = {prop.get("value", "").casefold() for prop in case.findall("properties/property")
                          if prop.get("name", "").casefold() == "category"}
            if (fixture not in expected_fixtures or not re.fullmatch(fixture_pattern(fixture), fullname)
                    or "gameassetvalidation" in categories):
                raise StageError("test_execution_failed", "Unity executed tests outside the approved fixture scope")
            covered.add(case)
            if case.get("result") in {"Passed", "Failed"}:
                executed.add(fixture)
    if covered != set(root.iter("test-case")):
        raise StageError("test_execution_failed", "Test report is missing assembly evidence")
    if executed != expected_fixtures:
        raise StageError("not_run", "One or more approved test fixtures did not execute")


def read_test_results(path, scope=None):
    invalid = "Unity Test Framework XML is missing, invalid or inconsistent"
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("XML too large")
        data = path.read_bytes()
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I) or b"\0" in data:
            raise ValueError("Unsupported XML encoding or declaration")
        root = ElementTree.fromstring(data)
        if root.tag != "test-run":
            raise ValueError("Not a test run")
        counts = {name: int(root.get(name, "0")) for name in ("total", "passed", "failed", "skipped", "inconclusive")}
        cases = list(root.iter("test-case"))
        results = [case.get("result") for case in cases]
        outcome = root.get("result")
        if (any(value < 0 for value in counts.values()) or counts["total"] != len(cases)
                or any(result not in {"Passed", "Failed", "Skipped", "Inconclusive"} for result in results)
                or any(results.count(name.title()) != counts[name] for name in counts if name != "total")):
            raise ValueError("Inconsistent counts")
    except (OSError, ElementTree.ParseError, ValueError) as error:
        raise StageError("test_execution_failed", invalid) from error
    for node in [root, *root.iter("test-suite"), *cases]:
        label = node.get("label")
        if label not in {"Error", "Invalid", "Cancelled", "NotRunnable"}:
            continue
        # NUnit uses Error on individual cases that threw in the test or setup.
        if label == "Error" and node.tag == "test-case" and node.get("result") == "Failed" and node.find("failure") is not None:
            continue
        raise StageError("test_execution_failed", "Unity reported a test execution error", result=counts)
    if scope:
        check_test_scope(root, scope)
    if counts["failed"]:
        failed_cases = [case for case in cases if case.get("result") == "Failed"]
        failures = [
            {"name": case.get("fullname", case.get("name", ""))[:512],
             "message": case.findtext("failure/message", "").strip()[:1024],
             "stack": case.findtext("failure/stack-trace", "").strip()[:1800]}
            for case in failed_cases[:3]
        ]
        raise StageError("tests_failed", "Unity test cases failed", result={**counts, "failed_tests": failures})
    if not counts["passed"]:
        raise StageError("not_run", "No Unity tests passed or failed", result=counts)
    if outcome != "Passed" or counts["inconclusive"]:
        raise StageError("test_execution_failed", "Unity did not report a conclusive passing test run", result=counts)
    return counts


class UnityExecutor:
    def __init__(self, settings, *, permit=None):
        self.settings = settings
        self.permit = permit

    def validate(self):
        cfg = self.settings
        try:
            project_file = cfg.project / PROJECT_VERSION
            if project_file.resolve(strict=True) != project_file:
                raise ValueError("Project version file uses a link")
            project_version = read_project_version(cfg.project)
        except (OSError, ValueError) as error:
            raise StageError("unsupported", "Cannot determine the Unity project version") from error
        if project_version != cfg.unity_version:
            raise StageError("unsupported", "ProjectVersion.txt does not match the configured Unity version")
        installed = unity_product_version(cfg.unity_exe)
        if os.name == "nt" and installed != project_version:
            raise StageError("unsupported", "Unity.exe product version does not match ProjectVersion.txt")

    def run(self, ctx, mode, legacy_log=None, *, scope=None):
        cfg = self.settings
        if mode != "startup_smoke":
            self.validate()
        assert_unity_closed(cfg.project)
        if mode in ("editmode", "playmode") and not has_test_framework(cfg.project):
            raise StageError("unsupported", "Unity Test Framework is missing from the project's packages",
                             result={"mode": mode, "status": "unsupported"})
        log = legacy_log or ctx.artifact(mode, ".log", "log")
        argv = [cfg.unity_exe, "-batchmode", "-nographics", "-projectPath", str(cfg.project), "-logFile", str(log)]
        xml = None
        if mode in ("editmode", "playmode"):
            xml = ctx.artifact(mode, ".xml", "test_report")
            argv += ["-runTests", "-testPlatform", mode, "-testResults", str(xml)]
            allowed = getattr(cfg, "test_assemblies", {})
            if allowed and not scope:
                raise StageError("unsupported", "Formal Unity tests require an approved fixture scope")
            if scope:
                fixtures = ";".join(fixture_pattern(name) for name in scope["fixtures"])
                argv += ["-assemblyNames", scope["assembly"], "-testFilter", fixtures, "-testCategory", "!GameAssetValidation"]
        else:
            argv += ["-quit"]
        with self.permit(ctx) if self.permit else nullcontext():
            assert_unity_closed(cfg.project)
            process = ctx.run(argv, mode, timeout=cfg.unity_timeout, env=system_env(), publish=True)
            # ProcessRunner returns only after verifying that its process tree has stopped.
            clear_exited_batch_lock(cfg.project)
        if mode == "startup_smoke":
            if process.exit_code:
                raise StageError("startup_failed", "Unity smoke failed", process.exit_code)
            return {"exit_code": 0}
        failure, version, imported, exited = inspect_log(log)
        report = {"mode": mode, "unity_version": version, "exit_code": process.exit_code, "log": log.name}
        if failure:
            raise StageError(failure, f"Unity {mode} reported {failure}", process.exit_code, report)
        if version != cfg.unity_version:
            raise StageError("startup_failed", "Unity did not confirm the expected engine version", process.exit_code, report)
        if xml:
            report["test_results"] = xml.name
            try:
                report.update(read_test_results(xml, scope))
            except StageError as error:
                error.exit_code = process.exit_code
                error.result = {**report, **error.result}
                raise
        if process.exit_code or (not xml and not (imported and exited)):
            kind = "test_execution_failed" if xml else "startup_failed"
            raise StageError(kind, f"Unity did not complete {mode} successfully", process.exit_code, report)
        return report
