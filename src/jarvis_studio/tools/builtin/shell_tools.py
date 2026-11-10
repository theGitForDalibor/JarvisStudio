"""Production terminal and shell execution tools backed by core.process."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.process import (
    ProcessExecutionSpec,
    ProcessResult,
    ProcessStatus,
    run_process,
)
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety
from jarvis_studio.tools.builtin.file_tools import resolve_safe_path

_MAX_SHELL_OUTPUT_BYTES_CAP = 1_048_576
_MIN_SHELL_OUTPUT_BYTES = 1024


class ShellCommandInput(BaseModel):
    """Schema for HITL-validated shell command execution arguments."""

    command: str = Field(..., description="The shell command string to execute.")
    cwd: str = Field(
        default=".",
        description="Working directory relative to workspace_root (strictly confined to workspace).",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Maximum execution wall-clock timeout in seconds.",
    )
    max_output_chars: int = Field(
        default=15000,
        gt=0,
        description="Maximum total characters returned in formatted tool output.",
    )

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("command must not be empty.")
        if "\x00" in value:
            raise ValueError("command must not contain NUL bytes.")
        return value


def resolve_shell_cwd(cwd: str = ".") -> Path:
    """Resolve and enforce workspace containment for shell ``cwd`` before creating directories."""
    settings = get_settings()
    ws_base = Path(settings.storage.workspace_root)
    # Ensure configured workspace_root exists first so resolve_safe_path can anchor "." or new
    # workspace-relative subdirectories without failing ancestor checks above workspace_root.
    ws_base.mkdir(parents=True, exist_ok=True)

    target_cwd = resolve_safe_path(cwd, workspace_root=ws_base)
    if target_cwd.exists() and not target_cwd.is_dir():
        raise ValueError(f"cwd points to a file instead of a directory: {cwd}")
    target_cwd.mkdir(parents=True, exist_ok=True)
    return target_cwd


def _create_win32_temp_cmd(command: str) -> Path:
    """Create an exclusive UTF-8 (no BOM, CRLF) temporary .cmd file in the OS temp directory."""
    normalized = (
        command.replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip("\n")
        .replace("\n", "\r\n")
    )
    script_body = f"@echo off\r\n@chcp 65001 >nul 2>&1\r\n{normalized}\r\n"
    payload = script_body.encode("utf-8")

    # Never place temp scripts in workspace_root; mkstemp uses O_CREAT | O_EXCL in OS temp dir.
    fd, raw_path = tempfile.mkstemp(prefix="jarvis_cmd_", suffix=".cmd")
    fd_wrapped = False
    try:
        handle = os.fdopen(fd, "wb")
        fd_wrapped = True
        with handle:
            handle.write(payload)
    except BaseException:
        if not fd_wrapped:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            Path(raw_path).unlink(missing_ok=True)
        raise
    return Path(raw_path)


def _cleanup_win32_temp_cmd(
    script_path: Path | None, *, pid: int | None = None
) -> None:
    """Delete temporary .cmd after process tree termination without masking errors/cancellations."""
    if script_path is None:
        return
    try:
        script_path.unlink(missing_ok=True)
    except PermissionError:
        # AV scanner or handle teardown can briefly hold file locks on Windows; one short retry.
        time.sleep(0.02)
        try:
            script_path.unlink(missing_ok=True)
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            logger.warning(
                f"Failed deleting temporary cmd script '{script_path}' "
                f"(pid={pid}, winerror={winerr}): {exc}"
            )
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        logger.warning(
            f"Failed deleting temporary cmd script '{script_path}' "
            f"(pid={pid}, winerror={winerr}): {exc}"
        )


def _compute_max_output_bytes(max_output_chars: int) -> int:
    """Compute bounded raw byte budget proportional to display character cap."""
    # Allow up to 4x bytes for multi-byte UTF-8 text while staying tightly bounded under floods.
    return min(
        max(max_output_chars * 4, _MIN_SHELL_OUTPUT_BYTES), _MAX_SHELL_OUTPUT_BYTES_CAP
    )


def _clip_for_display(text: str, max_output_chars: int) -> str:
    if len(text) <= max_output_chars:
        return text
    return (
        text[:max_output_chars]
        + f"\n... [Truncated: {len(text) - max_output_chars} chars omitted]"
    )


def _format_shell_result(
    result: ProcessResult,
    *,
    timeout_seconds: float,
    max_output_chars: int,
) -> str:
    """Render structured ProcessResult into consistent ToolMessage text."""
    if result.status == ProcessStatus.TIMEOUT:
        t_label = (
            str(int(timeout_seconds))
            if float(timeout_seconds).is_integer()
            else str(timeout_seconds)
        )
        parts = [f"Error: Command timed out after {t_label} seconds"]
        if result.stdout:
            parts.append(f"--- STDOUT (partial) ---\n{result.stdout}")
        if result.stderr:
            parts.append(f"--- STDERR (partial) ---\n{result.stderr}")
        return _clip_for_display("\n".join(parts).strip(), max_output_chars)

    if result.status == ProcessStatus.OUTPUT_LIMIT_EXCEEDED:
        header = (
            "Error: Output exceeded limit "
            f"(read {result.stdout_bytes_total + result.stderr_bytes_total} bytes, "
            f"retained {result.stdout_bytes_retained + result.stderr_bytes_retained} bytes); "
            "process terminated."
        )
        parts = [header]
        if result.stdout:
            parts.append(f"--- STDOUT (truncated) ---\n{result.stdout}")
        if result.stderr:
            parts.append(f"--- STDERR (truncated) ---\n{result.stderr}")
        return _clip_for_display("\n".join(parts).strip(), max_output_chars)

    if result.status == ProcessStatus.CANCELLED:
        return "Error: Command execution cancelled."

    if result.exit_code is None:
        err = result.error_message or "Unknown execution failure"
        return _clip_for_display(
            f"Error executing shell command: {err}", max_output_chars
        )

    combined = f"Exit Code: {result.exit_code}\n"
    if result.stdout:
        combined += f"--- STDOUT ---\n{result.stdout}\n"
    if result.stderr:
        combined += f"--- STDERR ---\n{result.stderr}\n"
    return _clip_for_display(combined.strip(), max_output_chars)


async def _execute_shell_command_async(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 60.0,
    max_output_chars: int = 15000,
) -> str:
    """Execute a shell command asynchronously through isolated Process Runtime."""
    validated = ShellCommandInput(
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    target_cwd = resolve_shell_cwd(validated.cwd)
    max_output_bytes = _compute_max_output_bytes(validated.max_output_chars)

    script_path: Path | None = None
    last_pid: int | None = None
    try:
        if sys.platform == "win32":
            script_path = _create_win32_temp_cmd(validated.command)
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            argv = [comspec, "/d", "/c", str(script_path)]
        else:
            posix_shell = (
                "/bin/bash"
                if Path("/bin/bash").exists()
                else (shutil.which("bash") or "/bin/sh")
            )
            argv = [posix_shell, "-c", validated.command]

        spec = ProcessExecutionSpec(
            argv=argv,
            cwd=target_cwd,
            timeout_seconds=float(validated.timeout_seconds),
            max_output_bytes=max_output_bytes,
        )
        result = await run_process(spec)
        last_pid = result.pid
        return _format_shell_result(
            result,
            timeout_seconds=float(validated.timeout_seconds),
            max_output_chars=validated.max_output_chars,
        )
    finally:
        # Process tree and Wait/Close already finished before run_process() exits or raises.
        _cleanup_win32_temp_cmd(script_path, pid=last_pid)


def _execute_shell_command_sync(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 60.0,
    max_output_chars: int = 15000,
) -> str:
    """Synchronous bridge invoking the exact same async Process Runtime path."""
    coro = _execute_shell_command_async(
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Inside an active loop, run asyncio.run in a single-worker thread so sync .invoke()
    # still enters AsyncProcessRunner + Proactor/JobObject rather than deadlocking the loop.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="jarvis-shell-sync",
    ) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


execute_shell_command = StructuredTool.from_function(
    func=_execute_shell_command_sync,
    coroutine=_execute_shell_command_async,
    name="execute_shell_command",
    description="Execute a system shell command within the workspace and capture stdout/stderr.",
    args_schema=ShellCommandInput,
)

# Tag with HIGH risk - strictly requires HITL approval
tag_tool_safety(
    execute_shell_command,
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="system",
)
