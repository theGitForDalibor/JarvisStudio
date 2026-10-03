"""Production filesystem tools with safety policies."""

import os
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

from jarvis_studio.config.settings import get_settings
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety


def _resolve_safe_path(target_path: str) -> Path:
    """Resolve target path against the configured workspace root."""
    settings = get_settings()
    workspace = Path(settings.storage.workspace_root).resolve()
    resolved = (workspace / target_path).resolve() if not Path(target_path).is_absolute() else Path(target_path).resolve()
    return resolved


@tool
def read_file(file_path: str, max_chars: int = 20000) -> str:
    """Read the text content of a file at the specified path."""
    try:
        path = _resolve_safe_path(file_path)
        if not path.exists():
            return f"Error: File does not exist at {file_path}"
        if not path.is_file():
            return f"Error: Path {file_path} is a directory, not a file"

        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n... [Truncated: {len(content) - max_chars} characters omitted]"
        return content
    except Exception as e:
        return f"Error reading file {file_path}: {e}"


@tool
def write_file(file_path: str, content: str, overwrite: bool = True) -> str:
    """Write or overwrite content to a file at the specified path."""
    try:
        path = _resolve_safe_path(file_path)
        if path.exists() and not overwrite:
            return f"Error: File {file_path} already exists and overwrite=False"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error writing file {file_path}: {e}"


@tool
def list_directory(directory_path: str = ".") -> str:
    """List files and subdirectories within a directory."""
    try:
        path = _resolve_safe_path(directory_path)
        if not path.exists():
            return f"Error: Directory does not exist at {directory_path}"
        if not path.is_dir():
            return f"Error: Path {directory_path} is not a directory"

        items = []
        for item in sorted(path.iterdir()):
            kind = "DIR" if item.is_dir() else "FILE"
            size = f"{item.stat().st_size} B" if item.is_file() else "-"
            items.append(f"[{kind}] {item.name} ({size})")

        return "\n".join(items) if items else "(Empty directory)"
    except Exception as e:
        return f"Error listing directory {directory_path}: {e}"


# Tag safety levels
tag_tool_safety(read_file, risk_level=RiskLevel.SAFE, category="filesystem")
tag_tool_safety(list_directory, risk_level=RiskLevel.SAFE, category="filesystem")
tag_tool_safety(
    write_file,
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="filesystem",
)
