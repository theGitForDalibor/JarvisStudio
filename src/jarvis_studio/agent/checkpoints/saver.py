"""Checkpointer persistence factory for LangGraph state.

Phase 2 Stage 3:
- Removes immortal module-level checkpointer caching across event loops.
- `sqlite` checkpointers MUST be created and closed inside an async lifecycle
  (`AgentRuntime` or `open_managed_sqlite_checkpointer`).
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger

# Retained strictly as `None` for Phase 1 test singleton-reset compatibility.
_cached_checkpointer: BaseCheckpointSaver | None = None


def create_memory_checkpointer() -> MemorySaver:
    """Create a fresh per-lifecycle in-memory LangGraph checkpointer."""
    return MemorySaver()


def get_checkpointer(
    backend: str | None = None, db_path: str | None = None
) -> BaseCheckpointSaver:
    """Create a synchronous checkpointer for `memory` mode or fail closed for `sqlite`."""
    del db_path
    effective_backend = (backend or get_settings().storage.checkpoint_backend).strip().lower()
    if effective_backend == "memory":
        logger.debug("Created ephemeral MemorySaver checkpointer")
        return create_memory_checkpointer()
    if effective_backend == "sqlite":
        raise RuntimeError(
            "SQLite checkpointer requires an async managed lifecycle; use AgentRuntime "
            "or open_managed_sqlite_checkpointer() instead of sync get_checkpointer()."
        )
    raise ValueError(f"Unsupported checkpoint_backend: {effective_backend!r}")
