"""Checkpointer persistence factory for LangGraph state."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from jarvis_studio.core.observability.logger import logger

_cached_checkpointer: BaseCheckpointSaver | None = None


def get_checkpointer(
    backend: str | None = None, db_path: str | None = None
) -> BaseCheckpointSaver:
    """Retrieve or create the designated checkpointer instance."""
    global _cached_checkpointer
    if _cached_checkpointer is not None:
        return _cached_checkpointer

    # LangGraph's SqliteSaver does not implement async aget_tuple / astream_events.
    # We use MemorySaver for in-memory graph execution (100% async compatible)
    # while permanent multi-turn conversation and session state are persisted in SQLite by SessionService.
    logger.info("Initialized MemorySaver (async-compatible checkpointing engine)")
    saver = MemorySaver()
    _cached_checkpointer = saver
    return saver
