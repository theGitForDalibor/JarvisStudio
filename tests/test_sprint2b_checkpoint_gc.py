"""Tests for Sprint 2B Checkpoint Garbage Collection & Maintenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from jarvis_studio.agent.hitl.store import HITLStore
from jarvis_studio.config.models import StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.persistence.checkpoint_gc import (
    clean_dangling_checkpoints,
    purge_task_checkpoint_async,
    purge_task_checkpoint_sync,
)
from jarvis_studio.core.persistence.recovery import reconcile_on_startup
from jarvis_studio.core.persistence.runtime_lock import RuntimeInstanceLock
from jarvis_studio.core.persistence.sqlite import sync_transaction
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.service.deliverable_manager import DeliverableManager
from jarvis_studio.service.task_service import TaskService


def _init_mock_checkpoints_db(db_path: Path) -> None:
    """Create minimal checkpoints and writes tables matching LangGraph schema."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """
        )
        conn.commit()


@pytest.mark.asyncio
async def test_purge_task_checkpoint_sync_and_async(tmp_path: Path) -> None:
    """Verify sync and async checkpoint purge methods remove thread records."""
    db_file = tmp_path / "test_cp_purge.db"
    _init_mock_checkpoints_db(db_file)

    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES (?, ?)",
            ("thread_sync_1", "cp_1"),
        )
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, ?, ?, ?, ?)",
            ("thread_sync_1", "cp_1", "t1", 0, "messages"),
        )
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES (?, ?)",
            ("thread_async_2", "cp_2"),
        )
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, ?, ?, ?, ?)",
            ("thread_async_2", "cp_2", "t2", 0, "messages"),
        )
        conn.commit()

    # 1. Purge thread_sync_1 via sync
    assert purge_task_checkpoint_sync("thread_sync_1", db_path=db_file) is True

    with sqlite3.connect(str(db_file)) as conn:
        cp_rows = conn.execute("SELECT thread_id FROM checkpoints WHERE thread_id = 'thread_sync_1'").fetchall()
        assert len(cp_rows) == 0
        wr_rows = conn.execute("SELECT thread_id FROM writes WHERE thread_id = 'thread_sync_1'").fetchall()
        assert len(wr_rows) == 0

        # thread_async_2 must remain intact
        rem = conn.execute("SELECT thread_id FROM checkpoints WHERE thread_id = 'thread_async_2'").fetchall()
        assert len(rem) == 1

    # 2. Purge thread_async_2 via async
    assert await purge_task_checkpoint_async("thread_async_2", db_path=db_file) is True

    with sqlite3.connect(str(db_file)) as conn:
        rem2 = conn.execute("SELECT thread_id FROM checkpoints WHERE thread_id = 'thread_async_2'").fetchall()
        assert len(rem2) == 0


