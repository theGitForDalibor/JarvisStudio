"""Tests for Sprint 2B Schema V5 migration and expanded task/run domain states."""

import sqlite3
from pathlib import Path

import pytest

from jarvis_studio.core.persistence.migrations import (
    APP_SCHEMA_VERSION,
    run_app_migrations,
)
from jarvis_studio.core.persistence.sqlite import open_sync_connection
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import (
    EventType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
)


def test_schema_v5_migration_and_domain_enums(tmp_path: Path):
    """Test clean migration from V4 baseline to V5 and verify extended CHECK constraints."""
    db_path = tmp_path / "v5_test.db"

    # 1. Initialize to V4 manually
    conn = open_sync_connection(db_path, enable_foreign_keys=False)
    try:
        conn.executescript(
            """
            CREATE TABLE chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT
            );
            CREATE TABLE agent_tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'CREATED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL',
                    'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'UNKNOWN', 'INTERRUPTED'
                )),
                query TEXT NOT NULL,
                initiator TEXT NOT NULL DEFAULT 'user',
                assigned_agent_id TEXT NOT NULL DEFAULT 'general_agent',
                current_run_id TEXT,
                plan_json TEXT,
                priority TEXT NOT NULL DEFAULT 'NORMAL',
                result_summary TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                archived_at REAL,
                runtime_id TEXT,
                error_message TEXT
            );
            CREATE TABLE agent_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_seq INTEGER NOT NULL,
                trigger_type TEXT NOT NULL CHECK (trigger_type IN ('initial', 'hitl_resume', 'recovery_resume')),
                status TEXT NOT NULL CHECK (status IN (
                    'RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED',
                    'CANCELLED', 'TIMEOUT', 'UNKNOWN'
                )),
                runtime_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                duration_ms INTEGER,
                checkpoint_id TEXT,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                UNIQUE (task_id, run_seq)
            );
            INSERT INTO chat_sessions VALUES ('sess_1', 'Session 1', 1.0, 1.0, NULL);
            INSERT INTO agent_tasks VALUES (
                'task_v4', 'sess_1', 'th_1', 'RUNNING', 'query', 'user',
                'general_agent', 'run_v4', NULL, 'NORMAL', NULL, 1.0, 1.0, NULL, NULL, 'rt', NULL
            );
            INSERT INTO agent_runs VALUES (
                'run_v4', 'task_v4', 1, 'initial', 'RUNNING', 'rt', 1.0, NULL, NULL, NULL, NULL
            );
            PRAGMA user_version = 4;
            """
        )
    finally:
        conn.close()

    # 2. Run migrations to advance to V5
    migrated_ver = run_app_migrations(db_path)
    assert migrated_ver == APP_SCHEMA_VERSION == 5

    # 3. Verify existing data preserved
    with open_sync_connection(db_path) as verify_conn:
        t_row = verify_conn.execute("SELECT status, query FROM agent_tasks WHERE task_id = 'task_v4'").fetchone()
        assert tuple(t_row) == ("RUNNING", "query")
        r_row = verify_conn.execute("SELECT status, trigger_type FROM agent_runs WHERE run_id = 'run_v4'").fetchone()
        assert tuple(r_row) == ("RUNNING", "initial")

        # 4. Insert new V5 states in agent_tasks: QUEUED, PAUSED, CANCELLING
        for status in ("QUEUED", "PAUSED", "CANCELLING"):
            verify_conn.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, session_id, thread_id, status, query, initiator,
                    assigned_agent_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"task_{status.lower()}", "sess_1", f"th_{status}", status, "test", "user", "agent", 1.0, 1.0),
            )

        # 5. Insert new V5 states in agent_runs: QUEUED, PAUSED, CANCELLING with trigger_types user_input, resume
        verify_conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at
            ) VALUES ('run_input', 'task_v4', 2, 'user_input', 'QUEUED', 'rt', 2.0)
            """
        )
        verify_conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at
            ) VALUES ('run_resume', 'task_v4', 3, 'resume', 'PAUSED', 'rt', 3.0)
            """
        )
        verify_conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at
            ) VALUES ('run_cancelling', 'task_v4', 4, 'resume', 'CANCELLING', 'rt', 4.0)
            """
        )

        # 6. Verify CHECK constraints reject invalid status or trigger_type
        with pytest.raises(sqlite3.IntegrityError):
            verify_conn.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, session_id, thread_id, status, query, initiator,
                    assigned_agent_id, created_at, updated_at
                ) VALUES ('task_invalid', 'sess_1', 'th_bad', 'FOO_BAR', 'test', 'user', 'agent', 1.0, 1.0)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            verify_conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at
                ) VALUES ('run_bad_trig', 'task_v4', 10, 'magic_trigger', 'RUNNING', 'rt', 10.0)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            verify_conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at
                ) VALUES ('run_bad_stat', 'task_v4', 11, 'initial', 'FLYING', 'rt', 11.0)
                """
            )

    # 7. Test coordinator read/write with new domain models
    coordinator = TaskCoordinator(db_path)
    q_task = coordinator.get_task("task_queued")
    assert q_task is not None
    assert q_task.status == TaskStatus.QUEUED

    p_task = coordinator.get_task("task_paused")
    assert p_task is not None
    assert p_task.status == TaskStatus.PAUSED

    runs = coordinator.list_runs("task_v4")
    assert len(runs) == 4
    run_dict = {r.run_id: r for r in runs}
    assert run_dict["run_input"].trigger_type == RunTriggerType.USER_INPUT
    assert run_dict["run_input"].status == RunStatus.QUEUED
    assert run_dict["run_resume"].trigger_type == RunTriggerType.RESUME
    assert run_dict["run_resume"].status == RunStatus.PAUSED


def test_event_type_extensions():
    """Verify new event types exist and have distinct values."""
    assert EventType.TASK_QUEUED == "task_queued"
    assert EventType.TASK_PAUSED == "task_paused"
    assert EventType.TASK_RESUMED == "task_resumed"
    assert EventType.TASK_CANCELLING == "task_cancelling"
    assert EventType.INSTRUCTION_APPENDED == "instruction_appended"
