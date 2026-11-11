# Sprint 1C Phase 1 Remediation — 独立审计证据包

> **审计前提说明**：Phase 1 Remediation 改动已提交于 `main` 分支 `commit e2144791b7c8b9f1d2d5a407f57daab43443d12a`（相对前次审计基线 `9005e5e99fd2874a4b3c18d19199ee9343d1bafa`）。当前工作区除本证据文档外无任何未提交修改、未做重置或提交。Linux/POSIX 环境尚未执行实机验证，本报告不宣称 Linux/POSIX 兼容性通过。

---

## 1. `git status --short`（创建本文件前实时状态）

- 当前分支：`main`
- 当前 HEAD：`e2144791b7c8b9f1d2d5a407f57daab43443d12a`

```text
（空输出 —— 工作区干净，Phase 1 Remediation 已在提交 e2144791b7c8b9f1d2d5a407f57daab43443d12a 中）
```

---

## 2. `git diff --stat`

### 2.1 对当前 `HEAD` (`e214479`) 的工作区未提交差异

```text
（空输出）
```

### 2.2 本轮 Remediation 提交相对基线 `9005e5e..e214479` 的完整差异统计

```text
 src/jarvis_studio/core/persistence/migrations.py |  12 +-
 src/jarvis_studio/core/persistence/sqlite.py     |   5 +-
 tests/conftest.py                                |  53 ++++
 tests/test_persistence_sqlite.py                 | 331 ++++++++++++++++++++---
 4 files changed, 364 insertions(+), 37 deletions(-)
```

---

## 3. `git diff -- tests/conftest.py`（`git diff 9005e5e..e214479 -- tests/conftest.py`）

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 7adc8d1..1cc4f99 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -1,9 +1,62 @@
 """Pytest fixtures and configuration."""
 
 import os
+import shutil
+from pathlib import Path
+
+import pytest
 
 # Pre-set test environment variables before tests run
 os.environ["OPENAI_API_KEY"] = "sk-test-mock-key"
 os.environ["CHECKPOINT_BACKEND"] = "memory"
 os.environ["WORKSPACE_ROOT"] = "./data/test_workspace"
 os.environ["HITL_ENABLED"] = "true"
+
+
+def _reset_runtime_singletons(*, close_stores: bool) -> None:
+    """Reset cached settings and persistence singletons for test isolation."""
+    import jarvis_studio.config.settings as settings_mod
+    import jarvis_studio.core.observability.trace_logger as trace_logger_mod
+    import jarvis_studio.memory.manager as memory_manager_mod
+    import jarvis_studio.service.agent_service as agent_service_mod
+    import jarvis_studio.service.session_service as session_service_mod
+
+    if close_stores:
+        if session_service_mod._global_session_service is not None:
+            session_service_mod._global_session_service.close()
+        if memory_manager_mod._global_memory_manager is not None:
+            memory_manager_mod._global_memory_manager.store.close()
+        if trace_logger_mod._global_trace_store is not None:
+            trace_logger_mod._global_trace_store.close()
+
+    session_service_mod._global_session_service = None
+    memory_manager_mod._global_memory_manager = None
+    trace_logger_mod._global_trace_store = None
+    agent_service_mod._global_agent_service = None
+    settings_mod._global_settings = None
+
+
+@pytest.fixture(scope="session", autouse=True)
+def _isolate_test_databases(tmp_path_factory: pytest.TempPathFactory):
+    """Redirect default app and vector SQLite paths to an isolated temp directory."""
+    db_dir: Path = tmp_path_factory.mktemp("jarvis_test_dbs")
+    prev_sqlite_path = os.environ.get("SQLITE_DB_PATH")
+    prev_vec_path = os.environ.get("VECTOR_DB_PATH")
+
+    os.environ["SQLITE_DB_PATH"] = str(db_dir / "jarvis_studio.db")
+    os.environ["VECTOR_DB_PATH"] = str(db_dir / "jarvis_vec.db")
+    _reset_runtime_singletons(close_stores=True)
+
+    try:
+        yield db_dir
+    finally:
+        _reset_runtime_singletons(close_stores=True)
+        if prev_sqlite_path is None:
+            os.environ.pop("SQLITE_DB_PATH", None)
+        else:
+            os.environ["SQLITE_DB_PATH"] = prev_sqlite_path
+        if prev_vec_path is None:
+            os.environ.pop("VECTOR_DB_PATH", None)
+        else:
+            os.environ["VECTOR_DB_PATH"] = prev_vec_path
+        shutil.rmtree(db_dir, ignore_errors=True)
```

---

## 4. `git diff -- src/jarvis_studio/core/persistence/sqlite.py src/jarvis_studio/core/persistence/migrations.py`（`git diff 9005e5e..e214479`）

```diff
diff --git a/src/jarvis_studio/core/persistence/migrations.py b/src/jarvis_studio/core/persistence/migrations.py
index d578494..5584525 100644
--- a/src/jarvis_studio/core/persistence/migrations.py
+++ b/src/jarvis_studio/core/persistence/migrations.py
@@ -223,11 +223,13 @@ def create_verified_backup(
             f"SQLite Backup API failed for {src_path} -> {bak_path}: {exc}"
         ) from exc
     finally:
