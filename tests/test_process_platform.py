"""Phase 1 tests for Sprint 1B process models and platform launcher."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import signal
import sys
import time
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from jarvis_studio.core.process import (
    ProcessExecutionSpec,
    ProcessIsolationError,
    ProcessLaunchError,
    spawn_isolated_process,
)
from jarvis_studio.core.process import platform as process_platform

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _is_win_pid_alive(pid: int) -> bool:
    """Return True if a Windows process PID is still running."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return bool(ok and exit_code.value == _STILL_ACTIVE)
    finally:
        kernel32.CloseHandle(handle)


def test_process_execution_spec_validation() -> None:
    """Reject empty commands, NUL bytes, and invalid bounds."""
    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=[])

    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=["   ", "-V"])

    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=["python", "ok\x00bad"])

    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=["python"], timeout_seconds=0)

    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=["python"], max_output_bytes=0)

    with pytest.raises(ValidationError):
        ProcessExecutionSpec(argv=["python"], drain_grace_seconds=-0.1)


async def test_spawn_isolated_process_captures_streams_and_exit_codes(
    tmp_path: Path,
) -> None:
    """Capture stdout/stderr asynchronously and observe real exit codes."""
    env = os.environ.copy()
    env["JARVIS_PHASE1_ENV"] = "isolated-ok"
    script = (
        "import os, pathlib, sys; "
        "sys.stdout.write(os.environ['JARVIS_PHASE1_ENV'] + '|' + str(pathlib.Path.cwd())); "
        "sys.stdout.flush(); "
        "sys.stderr.write('err-line'); "
        "sys.stderr.flush(); "
        "sys.exit(0)"
    )
    proc = await spawn_isolated_process(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", script],
            cwd=tmp_path,
            env=env,
        )
    )
    try:
        out, err, rc = await asyncio.gather(
            proc.stdout.read(),
            proc.stderr.read(),
            proc.wait_exit(),
        )
        assert rc == 0
        assert proc.returncode == 0
        assert out.decode("utf-8") == f"isolated-ok|{tmp_path}"
        assert err.decode("utf-8") == "err-line"
    finally:
        await proc.close()

    fail_proc = await spawn_isolated_process(
        ProcessExecutionSpec(argv=[sys.executable, "-c", "import sys; sys.exit(7)"])
    )
    try:
        rc = await fail_proc.wait_exit()
        assert rc == 7
        assert fail_proc.returncode == 7
    finally:
        await fail_proc.close()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object integration test"
)
async def test_windows_process_tree_kills_grandchild(tmp_path: Path) -> None:
    """Terminating the spawned process kills nested & detached descendants via Job Object."""
    ready_file = tmp_path / "pids.txt"
    child_code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"ready = Path({str(ready_file)!r})\n"
        "gc_proc = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        f"    creationflags={_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP},\n"
        ")\n"
        "ready.write_text(f'{os.getpid()}\\n{os.getppid()}\\n{gc_proc.pid}\\n', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    proc = await spawn_isolated_process(
        ProcessExecutionSpec(argv=[sys.executable, "-c", child_code])
    )
    tracked_pids: list[int] = []
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if ready_file.exists():
                lines = [
                    line.strip()
                    for line in ready_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if len(lines) == 3:
                    child_pid, child_ppid, grandchild_pid = (
                        int(lines[0]),
                        int(lines[1]),
                        int(lines[2]),
                    )
                    assert proc.pid in (child_pid, child_ppid)
                    tracked_pids = list(
                        dict.fromkeys([proc.pid, child_pid, grandchild_pid])
                    )
                    break
            await asyncio.sleep(0.05)

        assert len(tracked_pids) >= 2, "Child and grandchild failed to report readiness"
        for pid in tracked_pids:
            assert _is_win_pid_alive(pid)

        await proc.terminate_tree(graceful_timeout=0.5)
    finally:
        await proc.close()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if all(not _is_win_pid_alive(pid) for pid in tracked_pids):
            break
        await asyncio.sleep(0.05)

    for pid in tracked_pids:
        assert not _is_win_pid_alive(pid)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows HANDLE inheritance whitelist test"
)
async def test_windows_handle_list_blocks_unrelated_inheritable_handle(
    tmp_path: Path,
) -> None:
    """Explicitly inheritable parent handles outside HANDLE_LIST are not inherited."""
    import msvcrt

    secret_file = tmp_path / "secret_parent_only.txt"
    secret_file.write_text("do-not-inherit", encoding="utf-8")

    # Open an inheritable OS handle in parent that must NOT leak to the child.
    fd = os.open(secret_file, os.O_RDONLY)
    os.set_inheritable(fd, True)
    try:
        parent_handle = msvcrt.get_osfhandle(fd)
        probe_script = (
            "import ctypes, json, sys\n"
            "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
            "buf = ctypes.create_unicode_buffer(1024)\n"
            f"res = k32.GetFinalPathNameByHandleW(ctypes.c_void_p({parent_handle}), buf, 1024, 0)\n"
            "print(json.dumps({'ok': bool(res), 'path': buf.value if res else ''}))\n"
        )
        proc = await spawn_isolated_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", probe_script])
        )
        try:
            out, rc = await asyncio.gather(proc.stdout.read(), proc.wait_exit())
        finally:
            await proc.close()
    finally:
        os.close(fd)

    assert rc == 0
    assert str(secret_file.name) not in out.decode("utf-8")


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Proactor loop requirement test"
)
def test_windows_rejects_non_proactor_event_loop() -> None:
    """Fail-Closed when running on SelectorEventLoop instead of ProactorEventLoop."""
    loop = asyncio.SelectorEventLoop()
    try:
        with pytest.raises(ProcessLaunchError, match="ProactorEventLoop"):
            loop.run_until_complete(
                spawn_isolated_process(
                    ProcessExecutionSpec(argv=[sys.executable, "-c", "print('hi')"])
                )
            )
    finally:
        loop.close()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object Fail-Closed test"
)
async def test_windows_job_assignment_failure_fails_closed_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AssignProcessToJobObject fails, kill suspended child, close handles, raise ProcessIsolationError."""
    k32 = process_platform._K32
    orig_create = k32.CreateProcessW
    orig_resume = k32.ResumeThread
    orig_terminate = k32.TerminateProcess
    orig_close = k32.CloseHandle

    captured: dict[str, object] = {
        "pid": None,
        "h_process": None,
        "h_thread": None,
        "resumed": False,
        "terminated_handles": [],
        "closed_handles": [],
    }

    def spy_create(
        app_name,
        cmd_line,
        proc_attr,
        thread_attr,
        inherit_handles,
        creation_flags,
        env,
        cwd,
        startup_info,
        proc_info_ptr,
    ):
        ok = orig_create(
            app_name,
            cmd_line,
            proc_attr,
            thread_attr,
            inherit_handles,
            creation_flags,
            env,
            cwd,
            startup_info,
            proc_info_ptr,
        )
        if ok:
            info = proc_info_ptr._obj
            captured["pid"] = int(info.dwProcessId)
            captured["h_process"] = int(info.hProcess)
            captured["h_thread"] = int(info.hThread)
        return ok

    def fail_assign(job, proc):
        ctypes.set_last_error(5)
        return 0

    def spy_resume(h_thread):
        captured["resumed"] = True
        return orig_resume(h_thread)

    def spy_terminate(h_process, exit_code):
        captured["terminated_handles"].append(int(h_process))
        return orig_terminate(h_process, exit_code)

    def spy_close(handle):
        if handle:
            captured["closed_handles"].append(int(handle))
        return orig_close(handle)

    monkeypatch.setattr(k32, "CreateProcessW", spy_create)
    monkeypatch.setattr(k32, "AssignProcessToJobObject", fail_assign)
    monkeypatch.setattr(k32, "ResumeThread", spy_resume)
    monkeypatch.setattr(k32, "TerminateProcess", spy_terminate)
    monkeypatch.setattr(k32, "CloseHandle", spy_close)

    with pytest.raises(ProcessIsolationError, match="Win32 Job Object"):
        await spawn_isolated_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('should-not-run')"])
        )

    assert captured["resumed"] is False
    assert captured["h_process"] in captured["terminated_handles"]
    assert captured["h_process"] in captured["closed_handles"]
    assert captured["h_thread"] in captured["closed_handles"]
    if captured["pid"]:
        assert not _is_win_pid_alive(int(captured["pid"]))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ResumeThread failure test")
async def test_windows_resume_thread_failure_kills_and_cleans_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ResumeThread fails, terminate job/process, close handles, raise ProcessLaunchError."""
    k32 = process_platform._K32
    orig_create = k32.CreateProcessW
    orig_terminate_job = k32.TerminateJobObject
    orig_close = k32.CloseHandle

    captured: dict[str, object] = {
        "pid": None,
        "h_process": None,
        "h_thread": None,
        "job_terminated": False,
        "closed_handles": [],
    }

    def spy_create(
        app_name,
        cmd_line,
        proc_attr,
        thread_attr,
        inherit_handles,
        creation_flags,
        env,
        cwd,
        startup_info,
        proc_info_ptr,
    ):
        ok = orig_create(
            app_name,
            cmd_line,
            proc_attr,
            thread_attr,
            inherit_handles,
            creation_flags,
            env,
            cwd,
            startup_info,
            proc_info_ptr,
        )
        if ok:
            info = proc_info_ptr._obj
            captured["pid"] = int(info.dwProcessId)
            captured["h_process"] = int(info.hProcess)
            captured["h_thread"] = int(info.hThread)
        return ok

    def fail_resume(h_thread):
        ctypes.set_last_error(5)
        return 0xFFFFFFFF

    def spy_terminate_job(job, exit_code):
        captured["job_terminated"] = True
        return orig_terminate_job(job, exit_code)

    def spy_close(handle):
        if handle:
            captured["closed_handles"].append(int(handle))
        return orig_close(handle)

    monkeypatch.setattr(k32, "CreateProcessW", spy_create)
    monkeypatch.setattr(k32, "ResumeThread", fail_resume)
    monkeypatch.setattr(k32, "TerminateJobObject", spy_terminate_job)
    monkeypatch.setattr(k32, "CloseHandle", spy_close)

    with pytest.raises(ProcessLaunchError, match="ResumeThread"):
        await spawn_isolated_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('should-not-run')"])
        )

    assert captured["job_terminated"] is True
    assert captured["h_process"] in captured["closed_handles"]
    assert captured["h_thread"] in captured["closed_handles"]
    if captured["pid"]:
        assert not _is_win_pid_alive(int(captured["pid"]))


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object creation Fail-Closed test"
)
async def test_windows_create_job_failure_aborts_before_create_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If CreateJobObjectW fails, raise ProcessIsolationError without launching child."""
    k32 = process_platform._K32
    create_called = False

    def fail_create_job(_sec, _name):
        ctypes.set_last_error(8)
        return 0

    def spy_create_process(*_args, **_kwargs):
        nonlocal create_called
        create_called = True
        return 0

    monkeypatch.setattr(k32, "CreateJobObjectW", fail_create_job)
    monkeypatch.setattr(k32, "CreateProcessW", spy_create_process)

    with pytest.raises(ProcessIsolationError, match="CreateJobObjectW"):
        await spawn_isolated_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('never-launched')"])
        )

    assert create_called is False


async def test_close_idempotent_without_resource_warning() -> None:
    """Repeated close() is idempotent and releases pipes/handles without ResourceWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        proc = await spawn_isolated_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('clean')"])
        )
        await proc.stdout.read()
        await proc.stderr.read()
        rc = await proc.wait_exit()
        assert rc == 0

        await proc.close()
        await proc.close()
        del proc
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()

    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert resource_warnings == [], [str(w.message) for w in resource_warnings]


