"""SQLite persistent store for Long-Term Memory."""

import builtins
import sqlite3
import time
from pathlib import Path

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.memory.models import MemoryCategory, MemoryItem


class MemoryStore:
    """Persistent storage engine for Long-Term Memory records using SQLite."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            settings = get_settings()
            db_path = settings.storage.sqlite_path

        self.db_path = db_path
        self._in_memory_conn: sqlite3.Connection | None = None

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._in_memory_conn is None:
                self._in_memory_conn = sqlite3.connect(
                    ":memory:", check_same_thread=False
                )
                self._in_memory_conn.row_factory = sqlite3.Row
            return self._in_memory_conn

        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the memory table if it does not already exist."""
        conn = self._get_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ltm_memories (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                importance REAL DEFAULT 1.0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_category ON ltm_memories(category)"
        )
        conn.commit()
        if self.db_path != ":memory:":
            conn.close()

    def add(self, memory: MemoryItem) -> MemoryItem:
        """Insert a new memory record."""
        now = time.time()
        memory.created_at = now
        memory.updated_at = now
        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO ltm_memories (id, category, content, source, created_at, updated_at, importance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory.id,
                    memory.category.value
                    if isinstance(memory.category, MemoryCategory)
                    else memory.category,
                    memory.content,
                    memory.source,
                    memory.created_at,
                    memory.updated_at,
                    memory.importance,
                ),
            )
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

        logger.debug(f"Saved memory [{memory.category.value}] id={memory.id}")
        return memory

    def get(self, memory_id: str) -> MemoryItem | None:
        """Fetch a memory record by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM ltm_memories WHERE id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return MemoryItem(
                id=row["id"],
                category=MemoryCategory(row["category"]),
                content=row["content"],
                source=row["source"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                importance=row["importance"],
            )
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def update(self, memory: MemoryItem) -> MemoryItem | None:
        """Update an existing memory record."""
        memory.updated_at = time.time()
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                UPDATE ltm_memories
                SET category = ?, content = ?, source = ?, updated_at = ?, importance = ?
                WHERE id = ?
                """,
                (
                    memory.category.value
                    if isinstance(memory.category, MemoryCategory)
                    else memory.category,
                    memory.content,
                    memory.source,
                    memory.updated_at,
                    memory.importance,
                    memory.id,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
        finally:
            if self.db_path != ":memory:":
                conn.close()

        return memory

    def delete(self, memory_id: str) -> bool:
        """Delete a memory record by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("DELETE FROM ltm_memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def list(
        self,
        category: MemoryCategory | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """List stored memories, optionally filtered by category."""
        conn = self._get_connection()
        try:
            if category:
                cat_val = (
                    category.value if isinstance(category, MemoryCategory) else category
                )
                cursor = conn.execute(
                    "SELECT * FROM ltm_memories WHERE category = ? ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (cat_val, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM ltm_memories ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
            return [
                MemoryItem(
                    id=r["id"],
                    category=MemoryCategory(r["category"]),
                    content=r["content"],
                    source=r["source"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    importance=r["importance"],
                )
                for r in rows
            ]
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def search(
        self,
        query: str,
        category: MemoryCategory | None = None,
        limit: int = 10,
    ) -> builtins.list[MemoryItem]:
        """Simple keyword matching search across memory content."""
        pattern = f"%{query}%"
        conn = self._get_connection()
        try:
            if category:
                cat_val = (
                    category.value if isinstance(category, MemoryCategory) else category
                )
                cursor = conn.execute(
                    """
                    SELECT * FROM ltm_memories
                    WHERE category = ? AND content LIKE ?
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (cat_val, pattern, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM ltm_memories
                    WHERE content LIKE ?
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (pattern, limit),
                )
            rows = cursor.fetchall()
            return [
                MemoryItem(
                    id=r["id"],
                    category=MemoryCategory(r["category"]),
                    content=r["content"],
                    source=r["source"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    importance=r["importance"],
                )
                for r in rows
            ]
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def clear(self) -> None:
        """Clear all memories (primarily used for test cleanup)."""
        conn = self._get_connection()
        try:
            conn.execute("DELETE FROM ltm_memories")
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()
