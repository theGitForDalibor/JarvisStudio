"""Platform-specific isolated process launchers and tree termination primitives.

On Windows (win32 / ProactorEventLoop):
    Avoids the Spawn-to-Assign race in stdlib `asyncio.create_subprocess_exec` by
    using a custom Win32 `CreateProcessW` launcher with:
    1. `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` whitelist (only stdin/stdout/stderr handles inherited)
    2. `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT`
    3. `AssignProcessToJobObject` (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, no Breakaway) BEFORE `ResumeThread`
    4. Fail-Closed termination & full handle cleanup if Job assignment or `ResumeThread` fails
    5. IOCP overlapped named pipe attachment into `ProactorEventLoop`

On POSIX (Linux / macOS):
    Uses `asyncio.create_subprocess_exec(..., start_new_session=True)` and process-group
    termination via `os.killpg` (`SIGTERM` -> grace period -> `SIGKILL`).
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from contextlib import suppress
from ctypes import wintypes
from typing import Any

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.process.models import (
    ProcessExecutionSpec,
    ProcessIsolationError,
    ProcessLaunchError,
)


class SpawnedProcess(ABC):
    """Uniform handle to an OS-isolated running process and its async output streams."""

    @property
    @abstractmethod
    def pid(self) -> int:
        """OS process ID of the root spawned process."""

    @property
    @abstractmethod
    def stdout(self) -> asyncio.StreamReader:
        """Async stdout stream reader."""

    @property
    @abstractmethod
    def stderr(self) -> asyncio.StreamReader:
        """Async stderr stream reader."""

    @property
    @abstractmethod
    def returncode(self) -> int | None:
        """Process exit code if known, else None."""

    @abstractmethod
    async def wait_exit(self) -> int:
        """Await OS exit of the root process and return its exit code."""

    @abstractmethod
    async def terminate_tree(self, graceful_timeout: float = 2.0) -> None:
        """Deterministically terminate the entire spawned process tree (idempotent)."""

    @abstractmethod
    async def close(self) -> None:
        """Close all OS handles, transports, and Job/wait objects (idempotent)."""


# ---------------------------------------------------------------------------
# POSIX Implementation
# ---------------------------------------------------------------------------


def _posix_group_alive(pgid: int) -> bool:
    """Return True if any process in `pgid` still exists (even zombie)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process group still has members we lack permission to signal directly
        return True
    return True


