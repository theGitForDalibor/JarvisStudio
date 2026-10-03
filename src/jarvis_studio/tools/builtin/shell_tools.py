"""Production terminal and shell execution tools with HITL enforcement."""

import subprocess
import shlex
import sys
from pathlib import Path
from langchain_core.tools import tool

from jarvis_studio.config.settings import get_settings
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety


@tool
def execute_shell_command(
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 60,
    max_output_chars: int = 15000,
) -> str:
    """Execute a system shell command within the workspace and capture stdout/stderr."""
    settings = get_settings()
    workspace = Path(settings.storage.workspace_root).resolve()
    target_cwd = (workspace / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()

    if not target_cwd.exists():
        target_cwd.mkdir(parents=True, exist_ok=True)

    try:
        # Use powershell on windows or standard shell
        shell_executable = "powershell" if sys.platform == "win32" else "/bin/bash"

        result = subprocess.run(
            command,
            shell=True,
            executable=shell_executable if sys.platform != "win32" else None,
            cwd=str(target_cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = f"Exit Code: {result.returncode}\n"

        if stdout:
            combined += f"--- STDOUT ---\n{stdout}\n"
        if stderr:
            combined += f"--- STDERR ---\n{stderr}\n"

        if len(combined) > max_output_chars:
            return combined[:max_output_chars] + f"\n... [Truncated: {len(combined) - max_output_chars} chars omitted]"

        return combined.strip()

    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout_seconds} seconds"
    except Exception as e:
        return f"Error executing shell command: {e}"


# Tag with HIGH risk - strictly requires HITL approval
tag_tool_safety(
    execute_shell_command,
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="system",
)
