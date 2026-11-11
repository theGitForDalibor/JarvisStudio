"""Tests for Phase 1 unified SQLite connection governance and dual-DB migrations."""

import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlite_vec

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.trace_logger import ConversationTrace, TraceStore
from jarvis_studio.core.persistence import (
    APP_SCHEMA_VERSION,
    DEFAULT_BUSY_TIMEOUT_MS,
    VEC_SCHEMA_VERSION,
    BackupVerificationError,
    MigrationError,
    NestedTransactionError,
    initialize_sqlite_database,
    load_sqlite_vec,
    open_sync_connection,
    run_app_migrations,
    run_vec_migrations,
    sync_transaction,
    verify_sqlite_backup,
)
from jarvis_studio.core.persistence import migrations as migrations_mod
from jarvis_studio.core.persistence.sqlite import get_path_lock
from jarvis_studio.memory.models import MemoryCategory, MemoryItem
from jarvis_studio.memory.store import MemoryStore
from jarvis_studio.rag.splitter import DocumentChunk
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.service.session_service import SessionService


def test_persistence_imports_without_circular_dependency_or_aiosqlite():
    """Verify `core.persistence` imports before `observability` and omits `aiosqlite`."""
    code = (
        "import sys; "
        "import jarvis_studio.core.persistence; "
        "import jarvis_studio.core.persistence.sqlite; "
        "import jarvis_studio.core.persistence.migrations; "
        "assert 'aiosqlite' not in sys.modules; "
        "assert 'jarvis_studio.core.observability' not in sys.modules; "
        "from jarvis_studio.core.observability.trace_logger import TraceStore; "
        "assert TraceStore is not None"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"


def test_open_sync_connection_pragmas_and_blank_path_guard(tmp_path: Path):
    """Verify layered PRAGMAs, blank-path rejection, and exception cleanup."""
    for invalid in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="empty or whitespace"):
            open_sync_connection(invalid)

    db_file = tmp_path / "nested" / "app.db"
    conn = open_sync_connection(db_file, enable_foreign_keys=True)
    try:
        assert conn.isolation_level is None
        assert conn.in_transaction is False
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == DEFAULT_BUSY_TIMEOUT_MS
        # NORMAL synchronous mode equals 1 in SQLite
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
        assert conn.in_transaction is False
    finally:
        conn.close()

    # In-memory connection skips file creation and sets busy_timeout + FK
    mem_conn = open_sync_connection(":memory:", enable_foreign_keys=False)
    try:
        assert mem_conn.execute("PRAGMA busy_timeout").fetchone()[0] == DEFAULT_BUSY_TIMEOUT_MS
        assert mem_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        mem_conn.close()
    assert not (Path.cwd() / ":memory:").exists()
    with pytest.raises(ValueError, match="does not apply to ':memory:'"):
        get_path_lock(":memory:")


def test_default_stores_resolve_to_isolated_temp_databases():
    """Verify default Store construction resolves to pytest temp DBs, never project `data/`."""
    settings = get_settings()
    project_app_db = Path("data/jarvis_studio.db").resolve()
    project_vec_db = Path("data/jarvis_vec.db").resolve()

    assert Path(settings.storage.sqlite_path).resolve() != project_app_db
    assert Path(settings.rag.vector_db_path).resolve() != project_vec_db

    ss = SessionService()
    ms = MemoryStore()
    ts = TraceStore()
    vs = SqliteVecStore()
    try:
        for store, forbidden in [
            (ss, project_app_db),
            (ms, project_app_db),
            (ts, project_app_db),
            (vs, project_vec_db),
        ]:
            with store._db.read_connection() as conn:
                db_list = conn.execute("PRAGMA database_list").fetchall()
            main_file = next(r["file"] for r in db_list if r["name"] == "main")
            resolved = Path(main_file).resolve()
            assert resolved != forbidden
            assert resolved.parent == Path(settings.storage.sqlite_path).resolve().parent
            assert resolved.exists()
    finally:
        vs.close()
        ts.close()
        ms.close()
        ss.close()


