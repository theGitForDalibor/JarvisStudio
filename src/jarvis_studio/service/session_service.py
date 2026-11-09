"""Session and Thread lifecycle management with SQLite persistence."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger


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
        self.db_path = db_path or get_settings().storage.sqlite_path
        self._in_memory_conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._in_memory_conn is None:
                self._in_memory_conn = sqlite3.connect(
                    ":memory:", check_same_thread=False
                )
                self._in_memory_conn.row_factory = sqlite3.Row
            return self._in_memory_conn

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create sessions and messages tables if they do not exist."""
        conn = self._get_connection()
        try:
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
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def create_session(
        self, title: str | None = None, session_id: str | None = None
    ) -> SessionMetadata:
        """Explicitly create a new session."""
        now = time.time()
        sid = session_id or f"sess_{uuid.uuid4().hex[:10]}"
        sess_title = title or "新会话"
        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO chat_sessions (session_id, title, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, sess_title, now, now, json.dumps({})),
            )
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

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
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
                conn.commit()
                existing.updated_at = now
            finally:
                if self.db_path != ":memory:":
                    conn.close()
            return existing

        return self.create_session(title=title, session_id=session_id)

    def get_session(self, session_id: str) -> SessionMetadata | None:
        """Get session metadata and stats by ID."""
        conn = self._get_connection()
        try:
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
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def list_sessions(self) -> list[SessionMetadata]:
        """Return all tracked sessions sorted by most recent."""
        conn = self._get_connection()
        try:
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
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def update_session_title(
        self, session_id: str, title: str
    ) -> SessionMetadata | None:
        """Rename an existing session."""
        clean_title = title.strip()
        if not clean_title:
            return None

        conn = self._get_connection()
        try:
            now = time.time()
            cursor = conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (clean_title, now, session_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
        finally:
            if self.db_path != ":memory:":
                conn.close()

        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and all its messages."""
        conn = self._get_connection()
        try:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor = conn.execute(
                "DELETE FROM chat_sessions WHERE session_id = ?", (session_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            if self.db_path != ":memory:":
                conn.close()

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

        conn = self._get_connection()
        try:
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

            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

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
        conn = self._get_connection()
        try:
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
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def clear_session_messages(self, session_id: str) -> bool:
        """Remove all messages for a session while retaining session metadata."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            if self.db_path != ":memory:":
                conn.close()


_global_session_service: SessionService | None = None


def get_session_service() -> SessionService:
    """Retrieve the singleton SessionService instance."""
    global _global_session_service
    if _global_session_service is None:
        _global_session_service = SessionService()
    return _global_session_service
