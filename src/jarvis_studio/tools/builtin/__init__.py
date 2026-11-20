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
from jarvis_studio.tools.builtin.report_tools import publish_research_report
from jarvis_studio.tools.builtin.search_tools import search_web
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.builtin.web_fetch_tools import fetch_web_page
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry

_BUILTIN_TOOLS = (
    read_file,
    list_directory,
    write_file,
    execute_shell_command,
    save_personal_memory,
    recall_personal_memory,
    search_knowledge_base,
    index_workspace_directory,
    list_indexed_documents,
    search_web,
    fetch_web_page,
    publish_research_report,
)


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Idempotently register all builtin tools into the designated registry."""
    for builtin_tool in _BUILTIN_TOOLS:
        if registry.get(builtin_tool.name) is not builtin_tool:
            registry.register(builtin_tool)


# Register into default registry immediately
register_builtin_tools(get_default_registry())

__all__ = [
    "execute_shell_command",
    "fetch_web_page",
    "index_workspace_directory",
    "list_directory",
    "list_indexed_documents",
    "publish_research_report",
    "read_file",
    "recall_personal_memory",
    "register_builtin_tools",
    "save_personal_memory",
    "search_knowledge_base",
    "search_web",
    "write_file",
]
