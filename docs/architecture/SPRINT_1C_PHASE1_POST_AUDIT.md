# Sprint 1C — Phase 1 Post-Implementation Independent Audit

> **审计性质**：Read-Only Adversarial Post-Implementation Audit（只读审计，未修改任何源码、测试、配置、依赖或数据库状态，严禁启动 Phase 2）  
> **Git 基线**：Branch `main` @ `62ec97230bccd64532cb3fa6f66dc6931d34244d` (`Implement Phase 1 SQLite persistence and migration framework`)  
> **对照起点**：`a8a32ed4cd3cdfad9da883fa1ac751a80ca43e86`  
> **对照规范**：[`docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md`](file:///d:/myProject/JarvisStudio/docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md)（Section 5.1, 12.1, 15.1, 17.1）与 [`docs/architecture/SPRINT_1C_PHASE1_READINESS_AUDIT.md`](file:///d:/myProject/JarvisStudio/docs/architecture/SPRINT_1C_PHASE1_READINESS_AUDIT.md)

---

## 1. Git Baseline and Changed-File Scope

### 1.1 实际 Git 状态（创建本报告前只读核验）

- **当前分支与 HEAD**：`main` @ `62ec97230bccd64532cb3fa6f66dc6931d34244d`
- **Phase 1 起点提交**：`a8a32ed4cd3cdfad9da883fa1ac751a80ca43e86`
- **工作区状态**：`git status --short` 为空；`git ls-files --others --exclude-standard` 为空；`git diff`（对当前 `HEAD`）为空。
- **相对于起点提交 `a8a32ed..HEAD` 的完整变更集**（`13 files changed, 3138 insertions(+), 250 deletions(-)`）：
  - **新增核心代码与测试（4 项，符合 Phase 1 Allowlist）**：
    - `src/jarvis_studio/core/persistence/__init__.py`（+43）
    - `src/jarvis_studio/core/persistence/sqlite.py`（+318）
    - `src/jarvis_studio/core/persistence/migrations.py`（+342）
    - `tests/test_persistence_sqlite.py`（+510）
  - **修改现有代码与依赖（6 项，符合 Phase 1 Allowlist）**：
    - `pyproject.toml`（+1，仅增加 `"sqlite-vec>=0.1.9"`）
    - `uv.lock`（+2，仅登记直属依赖 `sqlite-vec>=0.1.9`，92 个锁版本零变更）
    - `src/jarvis_studio/service/session_service.py`（120 行变更）
    - `src/jarvis_studio/memory/store.py`（133 行变更）
    - `src/jarvis_studio/core/observability/trace_logger.py`（80 行变更）
    - `src/jarvis_studio/rag/store.py`（121 行变更）
  - **范围外混入同一提交的文档（3 项）**：
    - `docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md`（+1006）
    - `docs/architecture/SPRINT_1C_PERSISTENCE_RECONNAISSANCE.md`（+489）
    - `docs/architecture/SPRINT_1C_PHASE1_READINESS_AUDIT.md`（+223）
    *(注：`62ec972` 将前期架构设计/勘察/准备度审计文档与 Phase 1 代码合并提交；未修改任何禁止触碰的运行期文件如 `saver.py`、`hitl/*`、`nodes/*`、`agent_service.py`、`api/*`、`cli/*`，亦未新增 `runtime.py` 或任何 `aiosqlite` 引用。)*

### 1.2 项目本地 `data/` 目录生成的数据库备份与边车文件（只读二进制头检测）

- **Git 跟踪状态**：`git ls-files data` 为空；`git status --ignored --short data` 输出 `!! data/`（受 [`.gitignore:37`](file:///d:/myProject/JarvisStudio/.gitignore#L37) 忽略，均属未跟踪/被忽略的本地工件）。
- **`data/` 实际现存文件实测**：
  - `jarvis_studio.db`：`208,896` bytes，header `(2,2)`（`WAL`），`PRAGMA user_version = 1`
  - `jarvis_studio.bak.v0_to_v1.20261111T020135Z_32156_1.db`：`200,704` bytes，header `(2,2)`（`WAL`），`user_version = 0`
  - **`jarvis_studio.bak.v0_to_v1.20261111T020135Z_32156_1.db-shm`**：`32,768` bytes *(残余孤儿 SHM 边车)*
  - **`jarvis_studio.bak.v0_to_v1.20261111T020135Z_32156_1.db-wal`**：`0` bytes *(残余孤儿 WAL 边车)*
  - `jarvis_vec.db`：`6,443,008` bytes，header `(2,2)`（由原 `DELETE` 被测转为 `WAL`），`PRAGMA user_version = 1`
  - `jarvis_vec.bak.v0_to_v1.20261111T020629Z_18688_10.db`：`6,443,008` bytes，header `(2,2)`（`WAL`），`user_version = 0`
  - **`jarvis_vec.bak.v0_to_v1.20261111T020629Z_18688_10.db-shm`**：`32,768` bytes *(残余孤儿 SHM 边车)*
  - **`jarvis_vec.bak.v0_to_v1.20261111T020629Z_18688_10.db-wal`**：`0` bytes *(残余孤儿 WAL 边车)*
  - `test_workspace/`、`workspace/`（目录）

> **关键发现**：在 `migrations.py:219` 加入 `PRAGMA journal_mode = DELETE` **之前**执行的测试就已经改写并迁移了本地 `data/jarvis_studio.db` 与 `data/jarvis_vec.db`，至今在 `data/` 中遗留上述两套带 `-shm`/`-wal` 边车的 `.bak` 文件与已升级到 `user_version=1` 的真实开发数据库。

---

## 2. Full Implementation Architecture Review

### 2.1 分层与依赖边界

- [`sqlite.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L14-L23) 仅导入标准库与 `sqlite_vec`，使用 stdlib `logging.getLogger(__name__)`，未导入 `jarvis_studio.core.observability`、未引入 `aiosqlite`，成功避免了 `observability/__init__.py -> trace_logger.py -> core.persistence` 循环导入。
- [`__init__.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/__init__.py#L1-L43) 仅导出同步连接、事务、路径锁及 `v0 -> v1` 迁移符号，未包含 Phase 2 的 `RuntimeInstanceLock`、`configure_async_connection` 或 `AsyncSqliteSaver`。
- [`pyproject.toml`](file:///d:/myProject/JarvisStudio/pyproject.toml#L22-L24) 与 [`uv.lock`](file:///d:/myProject/JarvisStudio/uv.lock#L575-L616) 仅显式增加 `sqlite-vec>=0.1.9`（锁版本保持 `0.1.9`），未引入 `aiosqlite` 直属依赖。

### 2.2 架构细节与范围边界观察

1. **未授权范围内的测试隔离缺口**：虽然实现严格按设计第 15.1 节锁定未改 `tests/conftest.py`，但因此使**真实本地库在 pytest 运行期间发生不可逆状态迁徙与持久化写污染**，该问题直接违反测试隔离验收标准（见第 6 节）。
2. **`get_path_lock(":memory:")` 防御缺口**（[`sqlite.py:31-39`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L31-L39)）：当前内部调用方均先排除了 `":memory:"` 再调用 `get_path_lock()`，但该函数作为公开导出的函数未排除 `":memory:"`；若外部直接传入 `":memory:"`，`Path(":memory:").resolve()` 会按当前工作目录下文件名生成锁键，语义不够严谨。
3. **Phase 2 衔接提示**：4 个 Store 目前在 `__init__` 中无条件触发 `initialize_sqlite_database()` 与 `run_*_migrations()`；在 Phase 2 引入 `RuntimeInstanceLock` 时，入口层（API `lifespan` / CLI `_chat_loop`）必须确保在首次构造默认 `get_session_service()` / `get_trace_store()` / `get_memory_manager()` 之前先完成加锁，防止未持锁建库或迁移。

---

## 3. Connection Lifecycle Review

逐项核查 [`sqlite.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L46-L318) 的连接与事务所有权：

| 核查项 | 实现位置 | 审计结论与证据 |
| :--- | :--- | :--- |
| **磁盘连接所有权** | [`open_sync_connection`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L77-L110), [`sync_transaction`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L174-L229), [`SyncSQLiteSession.read_connection`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L271-L287) | **通过**：磁盘路径每次按需创建 `isolation_level=None`、`check_same_thread=False`、`busy_timeout=5000`、`synchronous=NORMAL` 连接，所有路径均在 `finally:` 内调用 `conn.close()`；`sync_transaction` 在 `in_memory_conn is None` 时 `owns_conn=True` 并必关闭。 |
| **`:memory:` 连接所有权** | [`SyncSQLiteSession`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L232-L318) | **通过**：每个 Store 持有独享 `_in_memory_conn`，`sync_transaction(..., in_memory_conn=conn)` 设 `owns_conn=False`，绝不关闭该连接；仅 `Store.close()` 关闭并置 `None`。 |
| **异常期清理** | [`open_sync_connection:98-109`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L98-L109), [`load_sqlite_vec:70-74`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L70-L74) | **通过**：连接后设 PRAGMA 或加载 `sqlite_vec` 失败时会先 `conn.close()` 再重抛；`enable_load_extension(False)` 处于 `finally` 块。 |
| **重复 `close()` 与关闭后误复用** | [`SyncSQLiteSession:260-318`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L260-L318) | **部分不一致（P3）**：<br>1. 4 个 Store 重复调用 `close()` 均为安全幂等；<br>2. `:memory:` Store 在 `close()` 后设 `_closed = True`，再用抛 `RuntimeError`；<br>3. **但磁盘 Store `close()` 不设 `_closed = True`**，调用 `store.close()` 后仍可继续执行读写操作（虽无句柄泄漏，但与 `:memory:` 的 use-after-close 行为不对称）。 |
| **`ROLLBACK` 异常保护** | [`sync_transaction:216-226`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L216-L226) | **通过（缺单测直接模拟）**：`except BaseException:` 在 `began_tx and conn.in_transaction` 时执行 `ROLLBACK`，对 `ROLLBACK` 自身可能抛出的 `sqlite3.Error` 捕获记 warning 并继续上抛原异常及关盘连接；不过现有单测未实际注入 `ROLLBACK` 失败场景。 |
| **禁止隐式嵌套事务** | [`sync_transaction:193-212`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L193-L212) | **通过**：在 `try` 启动前先检 `in_memory_conn.in_transaction`，触发 `NestedTransactionError` 时跳过 `ROLLBACK` 与 `close()`，外层未提交写保持完整并可正常提交。 |
| **共享 `:memory:` 线程安全** | [`SyncSQLiteSession:252-302`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L252-L302) | **通过（条件约束）**：通过实例级 `threading.RLock()` 在所有 `_in_memory_conn` 读写、事务与 `close()` 上加锁，防止多线程交错误报 `NestedTransactionError` 或游标并发损坏；注意若外部绕过 Store 直接持 `store._in_memory_conn` 从多线程调 `sync_transaction` 则不受 `self._lock` 保护。 |

---

## 4. Migration and Backup Review

逐项核查 [`migrations.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L1-L342) 与 [`initialize_sqlite_database`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L113-L171)：

- **WAL 切换安全**：`initialize_sqlite_database()` 对 `:memory:` 直接返回 `"memory"`；对磁盘路径通过 `get_path_lock(normalized)` 串行化初始化、确认 `not conn.in_transaction` 后查询/切换 `PRAGMA journal_mode = WAL`，校验返回值为 `'wal'` 并带最多 4 次 transient `locked/busy` 短退避重试，随后立即关闭连接。
- **并发与重复初始化 / 迁移**：多次连续构造 `SessionService` -> `MemoryStore` -> `TraceStore` 仅首轮运行 `v0 -> v1` 并生成 1 份备份；并发线程进入 `_run_migrations()` 通过 `get_path_lock(resolved)` 排队并在 `_apply_steps()` 进入 `BEGIN IMMEDIATE` 后再次读取 `PRAGMA user_version`（Double-Check），避免重复执行迁移。
- **备份唯一性与防覆盖**：[`_reserve_unique_backup_path`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L143-L155) 使用 `YYYYMMDDTHHMMSSZ` + `pid` + 线程安全全局单调 `_BACKUP_SEQ` + `candidate.exists()` 检查，杜绝同秒同进程碰撞覆盖已有备份；失败时仅当本次 `backup_completed == False` 才删除自身未写完文件，不触碰既往备份。
- **备份与 `quick_check` 潜在瑕疵（P2/P3）**：
  1. **新空库也会产生无意义备份（P2 边界）**：在全新不存在的磁盘路径首次构造 Store 时，`initialize_sqlite_database()` 先创建一个空白 `v0` 库文件，随后 `run_app_migrations()` / `run_vec_migrations()` 发现 `0 <= user_version < 1`，**无例外地为刚创建的空文件生成一份 `.bak.v0_to_v1.*.db` 备份**。每个 `tmp_path` 或新库初始阶段都会额外落盘空快照，且意外触达项目默认路径时还会生成本地 `.bak`。
  2. **外部 WAL-mode `.bak` 的只读验证边车（P3）**：[`create_verified_backup:219`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L219) 已补充 `PRAGMA journal_mode = DELETE` 确保新建备份单文件自包含；但若 [`verify_sqlite_backup`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L158-L197) 被用于校验其他 `WAL` 模式快照文件，`?mode=ro` 未加 `&immutable=1` 仍可能在其旁路留下 `-shm`/`-wal` 文件。
  3. **`create_verified_backup` 内 `dst_conn.close()` 异常连锁（P3）**：[`migrations.py:225-228`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L225-L228) 先 `dst_conn.close()` 再 `src_conn.close()`；若 `dst_conn.close()` 抛异常，`src_conn.close()` 将被跳过。
  4. **`:memory:` 跳过版本升级**：`:memory:` Store 不调用 `run_*_migrations()`，其 `PRAGMA user_version` 仍为 `0`（仅对磁盘强制升至 `1`）。
- **双库独立性与动态向量维度**：`_APP_V1_STATEMENTS` 覆写 4 张业务表与 3 个索引；`_VEC_V1_STATEMENTS` 仅覆写 `doc_files`、`doc_chunks` 与 `idx_chunk_doc`，保留 `vec_chunks USING vec0(..., embedding float[{self.dimension}])` 在 [`SqliteVecStore._init_db`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/rag/store.py#L105-L113) 动态构建，完全隔离双库 `user_version`。

---

## 5. Store Compatibility Review

核查 4 个已修改 Store 的公共契约与业务逻辑兼容性：

| Store 与位置 | 核心核查结论 | 细节与注意事项 |
| :--- | :--- | :--- |
| **`SessionService`**<br>[`session_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L43-L356) | **兼容（有条件）** | `add_message`（[`L264`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L264)）将 `get_or_create_session` 留在 `with self._db.transaction()` 外，`update_session_title`（[`L248`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L248)）将 `get_session` 留在事务退出后，无嵌套冲突；`create_session` 仍沿用 `INSERT OR REPLACE` 未提前改动语义；保留 `db_path or ...` 对空串静默回退默认库的行为。 |
| **`MemoryStore`**<br>[`memory/store.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/memory/store.py#L18-L243) | **兼容并修复泄漏** | `_init_db` 改走 `self._db.read_connection()` 与 `self._db.transaction()`，消除原 `_init_db()` 缺少 `try/finally` 的磁盘句柄泄漏；`clear()` 维持原清理语义；**注意 `db_path=""` 会因 `if db_path is None:` 触发 `ValueError`（与其他 3 个 Store 的 `or` 回退有微差）**；全局 [`_global_memory_manager`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/memory/manager.py#L64-L72) 行为不变。 |
| **`TraceStore`**<br>[`trace_logger.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/observability/trace_logger.py#L139-L263) | **完全兼容** | `save_trace` 保持单层 `INSERT OR REPLACE` 写入；`list_traces`/`get_trace` 走短读连接；移除每次连接的重复 `Path.mkdir`；`:memory:` 下 `__init__` 经 `_init_db()` 即刻完成 `_in_memory_conn` 建表。 |
| **`SqliteVecStore`**<br>[`rag/store.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/rag/store.py#L20-L284) | **兼容并加固** | 启用 `enable_foreign_keys=False, enable_sqlite_vec=True`；加载扩展后立即 `enable_load_extension(False)`；校验 `dimension` 必须为正整数（`dimension=0` 由旧 `or 1536` 改为严格报 `ValueError`）；`_delete_doc_internal(conn, doc_path)` 沿用外层连接无嵌套事务；支持自定义维度如 `32`。 |

- **Sprint 1A / Sprint 1B 回归核对**：`src/jarvis_studio/agent/`（HITL、Authorization、Nodes、Checkpoints）与 `src/jarvis_studio/core/process/`（Windows JobObject / POSIX Process Group / Runner）**零改动**，安全与进程清理契约无回退。

---

## 6. Test Isolation Review & Verification of Test Evidence

### 6.1 未隔离的测试写污染问题（Outstanding Acceptance Blocker）

核查全仓 20 个测试文件与 [`tests/conftest.py`](file:///d:/myProject/JarvisStudio/tests/conftest.py#L1-L9)：

1. **根本原因**：[`tests/conftest.py:5-9`](file:///d:/myProject/JarvisStudio/tests/conftest.py#L5-L9) 仅设定了 `CHECKPOINT_BACKEND="memory"` 与 `WORKSPACE_ROOT="./data/test_workspace"`，**未隔离 `SQLITE_DB_PATH` 与 `VECTOR_DB_PATH`**。同时以下既有测试在默认路径下构造 Store 或全局单例：
   - [`tests/test_sessions_api.py:9-57`](file:///d:/myProject/JarvisStudio/tests/test_sessions_api.py#L9-L57)：`TestClient(create_app())` + `get_session_service()` 直连 `data/jarvis_studio.db`。
   - [`tests/test_streaming.py:41-82`](file:///d:/myProject/JarvisStudio/tests/test_streaming.py#L41-L82)：`POST /api/v1/chat/stream` 与 `AgentService` 向真实 `data/jarvis_studio.db` 写入会话（`stream_test_01`、`test_stream_thread`）与 Trace。
   - [`tests/test_cli.py:30-44`](file:///d:/myProject/JarvisStudio/tests/test_cli.py#L30-L44)：`test_cli_chat_immediate_exit` 每次运行向 `data/jarvis_studio.db` 永久插入新 Session 行。
   - [`tests/test_rag.py:128-153`](file:///d:/myProject/JarvisStudio/tests/test_rag.py#L128-L153)：`test_rag_api_endpoints` 与 `test_cli_rag_commands` 无参实例化 `SqliteVecStore()` 直连 `data/jarvis_vec.db`。
   - [`tests/test_security_hardening_1a2.py:59-87`](file:///d:/myProject/JarvisStudio/tests/test_security_hardening_1a2.py#L59-L87)：`isolated_services` 隔离了 `_global_session_service` 与 `_global_agent_service`，但**未隔离 `_global_trace_store`**，Trace 仍写入 `data/jarvis_studio.db`。
   - [`tests/test_memory.py:18-30, 100-148`](file:///d:/myProject/JarvisStudio/tests/test_memory.py#L18-L30)：`memory_manager` 污染 `_global_memory_manager` 且从不复位，后续两个未带 fixture 的 API/CLI 用例依赖该执行顺序泄漏；若单跑 `-k test_cli_memory_commands` 则直接污染 `data/jarvis_studio.db`。
2. **与新测试对比**：[`tests/test_persistence_sqlite.py`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py) 全部 11 个用例自身严格使用 `tmp_path` 或 `":memory:"`，不直接访问 `data/*.db`；但**任何全仓 `pytest` 运行仍会构造默认 Store 并读写或迁移项目真实数据库**。
3. **范围决策**：**`tests/conftest.py` 必须被正式纳入 Phase 1 变更白名单**，作为 Phase 1 完成的必要验收修正；不解决测试库隔离前，Phase 1 不应被判定为可安全合并。

### 6.2 对已有测试执行日志与 `test_persistence_sqlite.py` 证据的对抗式审查

检查先前执行日志 [`task-2239.log`](file:///C:/Users/yangyimingWB5277/.gemini/antigravity/brain/3d7636e3-2334-46b5-9d1a-a833a187ffbb/.system_generated/tasks/task-2239.log)（Windows 64-bit, Python `3.12.14`, `135 passed, 12 skipped in 35.10s`, Ruff 增量检查通过）及 [`tests/test_persistence_sqlite.py`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L1-L510) 源码：

| 测试分类 | 具体测试函数 / 范围 | 真实覆盖度与缺口评估 |
| :--- | :--- | :--- |
| **确实验证失败路径** | `test_open_sync_connection_pragmas_and_blank_path_guard` (`L58`)<br>`test_open_sync_connection_closes_handle_on_setup_failure` (`L89`)<br>`test_sync_transaction_forbids_nested_transaction_without_breaking_outer` (`L301`)<br>`test_sqlite_vec_extension_lockdown_and_dedup` (`L365`)<br>`test_persistence_imports_without_circular_dependency_or_aiosqlite` (`L37`) | 真实测得空白路径 `ValueError`、设置扩展失败时关闭 spy 连接、4 个 Store 内 `NestedTransactionError` 不破坏外层事务、磁盘异常回滚、`enable_load_extension` 加载异常/成功后的闭锁、独立子进程导入顺序无环与无 `aiosqlite`。 |
| **存在桩替代或仅测快乐路径** | `test_initialize_sqlite_database_and_independent_dual_db_versions` (`L117`)<br>`test_backup_verification_blocks_corrupted_snapshots_and_preserves_old_backups` (`L163`)<br>`test_shared_app_db_multi_store_init_and_dynamic_vec_dimension` (`L211`)<br>`test_in_memory_stores_isolated_and_idempotent_close` (`L244`)<br>`test_concurrent_multithread_in_memory_and_disk_writes` (`L449`) | 1. `L188-L193` 测试"备份校验失败阻断迁移"时直接 stub 了 `create_verified_backup`，未打通真实的"先落盘损坏快照再经 `verify_sqlite_backup` 拦截"，也未测 `src_conn.backup()` 失败时清理半截 `.bak`；<br>2. `L264` 仅对 `SessionService` 断言了 `:memory:` `close()` 后再调用报 `RuntimeError`，未对其余 3 个 Store 显式断言；<br>3. `L449` 并发盘库测试覆盖了 App 3 个 Store，未把 `SqliteVecStore` 纳入多线程并发实测；<br>4. 尚未测 `COMMIT` / `ROLLBACK` 自身抛 `sqlite3.Error` 的保护路径。 |
| **未覆盖其声称行为（真实测试缺陷）** | **`test_memory_store_init_failure_does_not_leak_disk_connection`** ([`L405-L446`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L405-L446)) | **明确断言缺陷**：测试 docstring 声称验证 `_init_db()` DDL 失败时不泄漏磁盘连接，但 `L446` 仅断言 `assert closed_count >= 1`。实际上 `MemoryStore.__init__` 在触发 `CREATE TABLE` 前已依次打开并关闭 3 条短连接（`initialize_sqlite_database` #1、`run_app_migrations` 读版本 #2、`_init_db` 查 `sqlite_master` #3），**即便第 4 条（实际报错的 DDL 事务连接）完全泄漏，`closed_count == 3 >= 1` 依然会通过**！ |
| **Windows 上未执行的平台测试** | 12 个 `@pytest.mark.skipif(sys.platform == "win32", ...)` 测试（分布于 `test_process_platform.py`、`test_process_runner.py`、`test_shell_runtime_integration.py` 等）；**Linux 下 Phase 1 代码与 `sqlite-vec>=0.1.9` manylinux wheel** | 本轮未在 Linux/POSIX 上实跑任何测试。**严禁在未获取真实 Linux CI 执行输出前宣称 Linux 兼容性已通过**。 |

---

## 7. Regression Risk Register

| 编号 | 等级 | 问题 / 风险描述 | 实际代码或盘面证据 | 影响 |
| :--- | :---: | :--- | :--- | :--- |
| **PA-01** | **P1 (验收阻塞)** | **测试套件未隔离项目本地 DB，持续污染并迁移 `data/jarvis_studio.db` 与 `data/jarvis_vec.db`** | [`tests/conftest.py:5-9`](file:///d:/myProject/JarvisStudio/tests/conftest.py#L5-L9) 未设 `SQLITE_DB_PATH`/`VECTOR_DB_PATH`；`data/` 现存两库已升 `user_version=1` 并残留 `20261111T020135Z`/`020629Z` `.bak` + `-shm`/`-wal` 边车；`test_cli.py`/`test_streaming.py`/`test_sessions_api.py`/`test_rag.py` 每次运行仍直击 `data/*.db`。 | 破坏研发/用户本地数据目录洁净度与测试可重复性，构成明确的 Phase 1 验收未闭环项。 |
| **PA-02** | **P1 (测试缺陷)** | **`test_memory_store_init_failure_does_not_leak_disk_connection` 断言 `closed_count >= 1` 失效** | [`tests/test_persistence_sqlite.py:416-446`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L416-L446)：前 3 个前置连接已使 `closed_count == 3`，无法检出第 4 条报错连接是否泄漏。 | 提供虚假的防泄漏回归安全感。 |
| **PA-03** | **P2** | **Linux/POSIX 环境尚未执行 `pytest` 与 `sqlite-vec>=0.1.9` 实机验证** | Windows `12 skipped`；设计 `L987` 明确要求 Linux CI 实跑 `sqlite-vec==0.1.9` 扩展加载。 | Linux 系统 SQLite 动态扩展兼容性及 POSIX 全仓回归尚属未知。 |
| **PA-04** | **P2** | **新建空库即触发冗余空备份 + 既有 `data/` 残余历史 `-shm`/`-wal` 备份边车** | `data/*.bak...db-shm` (`32,768B`) 与 `-wal` (`0B`) 残存；`migrations.py` 对 `0` 版本新建文件亦生成空 `.bak`。 | `data/` 与部分测试目录产生多余空快照；旧残余边车需开发者知悉或安全归档清理策略。 |
| **PA-05** | **P2** | **`tests/test_persistence_sqlite.py` 部分异常路径存在测试盲区与 Mock 替代** | 缺 `COMMIT`/`ROLLBACK` 报错保护实测；缺 `SqliteVecStore` 多线程并发测试；缺真实损坏文件穿透 `run_app_migrations` 阻断；`:memory:` 关闭后阻断仅测了 `SessionService`。 | 边缘错误路径缺少回归护城河。 |
| **PA-06** | **P3** | **公共 helper 与入参细微语义边界** | `get_path_lock(":memory:")` 会解析成 CWD 文件路径；`MemoryStore("")` 报 `ValueError` 而其他 3 个 Store `or` 回退；磁盘 `Store.close()` 不阻断后续调用；`:memory:` `user_version` 留 `0`；`create_verified_backup` 内 `dst_conn.close()` 未用独立 `try/finally` 保护 `src_conn.close()`。 | 极低概率边界误用或资源清理顺序异常。 |

---

## 8. Exact Remediation Recommendations

仅提出最小、精确的修复与补正方案（本审计未执行修改，严禁在完成修复前进入 Phase 2）：

1. **将 [`tests/conftest.py`](file:///d:/myProject/JarvisStudio/tests/conftest.py) 正式纳入 Phase 1 变更范围并隔离测试数据库（解决 PA-01）**：
   - 在 [`tests/conftest.py`](file:///d:/myProject/JarvisStudio/tests/conftest.py) 增加 `autouse=True, scope="session"` 的测试数据库隔离 fixture，利用 pytest 原生 `tmp_path_factory.mktemp("jarvis_test_dbs")`：
     - 设 `os.environ["SQLITE_DB_PATH"] = str(db_dir / "jarvis_studio.db")` 与 `os.environ["VECTOR_DB_PATH"] = str(db_dir / "jarvis_vec.db")`（并用 `monkeypatch`/显式恢复并在进入/退出时将 `settings_mod._global_settings`、`session_service_mod._global_session_service`、`memory_manager_mod._global_memory_manager`、`trace_logger_mod._global_trace_store`、`agent_service_mod._global_agent_service` 安全释放并复位为 `None`）。
   - 同时核实各单元测试模块中 [`test_memory.py`](file:///d:/myProject/JarvisStudio/tests/test_memory.py) 的 `memory_manager` 与 [`test_security_hardening_1a2.py`](file:///d:/myProject/JarvisStudio/tests/test_security_hardening_1a2.py#L60-L87) 在独立 `-k` 运行或全仓运行下均落入上述 pytest 临时库，不再读写项目根 `data/jarvis_studio.db` 或 `data/jarvis_vec.db`。（经核查，现有 20 个测试文件中没有任何测试断言默认字面路径 `data/jarvis_studio.db` 或 `data/jarvis_vec.db`。）
   - 对于已被之前测试污染/迁徙的本地 `data/*.bak.*` 与 `-shm`/`-wal`：请由仓库维护者按需人工检查是否需要从最初 `v0` 备份恢复或保留已升版的 `data/`（审计阶段严禁自动删改）。
2. **修复 [`test_memory_store_init_failure_does_not_leak_disk_connection`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L405-L446) 断言缺陷（解决 PA-02）**：
   - 同时记录 `opened_count` 与 `closed_count`（并单独记录命中 `CREATE TABLE IF NOT EXISTS ltm_memories` 的目标连接实例是否调用 `close()`），严格断言 `ddl_target_closed is True` 且 `opened_count == closed_count`。
3. **补齐测试覆盖盲区与小边界加固（解决 PA-04/PA-05/PA-06）**：
   - 在 `tests/test_persistence_sqlite.py` 增补：(a) 实盘破坏性场景测试 `run_app_migrations`（如通过 `src_conn.backup()` 后 hook 使临时备份损坏触发 `verify_sqlite_backup` 真实抛 `BackupVerificationError` 并验证阻断与不留脏状态）；(b) `COMMIT` / `ROLLBACK` 自身故障保护测试；(c) 4 个 `:memory:` Store 全员验证 `close()` 后 `RuntimeError`；(d) `SqliteVecStore` 多线程并发写入测试。
   - 在 [`sqlite.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py#L31-L39) 使 `get_path_lock(":memory:")` 显式拒绝或规范处理 `":memory:"`；在 [`migrations.py:225-230`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L225-L230) 将 `dst_conn.close()` 与 `src_conn.close()` 拆入嵌套 `try/finally`；在 [`verify_sqlite_backup:166`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py#L166) 考虑使用 `?mode=ro&immutable=1`（针对独立备份只读验真不生成 WAL/SHM 边车）。
   - 核查提交历史规划（将已随 `62ec972` 一起提交的 3 份设计/审计文档按团队规范保留或分拆说明，并在新增上述修复后于 **Linux/POSIX CI** 跑通全量 `pytest -W error::ResourceWarning` 与 `sqlite-vec>=0.1.9` 验证）。

---

## 9. Final Verdict

**`REVISIONS REQUIRED`**

- **裁决依据**：
  1. Phase 1 同步连接治理（[`sqlite.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/sqlite.py)）、双库迁移框架（[`migrations.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/persistence/migrations.py)）及 4 个 Store 适配器的主干结构合理，未破坏 Sprint 1A/1B 冻结安全与进程契约；
  2. 但是，**全量测试执行仍会直接写污染并迁移项目本地数据库 `data/jarvis_studio.db` 与 `data/jarvis_vec.db`**（已在 `data/` 实际观测到 `WAL`、`user_version=1` 及残余备份与 `-shm`/`-wal` 边车），[`tests/conftest.py`](file:///d:/myProject/JarvisStudio/tests/conftest.py) **必须纳入 Phase 1 范围完成临时库隔离**才满足验收标准；
  3. [`test_memory_store_init_failure_does_not_leak_disk_connection`](file:///d:/myProject/JarvisStudio/tests/test_persistence_sqlite.py#L405-L446) 存在 `assert closed_count >= 1` 的假性通过断言缺陷；且 Linux/POSIX CI 验证尚未完成。
- **阶段门控声明**：**禁止在未完成上述修正并通过无污染测试验收与复核前开始 Phase 2。**
