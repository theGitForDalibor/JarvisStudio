"""Checkpoint Garbage Collection & Maintenance for LangGraph checkpoints."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from jarvis_studio.core.persistence.sqlite import (
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)

_logger = logging.getLogger(__name__)


def purge_task_checkpoint_sync(
    thread_id: str,
    *,
    db_path: str | Path | None = None,
    checkpointer: Any = None,
) -> bool:
    """Synchronously delete all checkpoints and writes for a thread.

    If checkpointer is provided and has `delete_thread`, attempts that first.
    Additionally, performs direct SQLite table cleanup when db_path is provided.
    """
    cleaned = False
    if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
        try:
            checkpointer.delete_thread(thread_id)
            cleaned = True
        except Exception as cp_exc:  # noqa: BLE001
            _logger.debug("checkpointer.delete_thread failed (falling back to direct DB): %s", cp_exc)

    if db_path is not None:
        norm_path = normalize_db_path(db_path)
        try:
            with sync_transaction(norm_path) as conn:
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "checkpoints" in tables:
                    conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                if "writes" in tables:
                    conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                if "checkpoint_blobs" in tables:
                    conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = ?", (thread_id,))
                if "checkpoint_writes" in tables:
                    conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = ?", (thread_id,))
            cleaned = True
        except Exception as db_exc:  # noqa: BLE001
            _logger.warning("Direct DB checkpoint purge failed for thread %s: %s", thread_id, db_exc)

    return cleaned


async def purge_task_checkpoint_async(
    thread_id: str,
    *,
    checkpointer: Any = None,
    db_path: str | Path | None = None,
) -> bool:
    """Asynchronously delete all checkpoints and writes for a thread."""
    cleaned = False
    if checkpointer is not None and hasattr(checkpointer, "adelete_thread"):
        try:
            await checkpointer.adelete_thread(thread_id)
            cleaned = True
        except Exception as cp_exc:  # noqa: BLE001
            _logger.debug("checkpointer.adelete_thread failed: %s", cp_exc)

    if db_path is not None:
        try:
            await asyncio.to_thread(purge_task_checkpoint_sync, thread_id, db_path=db_path)
            cleaned = True
        except Exception as db_exc:  # noqa: BLE001
            _logger.warning("Async direct DB checkpoint purge failed for thread %s: %s", thread_id, db_exc)

    return cleaned


def clean_dangling_checkpoints(
    db_path: str | Path,
    *,
    checkpointer: Any = None,
) -> dict[str, Any]:
    """Find and purge checkpoints not bound to any active agent task or chat session.

    A thread_id in `checkpoints` is dangling if:
    1. It does NOT exist in `agent_tasks.thread_id`
    2. It does NOT exist in `chat_sessions.session_id`

    Returns a summary dict with scanned_threads, purged_threads, and dangling_threads list.
    """
    norm_path = normalize_db_path(db_path)

    with open_sync_connection(norm_path) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "checkpoints" not in tables:
            return {
                "scanned_threads": 0,
                "purged_threads": 0,
                "dangling_threads": [],
            }

        # 1. Collect all distinct thread_ids in checkpoints
        all_threads = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id IS NOT NULL"
            ).fetchall()
            if r[0] is not None
        }

        # 2. Collect valid thread_ids from agent_tasks
        valid_task_threads = set()
        if "agent_tasks" in tables:
            valid_task_threads = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT thread_id FROM agent_tasks WHERE thread_id IS NOT NULL"
                ).fetchall()
                if r[0] is not None
            }

        # 3. Collect valid session_ids from chat_sessions
        valid_session_threads = set()
        if "chat_sessions" in tables:
            valid_session_threads = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT session_id FROM chat_sessions WHERE session_id IS NOT NULL"
                ).fetchall()
                if r[0] is not None
            }

        valid_threads = valid_task_threads | valid_session_threads
        dangling = sorted(list(all_threads - valid_threads))

    if not dangling:
        return {
            "scanned_threads": len(all_threads),
            "purged_threads": 0,
            "dangling_threads": [],
        }

    # 4. Purge all dangling threads atomically
    with sync_transaction(norm_path) as tx_conn:
        for tid in dangling:
            tx_conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (tid,))
            if "writes" in tables:
                tx_conn.execute("DELETE FROM writes WHERE thread_id = ?", (tid,))
            if "checkpoint_blobs" in tables:
                tx_conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = ?", (tid,))
            if "checkpoint_writes" in tables:
                tx_conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = ?", (tid,))

    _logger.info(
        "Cleaned %d dangling checkpoint threads out of %d total threads in %s",
        len(dangling),
        len(all_threads),
        norm_path,
    )
    return {
        "scanned_threads": len(all_threads),
        "purged_threads": len(dangling),
        "dangling_threads": dangling,
    }
