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

    # Migrate to latest schema
    new_ver = run_app_migrations(db_path)
    assert new_ver == APP_SCHEMA_VERSION == 5

    # Verify preserved data and new columns
    with open_sync_connection(db_path) as check_conn:
        row = check_conn.execute("SELECT * FROM agent_tasks WHERE task_id = 'task_v3'").fetchone()
        assert row is not None
        assert row[0] == "task_v3"
        assert row[1] == "sess_v3"
        assert row[4] == "test query"

        # Verify idempotency
        assert run_app_migrations(db_path) == 5


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
    """Direct SessionService.delete_session() defaults safely to preserving tasks, runs, deliverables, and task traces."""
    from jarvis_studio.core.observability.trace_logger import ConversationTrace, TraceStore
    from jarvis_studio.memory.models import MemoryCategory, MemoryItem
    from jarvis_studio.memory.store import MemoryStore

    sess_svc: SessionService = test_env["sess_svc"]
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    db_path = test_env["db_path"]
    ws_dir: Path = test_env["ws_dir"]

    trace_store = TraceStore(db_path=str(db_path))
    mem_store = MemoryStore(db_path=str(db_path))
    try:
        # 1. Create session + messages
        sess = sess_svc.create_session("User Research Session")
        sess_svc.add_message(sess.session_id, "user", "Please start research")

        # 2. Create completed task + run + deliverable under this session
        task1, run1 = task_svc.create_task(
            "Task 1 in session",
            session_id=sess.session_id,
            auto_start_run=True,
        )
        assert run1 is not None
        task_svc.coordinator.complete_task_and_run(task1.task_id, run1.run_id)
        deliv = deliv_mgr.create_deliverable(
            task_id=task1.task_id,
            run_id=run1.run_id,
            title="Research Brief",
            content="# Preserved Brief",
        )
        task2, _ = task_svc.create_task("Task 2 in session", session_id=sess.session_id)
        task_svc.coordinator.set_task_status(task2.task_id, TaskStatus.COMPLETED)

        # 3. Create both a task-linked trace and a session-only trace + LTM item
        tr_task = ConversationTrace(
            session_id=sess.session_id,
            run_id=run1.run_id,
            task_id=task1.task_id,
            query="Task trace query",
        )
        tr_task.finish(output="done")
        trace_store.save_trace(tr_task)

        tr_chat = ConversationTrace(
            session_id=sess.session_id,
            run_id="run_chat_only",
            task_id=None,
            query="Chat only query",
        )
        tr_chat.finish(output="hi")
        trace_store.save_trace(tr_chat)

        mem = mem_store.add(
            MemoryItem(
                category=MemoryCategory.FACT,
                content="Preserved fact",
                source_session_id=sess.session_id,
                source_task_id=task1.task_id,
            )
        )

        # 4. Call delete_session directly with NO keep_tasks arg (must default to keep_tasks=True)
        deleted = sess_svc.delete_session(sess.session_id)
        assert deleted is True

        # 5. Session + chat messages + taskless trace are deleted; no NOT NULL IntegrityError
        assert sess_svc.get_session(sess.session_id) is None
        assert sess_svc.get_messages(sess.session_id) == []
        assert trace_store.get_trace(tr_chat.trace_id) is None

        # 6. Tasks, runs, deliverables, task-linked traces, and LTM source_task_id are preserved
        t1 = task_svc.get_task(task1.task_id)
        t2 = task_svc.get_task(task2.task_id)
        assert t1 is not None and t1.session_id is None
        assert t2 is not None and t2.session_id is None
        assert len(task_svc.coordinator.list_runs(task1.task_id)) == 1
        assert deliv_mgr.get_deliverable(deliv.deliverable_id) is not None
        assert (ws_dir / deliv.file_path).is_file()

        with open_sync_connection(db_path) as conn:
            tr_row = conn.execute(
                "SELECT session_id, task_id FROM agent_conversation_traces WHERE trace_id = ?",
                (tr_task.trace_id,),
            ).fetchone()
            assert tr_row is not None
            assert tr_row[0] is None and tr_row[1] == task1.task_id

            ltm_row = conn.execute(
                "SELECT source_session_id, source_task_id FROM ltm_memories WHERE id = ?",
                (mem.id,),
            ).fetchone()
            assert ltm_row is not None
            assert ltm_row[0] is None and ltm_row[1] == task1.task_id
    finally:
        trace_store.close()
        mem_store.close()


