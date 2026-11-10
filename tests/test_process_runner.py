"""Phase 2 real-OS and concurrency tests for AsyncProcessRunner."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import itertools
import sys
import time
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_studio.core.process import (
    AsyncProcessRunner,
    ProcessExecutionSpec,
    ProcessIsolationError,
    ProcessStatus,
    SpawnedProcess,
    run_process,
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


def _runner_task_names() -> list[str]:
    """Return names of any still-active runner helper tasks on the current loop."""
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if not t.done()
        and (
            t.get_name().startswith("proc-")
            or t.get_name() == "process-resilient-cleanup"
        )
    ]


async def test_runner_success_and_nonzero_exit_codes() -> None:
    """Normal exit (0) returns SUCCESS and non-zero exit returns FAILED."""
    ok = await run_process(
        ProcessExecutionSpec(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('ok-out'); sys.stderr.write('ok-err')",
            ]
        )
    )
    assert ok.status == ProcessStatus.SUCCESS
    assert ok.exit_code == 0
    assert ok.stdout == "ok-out"
    assert ok.stderr == "ok-err"
    assert ok.timed_out is False
    assert ok.output_truncated is False
    assert ok.stdout_bytes_total == len(b"ok-out")
    assert ok.stdout_bytes_retained == len(b"ok-out")
    assert ok.stderr_bytes_total == len(b"ok-err")
    assert ok.stderr_bytes_retained == len(b"ok-err")
    assert _runner_task_names() == []

    fail = await run_process(
        ProcessExecutionSpec(argv=[sys.executable, "-c", "import sys; sys.exit(9)"])
    )
    assert fail.status == ProcessStatus.FAILED
    assert fail.exit_code == 9
    assert fail.error_message is not None
    assert _runner_task_names() == []

    missing = await run_process(
        ProcessExecutionSpec(argv=["jarvis_definitely_missing_binary_12345"])
    )
    assert missing.status == ProcessStatus.FAILED
    assert missing.exit_code is None
    assert missing.error_message is not None


async def test_runner_concurrent_large_stdout_and_stderr_without_deadlock() -> None:
    """Simultaneous >100KB stdout and >100KB stderr drain cleanly without pipe deadlock."""
    script = (
        "import sys\n"
        "chunk_out = b'A' * 4096\n"
        "chunk_err = b'B' * 4096\n"
        "for _ in range(30):\n"
        "    sys.stdout.buffer.write(chunk_out); sys.stdout.buffer.flush()\n"
        "    sys.stderr.buffer.write(chunk_err); sys.stderr.buffer.flush()\n"
    )
    total_each = 4096 * 30  # 122_880 bytes each -> 245_760 combined
    result = await run_process(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", script],
            max_output_bytes=300_000,
            timeout_seconds=15.0,
        )
    )
    assert result.status == ProcessStatus.SUCCESS
    assert result.exit_code == 0
    assert result.output_truncated is False
    assert result.stdout_bytes_total == total_each
    assert result.stderr_bytes_total == total_each
    assert result.stdout_bytes_retained == total_each
    assert result.stderr_bytes_retained == total_each
    assert len(result.stdout) == total_each
    assert len(result.stderr) == total_each
    assert _runner_task_names() == []


async def test_runner_handles_huge_single_line_without_newlines() -> None:
    """Chunked stream.read() prevents asyncio LimitOverrunError on >128KB lines."""
    size = 150_000
    script = f"import sys; sys.stdout.write('X' * {size}); sys.stdout.flush()"
    result = await AsyncProcessRunner(chunk_size=4096).run(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", script],
            max_output_bytes=200_000,
            timeout_seconds=15.0,
        )
    )
    assert result.status == ProcessStatus.SUCCESS
    assert len(result.stdout) == size
    assert result.stdout_bytes_total == size
    assert result.output_truncated is False


async def test_runner_exact_budget_boundary_vs_overflow_and_utf8_cut() -> None:
    """Exact max_output_bytes succeeds; +1 byte overflows, kills tree, and keeps valid UTF-8."""
    # Exact match == max_output_bytes stays SUCCESS
    exact = await run_process(
        ProcessExecutionSpec(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'12345'); sys.stderr.buffer.write(b'67890')",
            ],
            max_output_bytes=10,
        )
    )
    assert exact.status == ProcessStatus.SUCCESS
    assert exact.output_truncated is False
    assert exact.stdout == "12345"
    assert exact.stderr == "67890"
    assert exact.stdout_bytes_retained + exact.stderr_bytes_retained == 10

    # Overflow + multi-byte UTF-8 boundary cut + continuous flooder process killed
    # '你好世界' = 12 bytes UTF-8; repeating fills well beyond 100 bytes until killed.
    flood_script = (
        "import sys, time\n"
        "payload = '你好世界'.encode('utf-8')\n"
        "sys.stdout.buffer.write(payload * 50)\n"
        "sys.stdout.buffer.flush()\n"
        "for _ in range(1000):\n"
        "    try:\n"
        "        sys.stdout.buffer.write(payload * 100)\n"
        "        sys.stdout.buffer.flush()\n"
        "        time.sleep(0.01)\n"
        "    except OSError:\n"
        "        break\n"
    )
    # 100 bytes cuts cleanly in the middle of a 3-byte Chinese character!
    overflow = await run_process(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", flood_script],
            max_output_bytes=100,
            timeout_seconds=10.0,
        )
    )
    assert overflow.status == ProcessStatus.OUTPUT_LIMIT_EXCEEDED
    assert overflow.output_truncated is True
    assert overflow.timed_out is False
    assert overflow.stdout_bytes_retained + overflow.stderr_bytes_retained == 100
    assert overflow.stdout_bytes_total + overflow.stderr_bytes_total > 100
    # Must decode without raising or corrupting characters.
    assert overflow.stdout.startswith("你好世界")
    overflow.stdout.encode("utf-8")
    if overflow.pid and sys.platform == "win32":
        assert not _is_win_pid_alive(overflow.pid)
    assert _runner_task_names() == []


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object timeout tree kill test"
)
async def test_runner_timeout_terminates_process_tree(tmp_path: Path) -> None:
    """Timeout kills both child and detached grandchild and cleans all tasks."""
    pid_file = tmp_path / "timeout_pids.txt"
    child_code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"p = Path({str(pid_file)!r})\n"
        "gc = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        f"    creationflags={_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP},\n"
        ")\n"
        "sys.stdout.write('before-timeout\\n'); sys.stdout.flush()\n"
        "p.write_text(f'{os.getpid()}\\n{gc.pid}\\n', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    result = await run_process(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", child_code],
            timeout_seconds=0.8,
            graceful_termination_timeout=0.3,
        )
    )
    assert result.status == ProcessStatus.TIMEOUT
    assert result.timed_out is True
    assert result.cancelled is False
    assert "before-timeout" in result.stdout
    assert _runner_task_names() == []

    lines = [
        int(x.strip())
        for x in pid_file.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    assert len(lines) == 2
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if all(not _is_win_pid_alive(pid) for pid in lines):
            break
        await asyncio.sleep(0.05)
    for pid in lines:
        assert not _is_win_pid_alive(pid)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object cancellation tree kill test"
)
async def test_runner_external_cancellation_kills_tree_and_reraises(
    tmp_path: Path,
) -> None:
    """External Task.cancel() kills child + grandchild, frees tasks, and re-raises CancelledError."""
    ready_file = tmp_path / "cancel_pids.txt"
    child_code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"p = Path({str(ready_file)!r})\n"
        "gc = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        f"    creationflags={_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP},\n"
        ")\n"
        "p.write_text(f'{os.getpid()}\\n{gc.pid}\\n', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )

    task = asyncio.create_task(
        run_process(
            ProcessExecutionSpec(
                argv=[sys.executable, "-c", child_code],
                timeout_seconds=30.0,
            )
        )
    )
    pids: list[int] = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ready_file.exists():
            lines = [
                x.strip()
                for x in ready_file.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
            if len(lines) == 2:
                pids = [int(lines[0]), int(lines[1])]
                break
        await asyncio.sleep(0.05)

    assert len(pids) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _runner_task_names() == []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if all(not _is_win_pid_alive(pid) for pid in pids):
            break
        await asyncio.sleep(0.05)
    for pid in pids:
        assert not _is_win_pid_alive(pid)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows repeated cancellation resilience test"
)
async def test_runner_survives_repeated_task_cancel_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive Task.cancel() signals do not abort in-flight cleanup or leave background tasks."""
    ready_file = tmp_path / "multi_cancel.txt"
    child_code = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(ready_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )

    # Slow down terminate_tree slightly so secondary Task.cancel() hits squarely mid-cleanup.
    orig_term = process_platform.WindowsSpawnedProcess.terminate_tree
    cleanup_completed = False

    async def slow_terminate_tree(
        self: process_platform.WindowsSpawnedProcess,
        graceful_timeout: float = 2.0,
    ) -> None:
        nonlocal cleanup_completed
        await asyncio.sleep(0.15)
        await orig_term(self, graceful_timeout=graceful_timeout)
        cleanup_completed = True

    monkeypatch.setattr(
        process_platform.WindowsSpawnedProcess, "terminate_tree", slow_terminate_tree
    )

    task = asyncio.create_task(
        run_process(
            ProcessExecutionSpec(
                argv=[sys.executable, "-c", child_code],
                timeout_seconds=30.0,
                cleanup_max_seconds=5.0,
            )
        )
    )
    child_pid: int | None = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ready_file.exists():
            txt = ready_file.read_text(encoding="utf-8").strip()
            if txt.isdigit():
                child_pid = int(txt)
                break
        await asyncio.sleep(0.05)
    assert child_pid is not None

    # Deliver repeated consecutive cancellations while cleanup runs.
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_completed is True
    assert _runner_task_names() == []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _is_win_pid_alive(child_pid):
        await asyncio.sleep(0.05)
    assert not _is_win_pid_alive(child_pid)