-        if dst_conn is not None:
-            dst_conn.close()
-        src_conn.close()
-        if not backup_completed and bak_path.exists():
-            bak_path.unlink(missing_ok=True)
+        try:
+            if dst_conn is not None:
+                dst_conn.close()
+        finally:
+            src_conn.close()
+            if not backup_completed and bak_path.exists():
+                bak_path.unlink(missing_ok=True)
 
     verify_sqlite_backup(bak_path, expected_version=from_version)
     return bak_path
diff --git a/src/jarvis_studio/core/persistence/sqlite.py b/src/jarvis_studio/core/persistence/sqlite.py
index 3e0075f..e9bfa21 100644
--- a/src/jarvis_studio/core/persistence/sqlite.py
+++ b/src/jarvis_studio/core/persistence/sqlite.py
@@ -30,7 +30,10 @@ _PATH_LOCKS: dict[str, threading.RLock] = {}
 
 def get_path_lock(db_path: str | Path) -> threading.RLock:
     """Return an in-process re-entrant lock keyed by canonical resolved DB path."""
-    key = str(Path(normalize_db_path(db_path)).resolve())
+    normalized = normalize_db_path(db_path)
+    if normalized == ":memory:":
+        raise ValueError("get_path_lock does not apply to ':memory:' databases")
+    key = str(Path(normalized).resolve())
     with _PATH_LOCKS_GUARD:
         lock = _PATH_LOCKS.get(key)
         if lock is None:
```

---

## 5. `git diff -- tests/test_persistence_sqlite.py`（`git diff 9005e5e..e214479 -- tests/test_persistence_sqlite.py`）

```diff
diff --git a/tests/test_persistence_sqlite.py b/tests/test_persistence_sqlite.py
index 7f365ef..8607d72 100644
--- a/tests/test_persistence_sqlite.py
+++ b/tests/test_persistence_sqlite.py
@@ -10,6 +10,7 @@ from pathlib import Path
 import pytest
 import sqlite_vec
 
