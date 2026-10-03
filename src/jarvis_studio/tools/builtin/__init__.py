"""Builtin tools package for Jarvis Studio."""

from jarvis_studio.tools.builtin.file_tools import (
    list_directory,
    read_file,
    write_file,
)
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all builtin tools into the designated registry."""
    registry.register(read_file)
    registry.register(list_directory)
    registry.register(write_file)
    registry.register(execute_shell_command)


# Register into default registry immediately
register_builtin_tools(get_default_registry())

__all__ = [
    "read_file",
    "write_file",
    "list_directory",
    "execute_shell_command",
    "register_builtin_tools",
]
