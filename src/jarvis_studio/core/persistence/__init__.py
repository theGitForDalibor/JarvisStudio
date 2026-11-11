"""Core synchronous SQLite persistence and migration primitives (Phase 1)."""

from jarvis_studio.core.persistence.migrations import (
    APP_SCHEMA_VERSION,
    VEC_SCHEMA_VERSION,
    BackupVerificationError,
    MigrationError,
    create_verified_backup,
    run_app_migrations,
    run_vec_migrations,
    verify_sqlite_backup,
)
from jarvis_studio.core.persistence.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    NestedTransactionError,
    SyncSQLiteSession,
    initialize_sqlite_database,
    is_memory_db_path,
    load_sqlite_vec,
    normalize_db_path,
    open_sync_connection,
    sync_transaction,
)

__all__ = [
    "APP_SCHEMA_VERSION",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "VEC_SCHEMA_VERSION",
    "BackupVerificationError",
    "MigrationError",
    "NestedTransactionError",
    "SyncSQLiteSession",
    "create_verified_backup",
    "initialize_sqlite_database",
    "is_memory_db_path",
    "load_sqlite_vec",
    "normalize_db_path",
    "open_sync_connection",
    "run_app_migrations",
    "run_vec_migrations",
    "sync_transaction",
    "verify_sqlite_backup",
]
