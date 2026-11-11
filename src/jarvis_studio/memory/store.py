"""SQLite persistent store for Long-Term Memory."""

import builtins
import sqlite3
import time
from typing import Self

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    SyncSQLiteSession,
    initialize_sqlite_database,
)
from jarvis_studio.memory.models import MemoryCategory, MemoryItem


class MemoryStore:
    """Persistent storage engine for Long-Term Memory records using SQLite."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            settings = get_settings()
            db_path = settings.storage.sqlite_path

        self._db = SyncSQLiteSession(db_path, enable_foreign_keys=True)
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
        """Create the memory table if it does not already exist."""
        if not self._db.is_memory:
            with self._db.read_connection() as conn:
                has_table = (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ltm_memories'"
                    ).fetchone()
                    is not None
                )
            if has_table:
                return

        with self._db.transaction() as conn:
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

    def add(self, memory: MemoryItem) -> MemoryItem:
        """Insert a new memory record."""
        now = time.time()
        memory.created_at = now
        memory.updated_at = now
        with self._db.transaction() as conn:
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

        logger.debug(f"Saved memory [{memory.category.value}] id={memory.id}")
        return memory

    def get(self, memory_id: str) -> MemoryItem | None:
        """Fetch a memory record by ID."""
        with self._db.read_connection() as conn:
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

    def update(self, memory: MemoryItem) -> MemoryItem | None:
        """Update an existing memory record."""
        memory.updated_at = time.time()
        with self._db.transaction() as conn:
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
            if cursor.rowcount == 0:
                return None

        return memory

    def delete(self, memory_id: str) -> bool:
        """Delete a memory record by ID."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM ltm_memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    def list(
        self,
        category: MemoryCategory | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """List stored memories, optionally filtered by category."""
        with self._db.read_connection() as conn:
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

    def search(
        self,
        query: str,
        category: MemoryCategory | None = None,
        limit: int = 10,
    ) -> builtins.list[MemoryItem]:
        """Simple keyword matching search across memory content."""
        pattern = f"%{query}%"
        with self._db.read_connection() as conn:
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

    def clear(self) -> None:
        """Clear all memories (primarily used for test cleanup)."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM ltm_memories")
