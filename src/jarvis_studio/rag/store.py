"""SQLite + sqlite-vec vector store engine for local RAG knowledge base."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import sqlite_vec

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.rag.splitter import DocumentChunk


class SqliteVecStore:
    """High-performance local vector store using SQLite and the sqlite-vec extension."""

    def __init__(self, db_path: str | None = None, dimension: int | None = None):
        settings = get_settings()
        self.db_path = db_path or settings.rag.vector_db_path
        self.dimension = dimension or settings.rag.embedding_dimension
        self._in_memory_conn: sqlite3.Connection | None = None

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create and configure a SQLite connection loaded with sqlite-vec."""
        if self.db_path == ":memory:":
            if self._in_memory_conn is None:
                self._in_memory_conn = sqlite3.connect(
                    ":memory:", check_same_thread=False
                )
                self._in_memory_conn.row_factory = sqlite3.Row
                self._in_memory_conn.enable_load_extension(True)
                sqlite_vec.load(self._in_memory_conn)
            return self._in_memory_conn

        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return conn

    def _init_db(self) -> None:
        """Initialize relational tables and sqlite-vec virtual table."""
        conn = self._get_connection()
        try:
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
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

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
        conn = self._get_connection()

        try:
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

            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

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
        conn = self._get_connection()

        try:
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
        finally:
            if self.db_path != ":memory:":
                conn.close()

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
        conn = self._get_connection()
        try:
            self._delete_doc_internal(conn, doc_path)
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()
        return True

    def get_file_hash(self, doc_path: str) -> str | None:
        """Check if a file was previously indexed and get its recorded hash."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT file_hash FROM doc_files WHERE doc_path = ?", (doc_path,)
            )
            row = cursor.fetchone()
            return row["file_hash"] if row else None
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def list_indexed_files(self) -> list[dict[str, Any]]:
        """List all indexed documents and their chunk counts."""
        conn = self._get_connection()
        try:
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
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def clear(self) -> None:
        """Purge all vectors and chunks."""
        conn = self._get_connection()
        try:
            conn.execute("DELETE FROM vec_chunks")
            conn.execute("DELETE FROM doc_chunks")
            conn.execute("DELETE FROM doc_files")
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()