async def test_runner_handles_delayed_eof_from_grandchild_inheriting_pipe() -> None:
    """Process exit must complete within drain_grace even if descendant holds stdout pipe open."""
    # Part 1: Real OS process exits immediately after spawning a sleeping child inheriting stdout.
    child_code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], close_fds=False)\n"
        "sys.stdout.write('parent-finished\\n'); sys.stdout.flush()\n"
    )
    start = time.monotonic()
    res = await run_process(
        ProcessExecutionSpec(
            argv=[sys.executable, "-c", child_code],
            timeout_seconds=10.0,
            drain_grace_seconds=0.25,
        )
    )
    elapsed = time.monotonic() - start
    assert res.status == ProcessStatus.SUCCESS
    assert res.exit_code == 0
    assert "parent-finished" in res.stdout
    assert elapsed < 3.0
    assert _runner_task_names() == []

    # Part 2: Simulated permanently open pipe even after terminate_tree (POSIX setsid grandchild case).
    stdout_reader = asyncio.StreamReader()
    stderr_reader = asyncio.StreamReader()
    stdout_reader.feed_data(b"partial-data\n")
    # Intentionally DO NOT call feed_eof() on either StreamReader!

    fake_proc = MagicMock(spec=SpawnedProcess)
    fake_proc.pid = 54321
    fake_proc.returncode = 0
    fake_proc.stdout = stdout_reader
    fake_proc.stderr = stderr_reader
    fake_proc.wait_exit = AsyncMock(return_value=0)
    fake_proc.terminate_tree = AsyncMock()
    fake_proc.close = AsyncMock()

    orig_spawn = AsyncProcessRunner.run

    async def _run_with_fake() -> None:
        from jarvis_studio.core.process import runner as runner_mod

        old = runner_mod.spawn_isolated_process
        runner_mod.spawn_isolated_process = AsyncMock(return_value=fake_proc)
        try:
            r = await AsyncProcessRunner().run(
                ProcessExecutionSpec(
                    argv=["simulated"],
                    timeout_seconds=5.0,
                    drain_grace_seconds=0.1,
                )
            )
            assert r.status == ProcessStatus.SUCCESS
            assert r.stdout == "partial-data\n"
            fake_proc.close.assert_awaited_once()
        finally:
            runner_mod.spawn_isolated_process = old

    _ = orig_spawn
    await _run_with_fake()
    assert _runner_task_names() == []


