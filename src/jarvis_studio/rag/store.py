"""SQLite + sqlite-vec vector store engine for local RAG knowledge base."""

import json
import sqlite3
import time
from typing import Any, Self

import sqlite_vec

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.persistence.migrations import run_vec_migrations
from jarvis_studio.core.persistence.sqlite import (
    SyncSQLiteSession,
    initialize_sqlite_database,
)
from jarvis_studio.rag.splitter import DocumentChunk


class SqliteVecStore:
    """High-performance local vector store using SQLite and the sqlite-vec extension."""

    def __init__(self, db_path: str | None = None, dimension: int | None = None):
        settings = get_settings()
        resolved_path = db_path or settings.rag.vector_db_path
        resolved_dim = (
            dimension if dimension is not None else settings.rag.embedding_dimension
        )
        if not isinstance(resolved_dim, int) or resolved_dim <= 0:
            raise ValueError(
                f"Embedding dimension must be a positive integer, got {resolved_dim!r}"
            )
        self.dimension = resolved_dim
        self._db = SyncSQLiteSession(
            resolved_path,
            enable_foreign_keys=False,
            enable_sqlite_vec=True,
        )
        self.db_path = self._db.db_path

        if not self._db.is_memory:
            initialize_sqlite_database(
                self.db_path,
                enable_wal=True,
                enable_foreign_keys=False,
                enable_sqlite_vec=True,
            )
            run_vec_migrations(self.db_path)

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
        """Initialize relational tables and sqlite-vec virtual table."""
        if not self._db.is_memory:
            with self._db.read_connection() as conn:
                tbl_cnt = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('doc_files', 'doc_chunks', 'vec_chunks')"
                ).fetchone()[0]
            if tbl_cnt == 3:
                return

        with self._db.transaction() as conn:
            # Metadata table tracking indexed files
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS doc_files (
                    doc_path TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

            # Metadata table for individual document chunks
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS doc_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunk_doc ON doc_chunks(doc_path)"
            )

            # Virtual KNN vector table powered by sqlite-vec
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    embedding float[{self.dimension}]
                )
                """
            )

    def add_chunks(
        self,
        chunks: list[DocumentChunk],
        embeddings: list[list[float]],
        file_hash: str | None = None,
    ) -> int:
        """Insert or replace document chunks and corresponding vectors."""
        if not chunks or len(chunks) != len(embeddings):
            return 0

        doc_path = chunks[0].doc_path
        now = time.time()

        with self._db.transaction() as conn:
            # Remove any existing chunks for this document first
            self._delete_doc_internal(conn, doc_path)

            for chunk, vec in zip(chunks, embeddings):
                # 1. Insert into chunk metadata table
                conn.execute(
                    """
                    INSERT INTO doc_chunks (chunk_id, doc_path, chunk_index, content, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.doc_path,
                        chunk.chunk_index,
                        chunk.content,
                        json.dumps(chunk.metadata),
                        now,
                    ),
                )

                # 2. Insert into sqlite-vec virtual table
                serialized_vec = sqlite_vec.serialize_float32(vec)
                conn.execute(
                    """
                    INSERT INTO vec_chunks (chunk_id, embedding)
                    VALUES (?, ?)
                    """,
                    (chunk.chunk_id, serialized_vec),
                )

            # Record file indexing status
            if file_hash:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO doc_files (doc_path, file_hash, chunk_count, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (doc_path, file_hash, len(chunks), now),
                )

        logger.debug(f"Indexed {len(chunks)} chunk(s) for doc '{doc_path}'")
        return len(chunks)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 4,
        doc_path_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Perform KNN vector similarity search."""
        serialized_query = sqlite_vec.serialize_float32(query_embedding)
        results = []

        with self._db.read_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    v.chunk_id,
                    v.distance,
                    c.doc_path,
                    c.chunk_index,
                    c.content,
                    c.metadata
                FROM vec_chunks v
                JOIN doc_chunks c ON v.chunk_id = c.chunk_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance ASC
                """,
                (serialized_query, top_k * 2 if doc_path_prefix else top_k),
            )
            rows = cursor.fetchall()

            for r in rows:
                doc_path = r["doc_path"]
                if doc_path_prefix and not doc_path.startswith(doc_path_prefix):
                    continue

                meta = {}
                if r["metadata"]:
                    try:
                        meta = json.loads(r["metadata"])
                    except Exception:
                        meta = {}

                # Convert distance to similarity score: score = 1 / (1 + distance)
                dist = float(r["distance"])
                score = round(1.0 / (1.0 + max(0.0, dist)), 4)

                results.append(
                    {
                        "chunk_id": r["chunk_id"],
                        "doc_path": doc_path,
                        "chunk_index": r["chunk_index"],
                        "content": r["content"],
                        "metadata": meta,
                        "distance": dist,
                        "score": score,
                    }
                )

                if len(results) >= top_k:
                    break

        return results

    def _delete_doc_internal(self, conn: sqlite3.Connection, doc_path: str) -> None:
        """Internal helper to wipe a document's chunks from both tables."""
        cursor = conn.execute(
            "SELECT chunk_id FROM doc_chunks WHERE doc_path = ?", (doc_path,)
        )
        chunk_ids = [r["chunk_id"] for r in cursor.fetchall()]

        for cid in chunk_ids:
            conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (cid,))

        conn.execute("DELETE FROM doc_chunks WHERE doc_path = ?", (doc_path,))
        conn.execute("DELETE FROM doc_files WHERE doc_path = ?", (doc_path,))

    def delete_document(self, doc_path: str) -> bool:
        """Remove an entire document and its vectors from the store."""
        with self._db.transaction() as conn:
            self._delete_doc_internal(conn, doc_path)
        return True

    def get_file_hash(self, doc_path: str) -> str | None:
        """Check if a file was previously indexed and get its recorded hash."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                "SELECT file_hash FROM doc_files WHERE doc_path = ?", (doc_path,)
            )
            row = cursor.fetchone()
            return row["file_hash"] if row else None

    def list_indexed_files(self) -> list[dict[str, Any]]:
        """List all indexed documents and their chunk counts."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                "SELECT doc_path, file_hash, chunk_count, updated_at FROM doc_files ORDER BY updated_at DESC"
            )
            return [
                {
                    "doc_path": r["doc_path"],
                    "file_hash": r["file_hash"],
                    "chunk_count": r["chunk_count"],
                    "updated_at": r["updated_at"],
                }
                for r in cursor.fetchall()
            ]

    def clear(self) -> None:
        """Purge all vectors and chunks."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM vec_chunks")
            conn.execute("DELETE FROM doc_chunks")
            conn.execute("DELETE FROM doc_files")
