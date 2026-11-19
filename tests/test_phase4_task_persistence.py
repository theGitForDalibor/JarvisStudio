"""Phase 4 Test Suite: Agent Tasks/Runs State Machine, Single-Transaction Persistence,
Session Busy Guard & Cascade Deletion, Sticky Context, and Trace Pruning.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ExecutionOutcomeStatus,
    HITLManager,
)
from jarvis_studio.api.app import create_app
from jarvis_studio.config.models import StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.guard.route_guard import IntentRouteGuard, get_route_guard
from jarvis_studio.core.observability.trace_logger import (
    ConversationTrace,
    TraceStore,
)
from jarvis_studio.core.persistence.migrations import (
    APP_SCHEMA_VERSION,
    run_app_migrations,
)
from jarvis_studio.core.persistence.recovery import reconcile_on_startup
from jarvis_studio.core.persistence.runtime_lock import RuntimeInstanceLock
from jarvis_studio.core.persistence.sqlite import open_sync_connection
from jarvis_studio.core.runtime import AgentRuntime
from jarvis_studio.core.tasks.coordinator import TaskCoordinator
from jarvis_studio.core.tasks.models import (
    RunStatus,
    RunTriggerType,
    TaskStatus,
)
from jarvis_studio.memory.models import MemoryCategory, MemoryItem
from jarvis_studio.memory.store import MemoryStore
from jarvis_studio.service import agent_service as agent_svc_mod
from jarvis_studio.service import session_service as session_svc_mod
from jarvis_studio.service.session_service import (
    SessionBusyError,
    SessionService,
)


@pytest.fixture(autouse=True)
def _isolate_phase4_singletons():
    """Ensure Phase 4 tests execute in clean singleton isolation."""
    for mod, attr in (
        (session_svc_mod, "_global_session_service"),
        (agent_svc_mod, "_default_agent_service"),
    ):
        inst = getattr(mod, attr, None)
        if inst is not None and hasattr(inst, "close"):
            with contextlib.suppress(Exception):
                inst.close()
        setattr(mod, attr, None)
    yield
    for mod, attr in (
        (session_svc_mod, "_global_session_service"),
        (agent_svc_mod, "_default_agent_service"),
    ):
        inst = getattr(mod, attr, None)
        if inst is not None and hasattr(inst, "close"):
            with contextlib.suppress(Exception):
                inst.close()
        setattr(mod, attr, None)


# ---------------------------------------------------------------------------
# 1. Database Schema Migration v2 -> v3
# ---------------------------------------------------------------------------

def test_p4_mig_01_v2_to_v3_migration_and_constraints(tmp_path: Path) -> None:
    """v2 -> v3 migration creates agent_tasks, agent_runs, updates chat_messages FK cascade, and alters traces/ltm."""
    db_path = tmp_path / "app_v2_to_v3.db"

    # Seed v2 schema database
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
            CREATE TABLE chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE hitl_approval_requests (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                task_id TEXT,
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE tool_execution_outcomes (
                outcome_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                task_id TEXT,
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                outcome_status TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE agent_conversation_traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                run_id TEXT,
                query TEXT NOT NULL,
                status TEXT NOT NULL,
                start_time REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE ltm_memories (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO chat_sessions VALUES ('sess-1', 'Test Sess 1', 10.0, 10.0, NULL);
            INSERT INTO chat_messages VALUES ('m-1', 'sess-1', 'user', 'hello', NULL, 11.0);
            -- Also insert an orphan message to test defensive restore
            INSERT INTO chat_messages VALUES ('m-orphan', 'sess-orphan', 'user', 'orphan msg', NULL, 12.0);
            PRAGMA user_version = 2;
            """
        )
    finally:
        conn.close()

    assert APP_SCHEMA_VERSION >= 3
    assert run_app_migrations(db_path) == APP_SCHEMA_VERSION

    check_conn = open_sync_connection(db_path)
    try:
        assert check_conn.execute("PRAGMA user_version").fetchone()[0] == APP_SCHEMA_VERSION

        # Check orphan session restored
        restored = check_conn.execute("SELECT title FROM chat_sessions WHERE session_id = 'sess-orphan'").fetchone()
        assert restored is not None and restored[0] == "Restored Session"

        # Check agent_tasks CHECK constraint
        with pytest.raises(sqlite3.IntegrityError):
            check_conn.execute(
                """
                INSERT INTO agent_tasks (task_id, session_id, thread_id, status, query, created_at, updated_at)
                VALUES ('t-invalid', 'sess-1', 'sess-1', 'INVALID_STATUS', 'query', 1.0, 1.0)
                """
            )

        # Check agent_runs UNIQUE(task_id, run_seq)
        check_conn.execute(
            """
            INSERT INTO agent_tasks (task_id, session_id, thread_id, status, query, created_at, updated_at)
            VALUES ('t-1', 'sess-1', 'sess-1', 'RUNNING', 'query', 1.0, 1.0)
            """
        )
        check_conn.execute(
            """
            INSERT INTO agent_runs (run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at)
            VALUES ('r-1', 't-1', 1, 'initial', 'RUNNING', 'rt-1', 1.0)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            check_conn.execute(
                """
                INSERT INTO agent_runs (run_id, task_id, run_seq, trigger_type, status, runtime_id, started_at)
                VALUES ('r-duplicate-seq', 't-1', 1, 'initial', 'RUNNING', 'rt-1', 1.0)
                """
            )

        # Check chat_messages FK CASCADE
        check_conn.execute("DELETE FROM chat_sessions WHERE session_id = 'sess-orphan'")
        msg_orphan = check_conn.execute("SELECT 1 FROM chat_messages WHERE id = 'm-orphan'").fetchone()
        assert msg_orphan is None, "chat_messages row should cascade delete when parent chat_sessions row is deleted"

        # Check traces new columns
        check_conn.execute(
            """
            INSERT INTO agent_conversation_traces (
                trace_id, session_id, run_id, task_id, query, status, start_time, updated_at, payload
            ) VALUES ('tr-1', 'sess-1', 'r-1', 't-1', 'test query', 'completed', 10.0, 12.0, '{}')
            """
        )
        trace_row = check_conn.execute("SELECT task_id, updated_at FROM agent_conversation_traces WHERE trace_id = 'tr-1'").fetchone()
        assert trace_row[0] == "t-1" and trace_row[1] == 12.0

        # Check ltm new columns
        check_conn.execute(
            """
            INSERT INTO ltm_memories (id, category, content, created_at, updated_at, source_session_id, source_task_id)
            VALUES ('ltm-1', 'preference', 'likes python', 1.0, 1.0, 'sess-1', 't-1')
            """
        )
        ltm_row = check_conn.execute("SELECT source_session_id, source_task_id FROM ltm_memories WHERE id = 'ltm-1'").fetchone()
        assert ltm_row[0] == "sess-1" and ltm_row[1] == "t-1"
    finally:
        check_conn.close()


# ---------------------------------------------------------------------------
# 2. Task & Run State Machine
# ---------------------------------------------------------------------------

def test_p4_task_run_state_machine_and_sequencing(tmp_path: Path) -> None:
    """TaskCoordinator creates tasks, manages runs with incrementing sequences, and terminal transitions."""
    db_path = tmp_path / "tasks.db"
    coord = TaskCoordinator(db_path)
    sess_svc = SessionService(db_path)

    sess = sess_svc.create_session(title="Task Session", session_id="sess_task_test")
    task = coord.create_task(
        session_id=sess.session_id,
        thread_id=sess.session_id,
        query="Refactor database schema",
        runtime_id="rt_alpha",
    )
    assert task.status == TaskStatus.CREATED
    assert task.task_id.startswith("task_")

    # Run 1: Initial run
    run1 = coord.start_run(task.task_id, runtime_id="rt_alpha", trigger_type=RunTriggerType.INITIAL)
    assert run1.run_seq == 1
    assert run1.status == RunStatus.RUNNING

    t_reloaded = coord.get_task(task.task_id)
    assert t_reloaded is not None
    assert t_reloaded.status == TaskStatus.RUNNING
    assert t_reloaded.current_run_id == run1.run_id

    # Interrupt run 1 for HITL approval
    coord.pause_for_approval(task.task_id, run1.run_id, checkpoint_id="cp_step1")
    r1_reloaded = coord.get_run(run1.run_id)
    assert r1_reloaded.status == RunStatus.INTERRUPTED
    assert r1_reloaded.checkpoint_id == "cp_step1"
    assert r1_reloaded.duration_ms is not None
    assert coord.get_task(task.task_id).status == TaskStatus.WAITING_APPROVAL

    # Run 2: Resume 1
    run2 = coord.start_run(task.task_id, runtime_id="rt_alpha", trigger_type=RunTriggerType.HITL_RESUME)
    assert run2.run_seq == 2
    assert run2.status == RunStatus.RUNNING
    assert coord.get_task(task.task_id).status == TaskStatus.RUNNING

    # Interrupt run 2 for second approval
    coord.pause_for_approval(task.task_id, run2.run_id, checkpoint_id="cp_step2")
    assert coord.get_run(run2.run_id).status == RunStatus.INTERRUPTED
    assert coord.get_task(task.task_id).status == TaskStatus.WAITING_APPROVAL

    # Run 3: Resume 2 and complete
    run3 = coord.start_run(task.task_id, runtime_id="rt_alpha", trigger_type=RunTriggerType.HITL_RESUME)
    assert run3.run_seq == 3
    coord.complete_task_and_run(task.task_id, run3.run_id, checkpoint_id="cp_final")

    r3_reloaded = coord.get_run(run3.run_id)
    assert r3_reloaded.status == RunStatus.COMPLETED
    assert r3_reloaded.checkpoint_id == "cp_final"

    t_final = coord.get_task(task.task_id)
    assert t_final.status == TaskStatus.COMPLETED
    assert t_final.completed_at is not None

    runs = coord.list_runs(task.task_id)
    assert [r.run_seq for r in runs] == [1, 2, 3]
    assert [r.trigger_type for r in runs] == [
        RunTriggerType.INITIAL,
        RunTriggerType.HITL_RESUME,
        RunTriggerType.HITL_RESUME,
    ]


def test_p4_option_b_cancel_marks_unknown_when_in_flight(tmp_path: Path) -> None:
    """Option B: Cancelling a task while physical tool is in-flight marks both Task and Run as UNKNOWN."""
    db_path = tmp_path / "cancel_unknown.db"
    coord = TaskCoordinator(db_path)
    sess_svc = SessionService(db_path)

    sess = sess_svc.create_session(title="Cancel Sess", session_id="sess_cancel")
    task = coord.create_task(session_id=sess.session_id, thread_id=sess.session_id, query="Execute shell")
    run = coord.start_run(task.task_id, runtime_id="rt_1")

    # In-flight tool cancellation: is_unknown = True
    coord.cancel_task_and_run(
        task.task_id,
        run.run_id,
        is_unknown=True,
        error_message="Task execution cancelled while tool running",
    )

    t_cancelled = coord.get_task(task.task_id)
    r_cancelled = coord.get_run(run.run_id)
    assert t_cancelled.status == TaskStatus.UNKNOWN
    assert r_cancelled.status == RunStatus.UNKNOWN
    assert "cancelled while tool running" in t_cancelled.error_message


def test_p4_clean_cancel_marks_cancelled(tmp_path: Path) -> None:
    """Normal cancellation when no tool is in flight marks both Task and Run as CANCELLED."""
    db_path = tmp_path / "cancel_clean.db"
    coord = TaskCoordinator(db_path)
    sess_svc = SessionService(db_path)

    sess = sess_svc.create_session(title="Clean Cancel Sess", session_id="sess_clean_cancel")
    task = coord.create_task(session_id=sess.session_id, thread_id=sess.session_id, query="Vague request")
    run = coord.start_run(task.task_id, runtime_id="rt_1")

    coord.cancel_task_and_run(
        task.task_id,
        run.run_id,
        is_unknown=False,
        error_message="Cancelled by user",
    )

    t_cancelled = coord.get_task(task.task_id)
    r_cancelled = coord.get_run(run.run_id)
    assert t_cancelled.status == TaskStatus.CANCELLED
    assert r_cancelled.status == RunStatus.CANCELLED


# ---------------------------------------------------------------------------
# 3. Single-Transaction Message Persistence
# ---------------------------------------------------------------------------

def test_p4_single_transaction_message_persistence(tmp_path: Path) -> None:
    """SessionService.add_message atomically creates parent session and persists message with task_id and run_id."""
    db_path = tmp_path / "single_tx.db"
    sess_svc = SessionService(db_path)

    # Directly add message to a non-existent session
    new_sess_id = "sess_auto_created"
    msg = sess_svc.add_message(
        new_sess_id,
        role="user",
        content="Hello autonomous world",
        task_id="task_auto_1",
        run_id="run_auto_1",
        msg_type="chat",
    )

    assert msg.session_id == new_sess_id
    assert msg.task_id == "task_auto_1"
    assert msg.run_id == "run_auto_1"
    assert msg.msg_type == "chat"

    # Session was created in the same atomic transaction
    sess = sess_svc.get_session(new_sess_id)
    assert sess is not None
    assert sess.title == "Hello autonomous world"

    messages = sess_svc.get_messages(new_sess_id)
    assert len(messages) == 1
    assert messages[0].id == msg.id
    assert messages[0].task_id == "task_auto_1"
    assert messages[0].run_id == "run_auto_1"


# ---------------------------------------------------------------------------
# 4. Session Busy Guard & 409 Conflict
# ---------------------------------------------------------------------------

def test_p4_session_busy_guard_raises_and_returns_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_session and clear_session_messages raise SessionBusyError and return 409 when executions are running."""
    db_path = tmp_path / "busy_guard.db"
    sess_svc = SessionService(db_path)
    coord = TaskCoordinator(db_path)
    hitl_mgr = HITLManager(db_path)

    sess = sess_svc.create_session(title="Busy Sess", session_id="sess_busy_1")
    task = coord.create_task(sess.session_id, sess.session_id, query="Long operation")

    # While task is CREATED:
    assert sess_svc.is_session_busy(sess.session_id) is True
    assert coord.has_active_tasks(sess.session_id) is True
    with pytest.raises(SessionBusyError):
        sess_svc.delete_session(sess.session_id)

    run = coord.start_run(task.task_id, runtime_id="rt_1")

    # While task is RUNNING:
    assert sess_svc.is_session_busy(sess.session_id) is True
    with pytest.raises(SessionBusyError):
        sess_svc.delete_session(sess.session_id)
    with pytest.raises(SessionBusyError):
        sess_svc.clear_session_messages(sess.session_id)

    # Test FastAPI API endpoint returns 409 Conflict
    from jarvis_studio.api.routes import sessions as sessions_route_mod
    monkeypatch.setattr(sessions_route_mod, "get_session_service", lambda: sess_svc)
    app = create_app()
    with TestClient(app) as client:
        resp_del = client.delete(f"/api/v1/sessions/{sess.session_id}")
        assert resp_del.status_code == 409
        assert "cannot be deleted while active" in resp_del.json()["detail"]

        resp_clear = client.delete(f"/api/v1/sessions/{sess.session_id}/messages")
        assert resp_clear.status_code == 409
        assert "cannot be cleared while active" in resp_clear.json()["detail"]

    # Transition task to WAITING_APPROVAL with approval request in PENDING
    coord.pause_for_approval(task.task_id, run.run_id)
    appr = hitl_mgr.create_request(
        thread_id=sess.session_id,
        run_id=run.run_id,
        task_id=task.task_id,
        tool_name="write_file",
        arguments={"path": "foo.txt"},
        tool_call_id="call_busy_1",
        description="write foo",
    )
    # Status is WAITING_APPROVAL & PENDING -> MUST be busy!
    assert sess_svc.is_session_busy(sess.session_id) is True
    assert coord.has_active_tasks(sess.session_id) is True
    with pytest.raises(SessionBusyError):
        sess_svc.delete_session(sess.session_id)

    # Resolve to APPROVED -> still busy!
    hitl_mgr.resolve_request(appr.id, ApprovalDecision(decision="approve"), thread_id=sess.session_id)
    assert sess_svc.is_session_busy(sess.session_id) is True
    with pytest.raises(SessionBusyError):
        sess_svc.delete_session(sess.session_id)

    # Acquire execution lease -> EXECUTING -> still busy!
    hitl_mgr.acquire_execution_lease(
        appr.id,
        thread_id=sess.session_id,
        tool_call_id=appr.tool_call_id,
        arguments_hash=appr.arguments_hash,
        lease_owner_instance_id="rt_1",
        lease_acquired_run_id=run.run_id,
    )
    assert sess_svc.is_session_busy(sess.session_id) is True
    with pytest.raises(SessionBusyError):
        sess_svc.delete_session(sess.session_id)

    # Finalize execution -> no longer busy
    hitl_mgr.finalize_execution(appr.id, success=True, outcome_status=ExecutionOutcomeStatus.SUCCEEDED)
    coord.complete_task_and_run(task.task_id, run.run_id)

    assert sess_svc.is_session_busy(sess.session_id) is False
    assert coord.has_active_tasks(sess.session_id) is False


# ---------------------------------------------------------------------------
# 5. 8-Table Foreign-Key Cascade Deletion
# ---------------------------------------------------------------------------

def test_p4_cascade_delete_across_8_tables(tmp_path: Path) -> None:
    """delete_session cleanly cascade deletes records across all 8 tables and nullifies LTM pointers."""
    db_path = tmp_path / "cascade_8_tables.db"
    sess_svc = SessionService(db_path)
    coord = TaskCoordinator(db_path)
    hitl_mgr = HITLManager(db_path)
    trace_store = TraceStore(db_path)
    mem_store = MemoryStore(db_path)
    guard = get_route_guard()

    sess_id = "sess_cascade_target"
    sess_svc.create_session(title="Target Session", session_id=sess_id)

    # 1. Add sticky context
    guard.update_sticky_context(sess_id, {"active_file": "app.py"})

    # 2. Add message
    sess_svc.add_message(sess_id, role="user", content="hello")

    # 3. Add task and run
    task = coord.create_task(sess_id, sess_id, query="test cascade")
    run = coord.start_run(task.task_id, runtime_id="rt_1")

    # 4. Add approval and outcome
    appr = hitl_mgr.create_request(
        thread_id=sess_id,
        run_id=run.run_id,
        task_id=task.task_id,
        tool_name="write_file",
        arguments={"x": 1},
        tool_call_id="call_c_1",
        description="write",
    )
    hitl_mgr.resolve_request(appr.id, ApprovalDecision(decision="approve"), thread_id=sess_id)
    hitl_mgr.acquire_execution_lease(
        appr.id,
        thread_id=sess_id,
        tool_call_id=appr.tool_call_id,
        arguments_hash=appr.arguments_hash,
        lease_owner_instance_id="rt_1",
        lease_acquired_run_id=run.run_id,
    )
    hitl_mgr.finalize_execution(appr.id, success=True, outcome_status=ExecutionOutcomeStatus.SUCCEEDED)
    coord.complete_task_and_run(task.task_id, run.run_id)

    # 5. Add trace
    tr = ConversationTrace(session_id=sess_id, run_id=run.run_id, task_id=task.task_id, query="test cascade")
    tr.finish(output="done")
    trace_store.save_trace(tr)

    # 6. Add LTM memory linked to this session and task
    mem = mem_store.add(
        MemoryItem(
            category=MemoryCategory.PREFERENCE,
            content="Likes type hints",
            source_session_id=sess_id,
            source_task_id=task.task_id,
        )
    )

    # Verify all records exist before deletion
    with open_sync_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE session_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE session_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_runs WHERE task_id = ?", (task.task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM hitl_approval_requests WHERE thread_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_execution_outcomes WHERE thread_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_conversation_traces WHERE session_id = ?", (sess_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT source_session_id FROM ltm_memories WHERE id = ?", (mem.id,)).fetchone()[0] == sess_id

    # Execute explicit cascade deletion (opt-in legacy mode)
    deleted = sess_svc.delete_session(sess_id, keep_tasks=False)
    assert deleted is True

    # Verify all related records deleted across 8 tables
    with open_sync_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE session_id = ?", (sess_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (sess_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE session_id = ?", (sess_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_runs WHERE task_id = ?", (task.task_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM hitl_approval_requests WHERE thread_id = ?", (sess_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_execution_outcomes WHERE thread_id = ?", (sess_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_conversation_traces WHERE session_id = ?", (sess_id,)).fetchone()[0] == 0

        # LTM record itself is preserved, but source pointers are safely nullified
        ltm_row = conn.execute("SELECT source_session_id, source_task_id FROM ltm_memories WHERE id = ?", (mem.id,)).fetchone()
        assert ltm_row is not None
        assert ltm_row[0] is None
        assert ltm_row[1] is None

    # Sticky cache cleared
    assert guard.get_sticky_context(sess_id) == {}


# ---------------------------------------------------------------------------
# 6. Sticky Context Write-Through & Lazy-Load
# ---------------------------------------------------------------------------

def test_p4_sticky_context_write_through_and_lazy_load(tmp_path: Path) -> None:
    """Sticky context writes through to chat_sessions.metadata and lazy loads on cache miss."""
    db_path = tmp_path / "sticky.db"
    sess_svc = SessionService(db_path)
    session_svc_mod._global_session_service = sess_svc

    sess = sess_svc.create_session(title="Sticky Session", session_id="sess_sticky_1")
    guard = IntentRouteGuard()

    # Write sticky context
    guard.update_sticky_context(sess.session_id, {"active_file": "src/main.py", "theme": "dark"})
    assert guard.get_sticky_context(sess.session_id) == {"active_file": "src/main.py", "theme": "dark"}

    # Verify written to DB
    db_meta = sess_svc.get_session(sess.session_id).metadata
    assert db_meta["sticky_context"] == {"active_file": "src/main.py", "theme": "dark"}

    # Simulate in-memory cache eviction or process restart
    guard._sticky_cache.clear()
    assert sess.session_id not in guard._sticky_cache

    # Lazy-load should read from DB metadata
    loaded = guard.get_sticky_context(sess.session_id)
    assert loaded == {"active_file": "src/main.py", "theme": "dark"}
    assert sess.session_id in guard._sticky_cache

    # Clear sticky context
    guard.clear_sticky_context(sess.session_id)
    assert guard.get_sticky_context(sess.session_id) == {}
    assert "sticky_context" not in sess_svc.get_session(sess.session_id).metadata


# ---------------------------------------------------------------------------
# 7. Background Non-Blocking Trace Pruning
# ---------------------------------------------------------------------------

def test_p4_trace_retention_pruning(tmp_path: Path) -> None:
    """prune_old_traces prunes traces older than max_age_days and caps rows per session at max_rows_per_session."""
    db_path = tmp_path / "trace_prune.db"
    trace_store = TraceStore(db_path)

    now = time.time()
    old_time = now - (40 * 86400)  # 40 days old

    # 1. Insert 5 old traces
    for i in range(5):
        tr = ConversationTrace(
            trace_id=f"tr_old_{i}",
            session_id="sess_old",
            run_id=f"r_old_{i}",
            query="old query",
            start_time=old_time + i,
        )
        tr.finish(output="old output")
        trace_store.save_trace(tr)

    # 2. Insert 10 fresh traces in sess_overflow
    for i in range(10):
        tr = ConversationTrace(
            trace_id=f"tr_overflow_{i}",
            session_id="sess_overflow",
            run_id=f"r_ov_{i}",
            query=f"query {i}",
            start_time=now + i,
        )
        tr.finish(output=f"output {i}")
        trace_store.save_trace(tr)

    # Prune with max_age_days=30 and max_rows_per_session=5
    pruned_count = trace_store.prune_old_traces(max_age_days=30, max_rows_per_session=5)
    # 5 old traces deleted + 5 excess traces in sess_overflow deleted = 10 total pruned
    assert pruned_count == 10

    # sess_old should have 0 traces remaining
    assert len(trace_store.list_traces(session_id="sess_old")) == 0
    # sess_overflow should have exactly 5 most recent traces remaining
    ov_traces = trace_store.list_traces(session_id="sess_overflow")
    assert len(ov_traces) == 5
    # IDs should be 5, 6, 7, 8, 9 (the latest ones)
    remaining_ids = {t["trace_id"] for t in ov_traces}
    assert remaining_ids == {f"tr_overflow_{i}" for i in range(5, 10)}


# ---------------------------------------------------------------------------
# 8. Startup Recovery & Reconciliation of Stale Tasks/Runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4_startup_reconciliation_stale_tasks_and_runs(tmp_path: Path) -> None:
    """reconcile_on_startup reconciles orphaned RUNNING tasks and runs to INTERRUPTED or UNKNOWN."""
    db_path = tmp_path / "startup_recon.db"
    coord = TaskCoordinator(db_path)
    hitl_mgr = HITLManager(db_path)
    sess_svc = SessionService(db_path)

    sess = sess_svc.create_session(title="Recon Sess", session_id="sess_recon")

    # Task 1: Stale normal task in RUNNING
    t1 = coord.create_task(sess.session_id, sess.session_id, query="query 1")
    r1 = coord.start_run(t1.task_id, runtime_id="dead_runtime_1")

    # Task 2: Stale task associated with UNKNOWN approval
    t2 = coord.create_task(sess.session_id, sess.session_id, query="query 2")
    r2 = coord.start_run(t2.task_id, runtime_id="dead_runtime_1")
    appr2 = hitl_mgr.create_request(
        thread_id=sess.session_id,
        run_id=r2.run_id,
        task_id=t2.task_id,
        tool_name="write_file",
        arguments={"f": "x"},
        tool_call_id="call_recon_2",
        description="write x",
    )
    hitl_mgr.resolve_request(appr2.id, ApprovalDecision(decision="approve"), thread_id=sess.session_id)
    hitl_mgr.acquire_execution_lease(
        appr2.id,
        thread_id=sess.session_id,
        tool_call_id=appr2.tool_call_id,
        arguments_hash=appr2.arguments_hash,
        lease_owner_instance_id="dead_runtime_1",
        lease_acquired_run_id=r2.run_id,
    )
    # Crash occurred -> approval is EXECUTING

    from jarvis_studio.core.persistence.runtime_lock import RUNTIME_LOCK_FILENAME
    expected_lock_path = (db_path.parent / RUNTIME_LOCK_FILENAME).resolve()
    lock = RuntimeInstanceLock([expected_lock_path], instance_id="rt_new", persistence_paths=[db_path])
    lock.acquire()
    try:
        report = await reconcile_on_startup(
            hitl_store=hitl_mgr.store,
            current_runtime_id="rt_new",
            runtime_lock=lock,
            task_coordinator=coord,
        )

        assert report.stale_executing_marked_unknown == 1
        assert report.stale_tasks_reconciled == 2
        assert report.stale_runs_reconciled == 2

        # t1 & r1 were normal -> reconciled to INTERRUPTED
        assert coord.get_task(t1.task_id).status == TaskStatus.INTERRUPTED
        assert coord.get_run(r1.run_id).status == RunStatus.INTERRUPTED

        # t2 & r2 were linked to stale executing approval marked UNKNOWN -> reconciled to UNKNOWN
        assert coord.get_task(t2.task_id).status == TaskStatus.UNKNOWN
        assert coord.get_run(r2.run_id).status == RunStatus.UNKNOWN
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 9. AgentRuntime Zero-Warning Full Lifecycle with TaskCoordinator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4_agent_runtime_manages_task_coordinator_cleanly(tmp_path: Path) -> None:
    """AgentRuntime initializes and shuts down TaskCoordinator without warning or lock leaks."""
    settings = Settings(
        storage=StorageConfig(sqlite_path=str(tmp_path / "runtime_p4.db")),
    )

    async with AgentRuntime.open(settings, instance_id="rt_p4_test") as runtime:
        assert runtime.state.value == "ready"
        assert runtime.task_coordinator is not None
        assert isinstance(runtime.task_coordinator, TaskCoordinator)

        # AgentService is bound and uses the runtime's task coordinator
        assert runtime.agent_service.task_coordinator is runtime.task_coordinator

    assert runtime.state.value == "closed"
    with pytest.raises(RuntimeError):
        _ = runtime.task_coordinator