class PosixSpawnedProcess(SpawnedProcess):
    """POSIX process tree wrapper anchored by setsid() (`start_new_session=True`)."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None or proc.stderr is None:
            raise ProcessLaunchError(
                "Spawned POSIX process missing stdout/stderr pipes."
            )
        self._proc = proc
        self._pgid = proc.pid
        self._closed = False
        self._exited_event = asyncio.Event()
        self._has_exit_hook = False

        # CPython BaseSubprocessTransport.wait() does not resolve until all pipe FDs hit EOF.
        # Hooking protocol.process_exited lets wait_exit() resolve as soon as the root PID exits
        # even if a detached descendant still holds stdout/stderr open.
        transport = getattr(proc, "_transport", None)
        protocol = getattr(transport, "_protocol", None)
        orig_exited = getattr(protocol, "process_exited", None)
        if callable(orig_exited):
            self._has_exit_hook = True
            exited_event = self._exited_event

            def _on_process_exited() -> None:
                try:
                    orig_exited()
                finally:
                    exited_event.set()

            protocol.process_exited = _on_process_exited

        if proc.returncode is not None or getattr(protocol, "_process_exited", False):
            self._exited_event.set()

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def pgid(self) -> int:
        return self._pgid

    @property
    def stdout(self) -> asyncio.StreamReader:
        assert self._proc.stdout is not None
        return self._proc.stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        assert self._proc.stderr is not None
        return self._proc.stderr

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait_exit(self) -> int:
        if self._proc.returncode is not None:
            return self._proc.returncode
        if self._has_exit_hook:
            await self._exited_event.wait()
            if self._proc.returncode is not None:
                return self._proc.returncode
        return await self._proc.wait()

    async def terminate_tree(self, graceful_timeout: float = 2.0) -> None:
        if self._closed:
            return

        # Even if leader already exited (`returncode is not None`), descendants may still survive in `pgid`.
        try:
            os.killpg(self._pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            logger.warning(
                f"PermissionError signaling SIGTERM to process group {self._pgid}"
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + graceful_timeout
        while loop.time() < deadline:
            if not _posix_group_alive(self._pgid):
                return
            await asyncio.sleep(0.01)

        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pgid, signal.SIGKILL)

        # Brief bounded wait for process group disappearance after SIGKILL
        reap_deadline = loop.time() + 1.0
        while loop.time() < reap_deadline:
            if not _posix_group_alive(self._pgid):
                break
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        transport = getattr(self._proc, "_transport", None)
        close_fn = getattr(transport, "close", None)
        if callable(close_fn):
            with suppress(OSError, PermissionError):
                close_fn()
        await asyncio.sleep(0)


async def _spawn_posix_process(spec: ProcessExecutionSpec) -> PosixSpawnedProcess:
    cwd_str = str(spec.cwd) if spec.cwd is not None else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd_str,
            env=spec.env,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProcessLaunchError(
            f"Failed to launch POSIX process {spec.argv[0]!r}: {exc}"
        ) from exc

    return PosixSpawnedProcess(proc)


# ---------------------------------------------------------------------------
# Windows Implementation (ctypes CreateProcessW + Job Object + Proactor Pipes)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    from asyncio import windows_utils

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _STARTF_USESHOWWINDOW = 0x00000001
    _STARTF_USESTDHANDLES = 0x00000100
    _SW_HIDE = 0
    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _CREATE_NO_WINDOW = 0x08000000
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _HANDLE_FLAG_INHERIT = 0x00000001

    _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS = 9
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _STILL_ACTIVE = 259
    _WAIT_TIMEOUT_MS = 2000
    _RESUME_THREAD_FAILED = 0xFFFFFFFF

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _build_kernel32() -> ctypes.WinDLL:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE

        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryInformationJobObject.restype = wintypes.BOOL

        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL

        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL

        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateJobObject.restype = wintypes.BOOL

        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE

        k32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        k32.SetHandleInformation.restype = wintypes.BOOL

        k32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

        k32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        k32.UpdateProcThreadAttribute.restype = wintypes.BOOL

        k32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
        k32.DeleteProcThreadAttributeList.restype = None

        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        k32.CreateProcessW.restype = wintypes.BOOL

        k32.ResumeThread.argtypes = [wintypes.HANDLE]
        k32.ResumeThread.restype = wintypes.DWORD

        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL

        k32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetExitCodeProcess.restype = wintypes.BOOL

        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD

        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        return k32

    _K32 = _build_kernel32()

    def _safe_close_handle(handle: int | None) -> None:
        """Close a Win32 handle safely if valid and non-null."""
        if handle and handle != _INVALID_HANDLE_VALUE:
            _K32.CloseHandle(handle)

    def _create_isolated_job_object() -> int:
        """Create an anonymous Job Object configured with KILL_ON_JOB_CLOSE and NO breakaway."""
        job_handle = _K32.CreateJobObjectW(None, None)
        if not job_handle or job_handle == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise ProcessIsolationError(
                f"CreateJobObjectW failed (winerror={err}); refusing unmanaged process launch."
            )

        try:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ret_len = wintypes.DWORD()
            if not _K32.QueryInformationJobObject(
                job_handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(ret_len),
            ):
                err = ctypes.get_last_error()
                raise ProcessIsolationError(
                    f"QueryInformationJobObject failed (winerror={err})."
                )

            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            info.BasicLimitInformation.LimitFlags &= ~(
                _JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            )

            if not _K32.SetInformationJobObject(
                job_handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                err = ctypes.get_last_error()
                raise ProcessIsolationError(
                    f"SetInformationJobObject(KILL_ON_JOB_CLOSE) failed (winerror={err})."
                )
            return int(job_handle)
        except BaseException:
            _safe_close_handle(job_handle)
            raise

    def _encode_win32_env_block(env: dict[str, str]) -> Any:
        """Encode environment mapping into a UTF-16LE double-NUL-terminated block."""
        pairs: list[str] = []
        for k, v in env.items():
            if "\x00" in k or "\x00" in v:
                raise ValueError("Environment keys/values cannot contain NUL bytes.")
            pairs.append(f"{k}={v}")
        block = "\x00".join(pairs) + "\x00\x00"
        if not pairs:
            block = "\x00\x00"
        return ctypes.create_unicode_buffer(block)

    def _query_process_exit_code(h_process: int) -> int | None:
        code = wintypes.DWORD()
        if not _K32.GetExitCodeProcess(h_process, ctypes.byref(code)):
            return None
        if code.value == _STILL_ACTIVE:
            # Verify via WaitForSingleObject(0) in case process legitimately exited with 259
            wait_rc = _K32.WaitForSingleObject(h_process, 0)
            if wait_rc != 0:
                return None
        # Normalize unsigned 32-bit DWORD exit code to signed Python int for standard parity if > 0x7FFFFFFF?
        # Keep standard Python subprocess behavior (unsigned DWORD 0..0xFFFFFFFF).
        return int(code.value)

    class WindowsSpawnedProcess(SpawnedProcess):
        """Win32 Job-isolated process wired to ProactorEventLoop IOCP pipe readers."""

        def __init__(
            self,
            *,
            pid: int,
            h_process: int,
            job_handle: int,
            stdout_reader: asyncio.StreamReader,
            stderr_reader: asyncio.StreamReader,
            stdout_transport: asyncio.ReadTransport,
            stderr_transport: asyncio.ReadTransport,
            loop: asyncio.AbstractEventLoop,
        ) -> None:
            self._pid = pid
            self._h_process: int | None = h_process
            self._job_handle: int | None = job_handle
            self._stdout = stdout_reader
            self._stderr = stderr_reader
            self._stdout_transport: asyncio.ReadTransport | None = stdout_transport
            self._stderr_transport: asyncio.ReadTransport | None = stderr_transport
            self._loop = loop
            self._returncode: int | None = None
            self._wait_fut: asyncio.Future[Any] | None = None
            self._wait_lock = asyncio.Lock()
            self._closed = False

        @property
        def pid(self) -> int:
            return self._pid

        @property
        def stdout(self) -> asyncio.StreamReader:
            return self._stdout

        @property
        def stderr(self) -> asyncio.StreamReader:
            return self._stderr

        @property
        def returncode(self) -> int | None:
            if self._returncode is not None:
                return self._returncode
            if self._h_process:
                self._returncode = _query_process_exit_code(self._h_process)
            return self._returncode

        async def wait_exit(self) -> int:
            if self._returncode is not None:
                return self._returncode
            if not self._h_process:
                raise ProcessLaunchError(
                    "Cannot wait on process: hProcess already closed."
                )

            # Fast path if already exited
            rc = _query_process_exit_code(self._h_process)
            if rc is not None:
                self._returncode = rc
                return rc

            async with self._wait_lock:
                if self._returncode is not None:
                    return self._returncode
                if not self._h_process:
                    raise ProcessLaunchError(
                        "Cannot wait on process: hProcess already closed."
                    )

                proactor = getattr(self._loop, "_proactor", None)
                if proactor is None:
                    raise ProcessLaunchError("Proactor missing on Windows event loop.")

                fut = self._wait_fut
                if fut is None or fut.done():
                    fut = proactor.wait_for_handle(self._h_process)
                    self._wait_fut = fut

            await asyncio.shield(fut)
            if self._h_process:
                rc = _query_process_exit_code(self._h_process)
                if rc is not None:
                    self._returncode = rc
            if self._returncode is None and self._h_process:
                # Re-wait synchronously max brief check if handle just signaled.
                _K32.WaitForSingleObject(self._h_process, 50)
                self._returncode = _query_process_exit_code(self._h_process)
            return self._returncode if self._returncode is not None else -1

        async def terminate_tree(self, graceful_timeout: float = 2.0) -> None:
            if self._closed:
                return

            if self._job_handle:
                if not _K32.TerminateJobObject(self._job_handle, 1):
                    err = ctypes.get_last_error()
                    logger.warning(
                        f"TerminateJobObject failed for pid={self._pid} (winerror={err})"
                    )
                _safe_close_handle(self._job_handle)
                self._job_handle = None

            if self._h_process and _query_process_exit_code(self._h_process) is None:
                _K32.TerminateProcess(self._h_process, 1)

            # Bounded wait so OS finishes handle signal before transports/process handles close
            if self._h_process and _query_process_exit_code(self._h_process) is None:
                with suppress(TimeoutError, asyncio.CancelledError, ProcessLaunchError):
                    async with asyncio.timeout(min(graceful_timeout, 2.0)):
                        await self.wait_exit()

        async def close(self) -> None:
            if self._closed:
                return
            self._closed = True

            try:
                # Cancel/settle proactor wait_for_handle BEFORE closing hProcess
                wait_fut = self._wait_fut
                self._wait_fut = None
                if wait_fut is not None and not wait_fut.done():
                    wait_fut.cancel()
                    try:
                        await wait_fut
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            raise
                    except Exception as exc:  # noqa: BLE001 — close must proceed past already-settled wait failures
                        logger.debug(
                            f"Ignoring settled wait error during close for pid={self._pid}: {exc}"
                        )

                for attr in ("_stdout_transport", "_stderr_transport"):
                    tr = getattr(self, attr)
                    if tr is not None:
                        with suppress(OSError):
                            tr.close()
                        setattr(self, attr, None)

                # Yield once so ProactorBasePipeTransport._call_connection_lost closes PipeHandle cleanly
                await asyncio.sleep(0)
            finally:
                for attr in ("_stdout_transport", "_stderr_transport"):
                    tr = getattr(self, attr)
                    if tr is not None:
                        with suppress(OSError):
                            tr.close()
                        setattr(self, attr, None)

                if self._job_handle:
                    _safe_close_handle(self._job_handle)
                    self._job_handle = None

                if self._h_process:
                    if self._returncode is None:
                        self._returncode = _query_process_exit_code(self._h_process)
                    _safe_close_handle(self._h_process)
                    self._h_process = None

    async def _spawn_windows_process(
        spec: ProcessExecutionSpec,
    ) -> WindowsSpawnedProcess:
        loop = asyncio.get_running_loop()
        proactor = getattr(loop, "_proactor", None)
        if proactor is None or not hasattr(proactor, "wait_for_handle"):
            raise ProcessLaunchError(
                "Windows Process Runtime requires ProactorEventLoop; async overlapped pipes are unsupported on the active event loop."
            )

        # Fail-Closed: Create & configure Job Object BEFORE creating child process
        job_handle = _create_isolated_job_object()

        stdin_h: int | None = None
        stdout_rh: int | None = None
        stdout_wh: int | None = None
        stderr_rh: int | None = None
        stderr_wh: int | None = None
        attr_list_buf: Any = None
        attr_list_initialized = False
        h_process: int | None = None
        h_thread: int | None = None
        pid: int = 0
        stdout_transferred = False
        stderr_transferred = False
        stdout_transport: asyncio.ReadTransport | None = None
        stderr_transport: asyncio.ReadTransport | None = None

        try:
            sa = _SECURITY_ATTRIBUTES(
                nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                lpSecurityDescriptor=None,
                bInheritHandle=True,
            )
            stdin_h = int(
                _K32.CreateFileW(
                    "NUL",
                    _GENERIC_READ,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    ctypes.byref(sa),
                    _OPEN_EXISTING,
                    _FILE_ATTRIBUTE_NORMAL,
                    None,
                )
                or 0
            )
            if not stdin_h or stdin_h == _INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                stdin_h = None
                raise ProcessLaunchError(
                    f"Failed to open NUL for stdin (winerror={err})."
                )

            stdout_rh, stdout_wh = windows_utils.pipe(overlapped=(True, False))
            stderr_rh, stderr_wh = windows_utils.pipe(overlapped=(True, False))

            # Explicit handle inheritance control: ONLY child pipe ends may be inherited
            for child_h in (stdin_h, stdout_wh, stderr_wh):
                if not _K32.SetHandleInformation(
                    child_h, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
                ):
                    err = ctypes.get_last_error()
                    raise ProcessLaunchError(
                        f"SetHandleInformation(INHERIT) failed (winerror={err})."
                    )
            for parent_h in (stdout_rh, stderr_rh):
                _K32.SetHandleInformation(parent_h, _HANDLE_FLAG_INHERIT, 0)

            # PROC_THREAD_ATTRIBUTE_HANDLE_LIST strict whitelist
            list_size = ctypes.c_size_t(0)
            _K32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(list_size))
            if list_size.value == 0:
                raise ProcessLaunchError(
                    "InitializeProcThreadAttributeList returned 0 size."
                )
            attr_list_buf = ctypes.create_string_buffer(list_size.value)
            if not _K32.InitializeProcThreadAttributeList(
                attr_list_buf, 1, 0, ctypes.byref(list_size)
            ):
                err = ctypes.get_last_error()
                raise ProcessLaunchError(
                    f"InitializeProcThreadAttributeList failed (winerror={err})."
                )
            attr_list_initialized = True

            unique_handles = list(dict.fromkeys([stdin_h, stdout_wh, stderr_wh]))
            handle_array = (wintypes.HANDLE * len(unique_handles))(*unique_handles)
            if not _K32.UpdateProcThreadAttribute(
                attr_list_buf,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(handle_array),
                ctypes.sizeof(handle_array),
                None,
                None,
            ):
                err = ctypes.get_last_error()
                raise ProcessLaunchError(
                    f"UpdateProcThreadAttribute(HANDLE_LIST) failed (winerror={err})."
                )

            si = _STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES | _STARTF_USESHOWWINDOW
            si.StartupInfo.wShowWindow = _SW_HIDE
            si.StartupInfo.hStdInput = stdin_h
            si.StartupInfo.hStdOutput = stdout_wh
            si.StartupInfo.hStdError = stderr_wh
            si.lpAttributeList = ctypes.addressof(attr_list_buf)

            pi = _PROCESS_INFORMATION()
            cmdline = subprocess.list2cmdline(spec.argv)
            cmdline_buf = ctypes.create_unicode_buffer(cmdline)
            cwd_str = str(spec.cwd) if spec.cwd is not None else None
            env_buf = (
                _encode_win32_env_block(spec.env) if spec.env is not None else None
            )

            creation_flags = (
                _CREATE_SUSPENDED
                | _CREATE_NEW_PROCESS_GROUP
                | _CREATE_NO_WINDOW
                | _EXTENDED_STARTUPINFO_PRESENT
            )
            if env_buf is not None:
                creation_flags |= _CREATE_UNICODE_ENVIRONMENT

            try:
                created = _K32.CreateProcessW(
                    None,
                    cmdline_buf,
                    None,
                    None,
                    True,
                    creation_flags,
                    env_buf,
                    cwd_str,
                    ctypes.byref(si),
                    ctypes.byref(pi),
                )
                if not created:
                    err = ctypes.get_last_error()
                    raise ProcessLaunchError(
                        f"CreateProcessW failed for {spec.argv[0]!r} (winerror={err})."
                    )
                h_process = int(pi.hProcess)
                h_thread = int(pi.hThread)
                pid = int(pi.dwProcessId)
            finally:
                # Immediately close parent copies of child-inherited handles so EOF works
                for h in (stdin_h, stdout_wh, stderr_wh):
                    _safe_close_handle(h)
                stdin_h = stdout_wh = stderr_wh = None
                if attr_list_initialized:
                    _K32.DeleteProcThreadAttributeList(attr_list_buf)
                    attr_list_initialized = False

            # Keep references alive until CreateProcessW finished using attribute list
            _ = handle_array

            # 2. Assign suspended child process to Job Object BEFORE ANY instruction executes
            if not _K32.AssignProcessToJobObject(job_handle, h_process):
                err = ctypes.get_last_error()
                logger.error(
                    f"AssignProcessToJobObject failed for suspended pid={pid} (winerror={err}); enforcing Fail-Closed."
                )
                _K32.TerminateProcess(h_process, 1)
                _K32.WaitForSingleObject(h_process, _WAIT_TIMEOUT_MS)
                raise ProcessIsolationError(
                    f"Failed to assign PID {pid} to Win32 Job Object (winerror={err}); terminated fail-closed."
                )

            # 3. Resume suspended primary thread cleanly
            resume_rc = _K32.ResumeThread(h_thread)
            if resume_rc == _RESUME_THREAD_FAILED:
                err = ctypes.get_last_error()
                logger.error(
                    f"ResumeThread failed for pid={pid} (winerror={err}); killing job and cleaning up."
                )
                _K32.TerminateJobObject(job_handle, 1)
                _K32.TerminateProcess(h_process, 1)
                _K32.WaitForSingleObject(h_process, _WAIT_TIMEOUT_MS)
                raise ProcessLaunchError(
                    f"ResumeThread failed for PID {pid} (winerror={err}); process terminated."
                )

            _safe_close_handle(h_thread)
            h_thread = None

            # 4. Wire overlapped read pipes to ProactorEventLoop
            stdout_reader = asyncio.StreamReader()
            stderr_reader = asyncio.StreamReader()

            stdout_ph = windows_utils.PipeHandle(stdout_rh)
            stdout_transferred = True
            stdout_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stdout_reader, loop=loop),
                stdout_ph,
            )

            stderr_ph = windows_utils.PipeHandle(stderr_rh)
            stderr_transferred = True
            stderr_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stderr_reader, loop=loop),
                stderr_ph,
            )

            return WindowsSpawnedProcess(
                pid=pid,
                h_process=h_process,
                job_handle=job_handle,
                stdout_reader=stdout_reader,
                stderr_reader=stderr_reader,
                stdout_transport=stdout_transport,
                stderr_transport=stderr_transport,
                loop=loop,
            )

        except BaseException:
            # Deterministic cleanup on ANY partial failure or cancellation
            if attr_list_initialized and attr_list_buf is not None:
                _K32.DeleteProcThreadAttributeList(attr_list_buf)
            for h in (stdin_h, stdout_wh, stderr_wh):
                _safe_close_handle(h)
            if h_thread:
                _safe_close_handle(h_thread)
            if job_handle:
                if h_process:
                    _K32.TerminateJobObject(job_handle, 1)
                _safe_close_handle(job_handle)
            if h_process:
                _K32.TerminateProcess(h_process, 1)
                _K32.WaitForSingleObject(h_process, _WAIT_TIMEOUT_MS)
                _safe_close_handle(h_process)
            if stdout_transport is not None:
                with suppress(OSError):
                    stdout_transport.close()
            elif not stdout_transferred:
                _safe_close_handle(stdout_rh)
            if stderr_transport is not None:
                with suppress(OSError):
                    stderr_transport.close()
            elif not stderr_transferred:
                _safe_close_handle(stderr_rh)
            raise


# ---------------------------------------------------------------------------
# Unified Platform Dispatcher
# ---------------------------------------------------------------------------


async def spawn_isolated_process(spec: ProcessExecutionSpec) -> SpawnedProcess:
    """Spawn a process inside an OS-isolated process tree with async stdout/stderr pipes."""
    if sys.platform == "win32":
        return await _spawn_windows_process(spec)
    return await _spawn_posix_process(spec)
