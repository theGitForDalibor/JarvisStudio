"""Asynchronous SQLite Checkpointer governance and legacy compatibility preflight.

Phase 2 Stage 2:
- Explicitly configured `aiosqlite.Connection` aligned with Phase 1 PRAGMAs
  (`WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`, `timeout=5.0`).
- Pre-setup legacy `checkpoints` / `writes` schema and payload validation so
  `AsyncSqliteSaver.setup()` never runs blindly over incompatible tables.
- Verified `.bak` snapshot + transactional table-rename archival (`*_legacy_<ts>`)
  when incompatible legacy checkpoint state is detected on a writable runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from jarvis_studio.core.persistence.migrations import create_verified_backup
from jarvis_studio.core.persistence.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    get_path_lock,
    is_memory_db_path,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)

_logger = logging.getLogger(__name__)

_CHECKPOINTS_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "parent_checkpoint_id",
        "type",
        "checkpoint",
        "metadata",
    }
)
_CHECKPOINTS_PK_COLUMNS: tuple[str, ...] = (
    "thread_id",
    "checkpoint_ns",
    "checkpoint_id",
)

_WRITES_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "task_id",
        "idx",
        "channel",
        "type",
        "value",
    }
)
_WRITES_PK_COLUMNS: tuple[str, ...] = (
    "thread_id",
    "checkpoint_ns",
    "checkpoint_id",
    "task_id",
    "idx",
)

CompatibilityStatus = Literal[
    "memory",
    "fresh",
    "compatible",
    "isolated",
    "incompatible",
]


class CheckpointCompatibilityError(RuntimeError):
    """Raised when legacy checkpoint tables are incompatible and cannot proceed."""


class CheckpointCloseError(RuntimeError):
    """Raised when an async SQLite checkpointer connection fails to shut down cleanly."""


@dataclass(frozen=True)
class CheckpointCompatibilityReport:
    """Result of inspecting (and optionally isolating) legacy checkpoint tables."""

    db_path: str
    status: CompatibilityStatus
    checkpoints_rows: int = 0
    writes_rows: int = 0
    reason: str | None = None
    backup_path: Path | None = None
    archived_tables: tuple[str, ...] = ()


def _get_existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('checkpoints', 'writes')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _inspect_table_schema(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: frozenset[str],
    expected_pk: tuple[str, ...],
) -> str | None:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    if not rows:
        return f"{table_name}: empty PRAGMA table_info"

    columns = {str(row["name"]) for row in rows}
    missing = sorted(required_columns - columns)
    if missing:
        return f"{table_name}: missing columns {missing}"

    pk_pairs = sorted(
        ((int(row["pk"]), str(row["name"])) for row in rows if int(row["pk"]) > 0),
        key=lambda item: item[0],
    )
    actual_pk = tuple(name for _, name in pk_pairs)
    if actual_pk != expected_pk:
        return f"{table_name}: primary key mismatch expected {expected_pk}, got {actual_pk}"
    return None


def _inspect_payload_compatibility(
    conn: sqlite3.Connection,
) -> tuple[int, int, str | None]:
    serde = JsonPlusSerializer(pickle_fallback=False)
    cp_count = 0
    writes_count = 0

    cp_cur = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata FROM checkpoints"
    )
    for row in cp_cur:
        cp_count += 1
        cp_type = row["type"]
        cp_blob = row["checkpoint"]
        meta_blob = row["metadata"]
        try:
            decoded = serde.loads_typed((cp_type, cp_blob))
        except Exception as exc:  # noqa: BLE001 - intentional format compatibility probe
            msg = (
                f"checkpoints row ({row['thread_id']}, {row['checkpoint_id']}): "
                f"deserialization failed ({type(exc).__name__}: {exc})"
            )
            return cp_count, writes_count, msg
        if not isinstance(decoded, dict):
            msg = (
                f"checkpoints row ({row['thread_id']}, {row['checkpoint_id']}): "
                f"expected dict checkpoint payload, got {type(decoded).__name__}"
            )
            return cp_count, writes_count, msg
        if meta_blob is not None:
            try:
                parsed_meta = json.loads(meta_blob)
            except Exception as exc:  # noqa: BLE001 - intentional metadata probe
                msg = (
                    f"checkpoints row ({row['thread_id']}, {row['checkpoint_id']}): "
                    f"invalid metadata JSON ({type(exc).__name__}: {exc})"
                )
                return cp_count, writes_count, msg
            if not isinstance(parsed_meta, dict):
                msg = (
                    f"checkpoints row ({row['thread_id']}, {row['checkpoint_id']}): "
                    f"expected dict metadata, got {type(parsed_meta).__name__}"
                )
                return cp_count, writes_count, msg

    writes_cur = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, type, value FROM writes"
    )
    for row in writes_cur:
        writes_count += 1
        try:
            serde.loads_typed((row["type"], row["value"]))
        except Exception as exc:  # noqa: BLE001 - intentional write payload probe
            msg = (
                f"writes row ({row['thread_id']}, {row['checkpoint_id']}, "
                f"{row['task_id']}, {row['idx']}): "
                f"deserialization failed ({type(exc).__name__}: {exc})"
            )
            return cp_count, writes_count, msg

    return cp_count, writes_count, None


def _pick_archive_name(conn: sqlite3.Connection, base_table: str, ts: str) -> str:
    candidate = f"{base_table}_legacy_{ts}"
    suffix = 1
    while (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (candidate,),
        ).fetchone()
        is not None
    ):
        suffix += 1
        candidate = f"{base_table}_legacy_{ts}_{suffix}"
    return candidate


def _inspect_legacy_tables_sync(
    db_path: Path,
) -> tuple[bool, int, int, int, set[str], str | None]:
    conn = open_sync_connection(db_path, enable_foreign_keys=False)
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        user_version = int(version_row[0]) if version_row and version_row[0] is not None else 0

        existing = _get_existing_tables(conn)
        if not existing:
            return True, user_version, 0, 0, existing, None
        if existing != {"checkpoints", "writes"}:
            return (
                False,
                user_version,
                0,
                0,
                existing,
                f"partial checkpoint tables present: {sorted(existing)}",
            )

        schema_err = _inspect_table_schema(
            conn, "checkpoints", _CHECKPOINTS_REQUIRED_COLUMNS, _CHECKPOINTS_PK_COLUMNS
        ) or _inspect_table_schema(
            conn, "writes", _WRITES_REQUIRED_COLUMNS, _WRITES_PK_COLUMNS
        )
        if schema_err is not None:
            return False, user_version, 0, 0, existing, schema_err

        cp_count, writes_count, payload_err = _inspect_payload_compatibility(conn)
        if payload_err is not None:
            return False, user_version, cp_count, writes_count, existing, payload_err

        return True, user_version, cp_count, writes_count, existing, None
    finally:
        conn.close()


def verify_or_isolate_legacy_checkpoints(
    db_path: str | Path,
    *,
    isolate_incompatible: bool = True,
) -> CheckpointCompatibilityReport:
    """Validate pre-existing checkpoint tables before `AsyncSqliteSaver.setup()` runs.

    When `isolate_incompatible=False`, performs a non-destructive read-only inspection
    and returns `status='incompatible'` on mismatch without altering tables or creating backups.
    When `isolate_incompatible=True`, incompatible tables trigger a verified `.bak` snapshot
    followed by transactional table renaming (`*_legacy_<ts>`) so historical data is preserved.
    """
    normalized = normalize_db_path(db_path)
    if is_memory_db_path(normalized):
        return CheckpointCompatibilityReport(db_path=normalized, status="memory")

    target = Path(normalized)
    if not target.exists():
        return CheckpointCompatibilityReport(db_path=normalized, status="fresh")

    with get_path_lock(normalized):
        compatible, user_version, cp_count, writes_count, existing, reason = (
            _inspect_legacy_tables_sync(target)
        )
        if not existing:
            return CheckpointCompatibilityReport(db_path=normalized, status="fresh")
        if compatible:
            return CheckpointCompatibilityReport(
                db_path=normalized,
                status="compatible",
                checkpoints_rows=cp_count,
                writes_rows=writes_count,
            )
        if not isolate_incompatible:
            return CheckpointCompatibilityReport(
                db_path=normalized,
                status="incompatible",
                checkpoints_rows=cp_count,
                writes_rows=writes_count,
                reason=reason,
            )

        backup_path = create_verified_backup(
            target,
            from_version=user_version,
            to_version=user_version,
        )
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archived_names: list[str] = []
        with sync_transaction(target, enable_foreign_keys=False) as tx_conn:
            for table_name in ("checkpoints", "writes"):
                if table_name not in existing:
                    continue
                archive_name = _pick_archive_name(tx_conn, table_name, ts)
                tx_conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{archive_name}"')
                archived_names.append(archive_name)

        _logger.warning(
            "Isolated incompatible legacy checkpoint tables %s -> %s (backup=%s, reason=%s)",
            sorted(existing),
            archived_names,
            backup_path,
            reason,
        )
        return CheckpointCompatibilityReport(
            db_path=normalized,
            status="isolated",
            checkpoints_rows=cp_count,
            writes_rows=writes_count,
            reason=reason,
            backup_path=backup_path,
            archived_tables=tuple(archived_names),
        )


async def open_async_checkpoint_connection(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_foreign_keys: bool = True,
) -> aiosqlite.Connection:
    """Open an owned `aiosqlite.Connection` with explicit timeout and Phase 1 PRAGMAs."""
    if busy_timeout_ms <= 0:
        raise ValueError(f"busy_timeout_ms must be positive, got {busy_timeout_ms}")

    normalized = normalize_db_path(db_path)
    is_memory = is_memory_db_path(normalized)
    if not is_memory:
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(normalized, timeout=busy_timeout_ms / 1000.0)
    try:
        await conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if not is_memory:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
        fk_state = "ON" if enable_foreign_keys else "OFF"
        await conn.execute(f"PRAGMA foreign_keys = {fk_state}")
    except BaseException:
        await close_async_checkpoint_connection(conn)
        raise
    return conn


async def close_async_checkpoint_connection(
    conn: aiosqlite.Connection,
    *,
    join_timeout: float = 2.0,
) -> None:
    """Close an `aiosqlite.Connection` and confirm its worker thread exited cleanly."""
    worker_thread = getattr(conn, "_thread", None)
    close_exc: Exception | None = None
    try:
        await conn.close()
    except Exception as exc:  # noqa: BLE001 - join/verify worker thread before re-raising
        close_exc = exc

    # aiosqlite resolves the stop future just before `_connection_worker_thread` breaks
    # out of its loop; joining and verifying prevents lingering threads after shutdown.
    if isinstance(worker_thread, threading.Thread) and worker_thread.is_alive():
        await asyncio.to_thread(worker_thread.join, join_timeout)
        if worker_thread.is_alive():
            msg = (
                f"aiosqlite worker thread {worker_thread.name!r} "
                f"(ident={worker_thread.ident}) remained alive after join({join_timeout}s)"
            )
            _logger.error(msg)
            err = CheckpointCloseError(msg)
            if close_exc is not None:
                raise err from close_exc
            raise err

    if close_exc is not None:
        raise close_exc


@dataclass
class ManagedSqliteCheckpointer:
    """Owned bundle pairing an `AsyncSqliteSaver` with its lifecycle-managed connection."""

    saver: AsyncSqliteSaver
    conn: aiosqlite.Connection
    compatibility: CheckpointCompatibilityReport
    _closed: bool = False

    async def aclose(self) -> None:
        """Idempotently close the underlying `aiosqlite.Connection` and stop its worker."""
        if self._closed:
            return
        await close_async_checkpoint_connection(self.conn)
        self._closed = True


async def open_managed_sqlite_checkpointer(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_foreign_keys: bool = True,
    isolate_incompatible: bool = True,
) -> ManagedSqliteCheckpointer:
    """Run legacy compatibility preflight, open configured connection, and setup saver."""
    report = verify_or_isolate_legacy_checkpoints(
        db_path,
        isolate_incompatible=isolate_incompatible,
    )
    if report.status == "incompatible":
        raise CheckpointCompatibilityError(
            f"Legacy checkpoints in {report.db_path!r} are incompatible: {report.reason}"
        )

    conn = await open_async_checkpoint_connection(
        db_path,
        busy_timeout_ms=busy_timeout_ms,
        enable_foreign_keys=enable_foreign_keys,
    )
    try:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
    except BaseException:
        await close_async_checkpoint_connection(conn)
        raise

    return ManagedSqliteCheckpointer(
        saver=saver,
        conn=conn,
        compatibility=report,
    )
