"""Independent schema version management for `jarvis_studio.db` and `jarvis_vec.db`.

Phase 1 implements v0 -> v1 baseline alignment using `PRAGMA user_version`:
- Strictly independent migrations for core app DB and vector DB
- Pre-migration consistent backup via SQLite Backup API with unique filenames
- Read-only backup validation via `PRAGMA quick_check(1)` and `PRAGMA user_version`
- Atomic `BEGIN IMMEDIATE` migration transaction with in-tx version Double-Check
"""

import itertools
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from jarvis_studio.core.persistence.sqlite import (
    get_path_lock,
    is_memory_db_path,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)

APP_SCHEMA_VERSION = 2
VEC_SCHEMA_VERSION = 1

_logger = logging.getLogger(__name__)

_BACKUP_SEQ_LOCK = threading.Lock()
_BACKUP_SEQ = itertools.count(1)

_APP_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        metadata TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tool_calls TEXT,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id, created_at)",
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_ltm_category ON ltm_memories(category)",
    """
    CREATE TABLE IF NOT EXISTS agent_conversation_traces (
        trace_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        run_id TEXT,
        query TEXT NOT NULL,
        status TEXT NOT NULL,
        start_time REAL NOT NULL,
        total_duration_ms INTEGER,
        ttft_ms INTEGER,
        ttfa_ms INTEGER,
        ttlt_ms INTEGER,
        steps_count INTEGER,
        payload TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_traces_session ON agent_conversation_traces(session_id, start_time DESC)",
)

_APP_V2_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS hitl_approval_requests (
        id                      TEXT PRIMARY KEY,
        thread_id               TEXT NOT NULL CHECK (
                                    length(trim(thread_id)) > 0
                                    AND lower(trim(thread_id)) != 'default'
                                ),
        task_id                 TEXT,
        run_id                  TEXT NOT NULL CHECK (length(trim(run_id)) > 0),
        checkpoint_id           TEXT,
        tool_call_id            TEXT NOT NULL CHECK (length(trim(tool_call_id)) > 0),
        tool_name               TEXT NOT NULL CHECK (length(trim(tool_name)) > 0),
        arguments_json          TEXT NOT NULL,
        arguments_hash          TEXT NOT NULL CHECK (length(trim(arguments_hash)) > 0),
        risk_level              TEXT NOT NULL,
        status                  TEXT NOT NULL CHECK (status IN (
                                    'PENDING', 'APPROVED', 'MODIFIED', 'REJECTED',
                                    'EXECUTING', 'CONSUMED', 'FAILED', 'UNKNOWN'
                                )),
        decision                TEXT,
        description             TEXT NOT NULL,
        modified_arguments_json TEXT,
        resolution_reason       TEXT,
        lease_owner_instance_id TEXT,
        lease_acquired_run_id   TEXT,
        execution_error         TEXT,
        audit_note              TEXT,
        audit_actor             TEXT,
        audited_at              REAL,
        created_at              REAL NOT NULL,
        resolved_at             REAL,
        executed_at             REAL,
        completed_at            REAL,
        UNIQUE (thread_id, tool_call_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hitl_thread_status ON hitl_approval_requests(thread_id, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_status_lease_owner ON hitl_approval_requests(status, lease_owner_instance_id)",
    "CREATE INDEX IF NOT EXISTS idx_hitl_task_id ON hitl_approval_requests(task_id)",
    """
    CREATE TABLE IF NOT EXISTS tool_execution_outcomes (
        outcome_id       TEXT PRIMARY KEY,
        thread_id        TEXT NOT NULL CHECK (
                             length(trim(thread_id)) > 0
                             AND lower(trim(thread_id)) != 'default'
                         ),
        task_id          TEXT,
        run_id           TEXT NOT NULL CHECK (length(trim(run_id)) > 0),
        tool_call_id     TEXT NOT NULL CHECK (length(trim(tool_call_id)) > 0),
        approval_id      TEXT,
        tool_name        TEXT NOT NULL CHECK (length(trim(tool_name)) > 0),
        is_high_risk     INTEGER NOT NULL DEFAULT 0 CHECK (is_high_risk IN (0, 1)),
        outcome_status   TEXT NOT NULL CHECK (outcome_status IN (
                             'NOT_STARTED', 'SUCCEEDED', 'FAILED', 'PARTIAL', 'UNKNOWN'
                         )),
        side_effect_hint TEXT,
        result_summary   TEXT,
        error_message    TEXT,
        reconciled_by    TEXT,
        reconciled_note  TEXT,
        reconciled_at    REAL,
        started_at       REAL NOT NULL,
        finished_at      REAL,
        updated_at       REAL NOT NULL,
        FOREIGN KEY (approval_id) REFERENCES hitl_approval_requests(id) ON DELETE SET NULL,
        UNIQUE (thread_id, tool_call_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_outcome_approval
        ON tool_execution_outcomes(approval_id)
        WHERE approval_id IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_outcome_task_status ON tool_execution_outcomes(task_id, outcome_status)",
)

# `vec_chunks` virtual table depends on per-instance embedding dimension (e.g. 32 in tests,
# 1536 in production) and therefore remains created by `SqliteVecStore._init_db()`.
_VEC_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS doc_files (
        doc_path TEXT PRIMARY KEY,
        file_hash TEXT NOT NULL,
        chunk_count INTEGER NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_chunks (
        chunk_id TEXT PRIMARY KEY,
        doc_path TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        metadata TEXT,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunk_doc ON doc_chunks(doc_path)",
)


class MigrationError(RuntimeError):
    """Base error raised when SQLite schema migration cannot safely proceed."""


class BackupVerificationError(MigrationError):
    """Raised when pre-migration SQLite backup or read-only integrity check fails."""


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _read_file_user_version(
    db_path: Path,
    *,
    enable_foreign_keys: bool,
    enable_sqlite_vec: bool,
) -> int:
    conn = open_sync_connection(
        db_path,
        enable_foreign_keys=enable_foreign_keys,
        enable_sqlite_vec=enable_sqlite_vec,
    )
    try:
        return _read_user_version(conn)
    finally:
        conn.close()


def _reserve_unique_backup_path(
    src_path: Path, *, from_version: int, to_version: int
) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    pid = os.getpid()
    with _BACKUP_SEQ_LOCK:
        while True:
            seq = next(_BACKUP_SEQ)
            candidate = src_path.with_name(
                f"{src_path.stem}.bak.v{from_version}_to_v{to_version}.{ts}_{pid}_{seq}.db"
            )
            if not candidate.exists():
                return candidate


def verify_sqlite_backup(
    backup_path: str | Path, *, expected_version: int | None = None
) -> int:
    """Open a backup file in read-only URI mode and validate integrity + version."""
    target = Path(backup_path).resolve()
    if not target.is_file():
        raise BackupVerificationError(f"SQLite backup file does not exist: {target}")

    ro_uri = f"{target.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(ro_uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise BackupVerificationError(
            f"Unable to open SQLite backup {target} in read-only mode: {exc}"
        ) from exc

    try:
        qc_row = conn.execute("PRAGMA quick_check(1)").fetchone()
        qc_status = (
            str(qc_row[0]).lower()
            if qc_row is not None and qc_row[0] is not None
            else ""
        )
        if qc_status != "ok":
            raise BackupVerificationError(
                f"SQLite backup failed PRAGMA quick_check(1) for {target}: {qc_row!r}"
            )
        actual_version = _read_user_version(conn)
        if expected_version is not None and actual_version != expected_version:
            raise BackupVerificationError(
                f"SQLite backup user_version mismatch for {target}: "
                f"expected {expected_version}, got {actual_version}"
            )
        return actual_version
    except sqlite3.Error as exc:
        raise BackupVerificationError(
            f"SQLite integrity check failed for backup {target}: {exc}"
        ) from exc
    finally:
        conn.close()


def _run_cleanup_step(
    cleanup_errors: list[BaseException],
    description: str,
    action: Callable[[], object],
) -> None:
    """Execute one backup cleanup step while keeping subsequent steps independent."""
    try:
        action()
    except BaseException as exc:
        _logger.warning("%s: %s", description, exc)
        cleanup_errors.append(exc)


def create_verified_backup(
    db_path: str | Path,
    *,
    from_version: int,
    to_version: int,
) -> Path:
    """Create a uniquely named consistent snapshot via SQLite Backup API and verify it."""
    src_path = Path(db_path).resolve()
    bak_path = _reserve_unique_backup_path(
        src_path, from_version=from_version, to_version=to_version
    )

    src_conn = open_sync_connection(src_path, enable_foreign_keys=False)
    dst_conn: sqlite3.Connection | None = None
    backup_completed = False
    primary_exc: BaseException | None = None
    try:
        try:
            dst_conn = sqlite3.connect(str(bak_path), isolation_level=None)
            src_conn.backup(dst_conn)
            # Keep the backup snapshot self-contained as a single file without WAL/SHM sidecars.
            dst_conn.execute("PRAGMA journal_mode = DELETE")
            backup_completed = True
        except sqlite3.Error as exc:
            primary_exc = BackupVerificationError(
                f"SQLite Backup API failed for {src_path} -> {bak_path}: {exc}"
            )
            primary_exc.__cause__ = exc
        except BaseException as exc:
            primary_exc = exc
    finally:
        cleanup_errors: list[BaseException] = []
        if dst_conn is not None:
            _run_cleanup_step(
                cleanup_errors,
                f"Failed closing backup destination connection {bak_path}",
                dst_conn.close,
            )
        _run_cleanup_step(
            cleanup_errors,
            f"Failed closing backup source connection {src_path}",
            src_conn.close,
        )
        if not backup_completed:
            for artifact in (
                bak_path,
                Path(f"{bak_path}-journal"),
                Path(f"{bak_path}-wal"),
                Path(f"{bak_path}-shm"),
            ):
                _run_cleanup_step(
                    cleanup_errors,
                    f"Failed removing incomplete backup artifact {artifact}",
                    lambda path=artifact: path.unlink(missing_ok=True),
                )

        if primary_exc is not None:
            if cleanup_errors:
                primary_exc.add_note(
                    "Additional cleanup error(s) occurred during backup finalization: "
                    + "; ".join(f"{type(e).__name__}: {e}" for e in cleanup_errors)
                )
            raise primary_exc
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if len(cleanup_errors) > 1:
            raise BaseExceptionGroup(
                "Multiple cleanup errors occurred while finalizing SQLite backup",
                cleanup_errors,
            )

    verify_sqlite_backup(bak_path, expected_version=from_version)
    return bak_path


def _apply_steps(
    conn: sqlite3.Connection,
    *,
    target_version: int,
    steps: dict[int, tuple[str, ...]],
) -> int:
    locked_version = _read_user_version(conn)
    if locked_version < 0:
        raise MigrationError(
            f"Unsupported negative SQLite PRAGMA user_version: {locked_version}"
        )
    if locked_version >= target_version:
        return locked_version

    for next_ver in range(locked_version + 1, target_version + 1):
        for stmt in steps.get(next_ver, ()):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {int(next_ver)}")
    return target_version


def _run_migrations(
    db_path: str | Path,
    *,
    target_version: int,
    steps: dict[int, tuple[str, ...]],
    enable_foreign_keys: bool,
    enable_sqlite_vec: bool,
    in_memory_conn: sqlite3.Connection | None = None,
) -> int:
    normalized = normalize_db_path(db_path)
    if in_memory_conn is not None or is_memory_db_path(normalized):
        if in_memory_conn is None:
            raise ValueError(
                "in_memory_conn is required when migrating an ':memory:' database"
            )
        if _read_user_version(in_memory_conn) >= target_version:
            return _read_user_version(in_memory_conn)
        with sync_transaction(
            normalized,
            in_memory_conn=in_memory_conn,
            enable_foreign_keys=enable_foreign_keys,
            enable_sqlite_vec=enable_sqlite_vec,
        ) as conn:
            return _apply_steps(conn, target_version=target_version, steps=steps)

    resolved = Path(normalized).resolve()
    with get_path_lock(resolved):
        current_version = _read_file_user_version(
            resolved,
            enable_foreign_keys=enable_foreign_keys,
            enable_sqlite_vec=enable_sqlite_vec,
        )
        if current_version < 0:
            raise MigrationError(
                f"Unsupported negative SQLite PRAGMA user_version ({current_version}) in {resolved}"
            )
        if current_version >= target_version:
            return current_version

        bak_path = create_verified_backup(
            resolved,
            from_version=current_version,
            to_version=target_version,
        )
        _logger.info(
            "Verified pre-migration SQLite backup for %s: %s", resolved, bak_path.name
        )

        with sync_transaction(
            resolved,
            enable_foreign_keys=enable_foreign_keys,
            enable_sqlite_vec=enable_sqlite_vec,
        ) as conn:
            return _apply_steps(conn, target_version=target_version, steps=steps)


def run_app_migrations(
    db_path: str | Path,
    *,
    in_memory_conn: sqlite3.Connection | None = None,
) -> int:
    """Ensure core application database (`jarvis_studio.db`) is at `APP_SCHEMA_VERSION`."""
    return _run_migrations(
        db_path,
        target_version=APP_SCHEMA_VERSION,
        steps={1: _APP_V1_STATEMENTS, 2: _APP_V2_STATEMENTS},
        enable_foreign_keys=True,
        enable_sqlite_vec=False,
        in_memory_conn=in_memory_conn,
    )


def run_vec_migrations(
    db_path: str | Path,
    *,
    in_memory_conn: sqlite3.Connection | None = None,
) -> int:
    """Ensure RAG vector database (`jarvis_vec.db`) is at `VEC_SCHEMA_VERSION`."""
    return _run_migrations(
        db_path,
        target_version=VEC_SCHEMA_VERSION,
        steps={1: _VEC_V1_STATEMENTS},
        enable_foreign_keys=False,
        enable_sqlite_vec=True,
        in_memory_conn=in_memory_conn,
    )