@pytest.mark.asyncio
async def test_clean_dangling_checkpoints_preserves_valid_and_purges_orphans(tmp_path: Path) -> None:
    """Verify clean_dangling_checkpoints deletes orphan threads while keeping active tasks & sessions."""
    db_file = tmp_path / "test_cp_gc.db"
    _init_mock_checkpoints_db(db_file)

    # Initialize app schema tables
    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(db_file),
            workspace_root=str(tmp_path / "workspace"),
        )
    )
    rt = AgentRuntime(cfg, register_as_active=False)
    await rt.start()

    try:
        coord = rt.task_coordinator
        # Create an active task with thread_id="thread_active_task"
        task = coord.create_task(query="Task with active thread")
        active_task_thread = task.thread_id

        # Insert a chat session with session_id="session_chat_1"
        with sync_transaction(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO chat_sessions (session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("session_chat_1", "Active Session", 1000.0, 1000.0),
            )

        # Populate checkpoints table with:
        # - active_task_thread (valid)
        # - session_chat_1 (valid)
        # - orphan_thread_1 (dangling)
        # - orphan_thread_2 (dangling)
        with sqlite3.connect(str(db_file)) as conn:
            for tid in (active_task_thread, "session_chat_1", "orphan_thread_1", "orphan_thread_2"):
                conn.execute(
                    "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES (?, ?)",
                    (tid, f"cp_{tid}"),
                )
                conn.execute(
                    "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, ?, ?, ?, ?)",
                    (tid, f"cp_{tid}", "t", 0, "ch"),
                )
            conn.commit()

        # Run GC
        report = clean_dangling_checkpoints(db_file)
        assert report["scanned_threads"] == 4
        assert report["purged_threads"] == 2
        assert sorted(report["dangling_threads"]) == ["orphan_thread_1", "orphan_thread_2"]

        # Verify DB contents
        with sqlite3.connect(str(db_file)) as conn:
            remaining_cp = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()}
            assert remaining_cp == {active_task_thread, "session_chat_1"}

            remaining_wr = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM writes").fetchall()}
            assert remaining_wr == {active_task_thread, "session_chat_1"}

        # Running GC again should report 0 purged
        second_report = clean_dangling_checkpoints(db_file)
        assert second_report["purged_threads"] == 0
        assert second_report["dangling_threads"] == []
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_task_service_purge_task_cleans_checkpoints(tmp_path: Path) -> None:
    """Verify TaskService.purge_task removes checkpoints along with task DB records and deliverables."""
    db_file = tmp_path / "test_purge_task_cp.db"
    _init_mock_checkpoints_db(db_file)

    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(db_file),
            workspace_root=str(tmp_path / "workspace"),
        )
    )
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    try:
        coord = rt.task_coordinator
        deliv_mgr = DeliverableManager(db_path=db_file, workspace_root=tmp_path / "workspace")
        task_service = TaskService(coordinator=coord, deliverable_mgr=deliv_mgr)

        task, _ = task_service.create_task("Task to be permanently purged")
        thread_id = task.thread_id

        # Insert dummy checkpoints for this task's thread
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES (?, ?)",
                (thread_id, "cp_task_target"),
            )
            conn.execute(
                "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, ?, ?, ?, ?)",
                (thread_id, "cp_task_target", "t", 0, "ch"),
            )
            conn.commit()

        # Purge task with confirm=True
        purged = task_service.purge_task(task.task_id, confirm=True)
        assert purged is True

        # Checkpoints for thread_id must be completely gone
        with sqlite3.connect(str(db_file)) as conn:
            cp_rows = conn.execute("SELECT * FROM checkpoints WHERE thread_id = ?", (thread_id,)).fetchall()
            assert len(cp_rows) == 0
            wr_rows = conn.execute("SELECT * FROM writes WHERE thread_id = ?", (thread_id,)).fetchall()
            assert len(wr_rows) == 0
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_startup_reconciliation_cleans_dangling_checkpoints(tmp_path: Path) -> None:
    """Verify reconcile_on_startup runs checkpoint GC when check_checkpoints=True."""
    db_file = tmp_path / "test_startup_cp_gc.db"
    _init_mock_checkpoints_db(db_file)

    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(db_file),
            workspace_root=str(tmp_path / "workspace"),
        )
    )
    rt = AgentRuntime(cfg, register_as_active=False)
    await rt.start()

    try:
        # Insert an orphan checkpoint
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES (?, ?)",
                ("stale_orphan_1", "cp_stale"),
            )
            conn.commit()

        hitl_store = HITLStore(db_path=db_file)
        report = await reconcile_on_startup(
            hitl_store=hitl_store,
            current_runtime_id="recovery_test",
            runtime_lock=rt.lock,
            check_checkpoints=True,
        )
        assert report.dangling_checkpoints_purged == 1

        # Checkpoint should be deleted
        with sqlite3.connect(str(db_file)) as conn:
            rem = conn.execute("SELECT * FROM checkpoints WHERE thread_id = 'stale_orphan_1'").fetchall()
            assert len(rem) == 0
    finally:
        await rt.shutdown()
