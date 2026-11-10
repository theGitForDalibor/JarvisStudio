"""Production filesystem tools with comprehensive workspace safety policies."""

import os
from pathlib import Path

from langchain_core.tools import tool

from jarvis_studio.config.settings import get_settings
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety


def resolve_safe_path(
    target_path: str | Path,
    workspace_root: str | Path | None = None,
    allow_symlinks_outside: bool = False,
) -> Path:
    """Resolve and rigorously validate target_path within workspace_root.

    Guards against:
    1. Directory traversal (../../)
    2. Absolute paths outside workspace
    3. Windows drive jumping (C: -> D:)
    4. UNC paths (\\\\server\\share or //server/share)
    5. Symlink escape (links pointing outside workspace)
    6. Special characters (null bytes, invalid colons)
    7. Circular symlinks
    8. Escaping via non-existent nested parent directories

    Raises:
        ValueError: For malformed paths (null bytes, invalid characters, empty).
        PermissionError: If target path escapes workspace root or points to an external symlink.
    """
    if target_path is None:
        raise ValueError("Target path cannot be None")

    path_str = str(target_path).strip()
    if not path_str:
        raise ValueError("Target path cannot be empty")

    if "\x00" in path_str:
        raise ValueError("Access denied: Path contains null byte")

    # Windows UNC paths check (e.g. \\server\share or //server/share)
    if path_str.startswith(("\\\\", "//")):
        raise PermissionError(
            f"Access denied: UNC paths are not permitted ({path_str})"
        )

    # Colon check: colon is only valid at index 1 for Windows drive letter (e.g. C:)
    for idx, ch in enumerate(path_str):
        if ch == ":" and (idx != 1 or not path_str[0].isalpha()):
            raise ValueError(f"Access denied: Invalid colon in path ({path_str})")

    # Determine canonical workspace root
    if workspace_root is None:
        settings = get_settings()
        ws_base = Path(settings.storage.workspace_root)
    else:
        ws_base = Path(workspace_root)

    try:
        ws = Path(os.path.realpath(str(ws_base))).resolve()
    except (OSError, RuntimeError):
        ws = ws_base.resolve()

    raw = Path(path_str)
    try:
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (ws / raw).resolve()
    except (RecursionError, OSError) as e:
        raise PermissionError(f"Access denied: Symlink loop or unresolvable path: {e}")

    # Verify containment within workspace
    try:
        is_inside = candidate.is_relative_to(ws)
    except ValueError:
        # Raised on Windows when paths are on different drives
        is_inside = False

    if not is_inside:
        raise PermissionError(
            f"Access denied: Path '{path_str}' escapes workspace root '{ws}'"
        )

    # Check for symlink loops or unresolvable paths
    try:
        real_target = Path(os.path.realpath(str(candidate)))
    except (RecursionError, OSError) as e:
        raise PermissionError(f"Access denied: Symlink loop or unresolvable path: {e}")

    # Verify canonical target is within workspace
    if not allow_symlinks_outside:
        try:
            if not real_target.is_relative_to(ws):
                raise PermissionError(
                    f"Access denied: Path '{path_str}' resolves to '{real_target}' which is outside workspace root '{ws}'"
                )
        except ValueError:
            raise PermissionError(
                f"Access denied: Path '{path_str}' resolves outside workspace root '{ws}'"
            )

    # If candidate does not exist yet (e.g. creating new file), check existing ancestors
    if not candidate.exists():
        curr = candidate.parent
        while curr != curr.parent:
            if curr.exists():
                try:
                    real_curr = Path(os.path.realpath(str(curr)))
                    if not real_curr.is_relative_to(ws):
                        raise PermissionError(
                            f"Access denied: Parent directory '{curr}' resolves outside workspace root '{ws}'"
                        )
                except (RecursionError, OSError) as e:
                    raise PermissionError(f"Access denied: Parent symlink error: {e}")
                break
            curr = curr.parent

    return candidate


# Backward-compatible alias
_resolve_safe_path = resolve_safe_path


@tool
def read_file(file_path: str, max_chars: int = 20000) -> str:
    """Read the text content of a file at the specified path."""
    try:
        path = resolve_safe_path(file_path)
        if not path.exists():
            return f"Error: File does not exist at {file_path}"
        if not path.is_file():
            return f"Error: Path {file_path} is a directory, not a file"

        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return (
                content[:max_chars]
                + f"\n... [Truncated: {len(content) - max_chars} characters omitted]"
            )
        return content
    except Exception as e:  # noqa: BLE001
        return f"Error reading file {file_path}: {e}"


@tool
def write_file(file_path: str, content: str, overwrite: bool = True) -> str:
    """Write or overwrite content to a file at the specified path."""
    try:
        path = resolve_safe_path(file_path)
        if path.is_dir():
            return f"Error: Path {file_path} is a directory, not a file"
        if path.exists() and not overwrite:
            return f"Error: File {file_path} already exists and overwrite=False"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} characters to {file_path}"
    except Exception as e:  # noqa: BLE001
        return f"Error writing file {file_path}: {e}"


@tool
def list_directory(directory_path: str = ".") -> str:
    """List files and subdirectories within a directory."""
    try:
        path = resolve_safe_path(directory_path)
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
    except Exception as e:  # noqa: BLE001
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