+from jarvis_studio.config.settings import get_settings
 from jarvis_studio.core.observability.trace_logger import ConversationTrace, TraceStore
 from jarvis_studio.core.persistence import (
     APP_SCHEMA_VERSION,
@@ -27,6 +28,7 @@ from jarvis_studio.core.persistence import (
     verify_sqlite_backup,
 )
 from jarvis_studio.core.persistence import migrations as migrations_mod
+from jarvis_studio.core.persistence.sqlite import get_path_lock
 from jarvis_studio.memory.models import MemoryCategory, MemoryItem
 from jarvis_studio.memory.store import MemoryStore
 from jarvis_studio.rag.splitter import DocumentChunk
@@ -84,6 +86,42 @@ def test_open_sync_connection_pragmas_and_blank_path_guard(tmp_path: Path):
     finally:
         mem_conn.close()
     assert not (Path.cwd() / ":memory:").exists()
+    with pytest.raises(ValueError, match="does not apply to ':memory:'"):
+        get_path_lock(":memory:")
+
+
+def test_default_stores_resolve_to_isolated_temp_databases():
+    """Verify default Store construction resolves to pytest temp DBs, never project `data/`."""
+    settings = get_settings()
+    project_app_db = Path("data/jarvis_studio.db").resolve()
+    project_vec_db = Path("data/jarvis_vec.db").resolve()
+
+    assert Path(settings.storage.sqlite_path).resolve() != project_app_db
+    assert Path(settings.rag.vector_db_path).resolve() != project_vec_db
+
+    ss = SessionService()
+    ms = MemoryStore()
+    ts = TraceStore()
+    vs = SqliteVecStore()
+    try:
+        for store, forbidden in [
+            (ss, project_app_db),
+            (ms, project_app_db),
+            (ts, project_app_db),
+            (vs, project_vec_db),
+        ]:
+            with store._db.read_connection() as conn:
+                db_list = conn.execute("PRAGMA database_list").fetchall()
+            main_file = next(r["file"] for r in db_list if r["name"] == "main")
+            resolved = Path(main_file).resolve()
+            assert resolved != forbidden
+            assert resolved.parent == Path(settings.storage.sqlite_path).resolve().parent
+            assert resolved.exists()
+    finally:
+        vs.close()
+        ts.close()
+        ms.close()
+        ss.close()
 
 
 def test_open_sync_connection_closes_handle_on_setup_failure(
@@ -184,24 +222,72 @@ def test_backup_verification_blocks_corrupted_snapshots_and_preserves_old_backup
     with pytest.raises(BackupVerificationError, match="user_version mismatch"):
         verify_sqlite_backup(valid_bak, expected_version=0)
 
-    # Simulate backup verification raising during migration -> blocks migration & preserves files
-    def _fail_backup(*args, **kwargs):
-        raise BackupVerificationError("corrupt backup detected")
+    # Corrupt backup generated during real `create_verified_backup -> verify_sqlite_backup`
+    # must raise BackupVerificationError, abort migration, and leave user_version=0.
+    corrupted_paths: list[Path] = []
+    orig_backup_fn = migrations_mod.verify_sqlite_backup
 
-    monkeypatch.setattr(migrations_mod, "create_verified_backup", _fail_backup)
-    with pytest.raises(BackupVerificationError, match="corrupt backup detected"):
+    def _corrupt_snapshot_before_verify(backup_path, *, expected_version=None):
+        p = Path(backup_path)
+        corrupted_paths.append(p)
+        p.write_bytes(b"corrupted during snapshot write")
+        return orig_backup_fn(p, expected_version=expected_version)
+
+    monkeypatch.setattr(
+        migrations_mod, "verify_sqlite_backup", _corrupt_snapshot_before_verify
+    )
+    with pytest.raises(BackupVerificationError):
         run_app_migrations(app_db)
+    monkeypatch.undo()
+
+    assert len(corrupted_paths) == 1
+    assert corrupted_paths[0].exists()
+
+    # Seed user data first, then fail mid-`src_conn.backup()`: must clean incomplete
+    # backup artifact, close source/dest handles, and preserve pre-existing backups & data.
+    with sync_transaction(app_db) as seed_conn:
+        seed_conn.execute("CREATE TABLE IF NOT EXISTS keep_data (v INT)")
+        seed_conn.execute("INSERT INTO keep_data VALUES (42)")
+
+    pre_existing_baks = set(tmp_path.glob("studio.bak.*"))
+    backup_conn_closes: list[str] = []
+    real_connect = sqlite3.connect
+
+    class _TrackedSrcBackupConn(sqlite3.Connection):
+        def backup(self, target, *args, **kwargs):
+            raise sqlite3.OperationalError("simulated I/O error mid-backup")
+
+        def close(self) -> None:
+            backup_conn_closes.append("src")
+            super().close()
+
+    def _spy_open_for_backup(db_path, **kwargs):
+        return real_connect(
+            str(db_path),
+            isolation_level=None,
+            check_same_thread=False,
+            factory=_TrackedSrcBackupConn,
+        )
+
+    monkeypatch.setattr(migrations_mod, "open_sync_connection", _spy_open_for_backup)
+    with pytest.raises(BackupVerificationError, match="Backup API failed"):
+        run_app_migrations(app_db)
+    monkeypatch.undo()
+
+    # Source connection closed, incomplete backup cleaned up, pre-existing backups untouched
+    assert "src" in backup_conn_closes
+    assert pre_existing_baks == set(tmp_path.glob("studio.bak.*"))
 
     conn = open_sync_connection(app_db)
     try:
         assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
+        assert conn.execute("SELECT v FROM keep_data").fetchone()[0] == 42
     finally:
         conn.close()
     assert corrupt_bak.exists()
     assert valid_bak.exists()
 
     # Negative user_version must fail with MigrationError before touching anything
-    monkeypatch.undo()
     with sync_transaction(app_db) as neg_conn:
         neg_conn.execute("PRAGMA user_version = -1")
     with pytest.raises(MigrationError, match="negative"):
@@ -256,13 +342,15 @@ def test_in_memory_stores_isolated_and_idempotent_close(
         assert ss1.get_session(s1.session_id) is not None
         assert ss2.get_session(s1.session_id) is None
 
-    # Re-check idempotent close and post-close guard on all 4 Stores
+    # Re-check idempotent close and post-close guard on all 4 Stores (read & write)
     ss = SessionService(":memory:")
     ss.close()
     ss.close()
     assert ss._in_memory_conn is None
     with pytest.raises(RuntimeError, match="closed"):
         ss.create_session("after close")
+    with pytest.raises(RuntimeError, match="closed"):
+        ss.get_session("after close")
 
     ms1 = MemoryStore(":memory:")
     ms2 = MemoryStore(":memory:")
@@ -273,6 +361,10 @@ def test_in_memory_stores_isolated_and_idempotent_close(
     ms1.close()
     ms2.close()
     assert ms1._in_memory_conn is None
+    with pytest.raises(RuntimeError, match="closed"):
+        ms1.add(MemoryItem(id="b", category=MemoryCategory.FACT, content="closed"))
+    with pytest.raises(RuntimeError, match="closed"):
+        ms1.get("a")
 
     ts1 = TraceStore(":memory:")
     ts2 = TraceStore(":memory:")
@@ -282,8 +374,13 @@ def test_in_memory_stores_isolated_and_idempotent_close(
     assert ts1.get_trace(tr.trace_id) is not None
     assert ts2.get_trace(tr.trace_id) is None
     ts1.close()
+    ts1.close()
     ts2.close()
     assert ts1._in_memory_conn is None
+    with pytest.raises(RuntimeError, match="closed"):
+        ts1.save_trace(tr)
+    with pytest.raises(RuntimeError, match="closed"):
+        ts1.list_traces()
 
     vs1 = SqliteVecStore(":memory:", dimension=4)
     vs2 = SqliteVecStore(":memory:", dimension=4)
@@ -292,8 +389,13 @@ def test_in_memory_stores_isolated_and_idempotent_close(
     assert vs1.get_file_hash("f.txt") == "fh"
     assert vs2.get_file_hash("f.txt") is None
     vs1.close()
+    vs1.close()
     vs2.close()
     assert vs1._in_memory_conn is None
+    with pytest.raises(RuntimeError, match="closed"):
+        vs1.add_chunks([ch], [[1.0, 0.0, 0.0, 0.0]])
+    with pytest.raises(RuntimeError, match="closed"):
+        vs1.get_file_hash("f.txt")
 
     assert list(tmp_path.iterdir()) == []
 
@@ -362,6 +464,97 @@ def test_sync_transaction_forbids_nested_transaction_without_breaking_outer(
         verify_conn.close()
 
 
+def test_sync_transaction_commit_and_rollback_failure_cleanup(
+    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+):
+    """Verify connection cleanup and original error preservation when COMMIT or ROLLBACK fails."""
+    disk_db = tmp_path / "commit_rb_fail.db"
+    initialize_sqlite_database(disk_db)
+    with sync_transaction(disk_db) as conn:
+        conn.execute("CREATE TABLE items (id INT PRIMARY KEY)")
+
+    real_connect = sqlite3.connect
+    tracked_records: list[dict[str, object]] = []
+    fail_on_sql: dict[str, str | None] = {"target": "COMMIT"}
+
+    class _TxFaultConn(sqlite3.Connection):
+        def __init__(self, *args, **kwargs):
+            super().__init__(*args, **kwargs)
+            self._rec: dict[str, object] = {
+                "conn": self,
+                "closed": False,
+                "in_tx_at_close": None,
+            }
+            tracked_records.append(self._rec)
+
+        def execute(self, sql: str, *args, **kwargs):
+            if fail_on_sql["target"] and sql.strip().upper() == fail_on_sql["target"]:
+                raise sqlite3.OperationalError(f"simulated {fail_on_sql['target']} failure")
+            return super().execute(sql, *args, **kwargs)
+
+        def close(self) -> None:
+            self._rec["in_tx_at_close"] = self.in_transaction
+            try:
+                super().close()
+            finally:
+                self._rec["closed"] = True
+
+    def _spy_connect(*args, **kwargs):
+        kwargs["factory"] = _TxFaultConn
+        return real_connect(*args, **kwargs)
+
+    # 1. Disk COMMIT failure -> rolls back, closes conn, leaves zero uncommitted rows
+    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
+    with (
+        pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"),
+        sync_transaction(disk_db) as tx_conn,
+    ):
+        tx_conn.execute("INSERT INTO items VALUES (1)")
+    monkeypatch.undo()
+
+    assert len(tracked_records) == 1
+    assert tracked_records[0]["closed"] is True
+    assert tracked_records[0]["in_tx_at_close"] is False
+    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
+        tracked_records[0]["conn"].execute("SELECT 1")  # type: ignore[union-attr]
+
+    check = open_sync_connection(disk_db)
+    try:
+        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
+    finally:
+        check.close()
+
+    # 2. In-memory COMMIT failure -> rolls back tx cleanly and does NOT close shared conn
+    mem_conn = real_connect(":memory:", isolation_level=None, factory=_TxFaultConn)
+    try:
+        mem_conn.execute("CREATE TABLE mem_items (id INT)")
+        tracked_records[-1]["closed"] = False
+        with (
+            pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"),
+            sync_transaction(":memory:", in_memory_conn=mem_conn),
+        ):
+            mem_conn.execute("INSERT INTO mem_items VALUES (7)")
+        assert mem_conn.in_transaction is False
+        assert tracked_records[-1]["closed"] is False
+        assert mem_conn.execute("SELECT COUNT(*) FROM mem_items").fetchone()[0] == 0
+    finally:
+        mem_conn.close()
+
+    # 3. Disk ROLLBACK failure -> closes disk conn AND preserves original application error
+    fail_on_sql["target"] = "ROLLBACK"
+    tracked_records.clear()
+    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
+    with pytest.raises(ValueError, match="primary app error"), sync_transaction(disk_db) as tx_conn:
+        tx_conn.execute("INSERT INTO items VALUES (2)")
+        raise ValueError("primary app error")
+    monkeypatch.undo()
+
+    assert len(tracked_records) == 1
+    assert tracked_records[0]["closed"] is True
+    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
+        tracked_records[0]["conn"].execute("SELECT 1")  # type: ignore[union-attr]
+
+
 def test_sqlite_vec_extension_lockdown_and_dedup(monkeypatch: pytest.MonkeyPatch):
     """Verify enable_load_extension is disabled after load (even on error) and dedup works."""
     load_calls = 0
@@ -405,50 +598,75 @@ def test_sqlite_vec_extension_lockdown_and_dedup(monkeypatch: pytest.MonkeyPatch
 def test_memory_store_init_failure_does_not_leak_disk_connection(
     tmp_path: Path, monkeypatch: pytest.MonkeyPatch
 ):
-    """Verify fixing MemoryStore._init_db() closes disk connection even when DDL fails."""
+    """Verify every created connection (and specifically the failing DDL connection) closes cleanly."""
     db_file = tmp_path / "leak_check.db"
     initialize_sqlite_database(db_file)
     run_app_migrations(db_file)
-    # Remove ltm_memories so MemoryStore._init_db enters DDL path
+    # Remove ltm_memories so MemoryStore._init_db enters DDL transaction path
     with sync_transaction(db_file) as conn:
         conn.execute("DROP TABLE ltm_memories")
 
-    closed_count = 0
-    real_open = open_sync_connection
-
-    class _SpyConn:
-        def __init__(self, inner: sqlite3.Connection):
-            self._inner = inner
-
-        def __getattr__(self, name: str):
-            return getattr(self._inner, name)
+    real_connect = sqlite3.connect
+    records: list[dict[str, object]] = []
+
+    class _TrackedInitConnection(sqlite3.Connection):
+        def __init__(self, *args, **kwargs):
+            super().__init__(*args, **kwargs)
+            self._rec: dict[str, object] = {
+                "conn": self,
+                "closed": False,
+                "hit_target_ddl": False,
+                "in_tx_at_close": None,
+            }
+            records.append(self._rec)
 
         def execute(self, sql: str, *args, **kwargs):
             if "CREATE TABLE IF NOT EXISTS ltm_memories" in sql:
+                self._rec["hit_target_ddl"] = True
                 raise sqlite3.OperationalError("simulated init DDL error")
-            return self._inner.execute(sql, *args, **kwargs)
+            return super().execute(sql, *args, **kwargs)
 
         def close(self) -> None:
-            nonlocal closed_count
-            closed_count += 1
-            self._inner.close()
-
-    def _spy_open(*args, **kwargs):
-        return _SpyConn(real_open(*args, **kwargs))
+            self._rec["in_tx_at_close"] = self.in_transaction
+            try:
+                super().close()
+            finally:
+                self._rec["closed"] = True
 
-    monkeypatch.setattr(
-        "jarvis_studio.core.persistence.sqlite.open_sync_connection", _spy_open
-    )
+    def _spy_connect(*args, **kwargs):
+        kwargs["factory"] = _TrackedInitConnection
+        return real_connect(*args, **kwargs)
 
+    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
     with pytest.raises(sqlite3.OperationalError, match="simulated init DDL error"):
         MemoryStore(db_path=str(db_file))
+    monkeypatch.undo()
 
-    assert closed_count >= 1
+    # Every disk connection opened during MemoryStore.__init__ must be accounted for & closed
+    assert len(records) >= 4
+    target_conns = [r for r in records if r["hit_target_ddl"]]
+    assert len(target_conns) == 1
+    target = target_conns[0]
+    assert target["closed"] is True
+    assert target["in_tx_at_close"] is False
+    assert [r for r in records if not r["closed"]] == []
+    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
+        target["conn"].execute("SELECT 1")  # type: ignore[union-attr]
+
+    # Verify DB has no lingering write lock or active transaction afterward
+    with sync_transaction(db_file) as verify_conn:
+        verify_conn.execute("CREATE TABLE probe_post_err (id INT)")
+        assert (
+            verify_conn.execute(
+                "SELECT name FROM sqlite_master WHERE type='table' AND name='ltm_memories'"
+            ).fetchone()
+            is None
+        )
 
 
 def test_concurrent_multithread_in_memory_and_disk_writes(tmp_path: Path):
     """Verify multi-threaded mixed writes succeed on shared :memory: and disk Stores."""
-    # 1. Shared :memory: Store across 6 threads -> RLock must avoid false NestedTransactionError
+    # 1. Shared :memory: MemoryStore across 6 threads -> RLock avoids false NestedTransactionError
     with MemoryStore(":memory:") as mem_store:
         workers = 6
         per_worker = 10
@@ -472,7 +690,31 @@ def test_concurrent_multithread_in_memory_and_disk_writes(tmp_path: Path):
 
         assert len(mem_store.list(limit=100)) == workers * per_worker
 
-    # 2. Concurrent multi-Store initialization & mixed writes on fresh shared disk DB
+    # 2. Shared :memory: SqliteVecStore across 4 threads (concurrent writes + searches)
+    with SqliteVecStore(":memory:", dimension=8) as mem_vec:
+        vec_workers = 4
+        vec_barrier = threading.Barrier(vec_workers)
+
+        def _mem_vec_worker(idx: int) -> None:
+            vec_barrier.wait(timeout=5.0)
+            vec = [float(idx + 1)] + [0.1] * 7
+            ch = DocumentChunk(
+                chunk_id=f"mvc_{idx}",
+                doc_path=f"mem_doc_{idx}.md",
+                chunk_index=0,
+                content=f"memory vec {idx}",
+            )
+            assert mem_vec.add_chunks([ch], [vec], file_hash=f"mh_{idx}") == 1
+            matches = mem_vec.search(vec, top_k=2)
+            assert len(matches) >= 1
+
+        with ThreadPoolExecutor(max_workers=vec_workers) as pool:
+            for fut in [pool.submit(_mem_vec_worker, i) for i in range(vec_workers)]:
+                fut.result()
+
+        assert len(mem_vec.list_indexed_files()) == vec_workers
+
+    # 3. Concurrent multi-Store initialization & mixed writes on fresh shared disk DB
     disk_db = str(tmp_path / "concurrent_app.db")
     disk_workers = 6
     disk_barrier = threading.Barrier(disk_workers)
@@ -508,3 +750,30 @@ def test_concurrent_multithread_in_memory_and_disk_writes(tmp_path: Path):
     assert len(ts_check.list_traces(limit=50)) == disk_workers
     backups = list(tmp_path.glob("concurrent_app.bak.v0_to_v1.*.db"))
     assert len(backups) == 1
+
+    # 4. Concurrent SqliteVecStore initialization & writes/searches on fresh disk vector DB
+    disk_vec_db = str(tmp_path / "concurrent_vec.db")
+    vec_disk_workers = 4
+    vec_disk_barrier = threading.Barrier(vec_disk_workers)
+
+    def _disk_vec_worker(idx: int) -> None:
+        vec_disk_barrier.wait(timeout=5.0)
+        vs = SqliteVecStore(db_path=disk_vec_db, dimension=8)
+        vec = [float(idx + 1)] + [0.25] * 7
+        ch = DocumentChunk(
+            chunk_id=f"dvc_{idx}",
+            doc_path=f"disk_doc_{idx}.md",
+            chunk_index=0,
+            content=f"disk vec {idx}",
+        )
+        assert vs.add_chunks([ch], [vec], file_hash=f"dh_{idx}") == 1
+        assert len(vs.search(vec, top_k=2)) >= 1
+
+    with ThreadPoolExecutor(max_workers=vec_disk_workers) as pool:
+        for fut in [pool.submit(_disk_vec_worker, i) for i in range(vec_disk_workers)]:
+            fut.result()
+
+    vs_check = SqliteVecStore(db_path=disk_vec_db, dimension=8)
+    assert len(vs_check.list_indexed_files()) == vec_disk_workers
+    vec_backups = list(tmp_path.glob("concurrent_vec.bak.v0_to_v1.*.db"))
+    assert len(vec_backups) == 1
```

---

## 6. `tests/conftest.py` 完整文件内容（含第 7 项函数实现）

```python
"""Pytest fixtures and configuration."""

import os
import shutil
from pathlib import Path

import pytest

# Pre-set test environment variables before tests run
os.environ["OPENAI_API_KEY"] = "sk-test-mock-key"
os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["WORKSPACE_ROOT"] = "./data/test_workspace"
os.environ["HITL_ENABLED"] = "true"


def _reset_runtime_singletons(*, close_stores: bool) -> None:
    """Reset cached settings and persistence singletons for test isolation."""
    import jarvis_studio.config.settings as settings_mod
    import jarvis_studio.core.observability.trace_logger as trace_logger_mod
    import jarvis_studio.memory.manager as memory_manager_mod
    import jarvis_studio.service.agent_service as agent_service_mod
    import jarvis_studio.service.session_service as session_service_mod

    if close_stores:
        if session_service_mod._global_session_service is not None:
            session_service_mod._global_session_service.close()
        if memory_manager_mod._global_memory_manager is not None:
            memory_manager_mod._global_memory_manager.store.close()
        if trace_logger_mod._global_trace_store is not None:
            trace_logger_mod._global_trace_store.close()

    session_service_mod._global_session_service = None
    memory_manager_mod._global_memory_manager = None
    trace_logger_mod._global_trace_store = None
    agent_service_mod._global_agent_service = None
    settings_mod._global_settings = None


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_databases(tmp_path_factory: pytest.TempPathFactory):
    """Redirect default app and vector SQLite paths to an isolated temp directory."""
    db_dir: Path = tmp_path_factory.mktemp("jarvis_test_dbs")
    prev_sqlite_path = os.environ.get("SQLITE_DB_PATH")
    prev_vec_path = os.environ.get("VECTOR_DB_PATH")

    os.environ["SQLITE_DB_PATH"] = str(db_dir / "jarvis_studio.db")
    os.environ["VECTOR_DB_PATH"] = str(db_dir / "jarvis_vec.db")
    _reset_runtime_singletons(close_stores=True)

    try:
        yield db_dir
    finally:
        _reset_runtime_singletons(close_stores=True)
        if prev_sqlite_path is None:
            os.environ.pop("SQLITE_DB_PATH", None)
        else:
            os.environ["SQLITE_DB_PATH"] = prev_sqlite_path
        if prev_vec_path is None:
            os.environ.pop("VECTOR_DB_PATH", None)
        else:
            os.environ["VECTOR_DB_PATH"] = prev_vec_path
        shutil.rmtree(db_dir, ignore_errors=True)
```

---

## 7. `_reset_runtime_singletons()` 和 `_isolate_test_databases()` 完整实现

见第 6 节 [`tests/conftest.py:16-62`](file:///d:/myProject/JarvisStudio/tests/conftest.py#L16-L62)。

---

## 8. PA-02 连接追踪测试完整实现

位于 [`tests/test_persistence_sqlite.py:598-664`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L598-L664)：

```python
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
```

---

## 9. COMMIT / ROLLBACK 故障注入测试完整实现

位于 [`tests/test_persistence_sqlite.py:467-555`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L467-L555)：

```python
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
```

---

## 10. 备份损坏与备份中断清理测试完整实现

位于 [`tests/test_persistence_sqlite.py:201-294`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L201-L294)：

```python
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
```

---

## 11. `SqliteVecStore` 并发测试完整实现

位于 [`tests/test_persistence_sqlite.py:667-779`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L667-L779)（`SqliteVecStore` 的 `:memory:` 并发与磁盘并发分别在段 2 与段 4，完整函数如下）：

```python
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
```

---

## 12. 原始 Pytest 汇总输出及 12 项 Skip 具体原因

执行命令：`.\.venv\Scripts\python.exe -m pytest -rs -q -W error::ResourceWarning`（Windows 64-bit, Python `3.12.14`, SQLite `3.53.1`；Linux/POSIX 实机未运行）：

```text
...........................s............................................ [ 48%]
..........ssss................sssss...............................ss.... [ 96%]
.....                                                                    [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_file_path_security.py:128: POSIX cross-directory and root-tree escape semantics apply only on non-Windows
SKIPPED [1] tests\test_process_platform.py:583: POSIX real OS session & FD test
SKIPPED [1] tests\test_process_platform.py:639: POSIX real process-group grandchild kill test
SKIPPED [1] tests\test_process_platform.py:705: POSIX SIGTERM graceful termination test
SKIPPED [1] tests\test_process_platform.py:745: POSIX SIGKILL escalation real OS test
SKIPPED [1] tests\test_process_runner.py:799: POSIX real process-tree timeout test
SKIPPED [1] tests\test_process_runner.py:840: POSIX real external cancellation test
SKIPPED [1] tests\test_process_runner.py:892: POSIX repeated Task.cancel() resilience test
SKIPPED [1] tests\test_process_runner.py:960: POSIX detached setsid pipe EOF & FD test
SKIPPED [1] tests\test_process_runner.py:1021: POSIX concurrent mixed-outcome resource test
SKIPPED [1] tests\test_shell_runtime_integration.py:444: POSIX /bin/bash -c Shell Tool integration test
SKIPPED [1] tests\test_shell_runtime_integration.py:475: POSIX HITL + Executor E2E process-group cancel test
137 passed, 12 skipped in 66.25s (0:01:06)
```

---

## 13. `git diff --check` 和 `uv lock --check` 原始输出

### `git diff --check`

```text
（退出码 0，无输出）
```

### `uv lock --check`

```text
Resolved 92 packages in 4ms
```
