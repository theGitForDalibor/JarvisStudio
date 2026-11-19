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
from dataclasses import dataclass
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

_DELIVERABLE_WRITE_LOCKS_GUARD = threading.Lock()
_DELIVERABLE_WRITE_LOCKS: dict[str, threading.Lock] = {}


def _get_task_write_lock(task_id: str) -> threading.Lock:
    """Return a per-task lock serializing deliverable path allocation and persistence."""
    with _DELIVERABLE_WRITE_LOCKS_GUARD:
        lock = _DELIVERABLE_WRITE_LOCKS.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _DELIVERABLE_WRITE_LOCKS[task_id] = lock
        return lock


def _compute_file_sha256(path: Path) -> str:
    """Compute hex SHA-256 digest of a file using bounded memory chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class DeliverableIntegrityReport:
    """Diagnostic and compensation summary returned by `DeliverableManager.reconcile_storage`."""

    checked_records: int = 0
    healthy_records: int = 0
    missing_files: tuple[str, ...] = ()
    corrupted_hashes: tuple[str, ...] = ()
    orphan_files: tuple[str, ...] = ()
    stale_temp_files: tuple[str, ...] = ()
    cleaned_temp_files: tuple[str, ...] = ()
    cleaned_orphan_files: tuple[str, ...] = ()


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

        # Determine run_id and run_seq from DB and validate run ownership
        active_run_id = run_id
        active_run_seq = run_seq

        with self._read_conn() as conn:
            task_row = conn.execute(
                "SELECT task_id, current_run_id FROM agent_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Task not found: {task_id}")

            if active_run_id is None:
                active_run_id = task_row[1]  # current_run_id

            if active_run_id is not None:
                run_row = conn.execute(
                    "SELECT run_seq, task_id FROM agent_runs WHERE run_id = ?",
                    (active_run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError(f"Run '{active_run_id}' does not exist for task '{task_id}'")
                if run_row[1] != task_id:
                    raise ValueError(f"Run '{active_run_id}' does not belong to task '{task_id}'")
                if active_run_seq is None:
                    active_run_seq = int(run_row[0])

            if active_run_id is None:
                run_row = conn.execute(
                    "SELECT run_id, run_seq FROM agent_runs WHERE task_id = ? ORDER BY run_seq DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if run_row is not None:
                    active_run_id = run_row[0]
                    if active_run_seq is None:
                        active_run_seq = int(run_row[1])
                else:
                    raise ValueError(f"Cannot create deliverable for task '{task_id}' without an active run")

            if active_run_seq is None:
                max_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(run_seq), 1) FROM agent_runs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                active_run_seq = int(max_seq_row[0]) if max_seq_row and max_seq_row[0] else 1

        # Filename sanitization
        if file_name is None:
            safe_name = _sanitize_filename(title, default_ext=def_ext)
        else:
            safe_name = _sanitize_filename(Path(file_name).name, default_ext=def_ext)

        deliverable_id = f"deliv_{uuid.uuid4().hex[:12]}"
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if is_text:
            summary, preview = _extract_summary_and_preview(text_content)
        else:
            summary = f"Binary deliverable ({dtype.value}, {size_bytes} bytes)"
            preview = f"[Binary data: {size_bytes} bytes, SHA-256: {sha256[:16]}...]"

        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        rel_dir = Path("deliverables") / task_id / f"run_{active_run_seq}"

        with _get_task_write_lock(task_id):
            rel_path = (rel_dir / safe_name).as_posix()
            target_abs_path = resolve_safe_path(rel_path, workspace_root=self.workspace_root)

            # Avoid silent overwrite of an already-committed deliverable in the same run/directory
            with self._read_conn() as conn:
                db_conflict = conn.execute(
                    "SELECT 1 FROM agent_deliverables WHERE task_id = ? AND file_path = ? LIMIT 1",
                    (task_id, rel_path),
                ).fetchone()
            if target_abs_path.exists() or db_conflict is not None:
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                disambiguated = f"{stem}_{deliverable_id[-6:]}{suffix}"
                rel_path = (rel_dir / disambiguated).as_posix()
                target_abs_path = resolve_safe_path(rel_path, workspace_root=self.workspace_root)

            target_abs_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write with fsync and cleanup on failure
            tmp_filename = f".tmp_{deliverable_id}_{uuid.uuid4().hex[:8]}.tmp"
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

            now = time.time()

            # Insert metadata in database; rollback ONLY this newly written file if DB fails
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
                try:
                    target_abs_path.unlink(missing_ok=True)
                except Exception as clean_err:
                    _logger.warning(
                        "Failed to clean up deliverable file %s after DB error: %s",
                        target_abs_path,
                        clean_err,
                    )
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

    def _validate_task_dir(self, task_id: str) -> Path:
        """Validate task_id contains no path traversal and resolves inside deliverables_dir."""
        if not task_id or "/" in task_id or "\\" in task_id or task_id in (".", ".."):
            raise ValueError(f"Invalid task_id for deliverable directory operation: {task_id!r}")
        target_dir = (self.deliverables_dir / task_id).resolve()
        if not target_dir.is_relative_to(self.deliverables_dir) or target_dir.name != task_id:
            raise PermissionError(f"Refusing deliverable directory operation outside root: {target_dir}")
        return target_dir

    def stage_task_deliverables_dir(self, task_id: str) -> Path | None:
        """Atomically move workspace/deliverables/{task_id} to a hidden staging directory before DB purge."""
        target_dir = self._validate_task_dir(task_id)
        if not target_dir.exists():
            return None
        staged_dir = (self.deliverables_dir / f".purging_{task_id}_{uuid.uuid4().hex[:8]}").resolve()
        if not staged_dir.is_relative_to(self.deliverables_dir):
            raise PermissionError(f"Refusing staging directory outside root: {staged_dir}")
        os.replace(target_dir, staged_dir)
        return staged_dir

    def restore_staged_dir(self, staged_dir: Path, task_id: str) -> None:
        """Restore a staged deliverables directory back to its canonical location on rollback."""
        target_dir = self._validate_task_dir(task_id)
        resolved_staged = staged_dir.resolve()
        if (
            not resolved_staged.is_relative_to(self.deliverables_dir)
            or not resolved_staged.name.startswith(".purging_")
        ):
            raise PermissionError(f"Invalid staged directory for restore: {staged_dir}")
        if resolved_staged.exists() and not target_dir.exists():
            os.replace(resolved_staged, target_dir)

    def remove_staged_dir(self, staged_dir: Path) -> None:
        """Strictly remove a previously staged deliverables directory (raises on I/O errors)."""
        resolved_staged = staged_dir.resolve()
        if (
            not resolved_staged.is_relative_to(self.deliverables_dir)
            or not resolved_staged.name.startswith(".purging_")
        ):
            raise PermissionError(f"Refusing removal of invalid staged directory: {staged_dir}")
        if resolved_staged.exists() and resolved_staged.is_dir():
            shutil.rmtree(resolved_staged)
            _logger.info("Purged staged deliverables directory %s", resolved_staged)

    def delete_task_deliverables_dir(self, task_id: str) -> None:
        """Strictly delete all files and directory under workspace/deliverables/{task_id}."""
        target_dir = self._validate_task_dir(task_id)
        if target_dir.exists() and target_dir.is_dir():
            shutil.rmtree(target_dir)
            _logger.info("Deleted deliverables directory for task %s at %s", task_id, target_dir)

    def verify_deliverable(self, deliverable_id: str) -> tuple[bool, str | None]:
        """Verify DB metadata matches disk file presence, containment, and SHA-256 digest."""
        record = self.get_deliverable(deliverable_id)
        if record is None:
            return (False, "not_found")
        try:
            resolved = self.get_deliverable_file_path(deliverable_id)
        except (PermissionError, ValueError) as exc:
            return (False, f"unsafe_path: {exc}")
        except FileNotFoundError:
            return (False, "file_missing")

        if record.sha256_hash:
            actual_hash = _compute_file_sha256(resolved)
            if actual_hash != record.sha256_hash:
                return (False, "hash_mismatch")
        return (True, None)

    def reconcile_storage(
        self,
        *,
        task_id: str | None = None,
        cleanup_temp: bool = True,
        cleanup_orphans: bool = False,
        min_temp_age_seconds: float = 0.0,
    ) -> DeliverableIntegrityReport:
        """Inspect and optionally compensate DB/disk inconsistencies under workspace/deliverables.

        - Detects missing physical files (`missing_files`) and SHA-256 drift (`corrupted_hashes`).
        - Detects stale `.tmp_*.tmp` files and `.purging_*` crash staging dirs (`stale_temp_files`),
          cleaning them up when `cleanup_temp=True` and age >= `min_temp_age_seconds`.
        - Detects disk files with no matching DB record (`orphan_files`), deleting only when
          `cleanup_orphans=True` (disabled by default for safety).
        """
        scan_root = self._validate_task_dir(task_id) if task_id is not None else self.deliverables_dir

        with self._read_conn() as conn:
            conn.row_factory = sqlite3.Row
            if task_id is not None:
                rows = conn.execute(
                    "SELECT * FROM agent_deliverables WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_deliverables").fetchall()
            records = [_row_to_deliverable(r) for r in rows]

        known_paths: set[str] = set()
        healthy_records = 0
        missing_files: list[str] = []
        corrupted_hashes: list[str] = []

        for rec in records:
            if rec.file_path:
                known_paths.add(Path(rec.file_path).as_posix())
            ok, reason = self.verify_deliverable(rec.deliverable_id)
            if ok:
                healthy_records += 1
            elif reason == "hash_mismatch":
                corrupted_hashes.append(rec.deliverable_id)
            else:
                missing_files.append(rec.deliverable_id)

        stale_temp_files: list[str] = []
        cleaned_temp_files: list[str] = []
        orphan_files: list[str] = []
        cleaned_orphan_files: list[str] = []
        now = time.time()

        if scan_root.exists() and scan_root.is_dir():
            for root_str, dirnames, filenames in os.walk(scan_root, topdown=True):
                root_path = Path(root_str)
                # Inspect and handle stale .purging_* staging directories
                kept_dirs: list[str] = []
                for dname in dirnames:
                    dpath = (root_path / dname).resolve()
                    if dname.startswith(".purging_") and dpath.is_relative_to(self.deliverables_dir):
                        rel_dir_str = dpath.relative_to(self.workspace_root).as_posix()
                        stale_temp_files.append(rel_dir_str)
                        age = now - dpath.stat().st_mtime
                        if cleanup_temp and age >= min_temp_age_seconds:
                            shutil.rmtree(dpath)
                            cleaned_temp_files.append(rel_dir_str)
                        # Do not walk inside .purging_* directories
                    else:
                        kept_dirs.append(dname)
                dirnames[:] = kept_dirs

                # Inspect files in directory
                for fname in filenames:
                    fpath = (root_path / fname).resolve()
                    if not fpath.is_relative_to(self.deliverables_dir):
                        continue
                    rel_file_str = fpath.relative_to(self.workspace_root).as_posix()
                    if fname.startswith(".tmp_") and fname.endswith(".tmp"):
                        stale_temp_files.append(rel_file_str)
                        age = now - fpath.stat().st_mtime
                        if cleanup_temp and age >= min_temp_age_seconds:
                            fpath.unlink(missing_ok=True)
                            cleaned_temp_files.append(rel_file_str)
                    elif rel_file_str not in known_paths:
                        orphan_files.append(rel_file_str)
                        if cleanup_orphans:
                            fpath.unlink(missing_ok=True)
                            cleaned_orphan_files.append(rel_file_str)

        # If scoped to a specific task_id, also inspect sibling .purging_{task_id}_* dirs under deliverables_dir
        if task_id is not None and self.deliverables_dir.exists():
            for dpath in self.deliverables_dir.glob(f".purging_{task_id}_*"):
                resolved_d = dpath.resolve()
                if not resolved_d.is_relative_to(self.deliverables_dir) or not resolved_d.is_dir():
                    continue
                rel_dir_str = resolved_d.relative_to(self.workspace_root).as_posix()
                if rel_dir_str not in stale_temp_files:
                    stale_temp_files.append(rel_dir_str)
                age = now - resolved_d.stat().st_mtime
                if cleanup_temp and age >= min_temp_age_seconds:
                    shutil.rmtree(resolved_d)
                    cleaned_temp_files.append(rel_dir_str)

        return DeliverableIntegrityReport(
            checked_records=len(records),
            healthy_records=healthy_records,
            missing_files=tuple(missing_files),
            corrupted_hashes=tuple(corrupted_hashes),
            orphan_files=tuple(orphan_files),
            stale_temp_files=tuple(stale_temp_files),
            cleaned_temp_files=tuple(cleaned_temp_files),
            cleaned_orphan_files=tuple(cleaned_orphan_files),
        )


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