def test_open_sync_connection_closes_handle_on_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify connection is closed if post-connect extension setup raises."""
    closed_flags: list[bool] = []
    real_connect = sqlite3.connect

    class _ClosingSpyConnection(sqlite3.Connection):
        def close(self) -> None:
            closed_flags.append(True)
            super().close()

    def _spy_connect(*args, **kwargs):
        kwargs["factory"] = _ClosingSpyConnection
        return real_connect(*args, **kwargs)

    def _boom(_conn):
        raise RuntimeError("vec load failed")

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    monkeypatch.setattr(sqlite_vec, "load", _boom)

    with pytest.raises(RuntimeError, match="vec load failed"):
        open_sync_connection(tmp_path / "fail.db", enable_sqlite_vec=True)

    assert closed_flags == [True]


def test_initialize_sqlite_database_and_independent_dual_db_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify WAL init + independent user_version progression across app & vec DBs."""
    app_db = tmp_path / "app.db"
    vec_db = tmp_path / "vec.db"

    assert initialize_sqlite_database(":memory:") == "memory"

    assert initialize_sqlite_database(app_db, enable_wal=True) == "wal"
    assert initialize_sqlite_database(vec_db, enable_wal=True, enable_foreign_keys=False, enable_sqlite_vec=True) == "wal"

    assert run_app_migrations(app_db) == APP_SCHEMA_VERSION

    # Simulate failure during vec DB migration and verify app DB remains untouched
    def _failing_apply(conn, *, target_version, steps):
        conn.execute("CREATE TABLE partial_vec_tbl (x INT)")
        raise sqlite3.OperationalError("simulated vec migration error")

    monkeypatch.setattr(migrations_mod, "_apply_steps", _failing_apply)
    with pytest.raises(sqlite3.OperationalError, match="simulated vec migration error"):
        run_vec_migrations(vec_db)
    monkeypatch.undo()

    # Check app DB stays v1 while vec DB rolled back cleanly to v0 without partial table
    app_conn = open_sync_connection(app_db)
    vec_conn = open_sync_connection(vec_db, enable_foreign_keys=False, enable_sqlite_vec=True)
    try:
        assert app_conn.execute("PRAGMA user_version").fetchone()[0] == APP_SCHEMA_VERSION
        assert vec_conn.execute("PRAGMA user_version").fetchone()[0] == 0
        partial = vec_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='partial_vec_tbl'"
        ).fetchone()
        assert partial is None
    finally:
        vec_conn.close()
        app_conn.close()

    # Re-running vec migration after recovery succeeds cleanly
    assert run_vec_migrations(vec_db) == VEC_SCHEMA_VERSION
    vec_backups = sorted(tmp_path.glob("vec.bak.v0_to_v1.*.db"))
    assert len(vec_backups) == 2  # Neither attempt overwrote previous backup
    assert list(tmp_path.glob("vec.bak.*-shm")) == []
    assert list(tmp_path.glob("vec.bak.*-wal")) == []