def test_session_clear_messages_preserves_tasks_and_deliverables(test_env):
    """Direct SessionService.clear_session_messages() clears chat messages without deleting tasks or deliverables."""
    from jarvis_studio.core.observability.trace_logger import ConversationTrace, TraceStore

    sess_svc: SessionService = test_env["sess_svc"]
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    db_path = test_env["db_path"]

    trace_store = TraceStore(db_path=str(db_path))
    try:
        sess = sess_svc.create_session("Clear Messages Session")
        sess_svc.add_message(sess.session_id, "user", "First message")
        task, run = task_svc.create_task("Linked task", session_id=sess.session_id, auto_start_run=True)
        assert run is not None
        task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)
        deliv = deliv_mgr.create_deliverable(
            task_id=task.task_id,
            run_id=run.run_id,
            title="Report",
            content="# Saved across clear",
        )
        tr_chat = ConversationTrace(
            session_id=sess.session_id, run_id="r_chat", task_id=None, query="q1"
        )
        tr_chat.finish(output="ok")
        trace_store.save_trace(tr_chat)

        cleared = sess_svc.clear_session_messages(sess.session_id)
        assert cleared is True
        assert sess_svc.get_session(sess.session_id) is not None
        assert sess_svc.get_messages(sess.session_id) == []
        assert trace_store.get_trace(tr_chat.trace_id) is None
        assert task_svc.get_task(task.task_id) is not None
        assert len(task_svc.coordinator.list_runs(task.task_id)) == 1
        assert deliv_mgr.get_deliverable(deliv.deliverable_id) is not None
    finally:
        trace_store.close()


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

    # Events check (including parsed payload dict)
    events = task_svc.list_events(task.task_id, since_seq=0)
    assert len(events) >= 3  # TASK_CREATED + 2 RUN_STARTED
    assert events[0].event_type == EventType.TASK_CREATED
    assert events[0].seq == 1
    assert events[0].payload["query"] == "Multi-run analysis task"
    assert events[1].event_type == EventType.RUN_STARTED
    assert events[1].seq == 2
    assert events[1].payload["run_seq"] == 1
    assert events[2].event_type == EventType.RUN_STARTED
    assert events[2].seq == 3
    assert events[2].payload["run_seq"] == 2

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


def test_task_soft_delete_preserves_history_and_explicit_purge_cleans_up(test_env):
    """Ordinary delete_task/archive_task preserves Runs & Deliverables; explicit purge requires confirm=True."""
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    ws_dir: Path = test_env["ws_dir"]

    task, run = task_svc.create_task("Task with deliverables", auto_start_run=True)
    task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)

    deliv = deliv_mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run.run_id,
        title="Final Deliverable Report",
        content="# Sample report content",
    )
    deliv_path = Path(ws_dir / deliv.file_path)
    assert deliv_path.is_file()

    # Ordinary deletion must archive (soft-delete) and keep Runs & Deliverables intact
    deleted = task_svc.delete_task(task.task_id)
    assert deleted is True

    archived_task = task_svc.get_task(task.task_id)
    assert archived_task is not None
    assert archived_task.archived_at is not None
    assert [t.task_id for t in task_svc.list_tasks()] == []
    assert [t.task_id for t in task_svc.list_tasks(include_archived=True)] == [task.task_id]
    assert len(task_svc.coordinator.list_runs(task.task_id)) == 1
    assert deliv_mgr.get_deliverable(deliv.deliverable_id) is not None
    assert deliv_path.is_file()
    events = task_svc.list_events(task.task_id)
    assert any(e.event_type == EventType.TASK_ARCHIVED for e in events)

    # Explicit purge without confirm=True must fail closed
    with pytest.raises(ValueError, match="confirm=True"):
        task_svc.purge_task(task.task_id)
    assert task_svc.get_task(task.task_id) is not None
    assert deliv_path.is_file()

    # Explicit purge with confirm=True removes DB rows and physical deliverables dir
    purged = task_svc.purge_task(task.task_id, confirm=True)
    assert purged is True
    assert task_svc.get_task(task.task_id) is None
    assert deliv_mgr.get_deliverable(deliv.deliverable_id) is None
    assert not (ws_dir / "deliverables" / task.task_id).exists()


