"""Phase 3 integration tests for execute_shell_command over core.process and HITL."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import inspect
import os
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    get_hitl_manager,
)
from jarvis_studio.agent.nodes.executor import create_tool_executor_node
from jarvis_studio.tools.base import RiskLevel, get_tool_metadata
from jarvis_studio.tools.builtin import shell_tools
from jarvis_studio.tools.builtin.shell_tools import (
    ShellCommandInput,
    _create_win32_temp_cmd,
    execute_shell_command,
    resolve_shell_cwd,
)
from jarvis_studio.tools.registry import ToolRegistry

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
    """Return active helper task names from core.process.runner if any leaked."""
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if not t.done()
        and (
            t.get_name().startswith("proc-")
            or t.get_name() == "process-resilient-cleanup"
        )
    ]


def _patch_workspace(workspace_root: Path):
    """Return patch context overriding storage.workspace_root inside shell_tools."""
    fake_settings = SimpleNamespace(
        storage=SimpleNamespace(workspace_root=str(workspace_root))
    )
    return patch(
        "jarvis_studio.tools.builtin.shell_tools.get_settings",
        return_value=fake_settings,
    )


def test_shell_tool_metadata_schema_and_zero_legacy_subprocess_run() -> None:
    """Ensure execute_shell_command exposes async coroutine, HIGH risk tag, and no subprocess.run."""
    assert execute_shell_command.name == "execute_shell_command"
    assert execute_shell_command.coroutine is not None
    assert execute_shell_command.func is not None

    meta = get_tool_metadata(execute_shell_command)
    assert meta.risk_level == RiskLevel.HIGH
    assert meta.requires_approval is True

    # Modified args schema check (HITL safety invariant)
    parsed = ShellCommandInput.model_validate({"command": "echo safe"})
    assert parsed.cwd == "."
    assert parsed.timeout_seconds == 60.0
    assert parsed.max_output_chars == 15000

    with pytest.raises(ValidationError):
        ShellCommandInput.model_validate({"command": "   "})
    with pytest.raises(ValidationError):
        ShellCommandInput.model_validate({"command": "echo bad\x00cmd"})

    src = inspect.getsource(shell_tools)
    assert "subprocess.run" not in src
    assert "subprocess.Popen" not in src


async def test_shell_tool_dual_channel_sync_and_async_consistency(
    tmp_path: Path,
) -> None:
    """Async .ainvoke() and sync .invoke() share identical runtime, limits, and timeout semantics."""
    ws = tmp_path / "ws_dual"
    with _patch_workspace(ws):
        # 1. Normal execution through both channels
        cmd_ok = f'"{sys.executable}" -c "print(\'dual-ok\')"'
        async_ok = await execute_shell_command.ainvoke({"command": cmd_ok})
        sync_ok = execute_shell_command.invoke({"command": cmd_ok})
        assert "Exit Code: 0" in async_ok
        assert "dual-ok" in async_ok
        assert sync_ok == async_ok

        # 2. Timeout enforcement through both channels
        cmd_sleep = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        async_to = await execute_shell_command.ainvoke(
            {"command": cmd_sleep, "timeout_seconds": 0.4}
        )
        sync_to = execute_shell_command.invoke(
            {"command": cmd_sleep, "timeout_seconds": 0.4}
        )
        assert "Error: Command timed out after 0.4 seconds" in async_to
        assert sync_to == async_to

        # 3. Byte-budget overflow enforcement through both channels
        cmd_flood = (
            f'"{sys.executable}" -c "import sys; sys.stdout.write(\'F\' * 8000)"'
        )
        async_lim = await execute_shell_command.ainvoke(
            {"command": cmd_flood, "max_output_chars": 150}
        )
        sync_lim = execute_shell_command.invoke(
            {"command": cmd_flood, "max_output_chars": 150}
        )
        assert "Output exceeded limit" in async_lim
        assert "Output exceeded limit" in sync_lim

    assert _runner_task_names() == []


def test_shell_cwd_workspace_boundary_protection(tmp_path: Path) -> None:
    """Enforce all 6 workspace confinement cases on shell cwd before creating any directory."""
    ws = tmp_path / "ws_cwd"
    outside_target = tmp_path / "escaped_dir"

    with _patch_workspace(ws):
        # 1. Default "." anchors clean workspace
        resolved_root = resolve_shell_cwd(".")
        assert resolved_root == Path(os.path.realpath(str(ws)))
        assert ws.exists()

        # 2. Valid nested relative directory
        nested = resolve_shell_cwd("pkg/sub")
        assert nested.exists()
        assert nested.is_relative_to(resolved_root)

        # 3. Relative escape
        with pytest.raises(PermissionError, match="escapes workspace root"):
            resolve_shell_cwd("../escaped_dir")
        assert not outside_target.exists()

        # 4. System absolute path escape
        abs_outside = (
            r"C:\Windows\System32\jarvis_forbidden_test"
            if sys.platform == "win32"
            else "/etc/jarvis_forbidden_test"
        )
        with pytest.raises(PermissionError, match="escapes workspace root"):
            resolve_shell_cwd(abs_outside)

        # 5. Cross-drive / UNC / malformed inputs
        with pytest.raises(PermissionError, match="UNC paths"):
            resolve_shell_cwd(r"\\evil-server\share\dir")
        with pytest.raises(PermissionError, match="UNC paths"):
            resolve_shell_cwd("//evil-server/share/dir")
        with pytest.raises(ValueError, match="null byte"):
            resolve_shell_cwd("bad\x00dir")
        with pytest.raises(ValueError, match="Invalid colon"):
            resolve_shell_cwd("bad:subdir")
        with pytest.raises(ValueError, match="empty"):
            resolve_shell_cwd("   ")

        if sys.platform == "win32":
            other_drive = "Z:" if not str(ws).upper().startswith("Z:") else "Y:"
            with pytest.raises(PermissionError, match="escapes workspace root"):
                resolve_shell_cwd(f"{other_drive}\\outside_drive")

        # 6. Non-existent subdir beneath external symlink ancestor
        symlink_dir = ws / "link_outside"
        new_sub_via_link = "link_outside/created_outside"
        try:
            os.symlink(str(outside_target), str(symlink_dir), target_is_directory=True)
        except (OSError, NotImplementedError):
            orig_realpath = os.path.realpath

            def fake_realpath(path, *args, **kwargs):
                if "link_outside" in str(path):
                    return str(outside_target)
                return orig_realpath(path, *args, **kwargs)

            with (
                patch("os.path.realpath", side_effect=fake_realpath),
                pytest.raises(
                    PermissionError, match=r"(outside|escapes) workspace root"
                ),
            ):
                resolve_shell_cwd(new_sub_via_link)
        else:
            with pytest.raises(
                PermissionError, match=r"(outside|escapes) workspace root"
            ):
                resolve_shell_cwd(new_sub_via_link)
            assert not (outside_target / "created_outside").exists()

    # Tool-level call also refuses escaped cwd instead of creating directories outside workspace
    with _patch_workspace(ws):
        with pytest.raises(PermissionError):
            execute_shell_command.invoke(
                {"command": "echo nope", "cwd": "../never_created_outside"}
            )
        assert not (tmp_path / "never_created_outside").exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows .cmd wrapper lifecycle test"
)
async def test_shell_win32_cmd_quoting_multiline_utf8_and_temp_cleanup(
    tmp_path: Path,
) -> None:
    """Verify UTF-8 no-BOM CRLF .cmd scripts, complex quoting, exit codes, and clean unlinking."""
    ws = tmp_path / "ws_cmd"
    temp_root = Path(tempfile.gettempdir())
    before_cmds = set(temp_root.glob("jarvis_cmd_*.cmd"))

    # Inspect raw bytes produced by _create_win32_temp_cmd
    probe_script = _create_win32_temp_cmd("echo first\necho second")
    try:
        raw = probe_script.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")  # strictly NO UTF-8 BOM
        assert b"\r\n@chcp 65001 >nul 2>&1\r\n" in raw
        assert probe_script.parent.resolve() == temp_root.resolve()
        assert not probe_script.is_relative_to(ws)
    finally:
        probe_script.unlink(missing_ok=True)

    # Execute quoted binary path + UTF-8 echo + multiline && + non-zero exit code 5
    complex_cmd = (
        "echo 你好世界\n"
        f"\"{sys.executable}\" -c \"import sys; sys.stdout.buffer.write('引号测试\\n'.encode('utf-8'))\"\n"
        f'echo chain && "{sys.executable}" -c "import sys; sys.exit(5)"'
    )
    with _patch_workspace(ws):
        out = await execute_shell_command.ainvoke({"command": complex_cmd})

    assert "Exit Code: 5" in out
    assert "你好世界" in out
    assert "引号测试" in out
    assert "chain" in out
    assert list(ws.glob("*.cmd")) == []
    assert set(temp_root.glob("jarvis_cmd_*.cmd")) == before_cmds

    # Even if temp unlink raises OSError, do not clobber result or swallow CancelledError.
    with (
        _patch_workspace(ws),
        patch.object(Path, "unlink", side_effect=OSError("mocked file lock")),
    ):
        res_locked = await execute_shell_command.ainvoke(
            {"command": f'"{sys.executable}" -c "print(\'still-returned\')"'}
        )
        assert "Exit Code: 0" in res_locked
        assert "still-returned" in res_locked

        task = asyncio.create_task(
            execute_shell_command.ainvoke(
                {"command": f'"{sys.executable}" -c "import time; time.sleep(10)"'}
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Clean up the locked test leftovers manually since Path.unlink was patched above.
    for leftover in set(temp_root.glob("jarvis_cmd_*.cmd")) - before_cmds:
        leftover.unlink(missing_ok=True)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows HITL + Job Object E2E cancel test"
)
async def test_shell_executor_hitl_double_cancel_process_tree_and_temp_cleanup(
    tmp_path: Path,
) -> None:
    """End-to-end: HITL EXECUTING -> repeated Task.cancel() kills tree, deletes .cmd, marks FAILED."""
    ws = tmp_path / "ws_e2e"
    ws.mkdir(parents=True, exist_ok=True)
    ready_file = ws / "e2e_pids.txt"
    temp_root = Path(tempfile.gettempdir())
    before_cmds = set(temp_root.glob("jarvis_cmd_*.cmd"))

    child_script = (
        "import os, subprocess, sys, time; "
        f"gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        f"creationflags={_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}); "
        f"open({str(ready_file)!r}, 'w').write(f'{{os.getpid()}}\\n{{gc.pid}}\\n'); "
        "time.sleep(60)"
    )
    args = {"command": f'"{sys.executable}" -c "{child_script}"'}

    reg = ToolRegistry()
    reg.register(execute_shell_command)

    hitl = get_hitl_manager()
    suffix = uuid.uuid4().hex[:8]
    thread_id = f"thread_shell_e2e_{suffix}"
    call_id = f"call_shell_e2e_{suffix}"
    req = hitl.create_request(
        thread_id=thread_id,
        run_id=f"run_{suffix}",
        tool_name="execute_shell_command",
        arguments=args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(
        req.id,
        ApprovalDecision(decision="approve"),
        thread_id=thread_id,
    )

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="run shell",
                tool_calls=[
                    {"id": call_id, "name": "execute_shell_command", "args": args}
                ],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    with (
        warnings.catch_warnings(record=True) as caught,
        _patch_workspace(ws),
    ):
        warnings.simplefilter("always", ResourceWarning)
        exec_task = asyncio.create_task(executor(state))

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
        assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING
        for pid in pids:
            assert _is_win_pid_alive(pid)

        # Consecutive double cancellation against real executor + Job Object subprocess tree
        exec_task.cancel()
        await asyncio.sleep(0.02)
        exec_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await exec_task

        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()

    # 1. HITL lease atomically transitioned to FAILED (no replay possible)
    updated_req = hitl.get_request(req.id)
    assert updated_req.status == ApprovalStatus.FAILED
    assert "CancelledError" in (updated_req.execution_error or "")

    # 2. Both child and detached grandchild PIDs dead in OS
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if all(not _is_win_pid_alive(pid) for pid in pids):
            break
        await asyncio.sleep(0.05)
    for pid in pids:
        assert not _is_win_pid_alive(pid)

    # 3. Temp .cmd file deleted and zero runner tasks or ResourceWarnings left behind
    assert set(temp_root.glob("jarvis_cmd_*.cmd")) == before_cmds
    assert list(ws.glob("*.cmd")) == []
    assert _runner_task_names() == []
    rws = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert rws == [], [str(w.message) for w in rws]


def _is_posix_pid_live_or_zombie(pid: int) -> tuple[bool, bool]:
    """Return (exists_in_os, is_zombie) for a POSIX PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return (False, False)
    except PermissionError:
        return (True, False)

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            text = stat_path.read_text(encoding="utf-8", errors="ignore")
            rparen = text.rfind(")")
            if rparen != -1:
                return (True, text[rparen + 2 : rparen + 3] == "Z")
        except OSError:
            pass
    return (True, False)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX /bin/bash -c Shell Tool integration test"
)
async def test_shell_posix_bash_quoting_multiline_utf8_pipes_and_exit_code(
    tmp_path: Path,
) -> None:
    """Verify POSIX /bin/bash -c handles quotes, pipes, multiline UTF-8, and exit codes."""
    ws = tmp_path / "ws_posix_bash"
    temp_root = Path(tempfile.gettempdir())
    before_cmds = set(temp_root.glob("jarvis_cmd_*.cmd"))

    complex_cmd = (
        "printf '%s\\n' \"你好世界\"\n"
        f'"{sys.executable}" -c "print(\'quote-ok\')" | cat\n'
        f'echo "pipe-ok" && "{sys.executable}" -c "import sys; sys.exit(5)"'
    )
    with _patch_workspace(ws):
        out_async = await execute_shell_command.ainvoke({"command": complex_cmd})
        out_sync = execute_shell_command.invoke({"command": complex_cmd})

    for out in (out_async, out_sync):
        assert "Exit Code: 5" in out
        assert "你好世界" in out
        assert "quote-ok" in out
        assert "pipe-ok" in out

    assert list(ws.glob("*.cmd")) == []
    assert set(temp_root.glob("jarvis_cmd_*.cmd")) == before_cmds
    assert _runner_task_names() == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX HITL + Executor E2E process-group cancel test",
)
async def test_shell_posix_executor_hitl_double_cancel_kills_pgid_and_marks_failed(
    tmp_path: Path,
) -> None:
    """End-to-end on Linux: HITL EXECUTING -> double cancel kills bash pgid, frees FDs, marks FAILED."""
    ws = tmp_path / "ws_posix_e2e"
    ws.mkdir(parents=True, exist_ok=True)
    ready_file = ws / "posix_e2e_pids.txt"
    before_fds = (
        len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").exists() else None
    )

    child_script = (
        "import os, subprocess, sys, time; "
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(ready_file)!r}, 'w').write(f'{{os.getpid()}}\\n{{gc.pid}}\\n'); "
        "time.sleep(60)"
    )
    args = {"command": f'"{sys.executable}" -c "{child_script}"'}

    reg = ToolRegistry()
    reg.register(execute_shell_command)

    hitl = get_hitl_manager()
    suffix = uuid.uuid4().hex[:8]
    thread_id = f"thread_posix_e2e_{suffix}"
    call_id = f"call_posix_e2e_{suffix}"
    req = hitl.create_request(
        thread_id=thread_id,
        run_id=f"run_{suffix}",
        tool_name="execute_shell_command",
        arguments=args,
        tool_call_id=call_id,
    )
    hitl.resolve_request(
        req.id,
        ApprovalDecision(decision="approve"),
        thread_id=thread_id,
    )

    executor = create_tool_executor_node(reg)
    state = {
        "messages": [
            AIMessage(
                content="run posix shell",
                tool_calls=[
                    {"id": call_id, "name": "execute_shell_command", "args": args}
                ],
            )
        ],
        "run_context": {"session_id": thread_id},
        "approval_decision": {
            "decisions_by_call_id": {
                call_id: {
                    "approval_id": req.id,
                    "tool_call_id": call_id,
                    "decision": "approve",
                    "arguments_hash": req.arguments_hash,
                }
            }
        },
    }

    with (
        warnings.catch_warnings(record=True) as caught,
        _patch_workspace(ws),
    ):
        warnings.simplefilter("always", ResourceWarning)
        exec_task = asyncio.create_task(executor(state))

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
        assert hitl.get_request(req.id).status == ApprovalStatus.EXECUTING
        for pid in pids:
            assert _is_posix_pid_live_or_zombie(pid) == (True, False)

        # Consecutive double cancellation against real executor + POSIX process group
        exec_task.cancel()
        await asyncio.sleep(0.02)
        exec_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await exec_task

        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()

    # 1. HITL lease atomically transitioned to FAILED (no replay possible)
    updated_req = hitl.get_request(req.id)
    assert updated_req.status == ApprovalStatus.FAILED
    assert "CancelledError" in (updated_req.execution_error or "")

    # 2. All process group PIDs dead and non-zombie in OS
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if all(not _is_posix_pid_live_or_zombie(pid)[0] for pid in pids):
            break
        await asyncio.sleep(0.05)
    for pid in pids:
        assert _is_posix_pid_live_or_zombie(pid) == (False, False)

    # 3. Zero task, FD, or ResourceWarning leaks
    assert _runner_task_names() == []
    rws = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert rws == [], [str(w.message) for w in rws]
    if before_fds is not None:
        assert len(os.listdir("/proc/self/fd")) <= before_fds
