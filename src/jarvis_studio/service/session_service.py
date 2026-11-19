"""Session and Thread lifecycle management with SQLite persistence."""

import contextlib
import json
import sqlite3
import time
import uuid
from typing import Any, Self

from pydantic import BaseModel, Field

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    SyncSQLiteSession,
    initialize_sqlite_database,
)


class SessionBusyError(Exception):
    """Raised when a session operation cannot proceed because active tasks or executions are running."""


class ChatMessageRecord(BaseModel):
    """Stored chat message in a session."""

    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    session_id: str
    task_id: str | None = None
    run_id: str | None = None
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    msg_type: str | None = None
    tool_call_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class SessionMetadata(BaseModel):
    """Metadata for an active user thread/session."""

    session_id: str = Field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:10]}")
    title: str = "新会话"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    message_count: int = 0
    last_message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionService:
    """Service to track, persist, and manage sessions and message histories."""

    def __init__(self, db_path: str | None = None) -> None:
        resolved_path = db_path or get_settings().storage.sqlite_path
        self._db = SyncSQLiteSession(resolved_path, enable_foreign_keys=True)
        self.db_path = self._db.db_path
        if not self._db.is_memory:
            initialize_sqlite_database(
                self.db_path, enable_wal=True, enable_foreign_keys=True
            )
            run_app_migrations(self.db_path)
        else:
            run_app_migrations(self.db_path, in_memory_conn=self._in_memory_conn)
        self._init_db()

    @property
    def _in_memory_conn(self) -> sqlite3.Connection | None:
        return self._db.in_memory_conn

    def close(self) -> None:
        """Idempotently release any private in-memory SQLite connection."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_db(self) -> None:
        """Create sessions and messages tables if they do not exist."""
        if not self._db.is_memory:
            with self._db.read_connection() as conn:
                tbl_cnt = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('chat_sessions', 'chat_messages')"
                ).fetchone()[0]
            if tbl_cnt == 2:
                return

        with self._db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id           TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    task_id      TEXT,
                    run_id       TEXT,
                    role         TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    tool_calls   TEXT,
                    msg_type     TEXT,
                    tool_call_id TEXT,
                    created_at   REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_msg_task ON chat_messages(task_id)"
            )

    def create_session(
        self, title: str | None = None, session_id: str | None = None
    ) -> SessionMetadata:
        """Explicitly create a new session."""
        now = time.time()
        sid = session_id or f"sess_{uuid.uuid4().hex[:10]}"
        sess_title = title or "新会话"
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (session_id, title, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (sid, sess_title, now, now, json.dumps({})),
            )

        logger.info(f"Created session: {sid} ('{sess_title}')")
        return SessionMetadata(
            session_id=sid,
            title=sess_title,
            created_at=now,
            updated_at=now,
            message_count=0,
            last_message="",
        )

    def get_or_create_session(
        self, session_id: str | None = None, title: str | None = None
    ) -> SessionMetadata:
        """Retrieve existing session or instantiate a new one."""
        if not session_id:
            return self.create_session(title=title)

        existing = self.get_session(session_id)
        if existing:
            # Touch updated_at
            now = time.time()
            with self._db.transaction() as conn:
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
            existing.updated_at = now
            return existing

        return self.create_session(title=title, session_id=session_id)

    def get_session(self, session_id: str) -> SessionMetadata | None:
        """Get session metadata and stats by ID."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            # Get message count and last message
            msg_cursor = conn.execute(
                """
                SELECT COUNT(*) as cnt, 
                       (SELECT content FROM chat_messages WHERE session_id = ? ORDER BY created_at DESC LIMIT 1) as last_msg
                FROM chat_messages WHERE session_id = ?
                """,
                (session_id, session_id),
            )
            msg_stat = msg_cursor.fetchone()
            count = msg_stat["cnt"] if msg_stat else 0
            last_msg = msg_stat["last_msg"] if msg_stat and msg_stat["last_msg"] else ""

            meta = {}
            if row["metadata"]:
                with contextlib.suppress(Exception):
                    meta = json.loads(row["metadata"])

            return SessionMetadata(
                session_id=row["session_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=count,
                last_message=last_msg[:100],
                metadata=meta,
            )

    def list_sessions(self) -> list[SessionMetadata]:
        """Return all tracked sessions sorted by most recent."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                """
                SELECT s.session_id, s.title, s.created_at, s.updated_at, s.metadata,
                       COUNT(m.id) as message_count,
                       (SELECT content FROM chat_messages WHERE session_id = s.session_id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM chat_sessions s
                LEFT JOIN chat_messages m ON s.session_id = m.session_id
                GROUP BY s.session_id
                ORDER BY s.updated_at DESC
                """
            )
            results = []
            for row in cursor.fetchall():
                meta = {}
                if row["metadata"]:
                    with contextlib.suppress(Exception):
                        meta = json.loads(row["metadata"])
                results.append(
                    SessionMetadata(
                        session_id=row["session_id"],
                        title=row["title"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        message_count=row["message_count"] or 0,
                        last_message=(row["last_message"] or "")[:100],
                        metadata=meta,
                    )
                )
            return results

    def update_session_title(
        self, session_id: str, title: str
    ) -> SessionMetadata | None:
        """Rename an existing session."""
        clean_title = title.strip()
        if not clean_title:
            return None

        with self._db.transaction() as conn:
            now = time.time()
            cursor = conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (clean_title, now, session_id),
            )
            if cursor.rowcount == 0:
                return None

        return self.get_session(session_id)

    def is_session_busy(
        self, session_id: str, conn: sqlite3.Connection | None = None
    ) -> bool:
        """Check whether the session has any active tasks or in-flight executions."""
        def _check(c: sqlite3.Connection) -> bool:
            # Check agent_tasks
            tbl_tasks = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
            ).fetchone()
            if tbl_tasks:
                row = c.execute(
                    """
                    SELECT 1 FROM agent_tasks
                    WHERE session_id = ? AND status IN ('CREATED', 'RUNNING', 'WAITING_APPROVAL', 'EXECUTING_TOOL')
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if row:
                    return True

            # Check hitl_approval_requests
            tbl_hitl = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hitl_approval_requests'"
            ).fetchone()
            if tbl_hitl:
                row = c.execute(
                    """
                    SELECT 1 FROM hitl_approval_requests
                    WHERE (thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?))
                      AND status IN ('PENDING', 'APPROVED', 'MODIFIED', 'EXECUTING')
                    LIMIT 1
                    """,
                    (session_id, session_id),
                ).fetchone()
                if row:
                    return True

            # Check tool_execution_outcomes
            tbl_outcomes = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_execution_outcomes'"
            ).fetchone()
            if tbl_outcomes:
                row = c.execute(
                    """
                    SELECT 1 FROM tool_execution_outcomes
                    WHERE (thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?))
                      AND outcome_status = 'NOT_STARTED'
                    LIMIT 1
                    """,
                    (session_id, session_id),
                ).fetchone()
                if row:
                    return True

            return False

        if conn is not None:
            return _check(conn)
        with self._db.read_connection() as c:
            return _check(c)

    def delete_session(
        self,
        session_id: str,
        *,
        checkpointer: Any | None = None,
        keep_tasks: bool = True,
    ) -> bool:
        """Remove a session thread.

        Defaults to `keep_tasks=True`: unlinks tasks (`session_id = NULL`) and task-linked traces
        so user tasks, runs, and deliverables are preserved safely.
        Pass `keep_tasks=False` explicitly only when full legacy cascade deletion is intended.

        Raises SessionBusyError if any active task or execution is in progress.
        """
        with self._db.transaction() as conn:
            if self.is_session_busy(session_id, conn):
                raise SessionBusyError(
                    f"Session '{session_id}' cannot be deleted while active tasks or executions are running."
                )

            exists = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not exists:
                return False

            if keep_tasks:
                # Decoupled deletion: Preserve tasks, runs, outcomes, and deliverables.
                # Unlink tasks by setting session_id to NULL
                tbl_tasks = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
                ).fetchone()
                if tbl_tasks:
                    conn.execute("UPDATE agent_tasks SET session_id = NULL WHERE session_id = ?", (session_id,))

                # Unlink or clean conversation traces
                tbl_traces = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
                ).fetchone()
                if tbl_traces:
                    conn.execute(
                        "UPDATE agent_conversation_traces SET session_id = NULL WHERE session_id = ? AND task_id IS NOT NULL",
                        (session_id,),
                    )
                    conn.execute(
                        "DELETE FROM agent_conversation_traces WHERE session_id = ? AND task_id IS NULL",
                        (session_id,),
                    )

                # Delete session chat messages
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))

                # Unlink LTM memories from this session
                tbl_ltm = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ltm_memories'"
                ).fetchone()
                if tbl_ltm:
                    conn.execute(
                        "UPDATE ltm_memories SET source_session_id = NULL WHERE source_session_id = ?",
                        (session_id,),
                    )

                # Delete the session itself
                cursor = conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            else:
                # Cascade deletion across 8 tables in exact FK dependency order:
                # 1. agent_conversation_traces
                tbl_traces = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
                ).fetchone()
                if tbl_traces:
                    conn.execute(
                        """
                        DELETE FROM agent_conversation_traces
                        WHERE session_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                        """,
                        (session_id, session_id),
                    )

                # 2. tool_execution_outcomes
                tbl_outcomes = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_execution_outcomes'"
                ).fetchone()
                if tbl_outcomes:
                    conn.execute(
                        """
                        DELETE FROM tool_execution_outcomes
                        WHERE thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                        """,
                        (session_id, session_id),
                    )

                # 3. hitl_approval_requests
                tbl_hitl = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hitl_approval_requests'"
                ).fetchone()
                if tbl_hitl:
                    conn.execute(
                        """
                        DELETE FROM hitl_approval_requests
                        WHERE thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                        """,
                        (session_id, session_id),
                    )

                # 4. agent_deliverables & agent_events (when present)
                tbl_deliv = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_deliverables'"
                ).fetchone()
                if tbl_deliv:
                    conn.execute(
                        "DELETE FROM agent_deliverables WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                        (session_id,),
                    )
                tbl_events = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_events'"
                ).fetchone()
                if tbl_events:
                    conn.execute(
                        "DELETE FROM agent_events WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                        (session_id,),
                    )

                # 5. agent_runs
                tbl_runs = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'"
                ).fetchone()
                if tbl_runs:
                    conn.execute(
                        "DELETE FROM agent_runs WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                        (session_id,),
                    )

                # 5. agent_tasks
                tbl_tasks = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
                ).fetchone()
                if tbl_tasks:
                    conn.execute("DELETE FROM agent_tasks WHERE session_id = ?", (session_id,))

                # 6. chat_messages
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))

                # 7. ltm_memories (nullify source session/task pointers)
                tbl_ltm = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ltm_memories'"
                ).fetchone()
                if tbl_ltm:
                    conn.execute(
                        """
                        UPDATE ltm_memories
                        SET source_session_id = NULL, source_task_id = NULL
                        WHERE source_session_id = ?
                        """,
                        (session_id,),
                    )

                # 8. chat_sessions
                cursor = conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))

        # Post-transaction: clean sticky context
        try:
            from jarvis_studio.core.guard.route_guard import get_route_guard
            get_route_guard().clear_sticky_context(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed clearing sticky context for session {session_id}: {exc}")

        return cursor.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        msg_type: str | None = None,
        tool_call_id: str | None = None,
    ) -> ChatMessageRecord:
        """Append a message to session history in a single atomic transaction."""
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = time.time()
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        with self._db.transaction() as conn:
            # Single-transaction ensure session exists
            conn.execute(
                """
                INSERT OR IGNORE INTO chat_sessions (session_id, title, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, "新会话", now, now, json.dumps({})),
            )

            conn.execute(
                """
                INSERT INTO chat_messages (
                    id, session_id, task_id, run_id, role, content,
                    tool_calls, msg_type, tool_call_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    session_id,
                    task_id,
                    run_id,
                    role,
                    content,
                    tool_calls_json,
                    msg_type,
                    tool_call_id,
                    now,
                ),
            )

            # Auto-title session if currently default
            if role == "user":
                cursor = conn.execute(
                    "SELECT title FROM chat_sessions WHERE session_id = ?", (session_id,)
                )
                row = cursor.fetchone()
                if row and row["title"] in ("新会话", "New Session", "Default Workspace Session"):
                    snippet = content.strip().replace("\n", " ")
                    new_title = snippet[:24] + ("..." if len(snippet) > 24 else "")
                    conn.execute(
                        "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                        (new_title, now, session_id),
                    )
                else:
                    conn.execute(
                        "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
            else:
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                    (now, session_id),
                )

        return ChatMessageRecord(
            id=msg_id,
            session_id=session_id,
            task_id=task_id,
            run_id=run_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            msg_type=msg_type,
            tool_call_id=tool_call_id,
            created_at=now,
        )

    def get_messages(
        self, session_id: str, limit: int = 200
    ) -> list[ChatMessageRecord]:
        """Fetch chronological message history for a session."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, session_id, task_id, run_id, role, content,
                       tool_calls, msg_type, tool_call_id, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, limit),
            )
            messages = []
            for row in cursor.fetchall():
                tc = None
                if row["tool_calls"]:
                    try:
                        tc = json.loads(row["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        tc = None
                keys = set(row.keys())
                messages.append(
                    ChatMessageRecord(
                        id=row["id"],
                        session_id=row["session_id"],
                        task_id=row["task_id"] if "task_id" in keys else None,
                        run_id=row["run_id"] if "run_id" in keys else None,
                        role=row["role"],
                        content=row["content"],
                        tool_calls=tc,
                        msg_type=row["msg_type"] if "msg_type" in keys else None,
                        tool_call_id=row["tool_call_id"] if "tool_call_id" in keys else None,
                        created_at=row["created_at"],
                    )
                )
            return messages

    def clear_session_messages(
        self,
        session_id: str,
        *,
        keep_tasks: bool = True,
    ) -> bool:
        """Remove chat messages for a session while retaining session metadata.

        When `keep_tasks=True` (default), preserves tasks, runs, deliverables, HITL approvals,
        and task-linked traces; only clears `chat_messages` and task-less session traces.

        Raises SessionBusyError if any active task or execution is in progress.
        """
        with self._db.transaction() as conn:
            if self.is_session_busy(session_id, conn):
                raise SessionBusyError(
                    f"Session '{session_id}' messages cannot be cleared while active tasks or executions are running."
                )

            if keep_tasks:
                tbl_traces = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
                ).fetchone()
                if tbl_traces:
                    conn.execute(
                        "DELETE FROM agent_conversation_traces WHERE session_id = ? AND task_id IS NULL",
                        (session_id,),
                    )
                cursor = conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
                return cursor.rowcount > 0

            # Legacy explicit cascade clear (keep_tasks=False)
            # Clean traces
            tbl_traces = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversation_traces'"
            ).fetchone()
            if tbl_traces:
                conn.execute(
                    """
                    DELETE FROM agent_conversation_traces
                    WHERE session_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                    """,
                    (session_id, session_id),
                )

            # Clean outcomes
            tbl_outcomes = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_execution_outcomes'"
            ).fetchone()
            if tbl_outcomes:
                conn.execute(
                    """
                    DELETE FROM tool_execution_outcomes
                    WHERE thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                    """,
                    (session_id, session_id),
                )

            # Clean approvals
            tbl_hitl = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hitl_approval_requests'"
            ).fetchone()
            if tbl_hitl:
                conn.execute(
                    """
                    DELETE FROM hitl_approval_requests
                    WHERE thread_id = ? OR task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)
                    """,
                    (session_id, session_id),
                )

            # Clean deliverables & events
            tbl_deliv = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_deliverables'"
            ).fetchone()
            if tbl_deliv:
                conn.execute(
                    "DELETE FROM agent_deliverables WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                    (session_id,),
                )
            tbl_events = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_events'"
            ).fetchone()
            if tbl_events:
                conn.execute(
                    "DELETE FROM agent_events WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                    (session_id,),
                )

            # Clean runs
            tbl_runs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'"
            ).fetchone()
            if tbl_runs:
                conn.execute(
                    "DELETE FROM agent_runs WHERE task_id IN (SELECT task_id FROM agent_tasks WHERE session_id = ?)",
                    (session_id,),
                )

            # Clean tasks
            tbl_tasks = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
            ).fetchone()
            if tbl_tasks:
                conn.execute("DELETE FROM agent_tasks WHERE session_id = ?", (session_id,))

            # Clean messages
            cursor = conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            return cursor.rowcount > 0


_global_session_service: SessionService | None = None


def get_session_service() -> SessionService:
    """Retrieve the singleton SessionService instance."""
    global _global_session_service
    if _global_session_service is None:
        _global_session_service = SessionService()
    return _global_session_service


def reset_global_session_service() -> None:
    """Reset the global SessionService instance."""
    global _global_session_service
    if _global_session_service is not None:
        try:
            _global_session_service.close()
        except Exception:
            pass
        _global_session_service = None