async def test_posix_terminate_tree_sigterm_then_sigkill_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify POSIX process-group SIGTERM grace and SIGKILL escalation logic."""
    sent_signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        sent_signals.append((pgid, sig))

    monkeypatch.setattr(process_platform.os, "killpg", fake_killpg, raising=False)
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    monkeypatch.setattr(process_platform.signal, "SIGKILL", sigkill, raising=False)

    fake_proc = MagicMock(spec=asyncio.subprocess.Process)
    fake_proc.pid = 43210
    fake_proc.returncode = None
    fake_proc.stdout = AsyncMock(spec=asyncio.StreamReader)
    fake_proc.stderr = AsyncMock(spec=asyncio.StreamReader)

    # Case 1: Group still alive after SIGTERM -> escalates to SIGKILL.
    alive_checks = [True, True, False]

    def fake_group_alive(pgid: int) -> bool:
        return alive_checks.pop(0) if alive_checks else False

    async def fake_wait() -> int:
        fake_proc.returncode = -int(sigkill)
        return fake_proc.returncode

    fake_proc.wait = AsyncMock(side_effect=fake_wait)
    monkeypatch.setattr(process_platform, "_posix_group_alive", fake_group_alive)

    posix_proc = process_platform.PosixSpawnedProcess(fake_proc)
    await posix_proc.terminate_tree(graceful_timeout=0.01)

    assert sent_signals == [(43210, signal.SIGTERM), (43210, sigkill)]

    # Case 2: Group exits promptly after SIGTERM -> no SIGKILL needed.
    sent_signals.clear()
    fake_proc_fast = MagicMock(spec=asyncio.subprocess.Process)
    fake_proc_fast.pid = 43211
    fake_proc_fast.returncode = None
    fake_proc_fast.stdout = AsyncMock(spec=asyncio.StreamReader)
    fake_proc_fast.stderr = AsyncMock(spec=asyncio.StreamReader)
    fake_proc_fast.wait = AsyncMock(return_value=-int(signal.SIGTERM))

    monkeypatch.setattr(process_platform, "_posix_group_alive", lambda _pgid: False)
    posix_fast = process_platform.PosixSpawnedProcess(fake_proc_fast)
    await posix_fast.terminate_tree(graceful_timeout=0.1)

    assert sent_signals == [(43211, signal.SIGTERM)]
