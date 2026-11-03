"""Built-in Tools for interacting with Long-Term Memory (LTM)."""

import json

from langchain_core.tools import tool

from jarvis_studio.memory.manager import get_memory_manager
from jarvis_studio.memory.models import MemoryCategory
from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata


@tool
def save_personal_memory(
    content: str,
    category: str = "preference",
    importance: float = 1.0,
) -> str:
    """Save an important user preference, developer profile, project rule, or persistent fact to long-term memory.

    Args:
        content: The text description of the habit, preference, or fact to remember.
        category: Memory category ('preference', 'profile', 'project', or 'fact'). Default is 'preference'.
        importance: Priority weight between 0.0 and 1.0. Default is 1.0.
    """
    mgr = get_memory_manager()
    try:
        cat_enum = MemoryCategory(category.lower())
    except ValueError:
        cat_enum = MemoryCategory.PREFERENCE

    item = mgr.remember(
        content=content,
        category=cat_enum,
        importance=max(0.0, min(1.0, importance)),
    )
    return f"Successfully saved memory (id: {item.id}) under category '{item.category.value}': {item.content}"


mark_tool_metadata(
    save_personal_memory,
    name="save_personal_memory",
    description="Save an important user preference, developer profile, or persistent fact to long-term memory across sessions.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="MEMORY",
)


@tool
def recall_personal_memory(
    query: str,
    category: str | None = None,
    limit: int = 5,
) -> str:
    """Search and retrieve stored user preferences, profile notes, or persistent facts from long-term memory.

    Args:
        query: Keywords to search for.
        category: Optional category filter ('preference', 'profile', 'project', 'fact').
        limit: Maximum number of memories to return (default 5).
    """
    mgr = get_memory_manager()
    cat_enum = None
    if category:
        try:
            cat_enum = MemoryCategory(category.lower())
        except ValueError:
            cat_enum = None

    items = mgr.recall(query=query, category=cat_enum, limit=limit)
    if not items:
        return f"No memories found matching query '{query}'."

    results = []
    for item in items:
        results.append(
            {
                "id": item.id,
                "category": item.category.value,
                "content": item.content,
                "importance": item.importance,
            }
        )
    return json.dumps(results, ensure_ascii=False, indent=2)


mark_tool_metadata(
    recall_personal_memory,
    name="recall_personal_memory",
    description="Search and retrieve stored user preferences, profile notes, or persistent facts from long-term memory.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="MEMORY",
)
