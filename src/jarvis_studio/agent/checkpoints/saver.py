"""Checkpointer persistence factory for LangGraph state."""

import sqlite3
from pathlib import Path
from typing import Any, Optional
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger


_cached_checkpointer: Optional[BaseCheckpointSaver] = None


def get_checkpointer(backend: Optional[str] = None, db_path: Optional[str] = None) -> BaseCheckpointSaver:
    """Retrieve or create the designated checkpointer instance."""
    global _cached_checkpointer
    if _cached_checkpointer is not None:
        return _cached_checkpointer

    settings = get_settings()
    chosen_backend = backend or settings.storage.checkpoint_backend
    target_path = db_path or settings.storage.sqlite_path

    if chosen_backend.lower() == "sqlite":
        try:
            path_obj = Path(target_path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path_obj), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            logger.info(f"Initialized SqliteSaver at '{target_path}'")
            _cached_checkpointer = saver
            return saver
        except Exception as e:
            logger.warning(
                f"Failed to initialize SqliteSaver at '{target_path}': {e}. Falling back to MemorySaver."
            )
            saver = MemorySaver()
            _cached_checkpointer = saver
            return saver

    logger.info("Initialized MemorySaver (in-memory checkpointing)")
    saver = MemorySaver()
    _cached_checkpointer = saver
    return saver
