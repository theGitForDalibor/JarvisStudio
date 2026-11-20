"""TaskCoordinator: durable coordination of agent tasks and execution runs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from jarvis_studio.config import get_settings
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    initialize_sqlite_database,
    is_memory_db_path,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)
from jarvis_studio.core.tasks.models import (
    AgentRun,
    AgentTask,
    EventRecord,
    EventType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
)

_logger = logging.getLogger(__name__)


def _row_to_task(row: sqlite3.Row) -> AgentTask:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    return AgentTask(
        task_id=row["task_id"],
        session_id=row["session_id"],
        thread_id=row["thread_id"],
        status=TaskStatus(row["status"]),
        query=row["query"],
        initiator=row["initiator"],
        assigned_agent_id=row["assigned_agent_id"] if "assigned_agent_id" in keys else "general_agent",
        current_run_id=row["current_run_id"],
        plan_json=row["plan_json"] if "plan_json" in keys else None,
        knowledge_scope_json=(
            row["knowledge_scope_json"] if "knowledge_scope_json" in keys else None
        ),
        priority=row["priority"] if "priority" in keys else "NORMAL",
        result_summary=row["result_summary"] if "result_summary" in keys else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
        archived_at=(
            float(row["archived_at"])
            if ("archived_at" in keys and row["archived_at"] is not None)
            else None
        ),
        runtime_id=row["runtime_id"],
        error_message=row["error_message"],
    )


def _row_to_run(row: sqlite3.Row) -> AgentRun:
    return AgentRun(
        run_id=row["run_id"],
        task_id=row["task_id"],
        run_seq=int(row["run_seq"]),
        trigger_type=RunTriggerType(row["trigger_type"]),
        status=RunStatus(row["status"]),
        runtime_id=row["runtime_id"],
        started_at=float(row["started_at"]),
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        checkpoint_id=row["checkpoint_id"],
        error_message=row["error_message"],
    )


class TaskCoordinator:
    """SQLite-backed coordinator managing durable `agent_tasks` and `agent_runs`."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        raw_path = (
            db_path
            if db_path is not None
            else get_settings().storage.sqlite_path
        )
        self.db_path = normalize_db_path(raw_path)
        self._is_memory = is_memory_db_path(self.db_path)
        self._in_memory_conn: sqlite3.Connection | None = (
            open_sync_connection(":memory:", enable_foreign_keys=True)
            if self._is_memory
            else None
        )
        self._event_lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        if self._is_memory:
            run_app_migrations(self.db_path, in_memory_conn=self._in_memory_conn)
            return
        initialize_sqlite_database(self.db_path, enable_wal=True, enable_foreign_keys=True)
        run_app_migrations(self.db_path)

    @contextmanager
    def _read_conn(self):
        if self._is_memory:
            if self._in_memory_conn is None:
                raise RuntimeError("In-memory TaskCoordinator connection is closed")
            yield self._in_memory_conn
            return
        conn = open_sync_connection(self.db_path, enable_foreign_keys=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self):
        if self._is_memory and self._in_memory_conn is None:
            raise RuntimeError("In-memory TaskCoordinator connection is closed")
        with sync_transaction(
            self.db_path,
            in_memory_conn=self._in_memory_conn,
            enable_foreign_keys=True,
        ) as conn:
            yield conn

    def close(self) -> None:
        """Close connection and release resources."""
        if self._in_memory_conn is not None:
            self._in_memory_conn.close()
            self._in_memory_conn = None

    def create_task(
        self,
        session_id: str | None = None,
        thread_id: str | None = None,
        query: str = "",
        *,
        initiator: str = "user",
        assigned_agent_id: str = "general_agent",
        plan_json: str | None = None,
        knowledge_scope_json: str | None = None,
        priority: str = "NORMAL",
        result_summary: str | None = None,
        runtime_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentTask:
        """Insert a new task into `agent_tasks`."""
        effective_task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        effective_thread_id = thread_id or effective_task_id
        effective_session_id = session_id

        with self._tx() as conn:
            if effective_session_id is not None:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "chat_sessions" in tables:
                    row = conn.execute(
                        "SELECT 1 FROM chat_sessions WHERE session_id = ?",
                        (effective_session_id,),
                    ).fetchone()
                    if row is None:
                        _logger.debug(
                            "Session '%s' does not exist in chat_sessions; creating task independently with session_id=None",
                            effective_session_id,
                        )
                        effective_session_id = None

            task = AgentTask(
                task_id=effective_task_id,
                session_id=effective_session_id,
                thread_id=effective_thread_id,
                status=TaskStatus.CREATED,
                query=query,
                initiator=initiator,
                assigned_agent_id=assigned_agent_id,
                plan_json=plan_json,
                knowledge_scope_json=knowledge_scope_json,
                priority=priority,
                result_summary=result_summary,
                runtime_id=runtime_id,
            )

            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()]
            if "knowledge_scope_json" in cols:
                conn.execute(
                    """
                    INSERT INTO agent_tasks (
                        task_id, session_id, thread_id, status, query,
                        initiator, assigned_agent_id, current_run_id, plan_json,
                        knowledge_scope_json, priority, result_summary, created_at,
                        updated_at, completed_at, runtime_id, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.session_id,
                        task.thread_id,
                        task.status.value,
                        task.query,
                        task.initiator,
                        task.assigned_agent_id,
                        task.current_run_id,
                        task.plan_json,
                        task.knowledge_scope_json,
                        task.priority,
                        task.result_summary,
                        task.created_at,
                        task.updated_at,
                        task.completed_at,
                        task.runtime_id,
                        task.error_message,
                    ),
                )
            elif "assigned_agent_id" in cols:
                conn.execute(
                    """
                    INSERT INTO agent_tasks (
                        task_id, session_id, thread_id, status, query,
                        initiator, assigned_agent_id, current_run_id, plan_json,
                        priority, result_summary, created_at, updated_at,
                        completed_at, runtime_id, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.session_id,
                        task.thread_id,
                        task.status.value,
                        task.query,
                        task.initiator,
                        task.assigned_agent_id,
                        task.current_run_id,
                        task.plan_json,
                        task.priority,
                        task.result_summary,
                        task.created_at,
                        task.updated_at,
                        task.completed_at,
                        task.runtime_id,
                        task.error_message,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO agent_tasks (
                        task_id, session_id, thread_id, status, query,
                        initiator, current_run_id, created_at, updated_at,
                        completed_at, runtime_id, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.session_id or "default",
                        task.thread_id,
                        task.status.value,
                        task.query,
                        task.initiator,
                        task.current_run_id,
                        task.created_at,
                        task.updated_at,
                        task.completed_at,
                        task.runtime_id,
                        task.error_message,
                    ),
                )
        return task

    def queue_run(
        self,
        task_id: str,
        runtime_id: str,
        *,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> AgentRun:
        """Create and queue a run in QUEUED status under task_id with atomic DB single-flight guard."""
        now = time.time()
        with self._tx() as conn:
            t_row = conn.execute(
                "SELECT status, archived_at FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if t_row is None:
                raise KeyError(f"Task not found: {task_id}")
            t_status, t_archived = t_row[0], t_row[1]
            if t_archived is not None:
                raise ValueError(f"Task '{task_id}' is archived and cannot be executed")
            if t_status == TaskStatus.UNKNOWN.value:
                raise RuntimeError(
                    f"Task '{task_id}' is in UNKNOWN state due to uncertain external side effects "
                    "and cannot be automatically re-executed."
                )
            blocked_statuses = {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.EXECUTING_TOOL.value,
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.CANCELLING.value,
            }
            if trigger_type not in (RunTriggerType.USER_INPUT, RunTriggerType.RESUME):
                blocked_statuses.add(TaskStatus.COMPLETED.value)
            if t_status in blocked_statuses:
                raise ValueError(f"Task '{task_id}' cannot be queued while in state '{t_status}'")

            row = conn.execute(
                "SELECT COALESCE(MAX(run_seq), 0) FROM agent_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_seq = int(row[0]) + 1 if row is not None and row[0] is not None else 1

            run = AgentRun(
                run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                run_seq=next_seq,
                trigger_type=trigger_type,
                status=RunStatus.QUEUED,
                runtime_id=runtime_id,
                started_at=now,
                checkpoint_id=checkpoint_id,
            )
            conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, task_id, run_seq, trigger_type, status,
                    runtime_id, started_at, checkpoint_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.task_id,
                    run.run_seq,
                    run.trigger_type.value,
                    run.status.value,
                    run.runtime_id,
                    run.started_at,
                    run.checkpoint_id,
                ),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET current_run_id = ?, status = ?, runtime_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (run.run_id, TaskStatus.QUEUED.value, runtime_id, now, task_id),
            )
        return run

    def start_run(
        self,
        task_id: str,
        runtime_id: str,
        *,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
        enforce_single_flight: bool = False,
    ) -> AgentRun:
        """Create and start a new run under the given task, or transition an existing QUEUED run to RUNNING."""
        now = time.time()
        with self._tx() as conn:
            existing = None
            if run_id:
                conn.row_factory = sqlite3.Row
                existing_row = conn.execute(
                    "SELECT * FROM agent_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing_row:
                    existing = _row_to_run(existing_row)

            if existing is not None:
                if existing.status != RunStatus.QUEUED:
                    raise ValueError(
                        f"Queued run '{run_id}' cannot start from status '{existing.status.value}'"
                    )
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?, started_at = ?, runtime_id = ?,
                        checkpoint_id = COALESCE(?, checkpoint_id)
                    WHERE run_id = ?
                    """,
                    (RunStatus.RUNNING.value, now, runtime_id, checkpoint_id, run_id),
                )
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET current_run_id = ?, status = ?, runtime_id = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (run_id, TaskStatus.RUNNING.value, runtime_id, now, task_id),
                )
                existing.status = RunStatus.RUNNING
                existing.started_at = now
                existing.runtime_id = runtime_id
                if checkpoint_id:
                    existing.checkpoint_id = checkpoint_id
                return existing

            t_row = conn.execute(
                "SELECT status, archived_at FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if t_row is not None:
                t_status, t_archived = t_row[0], t_row[1]
                if t_archived is not None:
                    raise ValueError(f"Task '{task_id}' is archived and cannot be executed")
                if enforce_single_flight:
                    blocked_statuses = {
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                        TaskStatus.EXECUTING_TOOL.value,
                        TaskStatus.CANCELLING.value,
                    }
                    if trigger_type != RunTriggerType.HITL_RESUME:
                        blocked_statuses.add(TaskStatus.WAITING_APPROVAL.value)
                    if t_status in blocked_statuses:
                        raise ValueError(
                            f"Task '{task_id}' cannot start a new run while in state '{t_status}'"
                        )

            row = conn.execute(
                "SELECT COALESCE(MAX(run_seq), 0) FROM agent_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_seq = int(row[0]) + 1 if row is not None and row[0] is not None else 1

            run = AgentRun(
                run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                run_seq=next_seq,
                trigger_type=trigger_type,
                status=RunStatus.RUNNING,
                runtime_id=runtime_id,
                started_at=now,
                checkpoint_id=checkpoint_id,
            )
            conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, task_id, run_seq, trigger_type, status,
                    runtime_id, started_at, checkpoint_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.task_id,
                    run.run_seq,
                    run.trigger_type.value,
                    run.status.value,
                    run.runtime_id,
                    run.started_at,
                    run.checkpoint_id,
                ),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET current_run_id = ?, status = ?, runtime_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (run.run_id, TaskStatus.RUNNING.value, runtime_id, now, task_id),
            )
        return run

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update task status and timestamps."""
        now = time.time()
        is_terminal = status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMEOUT,
            TaskStatus.UNKNOWN,
        )
        with self._tx() as conn:
            if is_terminal:
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status = ?, updated_at = ?, completed_at = ?,
                        error_message = COALESCE(?, error_message)
                    WHERE task_id = ?
                    """,
                    (status.value, now, now, error_message, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status = ?, updated_at = ?,
                        error_message = COALESCE(?, error_message)
                    WHERE task_id = ?
                    """,
                    (status.value, now, error_message, task_id),
                )

    def pause_for_approval(
        self,
        task_id: str,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        """Transition task to WAITING_APPROVAL and run to INTERRUPTED."""
        now = time.time()
        with self._tx() as conn:
            started_row = conn.execute(
                "SELECT started_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started_row is not None and started_row[0] is not None:
                duration_ms = max(0, int((now - float(started_row[0])) * 1000))

            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, finished_at = ?, duration_ms = ?,
                    checkpoint_id = COALESCE(?, checkpoint_id)
                WHERE run_id = ?
                """,
                (RunStatus.INTERRUPTED.value, now, duration_ms, checkpoint_id, run_id),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskStatus.WAITING_APPROVAL.value, now, task_id),
            )

    def complete_task_and_run(
        self,
        task_id: str,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        """Transition both run and task to COMPLETED."""
        now = time.time()
        with self._tx() as conn:
            started_row = conn.execute(
                "SELECT started_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started_row is not None and started_row[0] is not None:
                duration_ms = max(0, int((now - float(started_row[0])) * 1000))

            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, finished_at = ?, duration_ms = ?,
                    checkpoint_id = COALESCE(?, checkpoint_id)
                WHERE run_id = ?
                """,
                (RunStatus.COMPLETED.value, now, duration_ms, checkpoint_id, run_id),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?, completed_at = ?
                WHERE task_id = ?
                """,
                (TaskStatus.COMPLETED.value, now, now, task_id),
            )

    def fail_task_and_run(
        self,
        task_id: str,
        run_id: str,
        *,
        error_message: str | None = None,
        checkpoint_id: str | None = None,
    ) -> None:
        """Transition both run and task to FAILED."""
        now = time.time()
        with self._tx() as conn:
            started_row = conn.execute(
                "SELECT started_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started_row is not None and started_row[0] is not None:
                duration_ms = max(0, int((now - float(started_row[0])) * 1000))

            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, finished_at = ?, duration_ms = ?,
                    error_message = ?, checkpoint_id = COALESCE(?, checkpoint_id)
                WHERE run_id = ?
                """,
                (RunStatus.FAILED.value, now, duration_ms, error_message, checkpoint_id, run_id),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?, completed_at = ?, error_message = ?
                WHERE task_id = ?
                """,
                (TaskStatus.FAILED.value, now, now, error_message, task_id),
            )

    def cancel_task_and_run(
        self,
        task_id: str,
        run_id: str,
        *,
        is_unknown: bool = False,
        error_message: str | None = None,
        checkpoint_id: str | None = None,
    ) -> None:
        """Transition both run and task to CANCELLED (or UNKNOWN if tool execution was in-flight)."""
        now = time.time()
        run_status = RunStatus.UNKNOWN if is_unknown else RunStatus.CANCELLED
        task_status = TaskStatus.UNKNOWN if is_unknown else TaskStatus.CANCELLED

        with self._tx() as conn:
            started_row = conn.execute(
                "SELECT started_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started_row is not None and started_row[0] is not None:
                duration_ms = max(0, int((now - float(started_row[0])) * 1000))

            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, finished_at = ?, duration_ms = ?,
                    error_message = ?, checkpoint_id = COALESCE(?, checkpoint_id)
                WHERE run_id = ?
                """,
                (run_status.value, now, duration_ms, error_message, checkpoint_id, run_id),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?, completed_at = ?, error_message = ?
                WHERE task_id = ?
                """,
                (task_status.value, now, now, error_message, task_id),
            )

    def pause_task_and_run(
        self,
        task_id: str,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        """Transition task and active run to PAUSED."""
        now = time.time()
        with self._tx() as conn:
            started_row = conn.execute(
                "SELECT started_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started_row is not None and started_row[0] is not None:
                duration_ms = max(0, int((now - float(started_row[0])) * 1000))

            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, finished_at = ?, duration_ms = ?,
                    checkpoint_id = COALESCE(?, checkpoint_id)
                WHERE run_id = ?
                """,
                (RunStatus.PAUSED.value, now, duration_ms, checkpoint_id, run_id),
            )
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskStatus.PAUSED.value, now, task_id),
            )

    def mark_cancelling(
        self,
        task_id: str,
        run_id: str | None = None,
    ) -> None:
        """Transition task and optional active run to CANCELLING."""
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskStatus.CANCELLING.value, now, task_id),
            )
            if run_id:
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?
                    WHERE run_id = ?
                    """,
                    (RunStatus.CANCELLING.value, run_id),
                )

    def get_task(self, task_id: str) -> AgentTask | None:
        """Retrieve task by task_id."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def get_run(self, run_id: str) -> AgentRun | None:
        """Retrieve run by run_id."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return _row_to_run(row) if row is not None else None

    def list_runs(self, task_id: str) -> list[AgentRun]:
        """List all runs for a task, ordered by run_seq ascending."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY run_seq ASC",
                (task_id,),
            ).fetchall()
            return [_row_to_run(r) for r in rows]

    def list_tasks(
        self,
        session_id: str | None = None,
        *,
        status: TaskStatus | str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[AgentTask]:
        """List tasks, optionally filtered by session_id, status, and archive state."""
        query = "SELECT * FROM agent_tasks"
        params: list[object] = []
        conditions: list[str] = []
        if not include_archived:
            conditions.append("archived_at IS NULL")
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            status_val = status.value if isinstance(status, TaskStatus) else str(status)
            conditions.append("status = ?")
            params.append(status_val)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_task(r) for r in rows]

    def get_task_by_thread_id(self, thread_id: str) -> AgentTask | None:
        """Retrieve the latest task bound to the given LangGraph thread_id."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE thread_id = ? ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def archive_task(self, task_id: str) -> AgentTask | None:
        """Soft-delete (archive) a task while preserving all Runs, Events, and Deliverables."""
        now = time.time()
        with self._tx() as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT 1 FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing is None:
                return None
            conn.execute(
                """
                UPDATE agent_tasks
                SET archived_at = COALESCE(archived_at, ?), updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def delete_task(self, task_id: str) -> bool:
        """Soft-delete a task via archive_task so Run and Deliverable history remain intact."""
        return self.archive_task(task_id) is not None

    def purge_task(
        self,
        task_id: str,
        *,
        before_commit_hook: Callable[[], None] | None = None,
    ) -> bool:
        """Permanently purge a task and all associated DB records inside one transaction.

        If `before_commit_hook` is supplied and raises, the database transaction rolls back
        atomically so DB state and external side effects remain consistent.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            if row[0] in (
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.EXECUTING_TOOL.value,
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.CANCELLING.value,
            ):
                raise ValueError(
                    f"Task '{task_id}' cannot be purged while in active state '{row[0]}'"
                )
            # Cascade delete in proper child-to-parent order
            conn.execute("DELETE FROM agent_deliverables WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM agent_events WHERE task_id = ?", (task_id,))
            tbl_traces = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
            ).fetchone()
            if tbl_traces:
                conn.execute("DELETE FROM agent_conversation_traces WHERE task_id = ?", (task_id,))
            tbl_outcomes = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_execution_outcomes'"
            ).fetchone()
            if tbl_outcomes:
                conn.execute("DELETE FROM tool_execution_outcomes WHERE task_id = ?", (task_id,))
            tbl_approvals = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hitl_approval_requests'"
            ).fetchone()
            if tbl_approvals:
                conn.execute("DELETE FROM hitl_approval_requests WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM agent_runs WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM agent_tasks WHERE task_id = ?", (task_id,))
            if before_commit_hook is not None:
                before_commit_hook()
            return True

    def record_event(
        self,
        task_id: str,
        event_type: EventType | str,
        *,
        run_id: str | None = None,
        payload: dict[str, object] | None = None,
        event_id: str | None = None,
    ) -> EventRecord:
        """Persist a user-observable business execution event with monotonic seq per task."""
        now = time.time()
        evt_type = event_type if isinstance(event_type, EventType) else EventType(str(event_type))
        payload_data = payload or {}
        payload_json = json.dumps(payload_data, ensure_ascii=False)
        e_id = event_id or f"evt_{uuid.uuid4().hex[:12]}"

        with self._event_lock:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM agent_events WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                next_seq = int(row[0]) + 1 if row is not None and row[0] is not None else 1

                conn.execute(
                    """
                    INSERT INTO agent_events (
                        event_id, task_id, run_id, seq, event_type, timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (e_id, task_id, run_id, next_seq, evt_type.value, now, payload_json),
                )
            record = EventRecord(
                event_id=e_id,
                task_id=task_id,
                run_id=run_id,
                seq=next_seq,
                event_type=evt_type,
                timestamp=now,
                payload_json=payload_json,
            )
            try:
                from jarvis_studio.core.observability.event_broadcaster import get_event_broadcaster

                get_event_broadcaster().broadcast(record)
            except Exception as b_exc:  # noqa: BLE001
                _logger.debug("Failed to broadcast event %s: %s", e_id, b_exc)
        return record

    def list_events_for_task(
        self,
        task_id: str,
        *,
        since_seq: int = 0,
        limit: int = 100,
    ) -> list[EventRecord]:
        """List events for task strictly after since_seq ordered by seq ASC."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE task_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (task_id, since_seq, limit),
            ).fetchall()
            results = []
            for r in rows:
                p_json = r["payload_json"] or "{}"
                evt_name = r["event_type"]
                event_type_enum = (
                    EventType(evt_name)
                    if evt_name in EventType._value2member_map_
                    else EventType.TASK_CREATED
                )
                results.append(
                    EventRecord(
                        event_id=r["event_id"],
                        task_id=r["task_id"],
                        run_id=r["run_id"],
                        seq=int(r["seq"]),
                        event_type=event_type_enum,
                        timestamp=float(r["timestamp"]),
                        payload_json=p_json,
                    )
                )
            return results

    def update_task_metadata(
        self,
        task_id: str,
        *,
        plan_json: str | None = None,
        result_summary: str | None = None,
        priority: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> None:
        """Update mutable metadata on a task."""
        now = time.time()
        updates: list[str] = ["updated_at = ?"]
        params: list[object] = [now]
        if plan_json is not None:
            updates.append("plan_json = ?")
            params.append(plan_json)
        if result_summary is not None:
            updates.append("result_summary = ?")
            params.append(result_summary)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if assigned_agent_id is not None:
            updates.append("assigned_agent_id = ?")
            params.append(assigned_agent_id)
        params.append(task_id)

        with self._tx() as conn:
            conn.execute(
                f"UPDATE agent_tasks SET {', '.join(updates)} WHERE task_id = ?",
                tuple(params),
            )

    def get_active_task_for_thread(self, thread_id: str) -> AgentTask | None:
        """Find the latest active task for thread (CREATED, QUEUED, RUNNING, WAITING_APPROVAL, EXECUTING_TOOL, CANCELLING)."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE thread_id = ? AND status IN ('CREATED', 'QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL', 'CANCELLING')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def has_active_tasks(self, session_id: str) -> bool:
        """Check whether the session has any actively queued, running, waiting, or cancelling task."""
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM agent_tasks
                WHERE session_id = ? AND status IN ('CREATED', 'QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL', 'CANCELLING')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return row is not None

    def get_task_by_approval(self, approval_id: str) -> AgentTask | None:
        """Find the agent task associated with a HITL approval request."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT t.* FROM agent_tasks t
                INNER JOIN hitl_approval_requests a ON t.task_id = a.task_id
                WHERE a.id = ?
                LIMIT 1
                """,
                (approval_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None


    def reconcile_stale_tasks_and_runs(self, current_runtime_id: str) -> tuple[int, int]:
        """Reconcile tasks and runs from prior crashes or dead runtimes.

        - Runs in 'RUNNING', 'QUEUED', 'CANCELLING' status become 'INTERRUPTED' (or 'UNKNOWN' if associated with UNKNOWN approval/outcome).
        - Tasks in 'RUNNING', 'QUEUED', 'EXECUTING_TOOL', 'CANCELLING' become 'INTERRUPTED' (or 'UNKNOWN' if associated with UNKNOWN approval/outcome).
        """
        now = time.time()
        with self._tx() as conn:
            # 1. Runs:
            cur_runs_unknown = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'UNKNOWN', finished_at = ?, error_message = 'Reconciled to UNKNOWN on runtime startup'
                WHERE status IN ('RUNNING', 'QUEUED', 'CANCELLING')
                  AND task_id IN (
                      SELECT task_id FROM hitl_approval_requests WHERE status = 'UNKNOWN'
                      UNION
                      SELECT task_id FROM tool_execution_outcomes WHERE outcome_status = 'UNKNOWN'
                  )
                """,
                (now,),
            )
            cur_runs_interrupted = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'INTERRUPTED', finished_at = ?, error_message = 'Interrupted by runtime shutdown/restart'
                WHERE status IN ('RUNNING', 'QUEUED', 'CANCELLING')
                """,
                (now,),
            )
            reconciled_runs = cur_runs_unknown.rowcount + cur_runs_interrupted.rowcount

            # 2. Tasks:
            cur_tasks_unknown = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'UNKNOWN', updated_at = ?, completed_at = ?, error_message = 'Reconciled to UNKNOWN on runtime startup'
                WHERE status IN ('CREATED', 'QUEUED', 'RUNNING', 'EXECUTING_TOOL', 'CANCELLING')
                  AND (
                      task_id IN (SELECT task_id FROM hitl_approval_requests WHERE status = 'UNKNOWN')
                      OR task_id IN (SELECT task_id FROM tool_execution_outcomes WHERE outcome_status = 'UNKNOWN')
                  )
                """,
                (now, now),
            )
            cur_tasks_interrupted = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'INTERRUPTED', updated_at = ?, error_message = 'Interrupted by runtime shutdown/restart'
                WHERE status IN ('CREATED', 'QUEUED', 'RUNNING', 'EXECUTING_TOOL', 'CANCELLING')
                """,
                (now,),
            )
            reconciled_tasks = cur_tasks_unknown.rowcount + cur_tasks_interrupted.rowcount

        return (reconciled_tasks, reconciled_runs)


_default_task_coordinator: TaskCoordinator | None = None
_default_task_coordinator_lock = threading.Lock()


def get_task_coordinator(db_path: str | Path | None = None) -> TaskCoordinator:
    """Return the active AgentRuntime's TaskCoordinator or process-wide default singleton."""
    global _default_task_coordinator
    if db_path is None:
        from jarvis_studio.core.runtime.agent_runtime import RuntimeState, get_active_runtime

        active_rt = get_active_runtime()
        if active_rt is not None and active_rt.state == RuntimeState.READY:
            return active_rt.task_coordinator

    if _default_task_coordinator is not None and db_path is None:
        return _default_task_coordinator

    with _default_task_coordinator_lock:
        if _default_task_coordinator is not None and db_path is None:
            return _default_task_coordinator
        coordinator = TaskCoordinator(db_path=db_path)
        if db_path is None:
            _default_task_coordinator = coordinator
        return coordinator


def reset_global_task_coordinator() -> None:
    """Close and clear the global TaskCoordinator singleton."""
    global _default_task_coordinator
    with _default_task_coordinator_lock:
        if _default_task_coordinator is not None:
            try:
                _default_task_coordinator.close()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Error closing global TaskCoordinator: %s", exc)
            _default_task_coordinator = None
