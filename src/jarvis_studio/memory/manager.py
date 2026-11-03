"""High-level Manager for Long-Term Memory (LTM)."""

from jarvis_studio.memory.models import MemoryCategory, MemoryItem
from jarvis_studio.memory.store import MemoryStore


class MemoryManager:
    """Manages cross-session long-term memory lifecycle, retrieval, and prompt injection."""

    def __init__(self, store: MemoryStore | None = None):
        self.store = store or MemoryStore()

    def remember(
        self,
        content: str,
        category: MemoryCategory = MemoryCategory.PREFERENCE,
        source: str | None = None,
        importance: float = 1.0,
    ) -> MemoryItem:
        """Store a new piece of information into long-term memory."""
        item = MemoryItem(
            category=category,
            content=content.strip(),
            source=source,
            importance=importance,
        )
        return self.store.add(item)

    def recall(
        self,
        query: str,
        category: MemoryCategory | None = None,
        limit: int = 5,
    ) -> list[MemoryItem]:
        """Search relevant memories matching a query."""
        return self.store.search(query=query, category=category, limit=limit)

    def forget(self, memory_id: str) -> bool:
        """Remove a memory record by ID."""
        return self.store.delete(memory_id)

    def list_all(
        self,
        category: MemoryCategory | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """Retrieve all active memories."""
        return self.store.list(category=category, limit=limit)

    def get_formatted_context(self, limit: int = 15) -> str:
        """Format top active user preferences and facts as a Markdown prompt block."""
        memories = self.store.list(limit=limit)
        if not memories:
            return ""

        lines = ["\n[User Long-Term Memory & Preferences]"]
        for m in memories:
            cat_tag = m.category.value.upper()
            lines.append(f"- [{cat_tag}] {m.content}")

        return "\n".join(lines)


_global_memory_manager: MemoryManager | None = None


def get_memory_manager(store: MemoryStore | None = None) -> MemoryManager:
    """Retrieve the global singleton MemoryManager instance."""
    global _global_memory_manager
    if _global_memory_manager is None or store is not None:
        _global_memory_manager = MemoryManager(store=store)
    return _global_memory_manager
