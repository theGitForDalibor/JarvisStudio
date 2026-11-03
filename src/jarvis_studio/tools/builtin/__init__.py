"""Builtin tools package for Jarvis Studio."""

from jarvis_studio.tools.builtin.file_tools import (
    list_directory,
    read_file,
    write_file,
)
from jarvis_studio.tools.builtin.memory_tools import (
    recall_personal_memory,
    save_personal_memory,
)
from jarvis_studio.tools.builtin.rag_tools import (
    index_workspace_directory,
    list_indexed_documents,
    search_knowledge_base,
)
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all builtin tools into the designated registry."""
    # File tools
    registry.register(read_file)
    registry.register(list_directory)
    registry.register(write_file)

    # Shell tools
    registry.register(execute_shell_command)

    # Long-Term Memory (LTM) tools
    registry.register(save_personal_memory)
    registry.register(recall_personal_memory)

    # Local Knowledge Base & RAG tools
    registry.register(search_knowledge_base)
    registry.register(index_workspace_directory)
    registry.register(list_indexed_documents)


# Register into default registry immediately
register_builtin_tools(get_default_registry())

__all__ = [
    "execute_shell_command",
    "index_workspace_directory",
    "list_directory",
    "list_indexed_documents",
    "read_file",
    "recall_personal_memory",
    "register_builtin_tools",
    "save_personal_memory",
    "search_knowledge_base",
    "write_file",
]
