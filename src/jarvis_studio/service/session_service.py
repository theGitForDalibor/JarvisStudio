"""Session and Thread lifecycle management with SQLite persistence."""

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


class ChatMessageRecord(BaseModel):
    """Stored chat message in a session."""

    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    session_id: str
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_calls: list[dict[str, Any]] | None = None
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
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id, created_at)"
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
                INSERT OR REPLACE INTO chat_sessions (session_id, title, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?)
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
                try:
                    meta = json.loads(row["metadata"])
                except Exception:
                    pass

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
                    try:
                        meta = json.loads(row["metadata"])
                    except Exception:
                        pass
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

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and all its messages."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor = conn.execute(
                "DELETE FROM chat_sessions WHERE session_id = ?", (session_id,)
            )
            return cursor.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> ChatMessageRecord:
        """Append a message to the session history, auto-updating title if first message."""
        self.get_or_create_session(session_id)
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = time.time()
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (id, session_id, role, content, tool_calls, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (msg_id, session_id, role, content, tool_calls_json, now),
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
            role=role,
            content=content,
            tool_calls=tool_calls,
            created_at=now,
        )

    def get_messages(
        self, session_id: str, limit: int = 200
    ) -> list[ChatMessageRecord]:
        """Fetch chronological message history for a session."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, session_id, role, content, tool_calls, created_at
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
                    except Exception:
                        pass
                messages.append(
                    ChatMessageRecord(
                        id=row["id"],
                        session_id=row["session_id"],
                        role=row["role"],
                        content=row["content"],
                        tool_calls=tc,
                        created_at=row["created_at"],
                    )
                )
            return messages

    def clear_session_messages(self, session_id: str) -> bool:
        """Remove all messages for a session while retaining session metadata."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            return cursor.rowcount > 0


_global_session_service: SessionService | None = None


def get_session_service() -> SessionService:
    """Retrieve the singleton SessionService instance."""
    global _global_session_service
    if _global_session_service is None:
        _global_session_service = SessionService()
    return _global_session_service
