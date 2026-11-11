"""Synchronous SQLite connection, transaction, and lifecycle primitives.

Phase 1 persistence foundation:
- Explicit transaction control (`isolation_level=None`) with `BEGIN IMMEDIATE`
- Strict no-nested-transaction guard (`NestedTransactionError`)
- Layered PRAGMA setup (`busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys`)
- Separated database initialization (`WAL`) vs schema migration
- Scoped `sqlite-vec` extension loading with `try/finally` extension lock-down

This module is intentionally a pure leaf dependency (stdlib + `sqlite_vec` only)
so importing it from `trace_logger` or any Store never creates circular imports.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

DEFAULT_BUSY_TIMEOUT_MS = 5000

_logger = logging.getLogger(__name__)
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def get_path_lock(db_path: str | Path) -> threading.RLock:
    """Return an in-process re-entrant lock keyed by canonical resolved DB path."""
    normalized = normalize_db_path(db_path)
    if normalized == ":memory:":
        raise ValueError("get_path_lock does not apply to ':memory:' databases")
    key = str(Path(normalized).resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class NestedTransactionError(RuntimeError):
    """Raised when `sync_transaction` is entered on a connection with an active transaction."""


def normalize_db_path(db_path: str | Path) -> str:
    """Validate and normalize a SQLite path string or `:memory:` marker."""
    raw = str(db_path)
    if not raw or not raw.strip():
        raise ValueError("SQLite db_path must not be empty or whitespace")
    return ":memory:" if raw == ":memory:" else raw


def is_memory_db_path(db_path: str | Path) -> bool:
    """Return True if `db_path` denotes an in-memory SQLite database."""
    return normalize_db_path(db_path) == ":memory:"


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the `sqlite-vec` extension transiently without leaving extension loading enabled."""
    already_loaded = (
        conn.execute(
            "SELECT 1 FROM pragma_function_list WHERE name = 'vec_version'"
        ).fetchone()
        is not None
    )
    if already_loaded:
        return

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def open_sync_connection(
    db_path: str | Path,
    *,
    enable_foreign_keys: bool = True,
    enable_sqlite_vec: bool = False,
) -> sqlite3.Connection:
    """Open a new configured synchronous SQLite connection owned by the caller.

    Callers are responsible for closing returned connections in a `finally` block
    (except Stores retaining a private `:memory:` connection until `Store.close()`).
    """
    normalized = normalize_db_path(db_path)
    is_memory = normalized == ":memory:"
    if not is_memory:
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        normalized,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        if not is_memory:
            conn.execute("PRAGMA synchronous = NORMAL")
        fk_state = "ON" if enable_foreign_keys else "OFF"
        conn.execute(f"PRAGMA foreign_keys = {fk_state}")
        if enable_sqlite_vec:
            load_sqlite_vec(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def initialize_sqlite_database(
    db_path: str | Path,
    *,
    enable_wal: bool = True,
    enable_foreign_keys: bool = True,
    enable_sqlite_vec: bool = False,
) -> str:
    """Initialize database-level settings (e.g., WAL) outside any active transaction.

    Returns the active journal mode (always `'memory'` for `:memory:` databases).
    """
    normalized = normalize_db_path(db_path)
    if normalized == ":memory:":
        return "memory"

    with get_path_lock(normalized):
        for attempt in range(4):
            conn = open_sync_connection(
                normalized,
                enable_foreign_keys=enable_foreign_keys,
                enable_sqlite_vec=enable_sqlite_vec,
            )
            try:
                if conn.in_transaction:
                    raise RuntimeError(
                        f"Cannot initialize journal_mode inside an active transaction ({normalized!r})"
                    )

                mode_row = conn.execute("PRAGMA journal_mode").fetchone()
                current_mode = (
                    str(mode_row[0]).lower()
                    if mode_row is not None and mode_row[0] is not None
                    else ""
                )
                if enable_wal and current_mode != "wal":
                    if conn.in_transaction:
                        raise RuntimeError(
                            f"Cannot switch to WAL inside an active transaction ({normalized!r})"
                        )
                    wal_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
                    current_mode = (
                        str(wal_row[0]).lower()
                        if wal_row is not None and wal_row[0] is not None
                        else ""
                    )
                    if current_mode != "wal":
                        raise sqlite3.OperationalError(
                            f"Expected journal_mode='wal' for {normalized!r}, got {current_mode!r}"
                        )
                return current_mode
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if attempt < 3 and ("locked" in msg or "busy" in msg):
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise
            finally:
                conn.close()
    raise AssertionError("Unreachable")


@contextmanager
def sync_transaction(
    db_path: str | Path,
    *,
    in_memory_conn: sqlite3.Connection | None = None,
    enable_foreign_keys: bool = True,
    enable_sqlite_vec: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Execute a strict non-nesting `BEGIN IMMEDIATE` synchronous transaction.

    - For disk databases (`in_memory_conn is None`), opens a dedicated connection
      and closes it unconditionally in `finally`.
    - For Store-private `:memory:` connections (`in_memory_conn is not None`),
      never closes `in_memory_conn` and raises `NestedTransactionError` if a
      transaction is already active without altering the outer transaction.
    """
    normalized = normalize_db_path(db_path)
    owns_conn = in_memory_conn is None

    if in_memory_conn is not None:
        if in_memory_conn.in_transaction:
            raise NestedTransactionError(
                f"Nested transaction is forbidden (db_path={normalized!r}, conn_id={id(in_memory_conn)})"
            )
        conn = in_memory_conn
    else:
        conn = open_sync_connection(
            normalized,
            enable_foreign_keys=enable_foreign_keys,
            enable_sqlite_vec=enable_sqlite_vec,
        )

    began_tx = False
    try:
        if conn.in_transaction:
            raise NestedTransactionError(
                f"Nested transaction is forbidden (db_path={normalized!r}, conn_id={id(conn)})"
            )
        conn.execute("BEGIN IMMEDIATE")
        began_tx = True
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if began_tx and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rb_exc:
                _logger.warning(
                    "SQLite rollback failed during exception cleanup (%s): %s",
                    normalized,
                    rb_exc,
                )
        raise
    finally:
        if owns_conn:
            conn.close()


class SyncSQLiteSession:
    """Reusable disk/in-memory connection and transaction coordinator for sync Stores.

    Serializes cross-thread access to a Store's private `:memory:` connection using
    a re-entrant `threading.RLock`, preventing false `NestedTransactionError` across
    threads while still failing fast on same-thread re-entrant transactions.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        enable_foreign_keys: bool = True,
        enable_sqlite_vec: bool = False,
    ) -> None:
        self.db_path = normalize_db_path(db_path)
        self.is_memory = self.db_path == ":memory:"
        self.enable_foreign_keys = enable_foreign_keys
        self.enable_sqlite_vec = enable_sqlite_vec
        self._in_memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def in_memory_conn(self) -> sqlite3.Connection | None:
        """Return the private `:memory:` connection, or None for disk/closed stores."""
        return self._in_memory_conn

    def _ensure_in_memory_conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("SQLite in-memory store has already been closed")
        if self._in_memory_conn is None:
            self._in_memory_conn = open_sync_connection(
                ":memory:",
                enable_foreign_keys=self.enable_foreign_keys,
                enable_sqlite_vec=self.enable_sqlite_vec,
            )
        return self._in_memory_conn

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a read connection without starting an implicit write transaction."""
        if self.is_memory:
            with self._lock:
                yield self._ensure_in_memory_conn()
            return

        conn = open_sync_connection(
            self.db_path,
            enable_foreign_keys=self.enable_foreign_keys,
            enable_sqlite_vec=self.enable_sqlite_vec,
        )
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an exclusive `BEGIN IMMEDIATE` transaction."""
        if self.is_memory:
            with self._lock:
                conn = self._ensure_in_memory_conn()
                with sync_transaction(
                    self.db_path,
                    in_memory_conn=conn,
                    enable_foreign_keys=self.enable_foreign_keys,
                    enable_sqlite_vec=self.enable_sqlite_vec,
                ) as tx_conn:
                    yield tx_conn
            return

        with sync_transaction(
            self.db_path,
            enable_foreign_keys=self.enable_foreign_keys,
            enable_sqlite_vec=self.enable_sqlite_vec,
        ) as tx_conn:
            yield tx_conn

    def close(self) -> None:
        """Idempotently release the private `:memory:` connection if present."""
        with self._lock:
            if self._in_memory_conn is not None:
                self._in_memory_conn.close()
                self._in_memory_conn = None
            if self.is_memory:
                self._closed = True