def test_backup_verification_blocks_corrupted_snapshots_and_preserves_old_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify quick_check(1) detects corruption and blocks migration before advancing user_version."""
    app_db = tmp_path / "studio.db"
    initialize_sqlite_database(app_db)

    # First create a corrupted backup file and validate direct verification fails
    corrupt_bak = tmp_path / "studio.bak.v0_to_v1.20261111T000000Z_1_99.db"
    corrupt_bak.write_bytes(b"corrupted non-sqlite header bytes")
    with pytest.raises(BackupVerificationError):
        verify_sqlite_backup(corrupt_bak, expected_version=0)

    # Also test version mismatch in backup validation
    valid_bak = tmp_path / "studio.bak.v0_to_v1.20261111T000000Z_1_100.db"
    c = sqlite3.connect(str(valid_bak))
    c.execute("CREATE TABLE ok (id INT)")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()
    assert verify_sqlite_backup(valid_bak, expected_version=5) == 5
    with pytest.raises(BackupVerificationError, match="user_version mismatch"):
        verify_sqlite_backup(valid_bak, expected_version=0)

    # Corrupt backup generated during real `create_verified_backup -> verify_sqlite_backup`
    # must raise BackupVerificationError, abort migration, and leave user_version=0.
    corrupted_paths: list[Path] = []
    orig_backup_fn = migrations_mod.verify_sqlite_backup

    def _corrupt_snapshot_before_verify(backup_path, *, expected_version=None):
        p = Path(backup_path)
        corrupted_paths.append(p)
        p.write_bytes(b"corrupted during snapshot write")
        return orig_backup_fn(p, expected_version=expected_version)

    monkeypatch.setattr(
        migrations_mod, "verify_sqlite_backup", _corrupt_snapshot_before_verify
    )
    with pytest.raises(BackupVerificationError):
        run_app_migrations(app_db)
    monkeypatch.undo()

    assert len(corrupted_paths) == 1
    assert corrupted_paths[0].exists()

    # Seed user data first, then fail mid-`src_conn.backup()`: must clean incomplete
    # backup artifact, close source/dest handles, and preserve pre-existing backups & data.
    with sync_transaction(app_db) as seed_conn:
        seed_conn.execute("CREATE TABLE IF NOT EXISTS keep_data (v INT)")
        seed_conn.execute("INSERT INTO keep_data VALUES (42)")

    pre_existing_baks = set(tmp_path.glob("studio.bak.*"))
    backup_conn_closes: list[str] = []
    real_connect = sqlite3.connect

    class _TrackedSrcBackupConn(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            raise sqlite3.OperationalError("simulated I/O error mid-backup")

        def close(self) -> None:
            backup_conn_closes.append("src")
            super().close()

    def _spy_open_for_backup(db_path, **kwargs):
        return real_connect(
            str(db_path),
            isolation_level=None,
            check_same_thread=False,
            factory=_TrackedSrcBackupConn,
        )

    monkeypatch.setattr(migrations_mod, "open_sync_connection", _spy_open_for_backup)
    with pytest.raises(BackupVerificationError, match="Backup API failed"):
        run_app_migrations(app_db)
    monkeypatch.undo()

    # Source connection closed, incomplete backup cleaned up, pre-existing backups untouched
    assert "src" in backup_conn_closes
    assert pre_existing_baks == set(tmp_path.glob("studio.bak.*"))

    conn = open_sync_connection(app_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT v FROM keep_data").fetchone()[0] == 42
    finally:
        conn.close()
    assert corrupt_bak.exists()
    assert valid_bak.exists()

    # Negative user_version must fail with MigrationError before touching anything
    with sync_transaction(app_db) as neg_conn:
        neg_conn.execute("PRAGMA user_version = -1")
    with pytest.raises(MigrationError, match="negative"):
        run_app_migrations(app_db)


def test_shared_app_db_multi_store_init_and_dynamic_vec_dimension(tmp_path: Path):
    """Verify shared app DB across Stores regardless of order & vec dimension flexibility."""
    app_db = str(tmp_path / "shared_app.db")
    vec_db = str(tmp_path / "custom_vec.db")

    # Initialize SessionService FIRST (advances user_version to 1), then MemoryStore & TraceStore
    ss = SessionService(db_path=app_db)
    ms = MemoryStore(db_path=app_db)
    ts = TraceStore(db_path=app_db)

    sess = ss.create_session(title="Shared DB Session")
    ss.add_message(sess.session_id, "user", "hello")
    ms.add(MemoryItem(id="m1", category=MemoryCategory.FACT, content="shared disk fact"))
    tr = ConversationTrace(session_id=sess.session_id, run_id="r1", query="hi")
    tr.finish(output="done")
    ts.save_trace(tr)

    assert len(ss.get_messages(sess.session_id)) == 1
    assert ms.get("m1") is not None
    assert ts.get_trace(tr.trace_id) is not None

    # Vector store with custom dimension=32 must not fail from v0->v1 migration
    vs = SqliteVecStore(db_path=vec_db, dimension=32)
    chunk = DocumentChunk(chunk_id="c1", doc_path="doc.md", chunk_index=0, content="hello vec")
    vec = [0.25] * 32
    assert vs.add_chunks([chunk], [vec], file_hash="h1") == 1
    assert len(vs.search(vec, top_k=1)) == 1

    # Re-open on same DB reads WAL + v1 cleanly
    vs2 = SqliteVecStore(db_path=vec_db, dimension=32)
    assert vs2.get_file_hash("doc.md") == "h1"


def test_in_memory_stores_isolated_and_idempotent_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify two :memory: Stores are isolated, keep no disk files, and close cleanly."""
    monkeypatch.chdir(tmp_path)

    with SessionService(":memory:") as ss1, SessionService(":memory:") as ss2:
        s1 = ss1.create_session(title="S1")
        ss1.add_message(s1.session_id, "user", "hello in-memory")
        renamed = ss1.update_session_title(s1.session_id, "Renamed S1")
        assert renamed is not None and renamed.title == "Renamed S1"
        assert len(ss1.get_messages(s1.session_id)) == 1
        assert ss1.get_session(s1.session_id) is not None
        assert ss2.get_session(s1.session_id) is None

    # Re-check idempotent close and post-close guard on all 4 Stores (read & write)
    ss = SessionService(":memory:")
    ss.close()
    ss.close()
    assert ss._in_memory_conn is None
    with pytest.raises(RuntimeError, match="closed"):
        ss.create_session("after close")
    with pytest.raises(RuntimeError, match="closed"):
        ss.get_session("after close")

    ms1 = MemoryStore(":memory:")
    ms2 = MemoryStore(":memory:")
    ms1.add(MemoryItem(id="a", category=MemoryCategory.PREFERENCE, content="pref A"))
    assert ms1.get("a") is not None
    assert ms2.get("a") is None
    ms1.close()
    ms1.close()
    ms2.close()
    assert ms1._in_memory_conn is None
    with pytest.raises(RuntimeError, match="closed"):
        ms1.add(MemoryItem(id="b", category=MemoryCategory.FACT, content="closed"))
    with pytest.raises(RuntimeError, match="closed"):
        ms1.get("a")

    ts1 = TraceStore(":memory:")
    ts2 = TraceStore(":memory:")
    tr = ConversationTrace(session_id="s1", run_id="r1", query="q1")
    tr.finish(output="ok")
    ts1.save_trace(tr)
    assert ts1.get_trace(tr.trace_id) is not None
    assert ts2.get_trace(tr.trace_id) is None
    ts1.close()
    ts1.close()
    ts2.close()
    assert ts1._in_memory_conn is None
    with pytest.raises(RuntimeError, match="closed"):
        ts1.save_trace(tr)
    with pytest.raises(RuntimeError, match="closed"):
        ts1.list_traces()

    vs1 = SqliteVecStore(":memory:", dimension=4)
    vs2 = SqliteVecStore(":memory:", dimension=4)
    ch = DocumentChunk(chunk_id="k1", doc_path="f.txt", chunk_index=0, content="vec")
    vs1.add_chunks([ch], [[1.0, 0.0, 0.0, 0.0]], file_hash="fh")
    assert vs1.get_file_hash("f.txt") == "fh"
    assert vs2.get_file_hash("f.txt") is None
    vs1.close()
    vs1.close()
    vs2.close()
    assert vs1._in_memory_conn is None
    with pytest.raises(RuntimeError, match="closed"):
        vs1.add_chunks([ch], [[1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(RuntimeError, match="closed"):
        vs1.get_file_hash("f.txt")

    assert list(tmp_path.iterdir()) == []


def test_sync_transaction_forbids_nested_transaction_without_breaking_outer(
    tmp_path: Path,
):
    """Verify NestedTransactionError leaves outer transaction and :memory: connection intact."""
    with SessionService(":memory:") as ss:
        with sync_transaction(ss.db_path, in_memory_conn=ss._in_memory_conn) as outer:
            outer.execute(
                "INSERT INTO chat_sessions (session_id, title, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
                ("outer_sess", "Outer", 1.0, 1.0, "{}"),
            )
            with (
                pytest.raises(NestedTransactionError),
                sync_transaction(ss.db_path, in_memory_conn=ss._in_memory_conn),
            ):
                pass
            with pytest.raises(NestedTransactionError):
                ss.create_session(title="Nested")
            assert outer.in_transaction is True
        assert ss.get_session("outer_sess") is not None

    for store, op, tx_kwargs in [
        (
            MemoryStore(":memory:"),
            lambda s: s.add(
                MemoryItem(id="m_nest", category=MemoryCategory.FACT, content="no")
            ),
            {},
        ),
        (
            TraceStore(":memory:"),
            lambda s: s.save_trace(
                ConversationTrace(session_id="s", run_id="r", query="q")
            ),
            {},
        ),
        (
            SqliteVecStore(":memory:", dimension=4),
            lambda s: s.delete_document("doc.md"),
            {"enable_foreign_keys": False, "enable_sqlite_vec": True},
        ),
    ]:
        with (
            store as s,
            sync_transaction(s.db_path, in_memory_conn=s._in_memory_conn, **tx_kwargs),
            pytest.raises(NestedTransactionError),
        ):
            op(s)

    # Disk rollback cleans uncommitted write and closes connection
    disk_db = tmp_path / "rb.db"
    initialize_sqlite_database(disk_db)
    with sync_transaction(disk_db) as conn:
        conn.execute("CREATE TABLE items (id INT PRIMARY KEY)")
    with pytest.raises(ValueError, match="abort tx"), sync_transaction(disk_db) as conn:
        conn.execute("INSERT INTO items VALUES (1)")
        raise ValueError("abort tx")
    verify_conn = open_sync_connection(disk_db)
    try:
        assert verify_conn.in_transaction is False
        assert verify_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        verify_conn.close()


def test_sync_transaction_commit_and_rollback_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify connection cleanup and original error preservation when COMMIT or ROLLBACK fails."""
    disk_db = tmp_path / "commit_rb_fail.db"
    initialize_sqlite_database(disk_db)
    with sync_transaction(disk_db) as conn:
        conn.execute("CREATE TABLE items (id INT PRIMARY KEY)")

    real_connect = sqlite3.connect
    tracked_records: list[dict[str, object]] = []
    fail_on_sql: dict[str, str | None] = {"target": "COMMIT"}

    class _TxFaultConn(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._rec: dict[str, object] = {
                "conn": self,
                "closed": False,
                "in_tx_at_close": None,
            }
            tracked_records.append(self._rec)

        def execute(self, sql: str, *args, **kwargs):
            if fail_on_sql["target"] and sql.strip().upper() == fail_on_sql["target"]:
                raise sqlite3.OperationalError(f"simulated {fail_on_sql['target']} failure")
            return super().execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._rec["in_tx_at_close"] = self.in_transaction
            try:
                super().close()
            finally:
                self._rec["closed"] = True

    def _spy_connect(*args, **kwargs):
        kwargs["factory"] = _TxFaultConn
        return real_connect(*args, **kwargs)

    # 1. Disk COMMIT failure -> rolls back, closes conn, leaves zero uncommitted rows
    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    with (
        pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"),
        sync_transaction(disk_db) as tx_conn,
    ):
        tx_conn.execute("INSERT INTO items VALUES (1)")
    monkeypatch.undo()

    assert len(tracked_records) == 1
    assert tracked_records[0]["closed"] is True
    assert tracked_records[0]["in_tx_at_close"] is False
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        tracked_records[0]["conn"].execute("SELECT 1")  # type: ignore[union-attr]

    check = open_sync_connection(disk_db)
    try:
        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        check.close()

    # 2. In-memory COMMIT failure -> rolls back tx cleanly and does NOT close shared conn
    mem_conn = real_connect(":memory:", isolation_level=None, factory=_TxFaultConn)
    try:
        mem_conn.execute("CREATE TABLE mem_items (id INT)")
        tracked_records[-1]["closed"] = False
        with (
            pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"),
            sync_transaction(":memory:", in_memory_conn=mem_conn),
        ):
            mem_conn.execute("INSERT INTO mem_items VALUES (7)")
        assert mem_conn.in_transaction is False
        assert tracked_records[-1]["closed"] is False
        assert mem_conn.execute("SELECT COUNT(*) FROM mem_items").fetchone()[0] == 0
    finally:
        mem_conn.close()

    # 3. Disk ROLLBACK failure -> closes disk conn AND preserves original application error
    fail_on_sql["target"] = "ROLLBACK"
    tracked_records.clear()
    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    with pytest.raises(ValueError, match="primary app error"), sync_transaction(disk_db) as tx_conn:
        tx_conn.execute("INSERT INTO items VALUES (2)")
        raise ValueError("primary app error")
    monkeypatch.undo()

    assert len(tracked_records) == 1
    assert tracked_records[0]["closed"] is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        tracked_records[0]["conn"].execute("SELECT 1")  # type: ignore[union-attr]


def test_sqlite_vec_extension_lockdown_and_dedup(monkeypatch: pytest.MonkeyPatch):
    """Verify enable_load_extension is disabled after load (even on error) and dedup works."""
    load_calls = 0
    real_load = sqlite_vec.load

    def _counting_load(conn: sqlite3.Connection) -> None:
        nonlocal load_calls
        load_calls += 1
        real_load(conn)

    monkeypatch.setattr(sqlite_vec, "load", _counting_load)

    conn = open_sync_connection(":memory:", enable_foreign_keys=False, enable_sqlite_vec=True)
    try:
        assert conn.execute("SELECT vec_version()").fetchone()[0]
        # Repeat load must skip via pragma_function_list
        load_sqlite_vec(conn)
        assert load_calls == 1
        # Extension loading must be disabled afterward
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            conn.load_extension("nonexistent_ext")
    finally:
        conn.close()

    # Now test failure path locks down extension permission
    bad_conn = open_sync_connection(":memory:", enable_foreign_keys=False, enable_sqlite_vec=False)
    try:
        def _failing_load(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("boom during load")

        monkeypatch.setattr(sqlite_vec, "load", _failing_load)
        with pytest.raises(RuntimeError, match="boom during load"):
            load_sqlite_vec(bad_conn)

        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            bad_conn.load_extension("nonexistent_ext")
    finally:
        bad_conn.close()


def test_memory_store_init_failure_does_not_leak_disk_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify every created connection (and specifically the failing DDL connection) closes cleanly."""
    db_file = tmp_path / "leak_check.db"
    initialize_sqlite_database(db_file)
    run_app_migrations(db_file)
    # Remove ltm_memories so MemoryStore._init_db enters DDL transaction path
    with sync_transaction(db_file) as conn:
        conn.execute("DROP TABLE ltm_memories")

    real_connect = sqlite3.connect
    records: list[dict[str, object]] = []

    class _TrackedInitConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._rec: dict[str, object] = {
                "conn": self,
                "closed": False,
                "hit_target_ddl": False,
                "in_tx_at_close": None,
            }
            records.append(self._rec)

        def execute(self, sql: str, *args, **kwargs):
            if "CREATE TABLE IF NOT EXISTS ltm_memories" in sql:
                self._rec["hit_target_ddl"] = True
                raise sqlite3.OperationalError("simulated init DDL error")
            return super().execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._rec["in_tx_at_close"] = self.in_transaction
            try:
                super().close()
            finally:
                self._rec["closed"] = True

    def _spy_connect(*args, **kwargs):
        kwargs["factory"] = _TrackedInitConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated init DDL error"):
        MemoryStore(db_path=str(db_file))
    monkeypatch.undo()

    # Every disk connection opened during MemoryStore.__init__ must be accounted for & closed
    assert len(records) >= 4
    target_conns = [r for r in records if r["hit_target_ddl"]]
    assert len(target_conns) == 1
    target = target_conns[0]
    assert target["closed"] is True
    assert target["in_tx_at_close"] is False
    assert [r for r in records if not r["closed"]] == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        target["conn"].execute("SELECT 1")  # type: ignore[union-attr]

    # Verify DB has no lingering write lock or active transaction afterward
    with sync_transaction(db_file) as verify_conn:
        verify_conn.execute("CREATE TABLE probe_post_err (id INT)")
        assert (
            verify_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ltm_memories'"
            ).fetchone()
            is None
        )


def test_concurrent_multithread_in_memory_and_disk_writes(tmp_path: Path):
    """Verify multi-threaded mixed writes succeed on shared :memory: and disk Stores."""
    # 1. Shared :memory: MemoryStore across 6 threads -> RLock avoids false NestedTransactionError
    with MemoryStore(":memory:") as mem_store:
        workers = 6
        per_worker = 10
        barrier = threading.Barrier(workers)

        def _mem_worker(idx: int) -> None:
            barrier.wait(timeout=5.0)
            for j in range(per_worker):
                mem_store.add(
                    MemoryItem(
                        id=f"m_{idx}_{j}",
                        category=MemoryCategory.PROJECT,
                        content=f"content {idx}-{j}",
                    )
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_mem_worker, i) for i in range(workers)]
            for fut in futures:
                fut.result()

        assert len(mem_store.list(limit=100)) == workers * per_worker

    # 2. Shared :memory: SqliteVecStore across 4 threads (concurrent writes + searches)
    with SqliteVecStore(":memory:", dimension=8) as mem_vec:
        vec_workers = 4
        vec_barrier = threading.Barrier(vec_workers)

        def _mem_vec_worker(idx: int) -> None:
            vec_barrier.wait(timeout=5.0)
            vec = [float(idx + 1)] + [0.1] * 7
            ch = DocumentChunk(
                chunk_id=f"mvc_{idx}",
                doc_path=f"mem_doc_{idx}.md",
                chunk_index=0,
                content=f"memory vec {idx}",
            )
            assert mem_vec.add_chunks([ch], [vec], file_hash=f"mh_{idx}") == 1
            matches = mem_vec.search(vec, top_k=2)
            assert len(matches) >= 1

        with ThreadPoolExecutor(max_workers=vec_workers) as pool:
            for fut in [pool.submit(_mem_vec_worker, i) for i in range(vec_workers)]:
                fut.result()

        assert len(mem_vec.list_indexed_files()) == vec_workers

    # 3. Concurrent multi-Store initialization & mixed writes on fresh shared disk DB
    disk_db = str(tmp_path / "concurrent_app.db")
    disk_workers = 6
    disk_barrier = threading.Barrier(disk_workers)

    def _disk_worker(idx: int) -> None:
        disk_barrier.wait(timeout=5.0)
        ss = SessionService(db_path=disk_db)
        ms = MemoryStore(db_path=disk_db)
        ts = TraceStore(db_path=disk_db)
        sess = ss.create_session(title=f"Sess {idx}", session_id=f"sess_t_{idx}")
        ss.add_message(sess.session_id, "user", f"msg from {idx}")
        ms.add(
            MemoryItem(
                id=f"dm_{idx}",
                category=MemoryCategory.FACT,
                content=f"disk fact {idx}",
            )
        )
        tr = ConversationTrace(session_id=sess.session_id, run_id=f"r_{idx}", query="q")
        tr.finish(output="ok")
        ts.save_trace(tr)

    with ThreadPoolExecutor(max_workers=disk_workers) as pool:
        futures = [pool.submit(_disk_worker, i) for i in range(disk_workers)]
        for fut in futures:
            fut.result()

    ss_check = SessionService(db_path=disk_db)
    ms_check = MemoryStore(db_path=disk_db)
    ts_check = TraceStore(db_path=disk_db)
    assert len(ss_check.list_sessions()) == disk_workers
    assert len(ms_check.list(limit=50)) == disk_workers
    assert len(ts_check.list_traces(limit=50)) == disk_workers
    backups = list(tmp_path.glob("concurrent_app.bak.v0_to_v1.*.db"))
    assert len(backups) == 1

    # 4. Concurrent SqliteVecStore initialization & writes/searches on fresh disk vector DB
    disk_vec_db = str(tmp_path / "concurrent_vec.db")
    vec_disk_workers = 4
    vec_disk_barrier = threading.Barrier(vec_disk_workers)

    def _disk_vec_worker(idx: int) -> None:
        vec_disk_barrier.wait(timeout=5.0)
        vs = SqliteVecStore(db_path=disk_vec_db, dimension=8)
        vec = [float(idx + 1)] + [0.25] * 7
        ch = DocumentChunk(
            chunk_id=f"dvc_{idx}",
            doc_path=f"disk_doc_{idx}.md",
            chunk_index=0,
            content=f"disk vec {idx}",
        )
        assert vs.add_chunks([ch], [vec], file_hash=f"dh_{idx}") == 1
        assert len(vs.search(vec, top_k=2)) >= 1

    with ThreadPoolExecutor(max_workers=vec_disk_workers) as pool:
        for fut in [pool.submit(_disk_vec_worker, i) for i in range(vec_disk_workers)]:
            fut.result()

    vs_check = SqliteVecStore(db_path=disk_vec_db, dimension=8)
    assert len(vs_check.list_indexed_files()) == vec_disk_workers
    vec_backups = list(tmp_path.glob("concurrent_vec.bak.v0_to_v1.*.db"))
    assert len(vec_backups) == 1
