"""Sprint 2A Integration Tests: TaskService, Session / Task Decoupling, Multi-Run, and Events."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from jarvis_studio.core.persistence.migrations import APP_SCHEMA_VERSION, run_app_migrations
from jarvis_studio.core.persistence.sqlite import open_sync_connection
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import EventType, RunStatus, TaskStatus
from jarvis_studio.service.deliverable_manager import DeliverableManager
from jarvis_studio.service.session_service import SessionService
from jarvis_studio.service.task_service import TaskService


@pytest.fixture
def test_env(tmp_path: Path):
    db_path = tmp_path / "agentstudio_test.db"
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    coordinator = TaskCoordinator(db_path=db_path)
    deliv_mgr = DeliverableManager(db_path=db_path, workspace_root=ws_dir)
    task_svc = TaskService(coordinator=coordinator, deliverable_mgr=deliv_mgr, db_path=db_path)
    sess_svc = SessionService(db_path=db_path)

    yield {
        "db_path": db_path,
        "ws_dir": ws_dir,
        "coordinator": coordinator,
        "deliv_mgr": deliv_mgr,
        "task_svc": task_svc,
        "sess_svc": sess_svc,
    }

    coordinator.close()
    deliv_mgr.close()
    sess_svc.close()


def test_schema_v4_migration_idempotent_and_preserves_v3(tmp_path: Path):
    """Schema V4 migration correctly runs on a fresh DB and preserves existing V3 data."""
    db_path = tmp_path / "v3_to_v4.db"

    # Seed v3 database manually
    conn = open_sync_connection(db_path)
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
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL,
                query TEXT NOT NULL,
                initiator TEXT NOT NULL DEFAULT 'user',
                current_run_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                runtime_id TEXT,
                error_message TEXT,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE agent_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_seq INTEGER NOT NULL,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                runtime_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                duration_ms INTEGER,
                checkpoint_id TEXT,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                UNIQUE (task_id, run_seq)
            );
            INSERT INTO chat_sessions VALUES ('sess_v3', 'V3 Session', 1.0, 1.0, NULL);
            INSERT INTO agent_tasks VALUES ('task_v3', 'sess_v3', 'sess_v3', 'RUNNING', 'test query', 'user', 'run_v3', 1.0, 1.0, NULL, 'rt', NULL);
            INSERT INTO agent_runs VALUES ('run_v3', 'task_v3', 1, 'initial', 'RUNNING', 'rt', 1.0, NULL, NULL, NULL, NULL);
            PRAGMA user_version = 3;
            """
        )
    finally:
        conn.close()

    # Migrate to V4
    new_ver = run_app_migrations(db_path)
    assert new_ver == APP_SCHEMA_VERSION == 4

    # Verify preserved data and new V4 columns
    with open_sync_connection(db_path) as check_conn:
        row = check_conn.execute("SELECT * FROM agent_tasks WHERE task_id = 'task_v3'").fetchone()
        assert row is not None
        assert row[0] == "task_v3"
        assert row[1] == "sess_v3"
        assert row[4] == "test query"

        # Verify idempotency
        assert run_app_migrations(db_path) == 4


def test_independent_task_creation_without_session(test_env):
    """Tasks can be created independently without a session_id."""
    task_svc: TaskService = test_env["task_svc"]

    task, run = task_svc.create_task(
        query="Research local vector DB options",
        session_id=None,
        priority="HIGH",
        assigned_agent_id="research_agent",
        auto_start_run=True,
    )

    assert task.task_id.startswith("task_")
    assert task.session_id is None
    assert task.priority == "HIGH"
    assert task.assigned_agent_id == "research_agent"
    assert task.status == TaskStatus.RUNNING
    assert run is not None
    assert run.run_seq == 1
    assert run.status == RunStatus.RUNNING

    # Query from coordinator
    fetched = task_svc.get_task(task.task_id)
    assert fetched is not None
    assert fetched.session_id is None
    assert fetched.query == "Research local vector DB options"


