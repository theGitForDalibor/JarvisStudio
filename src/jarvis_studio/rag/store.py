"""SQLite + sqlite-vec + FTS5 hybrid vector store engine for local RAG knowledge base."""

from __future__ import annotations

import hashlib
import json
import re
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


def _make_doc_id(doc_path: str) -> str:
    return f"doc_{hashlib.sha256(doc_path.encode('utf-8')).hexdigest()[:12]}"


def _tokenize_fts_query(query_text: str) -> str:
    """Build a safe FTS5 OR query from alphanumeric/CJK tokens."""
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query_text.lower())
    clean_tokens = [t for t in tokens if len(t) >= 2 or "\u4e00" <= t <= "\u9fff"]
    if not clean_tokens:
        clean_tokens = tokens
    if not clean_tokens:
        return ""
    # Quote each token to avoid FTS5 syntax errors on operators like AND/OR/NOT
    quoted = [f'"{tok}"' for tok in clean_tokens[:12]]
    return " OR ".join(quoted)


class SqliteVecStore:
    """Local hybrid vector + FTS5 store using SQLite and sqlite-vec."""

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
        self.embedding_model = settings.rag.embedding_model
        self._fts5_enabled = True
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
        """Initialize/upgrade relational tables, FTS5 index, and sqlite-vec virtual table."""
        with self._db.transaction() as conn:
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
            # Idempotent column upgrades for Sprint 3A Document entity metadata
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(doc_files)").fetchall()
            }
            extra_cols = {
                "doc_id": "TEXT",
                "title": "TEXT",
                "file_type": "TEXT",
                "status": "TEXT NOT NULL DEFAULT 'READY'",
                "error_message": "TEXT",
                "collection": "TEXT NOT NULL DEFAULT 'default'",
                "file_size_bytes": "INTEGER NOT NULL DEFAULT 0",
                "embedding_model": "TEXT",
                "embedding_dim": "INTEGER",
            }
            for col_name, col_def in extra_cols.items():
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE doc_files ADD COLUMN {col_name} {col_def}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_doc_files_id ON doc_files(doc_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_doc_files_collection ON doc_files(collection)"
            )

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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            # Check existing dimension if vec_chunks already exists
            vec_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
            ).fetchone()
            if vec_exists:
                dim_row = conn.execute(
                    "SELECT value FROM rag_meta WHERE key = 'embedding_dim'"
                ).fetchone()
                if dim_row is not None:
                    recorded_dim = int(dim_row[0])
                    if recorded_dim != self.dimension:
                        cnt = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
                        if cnt > 0:
                            raise ValueError(
                                f"Vector store dimension mismatch: existing index uses {recorded_dim}D "
                                f"with {cnt} chunk(s), but configured dimension is {self.dimension}D. "
                                "Clear/rebuild the knowledge base or align embedding_dimension."
                            )
                        # Empty vec_chunks -> safe to recreate with new dimension
                        conn.execute("DROP TABLE vec_chunks")
                        vec_exists = None

            if not vec_exists:
                conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                        chunk_id TEXT PRIMARY KEY,
                        embedding float[{self.dimension}]
                    )
                    """
                )

            conn.execute(
                "INSERT OR REPLACE INTO rag_meta (key, value) VALUES ('embedding_dim', ?)",
                (str(self.dimension),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO rag_meta (key, value) VALUES ('embedding_model', ?)",
                (str(self.embedding_model),),
            )

            # Initialize FTS5 virtual table for hybrid keyword + vector retrieval
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                        chunk_id UNINDEXED,
                        doc_path UNINDEXED,
                        content,
                        tokenize='unicode61'
                    )
                    """
                )
            except sqlite3.OperationalError as fts_err:
                self._fts5_enabled = False
                logger.warning(f"FTS5 unavailable; hybrid search will use vector-only ({fts_err})")

    def record_document_status(
        self,
        doc_path: str,
        *,
        status: str,
        file_hash: str = "",
        title: str | None = None,
        file_type: str | None = None,
        collection: str = "default",
        file_size_bytes: int = 0,
        chunk_count: int = 0,
        error_message: str | None = None,
        doc_id: str | None = None,
    ) -> str:
        """Upsert document ingestion lifecycle status (INDEXING / READY / FAILED)."""
        resolved_id = doc_id or _make_doc_id(doc_path)
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO doc_files (
                    doc_path, doc_id, title, file_type, file_hash, chunk_count,
                    status, error_message, collection, file_size_bytes,
                    embedding_model, embedding_dim, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_path) DO UPDATE SET
                    doc_id = COALESCE(excluded.doc_id, doc_files.doc_id),
                    title = COALESCE(excluded.title, doc_files.title),
                    file_type = COALESCE(excluded.file_type, doc_files.file_type),
                    file_hash = CASE WHEN excluded.file_hash != '' THEN excluded.file_hash ELSE doc_files.file_hash END,
                    chunk_count = excluded.chunk_count,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    collection = excluded.collection,
                    file_size_bytes = CASE WHEN excluded.file_size_bytes > 0 THEN excluded.file_size_bytes ELSE doc_files.file_size_bytes END,
                    embedding_model = excluded.embedding_model,
                    embedding_dim = excluded.embedding_dim,
                    updated_at = excluded.updated_at
                """,
                (
                    doc_path,
                    resolved_id,
                    title or doc_path.rsplit("/", 1)[-1],
                    file_type,
                    file_hash,
                    chunk_count,
                    status,
                    error_message,
                    collection,
                    file_size_bytes,
                    self.embedding_model,
                    self.dimension,
                    now,
                ),
            )
        return resolved_id

    def add_chunks(
        self,
        chunks: list[DocumentChunk],
        embeddings: list[list[float]],
        file_hash: str | None = None,
        *,
        title: str | None = None,
        file_type: str | None = None,
        collection: str = "default",
        file_size_bytes: int = 0,
        doc_id: str | None = None,
    ) -> int:
        """Insert or replace document chunks, FTS5 entries, and corresponding vectors."""
        if not chunks or len(chunks) != len(embeddings):
            return 0

        for vec in embeddings:
            if len(vec) != self.dimension:
                raise ValueError(
                    f"Embedding vector dimension {len(vec)} does not match store dimension {self.dimension}"
                )

        doc_path = chunks[0].doc_path
        resolved_id = doc_id or _make_doc_id(doc_path)
        now = time.time()

        with self._db.transaction() as conn:
            self._delete_doc_internal(conn, doc_path)

            for chunk, vec in zip(chunks, embeddings):
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
                        json.dumps(chunk.metadata, ensure_ascii=False),
                        now,
                    ),
                )

                serialized_vec = sqlite_vec.serialize_float32(vec)
                conn.execute(
                    """
                    INSERT INTO vec_chunks (chunk_id, embedding)
                    VALUES (?, ?)
                    """,
                    (chunk.chunk_id, serialized_vec),
                )

                if self._fts5_enabled:
                    conn.execute(
                        "INSERT INTO fts_chunks (chunk_id, doc_path, content) VALUES (?, ?, ?)",
                        (chunk.chunk_id, chunk.doc_path, chunk.content),
                    )

            conn.execute(
                """
                INSERT OR REPLACE INTO doc_files (
                    doc_path, doc_id, title, file_type, file_hash, chunk_count,
                    status, error_message, collection, file_size_bytes,
                    embedding_model, embedding_dim, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'READY', NULL, ?, ?, ?, ?, ?)
                """,
                (
                    doc_path,
                    resolved_id,
                    title or doc_path.rsplit("/", 1)[-1],
                    file_type,
                    file_hash or "",
                    len(chunks),
                    collection,
                    file_size_bytes,
                    self.embedding_model,
                    self.dimension,
                    now,
                ),
            )

        logger.debug(f"Indexed {len(chunks)} chunk(s) for doc '{doc_path}'")
        return len(chunks)

    def _matches_scope(
        self,
        row: sqlite3.Row,
        *,
        doc_path_prefix: str | None,
        collection: str | None,
        doc_id: str | None,
    ) -> bool:
        doc_path = row["doc_path"]
        if doc_path_prefix and not (
            doc_path.startswith(doc_path_prefix) or doc_path_prefix in doc_path
        ):
            return False
        if collection and (row["collection"] or "default") != collection:
            return False
        if doc_id and row["doc_id"] != doc_id:
            return False
        return True

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 4,
        doc_path_prefix: str | None = None,
        *,
        query_text: str | None = None,
        collection: str | None = None,
        doc_id: str | None = None,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Perform hybrid vector (sqlite-vec) + keyword (FTS5) similarity search."""
        serialized_query = sqlite_vec.serialize_float32(query_embedding)
        has_scope_filter = bool(doc_path_prefix or collection or doc_id)
        candidate_k = max(top_k * 6, 30) if has_scope_filter else max(top_k * 2, 10)

        candidates: dict[str, dict[str, Any]] = {}

        with self._db.read_connection() as conn:
            # 1. Vector KNN retrieval via sqlite-vec
            vec_rows = conn.execute(
                """
                SELECT
                    v.chunk_id,
                    v.distance,
                    c.doc_path,
                    c.chunk_index,
                    c.content,
                    c.metadata,
                    f.doc_id,
                    f.title,
                    f.collection
                FROM vec_chunks v
                JOIN doc_chunks c ON v.chunk_id = c.chunk_id
                LEFT JOIN doc_files f ON c.doc_path = f.doc_path
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance ASC
                """,
                (serialized_query, candidate_k),
            ).fetchall()

            for vec_rank, r in enumerate(vec_rows, start=1):
                if not self._matches_scope(
                    r, doc_path_prefix=doc_path_prefix, collection=collection, doc_id=doc_id
                ):
                    continue
                dist = float(r["distance"])
                vec_score = 1.0 / (1.0 + max(0.0, dist))
                meta = {}
                if r["metadata"]:
                    try:
                        meta = json.loads(r["metadata"])
                    except Exception:
                        meta = {}
                candidates[r["chunk_id"]] = {
                    "chunk_id": r["chunk_id"],
                    "doc_id": r["doc_id"] or _make_doc_id(r["doc_path"]),
                    "title": r["title"] or r["doc_path"].rsplit("/", 1)[-1],
                    "collection": r["collection"] or "default",
                    "doc_path": r["doc_path"],
                    "chunk_index": r["chunk_index"],
                    "content": r["content"],
                    "metadata": meta,
                    "distance": dist,
                    "vec_score": vec_score,
                    "vec_rank": vec_rank,
                    "fts_rank": None,
                }

            # 2. FTS5 keyword retrieval (BM25) when query_text is supplied
            fts_query = _tokenize_fts_query(query_text) if (query_text and self._fts5_enabled) else ""
            if fts_query:
                try:
                    fts_rows = conn.execute(
                        """
                        SELECT
                            fts.chunk_id,
                            bm25(fts_chunks) AS bm25_score,
                            c.doc_path,
                            c.chunk_index,
                            c.content,
                            c.metadata,
                            f.doc_id,
                            f.title,
                            f.collection
                        FROM fts_chunks fts
                        JOIN doc_chunks c ON fts.chunk_id = c.chunk_id
                        LEFT JOIN doc_files f ON c.doc_path = f.doc_path
                        WHERE fts_chunks MATCH ?
                        ORDER BY bm25_score ASC
                        LIMIT ?
                        """,
                        (fts_query, candidate_k),
                    ).fetchall()

                    for fts_rank, r in enumerate(fts_rows, start=1):
                        if not self._matches_scope(
                            r, doc_path_prefix=doc_path_prefix, collection=collection, doc_id=doc_id
                        ):
                            continue
                        cid = r["chunk_id"]
                        if cid in candidates:
                            candidates[cid]["fts_rank"] = fts_rank
                        else:
                            meta = {}
                            if r["metadata"]:
                                try:
                                    meta = json.loads(r["metadata"])
                                except Exception:
                                    meta = {}
                            candidates[cid] = {
                                "chunk_id": cid,
                                "doc_id": r["doc_id"] or _make_doc_id(r["doc_path"]),
                                "title": r["title"] or r["doc_path"].rsplit("/", 1)[-1],
                                "collection": r["collection"] or "default",
                                "doc_path": r["doc_path"],
                                "chunk_index": r["chunk_index"],
                                "content": r["content"],
                                "metadata": meta,
                                "distance": 1.0,
                                "vec_score": 0.45,
                                "vec_rank": None,
                                "fts_rank": fts_rank,
                            }
                except sqlite3.OperationalError as fts_err:
                    logger.debug(f"FTS5 search skipped: {fts_err}")

        # 3. Combine scores (Vector similarity + FTS5 RRF boost)
        ranked: list[dict[str, Any]] = []
        for item in candidates.values():
            base_score = float(item["vec_score"])
            rrf_boost = 0.0
            if item["fts_rank"] is not None:
                # Boost exact/keyword hits cleanly while keeping score in (0, 1]
                rrf_boost = 0.25 / (1.0 + 0.2 * (item["fts_rank"] - 1))
            final_score = round(min(1.0, base_score + rrf_boost), 4)
            if final_score < min_score:
                continue
            ranked.append(
                {
                    "chunk_id": item["chunk_id"],
                    "doc_id": item["doc_id"],
                    "title": item["title"],
                    "collection": item["collection"],
                    "doc_path": item["doc_path"],
                    "chunk_index": item["chunk_index"],
                    "content": item["content"],
                    "metadata": item["metadata"],
                    "distance": item["distance"],
                    "score": final_score,
                }
            )

        ranked.sort(key=lambda x: (-x["score"], x["distance"]))
        return ranked[:top_k]

    def _delete_doc_internal(self, conn: sqlite3.Connection, doc_path: str) -> None:
        """Internal helper to wipe a document's chunks from doc_chunks, vec_chunks, fts_chunks, and doc_files."""
        cursor = conn.execute(
            "SELECT chunk_id FROM doc_chunks WHERE doc_path = ?", (doc_path,)
        )
        chunk_ids = [r["chunk_id"] for r in cursor.fetchall()]

        for cid in chunk_ids:
            conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (cid,))
            if self._fts5_enabled:
                conn.execute("DELETE FROM fts_chunks WHERE chunk_id = ?", (cid,))

        conn.execute("DELETE FROM doc_chunks WHERE doc_path = ?", (doc_path,))
        conn.execute("DELETE FROM doc_files WHERE doc_path = ?", (doc_path,))

    def delete_document(self, doc_path: str) -> bool:
        """Remove an entire document and its vectors/FTS entries from the store."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM doc_files WHERE doc_path = ?", (doc_path,)
            ).fetchone()
            self._delete_doc_internal(conn, doc_path)
            return row is not None

    def delete_document_by_id(self, doc_id: str) -> bool:
        """Remove a document by its doc_id."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT doc_path FROM doc_files WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return False
            self._delete_doc_internal(conn, row["doc_path"])
            return True

    def get_document(self, doc_id_or_path: str) -> dict[str, Any] | None:
        """Fetch a single document record by doc_id or doc_path."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                """
                SELECT doc_path, doc_id, title, file_type, file_hash, chunk_count,
                       status, error_message, collection, file_size_bytes,
                       embedding_model, embedding_dim, updated_at
                FROM doc_files
                WHERE doc_id = ? OR doc_path = ?
                LIMIT 1
                """,
                (doc_id_or_path, doc_id_or_path),
            ).fetchone()
            if row is None:
                return None
            return {
                "doc_id": row["doc_id"] or _make_doc_id(row["doc_path"]),
                "doc_path": row["doc_path"],
                "title": row["title"] or row["doc_path"].rsplit("/", 1)[-1],
                "file_type": row["file_type"],
                "file_hash": row["file_hash"],
                "chunk_count": row["chunk_count"],
                "status": row["status"],
                "error_message": row["error_message"],
                "collection": row["collection"],
                "file_size_bytes": row["file_size_bytes"],
                "embedding_model": row["embedding_model"],
                "embedding_dim": row["embedding_dim"],
                "updated_at": row["updated_at"],
            }

    def get_file_hash(self, doc_path: str) -> str | None:
        """Check if a file was previously indexed and get its recorded hash."""
        with self._db.read_connection() as conn:
            cursor = conn.execute(
                "SELECT file_hash, status FROM doc_files WHERE doc_path = ?", (doc_path,)
            )
            row = cursor.fetchone()
            if row and row["status"] == "READY":
                return row["file_hash"]
            return None

    def list_indexed_files(self, *, collection: str | None = None) -> list[dict[str, Any]]:
        """List all indexed documents and their lifecycle metadata."""
        query = (
            "SELECT doc_path, doc_id, title, file_type, file_hash, chunk_count, "
            "status, error_message, collection, file_size_bytes, updated_at "
            "FROM doc_files"
        )
        params: tuple[Any, ...] = ()
        if collection:
            query += " WHERE collection = ?"
            params = (collection,)
        query += " ORDER BY updated_at DESC"

        with self._db.read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "doc_id": r["doc_id"] or _make_doc_id(r["doc_path"]),
                    "doc_path": r["doc_path"],
                    "title": r["title"] or r["doc_path"].rsplit("/", 1)[-1],
                    "file_type": r["file_type"],
                    "file_hash": r["file_hash"],
                    "chunk_count": r["chunk_count"],
                    "status": r["status"],
                    "error_message": r["error_message"],
                    "collection": r["collection"],
                    "file_size_bytes": r["file_size_bytes"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def clear(self) -> None:
        """Purge all vectors, FTS entries, and chunks."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM vec_chunks")
            if self._fts5_enabled:
                conn.execute("DELETE FROM fts_chunks")
            conn.execute("DELETE FROM doc_chunks")
            conn.execute("DELETE FROM doc_files")
