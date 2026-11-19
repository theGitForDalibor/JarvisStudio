"""TaskCoordinator: durable coordination of agent tasks and execution runs."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
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
    RunStatus,
    RunTriggerType,
    TaskStatus,
)

_logger = logging.getLogger(__name__)


def _row_to_task(row: sqlite3.Row) -> AgentTask:
    return AgentTask(
        task_id=row["task_id"],
        session_id=row["session_id"],
        thread_id=row["thread_id"],
        status=TaskStatus(row["status"]),
        query=row["query"],
        initiator=row["initiator"],
        current_run_id=row["current_run_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
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
        session_id: str,
        thread_id: str,
        query: str,
        *,
        initiator: str = "user",
        runtime_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentTask:
        """Insert a new task into `agent_tasks`."""
        task = AgentTask(
            task_id=task_id or f"task_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            thread_id=thread_id,
            status=TaskStatus.CREATED,
            query=query,
            initiator=initiator,
            runtime_id=runtime_id,
        )
        with self._tx() as conn:
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
                    task.session_id,
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

    def start_run(
        self,
        task_id: str,
        runtime_id: str,
        *,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> AgentRun:
        """Create and start a new run under the given task, bumping run_seq."""
        now = time.time()
        with self._tx() as conn:
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

    def list_tasks(self, session_id: str) -> list[AgentTask]:
        """List all tasks for a session, ordered by created_at descending."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            return [_row_to_task(r) for r in rows]

    def get_active_task_for_thread(self, thread_id: str) -> AgentTask | None:
        """Find the latest active task for thread (CREATED, RUNNING, WAITING_APPROVAL, EXECUTING_TOOL)."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE thread_id = ? AND status IN ('CREATED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def has_active_tasks(self, session_id: str) -> bool:
        """Check whether the session has any actively running, waiting, or executing task."""
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM agent_tasks
                WHERE session_id = ? AND status IN ('CREATED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL')
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

        - Runs in 'RUNNING' status become 'INTERRUPTED' (or 'UNKNOWN' if associated with UNKNOWN approval/outcome).
        - Tasks in 'RUNNING' or 'EXECUTING_TOOL' become 'INTERRUPTED' (or 'UNKNOWN' if associated with UNKNOWN approval/outcome).
        """
        now = time.time()
        with self._tx() as conn:
            # 1. Runs:
            cur_runs_unknown = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'UNKNOWN', finished_at = ?, error_message = 'Reconciled to UNKNOWN on runtime startup'
                WHERE status = 'RUNNING'
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
                WHERE status = 'RUNNING'
                """,
                (now,),
            )
            reconciled_runs = cur_runs_unknown.rowcount + cur_runs_interrupted.rowcount

            # 2. Tasks:
            cur_tasks_unknown = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'UNKNOWN', updated_at = ?, completed_at = ?, error_message = 'Reconciled to UNKNOWN on runtime startup'
                WHERE status IN ('CREATED', 'RUNNING', 'EXECUTING_TOOL')
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
                WHERE status IN ('CREATED', 'RUNNING', 'EXECUTING_TOOL')
                """,
                (now,),
            )
            reconciled_tasks = cur_tasks_unknown.rowcount + cur_tasks_interrupted.rowcount

        return (reconciled_tasks, reconciled_runs)


_default_task_coordinator: TaskCoordinator | None = None
_default_task_coordinator_lock = threading.Lock()


def get_task_coordinator(db_path: str | Path | None = None) -> TaskCoordinator:
    """Return the process-wide default TaskCoordinator singleton."""
    global _default_task_coordinator
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