async def test_runner_concurrent_executions_isolated() -> None:
    """Multiple concurrent AsyncProcessRunner executions complete independently without mixing output."""

    async def run_one(idx: int) -> str:
        res = await run_process(
            ProcessExecutionSpec(
                argv=[
                    sys.executable,
                    "-c",
                    f"import time; time.sleep(0.05); print('worker-{idx}')",
                ]
            )
        )
        assert res.status == ProcessStatus.SUCCESS
        return res.stdout.strip()

    outputs = await asyncio.gather(*(run_one(i) for i in range(5)))
    assert outputs == [f"worker-{i}" for i in range(5)]
    assert _runner_task_names() == []


async def test_runner_does_not_block_event_loop_heartbeat() -> None:
    """Running a sleeping real child does not block concurrent asyncio heartbeat ticks."""
    tick_times: list[float] = []
    stop_heartbeat = False

    async def heartbeat() -> None:
        while not stop_heartbeat:
            tick_times.append(time.monotonic())
            await asyncio.sleep(0.02)

    hb = asyncio.create_task(heartbeat())
    try:
        res = await run_process(
            ProcessExecutionSpec(
                argv=[
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.4); print('done')",
                ]
            )
        )
        assert res.status == ProcessStatus.SUCCESS
    finally:
        stop_heartbeat = True
        await hb

    assert len(tick_times) >= 8
    max_gap = max(b - a for a, b in itertools.pairwise(tick_times))
    assert max_gap < 0.25