def test_session_deletion_keeps_tasks_unlinked(test_env):
    """Deleting a session with keep_tasks=True preserves tasks and unlinks session_id to NULL."""
    sess_svc: SessionService = test_env["sess_svc"]
    task_svc: TaskService = test_env["task_svc"]
    db_path = test_env["db_path"]

    # 1. Create a session
    sess = sess_svc.create_session("User Research Session")
    sess_svc.add_message(sess.session_id, "user", "Please start research")

    # 2. Create tasks under this session
    task1, _ = task_svc.create_task("Task 1 in session", session_id=sess.session_id)
    task2, _ = task_svc.create_task("Task 2 in session", session_id=sess.session_id)
    task_svc.coordinator.set_task_status(task1.task_id, TaskStatus.COMPLETED)
    task_svc.coordinator.set_task_status(task2.task_id, TaskStatus.COMPLETED)

    # 3. Delete session with keep_tasks=True
    deleted = sess_svc.delete_session(sess.session_id, keep_tasks=True)
    assert deleted is True

    # 4. Check that session and its messages are gone
    assert sess_svc.get_session(sess.session_id) is None
    assert sess_svc.get_messages(sess.session_id) == []

    # 5. Check that tasks are still intact with session_id = NULL
    t1 = task_svc.get_task(task1.task_id)
    t2 = task_svc.get_task(task2.task_id)
    assert t1 is not None
    assert t1.session_id is None
    assert t2 is not None
    assert t2.session_id is None


def test_multi_run_sequencing_and_event_logging(test_env):
    """Tasks can have multiple runs with monotonic seq numbers and observable event logging."""
    task_svc: TaskService = test_env["task_svc"]

    task, _ = task_svc.create_task("Multi-run analysis task")
    assert task.status == TaskStatus.CREATED

    # Run 1
    run1 = task_svc.start_run(task.task_id, runtime_id="rt_1")
    assert run1.run_seq == 1

    # Run 2
    run2 = task_svc.start_run(task.task_id, runtime_id="rt_1")
    assert run2.run_seq == 2

    # Events check
    events = task_svc.list_events(task.task_id, since_seq=0)
    assert len(events) >= 3  # TASK_CREATED + 2 RUN_STARTED
    assert events[0].event_type == EventType.TASK_CREATED
    assert events[0].seq == 1
    assert events[1].event_type == EventType.RUN_STARTED
    assert events[1].seq == 2
    assert events[2].event_type == EventType.RUN_STARTED
    assert events[2].seq == 3

    # Incremental since_seq
    inc_events = task_svc.list_events(task.task_id, since_seq=2)
    assert len(inc_events) == 1
    assert inc_events[0].seq == 3


def test_task_cancellation(test_env):
    """Task cancellation terminates active run and marks task CANCELLED."""
    task_svc: TaskService = test_env["task_svc"]

    task, run = task_svc.create_task("Long running task", auto_start_run=True)
    assert task.status == TaskStatus.RUNNING

    cancelled = task_svc.cancel_task(task.task_id, reason="User clicked stop")
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.error_message == "User clicked stop"

    run_refreshed = task_svc.coordinator.get_run(run.run_id)
    assert run_refreshed.status == RunStatus.CANCELLED


def test_task_deletion_purges_deliverables_and_db_records(test_env):
    """Deleting a task removes database records and physical deliverable directory."""
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    ws_dir: Path = test_env["ws_dir"]

    task, run = task_svc.create_task("Task with deliverables", auto_start_run=True)
    task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)

    # Create a deliverable
    deliv = deliv_mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run.run_id,
        title="Final Deliverable Report",
        content="# Sample report content",
    )
    assert Path(ws_dir / deliv.file_path).is_file()

    # Active task cannot be deleted if running, but our task is COMPLETED
    deleted = task_svc.delete_task(task.task_id)
    assert deleted is True

    # Database records deleted
    assert task_svc.get_task(task.task_id) is None
    assert deliv_mgr.get_deliverable(deliv.deliverable_id) is None

    # Physical directory deleted
    task_deliv_dir = ws_dir / "deliverables" / task.task_id
    assert not task_deliv_dir.exists()
