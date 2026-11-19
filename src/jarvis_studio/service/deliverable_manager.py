"""DeliverableManager: durable lifecycle, safe filesystem storage, and atomic delivery of task deliverables."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jarvis_studio.config import get_settings
from jarvis_studio.core.persistence.migrations import run_app_migrations
from jarvis_studio.core.persistence.sqlite import (
    initialize_sqlite_database,
    is_memory_db_path,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)
from jarvis_studio.core.tasks.models import DeliverableRecord, DeliverableType
from jarvis_studio.tools.builtin.file_tools import resolve_safe_path

_logger = logging.getLogger(__name__)

# Hard limits for deliverable artifacts
MAX_DELIVERABLE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_PREVIEW_LENGTH = 2000
MAX_SUMMARY_LENGTH = 300


def _sanitize_filename(name: str, default_ext: str = ".txt") -> str:
    """Sanitize user-provided or generated title to a safe local filename."""
    # Replace invalid chars and spaces with underscores
    cleaned = re.sub(r'[\\/*?:"<>|\s]+', "_", name).strip("._")
    if not cleaned:
        cleaned = f"deliverable_{uuid.uuid4().hex[:8]}"
    if "." not in cleaned:
        cleaned += default_ext
    return cleaned


def _infer_extension_and_mime(deliverable_type: DeliverableType | str) -> tuple[str, str]:
    """Map deliverable type to default file extension and MIME type."""
    dtype = deliverable_type.value if isinstance(deliverable_type, DeliverableType) else str(deliverable_type)
    if dtype == DeliverableType.MARKDOWN_REPORT.value:
        return (".md", "text/markdown; charset=utf-8")
    if dtype == DeliverableType.CODE_PATCH.value:
        return (".patch", "text/x-diff; charset=utf-8")
    if dtype == DeliverableType.DATA_TABLE.value:
        return (".csv", "text/csv; charset=utf-8")
    if dtype == DeliverableType.JSON_ARTIFACT.value:
        return (".json", "application/json; charset=utf-8")
    return (".txt", "text/plain; charset=utf-8")


def _extract_summary_and_preview(text: str) -> tuple[str, str]:
    """Extract a short summary and up to 2000 chars preview from text."""
    preview = text[:MAX_PREVIEW_LENGTH]
    # Clean leading whitespace and extract first meaningful paragraph(s)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if paragraphs:
        # If first paragraph is a header (# Title), take second paragraph if available
        first = paragraphs[0]
        if first.startswith("#") and len(paragraphs) > 1:
            summary = paragraphs[1][:MAX_SUMMARY_LENGTH]
        else:
            summary = first[:MAX_SUMMARY_LENGTH]
    else:
        summary = text[:MAX_SUMMARY_LENGTH]
    return summary, preview


def _row_to_deliverable(row: sqlite3.Row) -> DeliverableRecord:
    dtype_str = row["deliverable_type"]
    dtype = DeliverableType(dtype_str) if dtype_str in DeliverableType._value2member_map_ else DeliverableType.FILE
    return DeliverableRecord(
        deliverable_id=row["deliverable_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        title=row["title"],
        deliverable_type=dtype,
        file_path=row["file_path"],
        mime_type=row["mime_type"],
        file_size_bytes=int(row["file_size_bytes"] or 0),
        sha256_hash=row["sha256_hash"],
        summary=row["summary"],
        content_preview=row["content_preview"],
        metadata_json=row["metadata_json"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class DeliverableManager:
    """Manages creation, safe filesystem persistence, querying, and download of deliverables."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        settings = get_settings()
        default_db = getattr(settings.storage, "sqlite_path", None) or getattr(settings.storage, "db_path", "./data/jarvis_studio.db")
        self.db_path = normalize_db_path(db_path or default_db)
        self.workspace_root = Path(workspace_root or settings.storage.workspace_root).resolve()
        self.deliverables_dir = (self.workspace_root / "deliverables").resolve()
        self.deliverables_dir.mkdir(parents=True, exist_ok=True)

        self._is_memory = is_memory_db_path(self.db_path)
        self._in_memory_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._in_memory_conn = open_sync_connection(self.db_path, enable_foreign_keys=True)
            initialize_sqlite_database(self.db_path, in_memory_conn=self._in_memory_conn)
            run_app_migrations(self.db_path, in_memory_conn=self._in_memory_conn)
        else:
            initialize_sqlite_database(self.db_path)
            run_app_migrations(self.db_path)

    @contextmanager
    def _read_conn(self):
        if self._is_memory:
            if self._in_memory_conn is None:
                raise RuntimeError("In-memory DeliverableManager connection is closed")
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
            raise RuntimeError("In-memory DeliverableManager connection is closed")
        with sync_transaction(
            self.db_path,
            in_memory_conn=self._in_memory_conn,
            enable_foreign_keys=True,
        ) as conn:
            yield conn

    def close(self) -> None:
        """Close resources and connections."""
        if self._in_memory_conn is not None:
            self._in_memory_conn.close()
            self._in_memory_conn = None

    def create_deliverable(
        self,
        task_id: str,
        title: str,
        content: str | bytes,
        *,
        run_id: str | None = None,
        run_seq: int | None = None,
        deliverable_type: DeliverableType | str = DeliverableType.MARKDOWN_REPORT,
        file_name: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeliverableRecord:
        """Atomically persist a deliverable file to disk and record metadata in database."""
        if not title or not title.strip():
            raise ValueError("Deliverable title cannot be empty")

        # Convert content to bytes
        if isinstance(content, str):
            raw_bytes = content.encode("utf-8")
            is_text = True
            text_content = content
        elif isinstance(content, (bytes, bytearray)):
            raw_bytes = bytes(content)
            try:
                text_content = raw_bytes.decode("utf-8")
                is_text = True
            except UnicodeDecodeError:
                text_content = ""
                is_text = False
        else:
            raise TypeError(f"Deliverable content must be str or bytes, got {type(content).__name__}")

        # Size check
        size_bytes = len(raw_bytes)
        if size_bytes > MAX_DELIVERABLE_SIZE_BYTES:
            raise ValueError(
                f"Deliverable content size ({size_bytes} bytes) exceeds maximum limit ({MAX_DELIVERABLE_SIZE_BYTES} bytes)"
            )
        if size_bytes == 0:
            raise ValueError("Deliverable content cannot be empty")

        dtype = (
            deliverable_type
            if isinstance(deliverable_type, DeliverableType)
            else DeliverableType(str(deliverable_type))
        )
        def_ext, def_mime = _infer_extension_and_mime(dtype)
        final_mime = mime_type or def_mime

        # Determine run_id and run_seq from DB if missing
        active_run_id = run_id
        active_run_seq = run_seq

        with self._read_conn() as conn:
            # Verify task exists
            task_row = conn.execute(
                "SELECT task_id, current_run_id FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Task not found: {task_id}")

            if active_run_id is None:
                active_run_id = task_row[1]  # current_run_id

            if active_run_id is not None and active_run_seq is None:
                run_row = conn.execute(
                    "SELECT run_seq FROM agent_runs WHERE run_id = ?",
                    (active_run_id,),
                ).fetchone()
                if run_row is not None:
                    active_run_seq = int(run_row[0])

            if active_run_seq is None:
                max_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(run_seq), 1) FROM agent_runs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                active_run_seq = int(max_seq_row[0]) if max_seq_row and max_seq_row[0] else 1

            # If still no run_id, locate or generate one
            if active_run_id is None:
                run_row = conn.execute(
                    "SELECT run_id FROM agent_runs WHERE task_id = ? ORDER BY run_seq DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if run_row is not None:
                    active_run_id = run_row[0]
                else:
                    raise ValueError(f"Cannot create deliverable for task '{task_id}' without an active run")

        # Filename sanitization
        if file_name is None:
            safe_name = _sanitize_filename(title, default_ext=def_ext)
        else:
            safe_name = _sanitize_filename(Path(file_name).name, default_ext=def_ext)

        rel_dir = Path("deliverables") / task_id / f"run_{active_run_seq}"
        rel_path = (rel_dir / safe_name).as_posix()

        # Target physical path resolution with safety validation
        target_abs_path = resolve_safe_path(rel_path, workspace_root=self.workspace_root)
        target_abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write with fsync and cleanup on failure
        tmp_filename = f".tmp_{uuid.uuid4().hex}.tmp"
        tmp_abs_path = target_abs_path.parent / tmp_filename

        try:
            with open(tmp_abs_path, "wb") as f:
                f.write(raw_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_abs_path, target_abs_path)
        except Exception:
            if tmp_abs_path.exists():
                try:
                    tmp_abs_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise

        # Hash and summary
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if is_text:
            summary, preview = _extract_summary_and_preview(text_content)
        else:
            summary = f"Binary deliverable ({dtype.value}, {size_bytes} bytes)"
            preview = f"[Binary data: {size_bytes} bytes, SHA-256: {sha256[:16]}...]"

        now = time.time()
        deliverable_id = f"deliv_{uuid.uuid4().hex[:12]}"
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

        # Insert metadata in database; rollback written file if DB fails
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_deliverables (
                        deliverable_id, task_id, run_id, title, deliverable_type,
                        file_path, mime_type, file_size_bytes, sha256_hash,
                        summary, content_preview, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        deliverable_id,
                        task_id,
                        active_run_id,
                        title,
                        dtype.value,
                        rel_path,
                        final_mime,
                        size_bytes,
                        sha256,
                        summary,
                        preview,
                        meta_json,
                        now,
                        now,
                    ),
                )
        except Exception:
            # DB failed, remove written physical file to avoid orphan inconsistency
            try:
                target_abs_path.unlink(missing_ok=True)
            except Exception as clean_err:
                _logger.warning("Failed to clean up deliverable file %s after DB error: %s", target_abs_path, clean_err)
            raise

        return DeliverableRecord(
            deliverable_id=deliverable_id,
            task_id=task_id,
            run_id=active_run_id,
            title=title,
            deliverable_type=dtype,
            file_path=rel_path,
            mime_type=final_mime,
            file_size_bytes=size_bytes,
            sha256_hash=sha256,
            summary=summary,
            content_preview=preview,
            metadata_json=meta_json,
            created_at=now,
            updated_at=now,
        )

    def get_deliverable(self, deliverable_id: str) -> DeliverableRecord | None:
        """Look up deliverable record by ID."""
        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_deliverables WHERE deliverable_id = ?",
                (deliverable_id,),
            ).fetchone()
            return _row_to_deliverable(row) if row is not None else None

    def list_deliverables_for_task(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
    ) -> list[DeliverableRecord]:
        """List deliverables for a task, optionally filtered by run_id."""
        query = "SELECT * FROM agent_deliverables WHERE task_id = ?"
        params: list[Any] = [task_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at DESC"

        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_deliverable(r) for r in rows]

    def get_deliverable_file_path(self, deliverable_id: str) -> Path:
        """Resolve and strictly validate the physical path of a deliverable within workspace/deliverables.

        Raises:
            KeyError: If deliverable_id does not exist.
            PermissionError: If the path attempts directory traversal or escapes deliverables directory.
            FileNotFoundError: If the physical file does not exist on disk.
        """
        record = self.get_deliverable(deliverable_id)
        if record is None:
            raise KeyError(f"Deliverable not found: {deliverable_id}")
        if not record.file_path:
            raise FileNotFoundError(f"Deliverable {deliverable_id} has no associated file_path")

        # Resolve path safely inside workspace
        resolved = resolve_safe_path(record.file_path, workspace_root=self.workspace_root)

        # Enforce that it is strictly contained within workspace/deliverables
        expected_root = self.deliverables_dir
        if not resolved.is_relative_to(expected_root):
            raise PermissionError(
                f"Access denied: Deliverable file ({resolved}) escapes deliverables root ({expected_root})"
            )

        if not resolved.is_file():
            raise FileNotFoundError(f"Deliverable file missing on disk: {resolved}")

        return resolved

    def read_deliverable_content(self, deliverable_id: str) -> bytes:
        """Read full raw content of a deliverable file."""
        path = self.get_deliverable_file_path(deliverable_id)
        return path.read_bytes()

    def delete_task_deliverables_dir(self, task_id: str) -> None:
        """Physically delete all files and directory under workspace/deliverables/{task_id}."""
        target_dir = (self.deliverables_dir / task_id).resolve()
        if not target_dir.is_relative_to(self.deliverables_dir):
            _logger.warning("Refusing to delete deliverables directory outside root: %s", target_dir)
            return
        if target_dir.name != task_id:
            _logger.warning("Sanity check failed: target dir name %s != task_id %s", target_dir.name, task_id)
            return

        if target_dir.exists() and target_dir.is_dir():
            shutil.rmtree(target_dir, ignore_errors=True)
            _logger.info("Deleted deliverables directory for task %s at %s", task_id, target_dir)


_default_deliverable_manager: DeliverableManager | None = None
_default_deliverable_manager_lock = threading.Lock()


def get_deliverable_manager(
    db_path: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> DeliverableManager:
    """Return process-wide default DeliverableManager singleton."""
    global _default_deliverable_manager
    if _default_deliverable_manager is not None and db_path is None and workspace_root is None:
        return _default_deliverable_manager

    with _default_deliverable_manager_lock:
        if _default_deliverable_manager is not None and db_path is None and workspace_root is None:
            return _default_deliverable_manager
        mgr = DeliverableManager(db_path=db_path, workspace_root=workspace_root)
        if db_path is None and workspace_root is None:
            _default_deliverable_manager = mgr
        return mgr


def reset_global_deliverable_manager() -> None:
    """Close and clear the global DeliverableManager singleton."""
    global _default_deliverable_manager
    with _default_deliverable_manager_lock:
        if _default_deliverable_manager is not None:
            try:
                _default_deliverable_manager.close()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Error closing global DeliverableManager: %s", exc)
            _default_deliverable_manager = None
