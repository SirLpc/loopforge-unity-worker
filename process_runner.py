"""Run fixed argv with live log files and a verified process-tree lifetime."""

import ctypes
import os
import signal
import subprocess
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class StageError(Exception):
    def __init__(self, kind, message, exit_code=None, result=None):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code
        self.result = result or {}


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: Path
    stderr: Path


if os.name == "nt":
    import _winapi
    import msvcrt
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD),
        ]

    class JobLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("Times", ctypes.c_int64 * 4), ("TotalPageFaultCount", w.DWORD),
            ("TotalProcesses", w.DWORD), ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD),
        ]

    def win_function(name, result, *args):
        function = getattr(kernel, name)
        function.restype = result
        function.argtypes = args
        return function

    create_job = win_function("CreateJobObjectW", w.HANDLE, w.LPVOID, w.LPCWSTR)
    set_job = win_function("SetInformationJobObject", w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
    query_job = win_function("QueryInformationJobObject", w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID)
    assign_job = win_function("AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
    kill_job = win_function("TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
    resume_thread = win_function("ResumeThread", w.DWORD, w.HANDLE)
    create_mutex = win_function("CreateMutexW", w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)

    def checked(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    class WindowsProcessTree:
        def __init__(self):
            self.job = checked(create_job(None, None))
            self.process = None
            self.assigned = False
            limits = JobLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not set_job(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                _winapi.CloseHandle(self.job)
                raise error

        def start(self, argv, cwd, env, streams):
            handles = []
            try:
                for stream in streams:
                    handle = msvcrt.get_osfhandle(stream.fileno())
                    handles.append(_winapi.DuplicateHandle(
                        _winapi.GetCurrentProcess(), handle, _winapi.GetCurrentProcess(),
                        0, True, _winapi.DUPLICATE_SAME_ACCESS,
                    ))
                info = subprocess.STARTUPINFO(
                    dwFlags=subprocess.STARTF_USESTDHANDLES, hStdInput=handles[0],
                    hStdOutput=handles[1], hStdError=handles[2], lpAttributeList={"handle_list": handles},
                )
                self.process, thread, _, _ = _winapi.CreateProcess(
                    argv[0], subprocess.list2cmdline(argv), None, None, True,
                    0x4 | subprocess.CREATE_NO_WINDOW, env, str(cwd), info,  # CREATE_SUSPENDED
                )
                try:
                    checked(assign_job(self.job, self.process))
                    self.assigned = True
                    if resume_thread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    _winapi.CloseHandle(thread)
            finally:
                for handle in handles:
                    _winapi.CloseHandle(handle)

        def poll(self):
            if _winapi.WaitForSingleObject(self.process, 0) == _winapi.WAIT_OBJECT_0:
                return _winapi.GetExitCodeProcess(self.process)
            return None

        def empty(self):
            if self.process is None:
                return True
            if not self.assigned:
                return self.poll() is not None
            stats = Accounting()
            checked(query_job(self.job, 1, ctypes.byref(stats), ctypes.sizeof(stats), None))
            return stats.ActiveProcesses == 0 and self.poll() is not None

        def kill(self):
            if self.assigned:
                checked(kill_job(self.job, 1))
            else:
                _winapi.TerminateProcess(self.process, 1)

        def close(self):
            if self.process is not None:
                _winapi.CloseHandle(self.process)
            _winapi.CloseHandle(self.job)

else:
    class PosixProcessTree:
        def __init__(self):
            self.process = None

        def start(self, argv, cwd, env, streams):
            self.process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=streams[0], stdout=streams[1], stderr=streams[2],
                start_new_session=True,
            )

        def poll(self):
            return self.process.poll()

        def empty(self):
            if self.process is None:
                return True
            self.poll()
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return True
            return False

        def kill(self):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def close(self):
            if self.process is not None:
                self.process.poll()


@contextmanager
def reserve_project(project):
    if os.name != "nt":
        yield
        return
    key = sha256(os.path.normcase(str(project)).encode()).hexdigest()
    ctypes.set_last_error(0)
    handle = checked(create_mutex(None, False, "Global\\UnityWorker-" + key))
    existing = ctypes.get_last_error() == 183
    try:
        if existing:
            raise RuntimeError("A Unity Worker is already serving this project")
        yield
    finally:
        _winapi.CloseHandle(handle)


class ProcessRunner:
    def run(self, argv, cwd, prefix, *, timeout, cancel=None, data=None, env=None, max_output=None):
        if cancel is not None and cancel.is_set():
            raise StageError("cancelled", "Cancellation requested")
        out = prefix.with_name(prefix.name + ".stdout.log")
        err = prefix.with_name(prefix.name + ".stderr.log")
        infile = prefix.with_name(prefix.name + ".stdin")
        tree = WindowsProcessTree() if os.name == "nt" else PosixProcessTree()
        try:
            if data is not None:
                with infile.open("xb") as target:
                    target.write(data)
            with open(infile if data is not None else os.devnull, "rb") as stdin:
                with out.open("xb") as stdout, err.open("xb") as stderr:
                    tree.start(argv, cwd, env, (stdin, stdout, stderr))
                    deadline = time.monotonic() + timeout
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise StageError("cancelled", "Cancellation requested")
                        if max_output is not None and out.stat().st_size > max_output:
                            raise StageError("output_limit", "Command output exceeded the configured limit")
                        code = tree.poll()
                        if code is not None:
                            return ProcessResult(code, out, err)
                        if time.monotonic() >= deadline:
                            raise StageError("timeout", "Process exceeded its time limit")
                        time.sleep(0.05)
        finally:
            try:
                self._confirm_stopped(tree)
            finally:
                # Cleanup can fail while an unverified process still holds a file handle.
                with suppress(OSError):
                    tree.close()
                if data is not None:
                    with suppress(OSError):
                        infile.unlink(missing_ok=True)

    @staticmethod
    def _confirm_stopped(tree):
        try:
            if tree.empty():
                return
            tree.kill()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if tree.empty():
                    return
                time.sleep(0.05)
        except OSError as error:
            raise StageError("recovery_required", "Could not verify that the process tree has stopped") from error
        raise StageError("recovery_required", "Process tree did not stop")