async def test_runner_emits_no_resource_warning_across_modes() -> None:
    """Normal, timeout, overflow, and cancelled executions emit zero ResourceWarnings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)

        # Normal
        await run_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('rw-ok')"])
        )
        # Overflow
        await run_process(
            ProcessExecutionSpec(
                argv=[sys.executable, "-c", "import sys; sys.stdout.write('A'*500)"],
                max_output_bytes=50,
            )
        )
        # Timeout
        await run_process(
            ProcessExecutionSpec(
                argv=[sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.3,
            )
        )
        # Cancelled
        cancel_task = asyncio.create_task(
            run_process(
                ProcessExecutionSpec(
                    argv=[sys.executable, "-c", "import time; time.sleep(5)"],
                    timeout_seconds=5.0,
                )
            )
        )
        await asyncio.sleep(0.15)
        cancel_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel_task

        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()

    rws = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert rws == [], [str(w.message) for w in rws]
    assert _runner_task_names() == []


async def test_runner_priority_rules_and_soft_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OUTPUT_LIMIT_EXCEEDED > TIMEOUT and soft cancel_event -> CANCELLED."""
    from jarvis_studio.core.process import runner as runner_mod

    # 1. Timeout occurs while overflow cleanup is active -> OUTPUT_LIMIT_EXCEEDED wins.
    stdout_reader = asyncio.StreamReader()
    stderr_reader = asyncio.StreamReader()
    fake_proc = MagicMock(spec=SpawnedProcess)
    fake_proc.pid = 60001
    fake_proc.returncode = None
    fake_proc.stdout = stdout_reader
    fake_proc.stderr = stderr_reader

    async def never_exit() -> int:
        await asyncio.sleep(10)
        return 0

    fake_proc.wait_exit = never_exit
    fake_proc.terminate_tree = AsyncMock()
    fake_proc.close = AsyncMock()

    monkeypatch.setattr(
        runner_mod, "spawn_isolated_process", AsyncMock(return_value=fake_proc)
    )
    stdout_reader.feed_data(b"Z" * 50)

    res = await run_process(
        ProcessExecutionSpec(
            argv=["mocked"],
            max_output_bytes=10,
            timeout_seconds=0.05,
            drain_grace_seconds=0.05,
        )
    )
    assert res.status == ProcessStatus.OUTPUT_LIMIT_EXCEEDED
    assert res.timed_out is False
    assert res.output_truncated is True

    # 2. Soft cancel_event -> ProcessStatus.CANCELLED
    stdout_soft = asyncio.StreamReader()
    stderr_soft = asyncio.StreamReader()
    soft_proc = MagicMock(spec=SpawnedProcess)
    soft_proc.pid = 60002
    soft_proc.returncode = None
    soft_proc.stdout = stdout_soft
    soft_proc.stderr = stderr_soft
    soft_proc.wait_exit = never_exit
    soft_proc.terminate_tree = AsyncMock()
    soft_proc.close = AsyncMock()
    monkeypatch.setattr(
        runner_mod, "spawn_isolated_process", AsyncMock(return_value=soft_proc)
    )

    cancel_ev = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, cancel_ev.set)
    soft_res = await run_process(
        ProcessExecutionSpec(
            argv=["mocked"],
            timeout_seconds=5.0,
            drain_grace_seconds=0.05,
        ),
        cancel_event=cancel_ev,
    )
    assert soft_res.status == ProcessStatus.CANCELLED
    assert soft_res.cancelled is True
    assert soft_res.timed_out is False
    soft_proc.terminate_tree.assert_awaited_once()
    soft_proc.close.assert_awaited_once()
    assert _runner_task_names() == []


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Object Fail-Closed check"
)
async def test_runner_propagates_job_isolation_error_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncProcessRunner must propagate ProcessIsolationError instead of falling back."""
    k32 = process_platform._K32

    def fail_create_job(_sec, _name):
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(k32, "CreateJobObjectW", fail_create_job)

    with pytest.raises(ProcessIsolationError):
        await run_process(
            ProcessExecutionSpec(argv=[sys.executable, "-c", "print('unmanaged')"])
        )


@pytest.mark.parametrize("mode", ["timeout", "overflow"])
async def test_runner_cancellation_during_timeout_or_overflow_reraises(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation colliding with Timeout or Output Limit still completes cleanup and re-raises."""
    from jarvis_studio.core.process import runner as runner_mod

    async def never_exit() -> int:
        await asyncio.sleep(10)
        return 0

    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()

    stdout_reader = asyncio.StreamReader()
    stderr_reader = asyncio.StreamReader()
    proc = MagicMock(spec=SpawnedProcess)
    proc.pid = 61000 if mode == "timeout" else 61001
    proc.returncode = None
    proc.stdout = stdout_reader
    proc.stderr = stderr_reader
    proc.wait_exit = never_exit

    async def _blocking_terminate(graceful_timeout: float = 2.0) -> None:
        entered_cleanup.set()
        await release_cleanup.wait()

    proc.terminate_tree = AsyncMock(side_effect=_blocking_terminate)
    proc.close = AsyncMock()
    monkeypatch.setattr(
        runner_mod, "spawn_isolated_process", AsyncMock(return_value=proc)
    )

    if mode == "overflow":
        stdout_reader.feed_data(b"OVERFLOW" * 20)

    spec = ProcessExecutionSpec(
        argv=["mocked-collision"],
        timeout_seconds=0.05 if mode == "timeout" else 5.0,
        max_output_bytes=20 if mode == "overflow" else 1024,
        drain_grace_seconds=0.05,
        cleanup_max_seconds=5.0,
    )
    task = asyncio.create_task(run_process(spec))
    await asyncio.wait_for(entered_cleanup.wait(), timeout=2.0)

    # Cancel mid-cleanup after Timeout / Output Limit has already triggered.
    task.cancel()
    await asyncio.sleep(0.02)
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    proc.close.assert_awaited_once()
    assert _runner_task_names() == []