def test_task_purge_staging_failure_aborts_cleanly(
    test_env, monkeypatch: pytest.MonkeyPatch
):
    """If initial directory staging fails, DB records are never touched and files remain intact."""
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    ws_dir: Path = test_env["ws_dir"]

    task, run = task_svc.create_task("Staging fail task", auto_start_run=True)
    task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)
    deliv = deliv_mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run.run_id,
        title="Safe Report",
        content="# Unmoved content",
    )
    deliv_path = Path(ws_dir / deliv.file_path)
    assert deliv_path.is_file()

    def _fail_stage(task_id: str):
        raise PermissionError("Simulated directory staging lock")

    monkeypatch.setattr(deliv_mgr, "stage_task_deliverables_dir", _fail_stage)

    with pytest.raises(PermissionError, match="Simulated directory staging lock"):
        task_svc.purge_task(task.task_id, confirm=True)

    assert task_svc.get_task(task.task_id) is not None
    assert deliv_mgr.get_deliverable(deliv.deliverable_id) is not None
    assert deliv_path.is_file()
    assert deliv_path.read_text(encoding="utf-8") == "# Unmoved content"


def test_task_purge_sqlite_commit_failure_restores_staged_files_and_preserves_db(
    test_env, monkeypatch: pytest.MonkeyPatch
):
    """Key Failure Injection: Staging succeeds, but SQLite COMMIT fails.

    Verifies the two-phase cleanup protocol:
    1. Deliverables directory was staged to `.purging_{task_id}_{uuid}`.
    2. SQLite COMMIT fails, causing database rollback.
    3. The protocol catches the commit failure, restores `.purging_*` back to `deliverables/{task_id}`.
    4. Both database records and original filesystem files are 100% recovered and consistent.
    """
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    ws_dir: Path = test_env["ws_dir"]

    task, run = task_svc.create_task("Commit fail task", auto_start_run=True)
    task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)
    deliv = deliv_mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run.run_id,
        title="Critical Deliverable",
        content="# Mission Critical Report Content",
    )
    deliv_path = Path(ws_dir / deliv.file_path)
    assert deliv_path.is_file()

    # Inject failure specifically when SQLite commits the purge transaction
    from contextlib import contextmanager

    orig_tx = task_svc.coordinator._tx

    @contextmanager
    def _failing_tx():
        with orig_tx() as conn:
            yield conn
            # Simulating disk/WAL I/O failure on final commit
            raise sqlite3.OperationalError("Simulated disk I/O error during SQLite COMMIT")

    monkeypatch.setattr(task_svc.coordinator, "_tx", _failing_tx)

    with pytest.raises(sqlite3.OperationalError, match="Simulated disk I/O error during SQLite COMMIT"):
        task_svc.purge_task(task.task_id, confirm=True)

    # Undo patch to verify state
    monkeypatch.undo()

    # 1. Database records MUST remain completely rolled back (preserved)
    assert task_svc.get_task(task.task_id) is not None
    assert len(task_svc.coordinator.list_runs(task.task_id)) == 1
    assert deliv_mgr.get_deliverable(deliv.deliverable_id) is not None

    # 2. Filesystem deliverables directory MUST be 100% restored and valid
    assert deliv_path.is_file()
    assert deliv_path.read_text(encoding="utf-8") == "# Mission Critical Report Content"
    ok, err = deliv_mgr.verify_deliverable(deliv.deliverable_id)
    assert ok is True, f"Deliverable verification failed: {err}"

    # 3. No leftover `.purging_*` temporary staging directories
    leftover_staging = list((ws_dir / "deliverables").glob(".purging_*"))
    assert leftover_staging == []


def test_task_purge_crash_recovery_restores_uncommitted_staged_files_on_reconcile(test_env):
    """Crash Recovery: Process dies after staging but before DB commit -> reconcile_storage restores files."""
    task_svc: TaskService = test_env["task_svc"]
    deliv_mgr: DeliverableManager = test_env["deliv_mgr"]
    ws_dir: Path = test_env["ws_dir"]

    task, run = task_svc.create_task("Crash task", auto_start_run=True)
    task_svc.coordinator.complete_task_and_run(task.task_id, run.run_id)
    deliv = deliv_mgr.create_deliverable(
        task_id=task.task_id,
        run_id=run.run_id,
        title="Crash Recoverable",
        content="# Restored after crash",
    )
    deliv_path = Path(ws_dir / deliv.file_path)
    assert deliv_path.is_file()

    # Simulate crash: stage directory to .purging_{task_id}_{uuid}, without committing purge in DB
    staged = deliv_mgr.stage_task_deliverables_dir(task.task_id)
    assert staged is not None and staged.exists()
    assert not deliv_path.exists()

    # On subsequent storage reconciliation, because task is still in DB, reconcile restores it
    report = deliv_mgr.reconcile_storage(task_id=task.task_id)
    assert deliv_path.is_file()
    assert deliv_path.read_text(encoding="utf-8") == "# Restored after crash"
    assert not staged.exists()
    assert report.healthy_records == 1
