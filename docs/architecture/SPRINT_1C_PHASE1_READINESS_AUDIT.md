# Sprint 1C — Phase 1 Implementation Readiness Audit

> **审计角色**：Independent Implementation Readiness Auditor（只读审计，未修改任何源码、测试或依赖配置）  
> **审计基线**：Branch `main` @ `a8a32ed4cd3cdfad9da883fa1ac751a80ca43e86` (`a8a32ed`)  
> **对照设计**：[`docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md`](file:///d:/myProject/JarvisStudio/docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md)（Final Design Freeze，核心审查第 5、12、14、15、16、17、18 节）  
> **运行环境实测**：Windows 64-bit（`MSC v.1944`），Python `3.12.14`，SQLite `3.53.1`，`uv 0.12.9`，`pytest 9.1.1`，`ruff 0.16.10`

---

## 1. Executive Summary

### 1.1 仓库基线与实测状态

- **Git 基线**：当前位于 `main @ a8a32ed4cd3cdfad9da883fa1ac751a80ca43e86`，与设计文档第 1 行声明基线完全一致。工作区无任何已跟踪文件修改；仅存在设计阶段的两个未跟踪文档（`docs/architecture/SPRINT_1C_PERSISTENCE_DESIGN.md`、`docs/architecture/SPRINT_1C_PERSISTENCE_RECONNAISSANCE.md`）。Phase 1 目标包 `src/jarvis_studio/core/persistence/` 与 `tests/test_persistence_sqlite.py` 均尚未创建。
- **全仓测试基线**（Windows `.venv` 实测）：
  - 命令：`.\.venv\Scripts\python.exe -m pytest -q -W error::ResourceWarning`
  - 结果：**`124 passed, 12 skipped in 36.82s`**（退出码 `0`，零失败，零 `ResourceWarning`）。12 个 skip 均为 Windows 下 POSIX 专属测试；Linux/POSIX 行为与 `sqlite-vec>=0.1.9` Linux wheel 加载无法在本 Windows 主机本地验证，必须在 Linux CI 中实机跑通。
- **Ruff 基线**（`.venv` 实测）：
  - `ruff check src tests` 报 **`Found 29 errors`**（既有历史代码包含 `BLE001`、`S110`、`RUF010`、`I001`、`F841`）。
  - Phase 1 涉及的 4 个 Store 文件中共有 **`7 errors`**（`session_service.py` 含 3 处 `BLE001` + 3 处 `S110`；`rag/store.py` 含 1 处 `BLE001`；`memory/store.py` 与 `trace_logger.py` 为 `0` 报错）。
- **盘面库实态**（只读 URI `mode=ro` 核验）：
  - `data/jarvis_studio.db`：处于 `WAL` 模式、`PRAGMA user_version = 0`，已有表 `chat_sessions`、`chat_messages`、`ltm_memories`、`agent_conversation_traces`、`checkpoints`、`writes` 及 3 个索引。**实测副效应**：运行一次 `pytest` 基线后，因部分既有测试未隔离 `SQLITE_DB_PATH` 与 `get_trace_store()`，该真实库发生写入（`traces` 由设计勘察时 `149` 增至 `155`，`messages` `24 -> 28`，`sessions` `8 -> 9`）。
  - `data/jarvis_vec.db`（`6,443,008` 字节）：处于 `DELETE` 模式、`PRAGMA user_version = 0`，已有 `doc_files`（15 行）、`doc_chunks`（77 行）、`idx_chunk_doc` 及 `vec_chunks`（`float[1536]`）。

### 1.2 核心审计结论

- **最终结论**：**`APPROVED WITH NOTES`**
- **是否可启动 Phase 1**：**可以推进 Phase 1 实施**，但实施者必须严格遵守本报告第 4、7、8 节明确列出的 **5 项 P2 工程边界约束**（且需由架构侧确认第 8.2 节关于 `tests/conftest.py` 测试数据库隔离是否保持按设计延至 Phase 2 并接受首次测试触达本地 `data/*.db` 的 `v0 -> v1` 迁移，或提前纳入 Phase 1）。
- **核心依据**：
  1. 既有 4 个同步 Store（`SessionService`、`MemoryStore`、`TraceStore`、`SqliteVecStore`）在当前源码中均**不存在跨方法嵌套事务或隐式多方法共享事务**，与 `isolation_level=None + BEGIN IMMEDIATE + NestedTransactionError` 完全兼容。
  2. `sqlite-vec==0.1.9` 已存在于 `uv.lock`（含 Windows/Linux/macOS wheels），实测在 `WAL + BEGIN IMMEDIATE + enable_load_extension(False)` 及无扩展只读 `PRAGMA quick_check(1)` 备份校验下均工作正常。
  3. 识别出设计未明示的 **5 个关键实现边界（P2）**：
     - `core/persistence/` 导入 `core.observability.logger` 会与 `core/observability/__init__.py -> trace_logger.py` 构成真实循环导入崩溃；
     - `v0 -> v1` 若假设 `jarvis_studio.db` 必然已建全 4 张表会在先初始化单一 Store 的空白盘面库上报错，且 `run_vec_migrations()` 绝不能硬编码 `vec_chunks` 的维度（测试传入 `dimension=32`，生产默认 `1536`）；
     - 同一 `:memory:` Store 若被多线程并发使用，不加 `threading.RLock` 会使并发线程在 `if conn.in_transaction:` 误触发 `NestedTransactionError`；
     - `SessionService.add_message`（`get_or_create_session`）与 `update_session_title`（`get_session`）必须保持在 `sync_transaction` 之外，`open_sync_connection` 失败路径必须关闭半开连接，`ROLLBACK` 失败保护不得掩盖原异常，`migrations` 备份后缀 `<seq>` 需强制生成且 URI 应使用 `Path.resolve().as_uri() + "?mode=ro"`；
     - 既有未隔离测试（`test_cli.py`、`test_streaming.py`、`test_sessions_api.py`、`test_rag.py`、`test_memory.py` 部分用例及 `test_security_hardening_1a2.py` 的 Trace 写入）当前直连真实 `data/*.db`，且 `test_memory.py` 存在跨用例复用 `_global_memory_manager` 的清理陷阱。

---

## 2. Source Audit Evidence

所有结论均基于对当前工作区真实文件的逐行核验：

| 模块 / 文件 | 符号与精确行号 | 当前真实行为 | 对 Phase 1 的直接影响 |
| :--- | :--- | :--- | :--- |
| `src/jarvis_studio/service/session_service.py` | `__init__` (`L42-L45`), `_get_connection` (`L47-L59`) | `db_path or settings.storage.sqlite_path`；`:memory:` 懒创建 `self._in_memory_conn` (`check_same_thread=False`, `row_factory=Row`)；磁盘路径每次新建连接并 `mkdir`。无 `close()`。 | 可无缝委托 `open_sync_connection()`；需统一补 `close()` 与 `:memory:` 实例锁。 |
| `src/jarvis_studio/service/session_service.py` | `create_session` (`L96-L125`), `get_or_create_session` (`L127-L151`) | `create_session` 使用 `INSERT OR REPLACE` (`L107`)；`get_or_create_session` 先调用 `get_session` (`L134`，独立连接已关) 后更新 `updated_at` (`L138-L148`) 或调 `create_session` (`L151`)。 | **Phase 1 不可提前重构** `INSERT OR REPLACE`（属 Phase 4 行为范围，防打破既有调用期望）；两步间无连接共享，无嵌套。 |
| `src/jarvis_studio/service/session_service.py` | `update_session_title` (`L236-L258`), `add_message` (`L274-L333`) | `update_session_title` 提交并关连后于 `L258` 调用 `self.get_session(session_id)`；`add_message` 先于 `L282` 调用 `self.get_or_create_session(session_id)`，之后 `L287-L324` 写消息与更新标题。 | Phase 1 改造时，**必须维持 `L258` 与 `L282` 位于 `sync_transaction()` 外部**，否则立即触发 `NestedTransactionError`。 |
| `src/jarvis_studio/service/session_service.py` | `get_session` (`L153-L195`), `list_sessions` (`L197-L234`), `delete_session` (`L260-L272`), `get_messages` (`L335-L372`), `clear_session_messages` (`L374-L385`), `get_session_service` (`L388-L396`) | 全部读方法均为独立只读连接；`delete`/`clear` 为单层写入；`_global_session_service` 延迟创建（不在 import 期 init DB）。 | 只读方法走短连接（非写事务）；单层写走 `sync_transaction()`；无 import 期建库。 |
| `src/jarvis_studio/memory/store.py` | `__init__` (`L16-L27`), `_get_connection` (`L29-L40`), `_init_db` (`L42-L63`) | 使用 `if db_path is None:`（与其他 3 个 Store 的 `db_path or ...` 不同）；**`_init_db()` 缺少 `try/finally: conn.close()`** (`L44-L63`)，若 DDL 异常磁盘连接泄漏；无 `close()`。 | 修复 `_init_db()` 泄漏与增加 `close()` 不改变外部语义；需保留 `db_path == ""` 兼容性或在连接工厂拒绝空白串。 |
| `src/jarvis_studio/memory/store.py` | `add` (`L65-L95`), `get` (`L97-L118`), `update` (`L120-L149`), `delete` (`L151-L160`), `list` (`L162-L198`), `search` (`L200-L248`), `clear` (`L250-L258`) | 全部为独立单层 CRUD，无跨方法互调。注：`L94` 日志访问 `memory.category.value`（若传 `str` 原样存在既有小缺陷，不属 Phase 1 范畴）。 | 写方法切 `sync_transaction()`、读方法切短连接，100% 兼容 `NestedTransactionError` 禁令。 |
| `src/jarvis_studio/memory/manager.py` | `MemoryManager` (`L7-L61`), `get_memory_manager` (`L64-L72`) | `_global_memory_manager` 延迟初始化 (`store or MemoryStore()`)。 | 无 import 期建库。 |
| `src/jarvis_studio/core/observability/trace_logger.py` | `TraceStore.__init__` (`L138-L144`), `_get_connection` (`L146-L152`), `_init_db` (`L154-L182`), `save_trace` (`L184-L215`), `list_traces` (`L217-L246`), `get_trace` (`L248-L262`), `get_trace_store` (`L265-L273`) | `__init__` 在 `":memory:"` 时立即创建 `_in_memory_conn`；每次磁盘连接重复执行 `Path.mkdir` (`L149`)；`save_trace` 用 `INSERT OR REPLACE`；无后台线程/异步调用；无 `close()`；单例延迟创建。 | 迁移至 `sqlite.py` 后完全保持既有 API 与行为；仅需补齐 `close()` 与 `WAL + busy_timeout`。 |
| `src/jarvis_studio/core/observability/__init__.py` | 包初始化 (`L3-L9`) | 第 3 行导入 `logger`，第 4 行**立即导入 `trace_logger`** (`TraceStore`, `get_trace_store`)。 | **循环导入隐患（P2）**：若 `core/persistence/` 在模块顶层导入 `core.observability.logger`，任意模块优先导入 `core/persistence/` 会拉起 `observability.__init__ -> trace_logger -> core/persistence`，必然 `ImportError`！ |
| `src/jarvis_studio/rag/store.py` | `SqliteVecStore.__init__` (`L19-L28`), `_get_connection` (`L30-L46`), `_init_db` (`L48-L93`) | 每次打开磁盘连接执行 `enable_load_extension(True); sqlite_vec.load(conn)`（**从未执行 `enable_load_extension(False)`**）；`:memory:` 连接首次同样开扩展未关。`_init_db` 使用动态 `float[{self.dimension}]` 建虚拟表 `vec_chunks`。 | 必须改为 `try/finally: enable_load_extension(False)` 并仅对未加载连接加载（防 `:memory:` 重复加载）；**`run_vec_migrations()` 不可硬编码 `1536` 维 `vec_chunks`**。 |
| `src/jarvis_studio/rag/store.py` | `add_chunks` (`L95-L156`), `search` (`L158-L222`), `_delete_doc_internal` (`L224-L235`), `delete_document` (`L237-L246`), `get_file_hash` (`L248-L259`), `list_indexed_files` (`L261-L279`), `clear` (`L281-L291`) | `add_chunks` 与 `delete_document` 在单一外层连接内调用 `_delete_doc_internal(conn, doc_path)`；`_delete_doc_internal` 仅执行 `DELETE`，不开/关/提交连接。`sqlite_vec.serialize_float32` 用于写和 `MATCH` 查。 | 写方法外层套 `sync_transaction(..., enable_sqlite_vec=True, enable_foreign_keys=False)`，内部 `_delete_doc_internal` 直接复用 `conn`，零嵌套冲突。 |
| `src/jarvis_studio/agent/checkpoints/saver.py` | `get_checkpointer` (`L8-L25`) | Phase 1 前仍是 `MemorySaver` 缓存。 | Phase 1 禁改清单明确锁定不可改动，零冲突。 |

---

## 3. Store Compatibility Matrix

| Store 类 | 当前连接创建方式 | 连接关闭权 | 事务边界与嵌套风险 | `:memory:` 行为 | 缺失的 `close()` | 多线程风险 | Phase 1 是否受阻 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :---: |
| **`SessionService`**<br>(`service/session_service.py`) | `:memory:` 懒创建私有连接；磁盘每次 `sqlite3.connect(..., check_same_thread=False)` | 磁盘：各方法 `finally` 关闭；`:memory:`：永不关（待补 `close()`） | 单层事务；`add_message` 与 `update_session_title` 调用它法均在连接生命周期**外**，无嵌套 | 单例独享 `_in_memory_conn`；无 `cache=shared` | 缺 `close()`，无上下文管理 | 共享 `:memory:` 若并发进 `sync_transaction` 会在 `in_transaction` 误判（需 `RLock`） | **否**（边界清晰即可接入） |
| **`MemoryStore`**<br>(`memory/store.py`) | `:memory:` 懒创建私有连接；磁盘每次 `sqlite3.connect(..., check_same_thread=False)` | 磁盘：CRUD `finally` 关闭，但 **`_init_db()` 缺 `try/finally`（真实泄漏）**；`:memory:`：永不关 | 单层 CRUD；无跨方法互调，无嵌套 | 单例独享 `_in_memory_conn`；`test_memory.py` teardown 仅调 `clear()` | 缺 `close()`，`_init_db` 异常路径泄漏 | 同上（共享 `:memory:` 需 `RLock`；磁盘需WAL+`busy_timeout`） | **否** |
| **`TraceStore`**<br>(`core/observability/trace_logger.py`) | `__init__` 对 `:memory:` 立即建连；磁盘每次 `sqlite3.connect(..., check_same_thread=False)` | 磁盘：各方法 `finally` 关闭；`:memory:`：永不关 | 单层 `CREATE`/`INSERT OR REPLACE`/`SELECT`；无嵌套；无后台线程 | 单例独享 `_in_memory_conn` | 缺 `close()` | 同上（共享 `:memory:` 需 `RLock`） | **否** |
| **`SqliteVecStore`**<br>(`rag/store.py`) | `:memory:` 懒建并单次加载 `sqlite_vec`；磁盘每次建连并加载 `sqlite_vec` | 磁盘：各方法 `finally` 关闭；`:memory:`：永不关 | `_delete_doc_internal(conn, ...)` 复用外层 `conn` 且无自管 TX；零嵌套 | 单例独享 `_in_memory_conn` | 缺 `close()`；未重置 `enable_load_extension(False)` | 同上（共享 `:memory:` 需 `RLock`；磁盘大向量写可能并发持锁，依赖 `WAL+busy_timeout=5000`） | **否**（须保留动态 `self.dimension` 建表） |

---

## 4. Persistence Layer Contract Audit

对照设计文档第 12.1 节（`L709-L757`）、第 5.1 节（`L182-L204`）、第 17.1 节（`L943-L961`），逐接口审计潜在未定义边界：

### 4.1 `open_sync_connection(db_path, *, enable_foreign_keys=True, enable_sqlite_vec=False)`

- **契约核验**：设计要求磁盘库使用 `isolation_level=None` 创建全新连接、建父目录、首设 `PRAGMA busy_timeout=5000`、设 `PRAGMA synchronous=NORMAL`、按需设 `PRAGMA foreign_keys=ON`、`sqlite_vec` 瞬时加载后闭锁，并由调用方负责关闭。
- **发现的未闭合/待明确边界**：
  1. **构建期异常泄漏风险**：在 `sqlite3.connect()` 成功之后，若设置 `PRAGMA` 或 `load_sqlite_vec(conn)` 抛错，`open_sync_connection()` **必须捕获异常并 `conn.close()` 后再重抛**，否则在返回给调用方前已发生连接句柄泄漏。
  2. **`:memory:` 连接的 `PRAGMA` 差异化处理**（对齐第 5.1 节 `L188-L191`）：当 `db_path == ":memory:"` 时，`open_sync_connection(":memory:", ...)` 仍应使用 `isolation_level=None`、`row_factory = sqlite3.Row`、`check_same_thread=False`，设 `busy_timeout=5000`、按需 `foreign_keys=ON`，但**必须跳过 `PRAGMA synchronous=NORMAL`（及不建任何磁盘目录/文件）**。
  3. **空白路径与 `Path` 入参防御**：统一判定 `str(db_path) == ":memory:"`；对非 `:memory:` 若 `not str(db_path).strip()` 应直接抛 `ValueError`，防止意外在工作目录打开 `.` 或触发 `"or"` 隐式回退生产库。
  4. **无隐式事务切换证实**（Python `3.12.14` + SQLite `3.53.1` 实测）：`isolation_level=None` 下执行 `SELECT` 和 DDL (`CREATE TABLE` / `PRAGMA`) 均保持 `conn.in_transaction == False`；仅显式 `BEGIN IMMEDIATE` 后置为 `True`，`COMMIT` / `ROLLBACK` 后复位 `False`。不改变任何现有纯 `SELECT` 或已有 `conn.commit()` 单测行为（只要写逻辑放进 `sync_transaction`）。

### 4.2 `:memory:` 私有连接与并发锁语义

- **契约核验**：设计要求每个 `:memory:` Store 实例独享 `self._in_memory_conn`，`tests/` 检索证实当前**不存在**跨连接共享 `:memory:` 假设，也**不存在** `file::memory:?cache=shared` 或 `sqlite3` 测试直访 `_in_memory_conn` / `_get_connection` 的断言。
- **发现的未闭合/待明确边界**：
  1. **跨线程假阳性 `NestedTransactionError` 边界**：`open_sync_connection(":memory:")` 配置 `check_same_thread=False` 后，若多线程并发调用同一个 `:memory:` Store 实例，线程 A 执行 `BEGIN IMMEDIATE` 会使 `self._in_memory_conn.in_transaction == True`，此时线程 B 进入 `sync_transaction` 检查 `if conn.in_transaction:` 会**误报 `NestedTransactionError`** 或触发跨线程 SQLite cursor 状态错乱。**规范要求**：每个持有 `_in_memory_conn` 的 Store 必须配置实例级 `threading.RLock`，在所有访问 `_in_memory_conn` 的读写/事务/关闭方法外加锁——从而保证**多线程顺序排队等待不误报，同线程嵌套重入仍 100% 稳定抛出 `NestedTransactionError`**。
  2. **重复 `close()` 与 `close()` 后再使用（Use-After-Close）**：`Store.close()` 必须完全幂等（先判断 `self._in_memory_conn is not None`，关闭并置 `None`；磁盘 Store 为安全幂等空操作），推荐实现 `__enter__`/`__exit__`。针对 `:memory:` Store 一旦显式调用 `close()` 即销毁内存库，建议记录 `self._closed = True` 并在关闭后再次操作时抛出明确错误（如 `RuntimeError("Store connection is closed")`），避免 `_get_connection()` 懒重建出一张无表空白库引发迷惑性 `no such table` 错误。
  3. **Python `3.12.14` `ResourceWarning` 实测特性**：子进程实测 `python -W error::ResourceWarning` 下 CPython `3.12.14` 的 `sqlite3.Connection.__del__` 未主动触发 `ResourceWarning`（属 Python `3.13+` 收紧特性），但 Phase 1 仍应通过 `Store.close()` 与 `test_persistence_sqlite.py` 显式关闭验证零泄漏，向前兼容 Python 3.13+。

### 4.3 `sync_transaction(db_path, *, in_memory_conn=None, enable_foreign_keys=True, enable_sqlite_vec=False)`

- **契约核验**：磁盘路径自建自关、`:memory:` 复用 `in_memory_conn` 且绝不关闭、`if conn.in_transaction:` 首步抛 `NestedTransactionError`（带 `db_path` 与连接上下文信息）、`BEGIN IMMEDIATE` 启动、正常 `COMMIT`、异常 `ROLLBACK`。
- **发现的未闭合/待明确边界**：
  1. **`NestedTransactionError` 分支禁触外层事务**：当 `conn.in_transaction` 在进入前已为 `True` 时，必须**在 `try` 之外**（或标记 `began_tx = False`）直接抛 `NestedTransactionError`，绝对不得执行 `ROLLBACK` 或 `close()`，保护调用方外层事务与 `:memory:` 连接。
  2. **`COMMIT` 失败与 `ROLLBACK` 二次异常保护**：若业务 SQL 或 `COMMIT` 本身抛出 `OperationalError`（如 `SQLITE_BUSY`、磁盘满），进入异常处理时仅当 `began_tx and conn.in_transaction` 时执行 `conn.execute("ROLLBACK")`，且必须用 `try ... except sqlite3.Error:` 包裹 `ROLLBACK` 自身可能出现的断连失败，确保：(a) 原始主异常不丢失；(b) `finally` 中新建的磁盘连接一定被 `conn.close()`。
  3. **显式有限重试边界**：设计文档第 12.3 节（`L785`）与第 15.1 节（`L879`）提及"对纯本地 SQLite 短写事务遇到瞬时 `SQLITE_BUSY` 可带有限次数短退避重试"，但未规定由 `sync_transaction()` 内部重试还是调用方控制。由于在写逻辑中途任意重放 caller 回调不安全，**仅允许在 `BEGIN IMMEDIATE` 获取写锁阶段（进入业务回调前、且仅限新建连接或未处于外层事务时）做极短有限重试（如最多 2 次），严禁在 `yield` 返回给调用方之后自动重复执行调用方代码块**（否则可能违背单次执行契约）。更稳妥做法：Phase 1 基础 `sync_transaction` 不做内部盲重试，仅靠 `busy_timeout=5000`；若需要并发写重试测试 helper，限定于仅重试 `BEGIN IMMEDIATE` 或幂等短事务入口并明确注记。

### 4.4 `load_sqlite_vec(conn)`

- **实测验证**：`sqlite-vec==0.1.9` 未开 `enable_load_extension(True)` 时 `sqlite_vec.load(conn)` 报 `OperationalError: not authorized`；开启加载并 `finally: conn.enable_load_extension(False)` 后，`SELECT vec_version()` 返回 `'v0.1.9'`，且 `vec0` 插入/删除/KNN `MATCH` 与 `conn.backup()` 均完全正常。
- **去重实现边界**：设计要求避免在 `_in_memory_conn` 重复加载（`L197`）。实测可通过 SQLite 零异常元数据表判定是否已加载：
  `SELECT 1 FROM pragma_function_list WHERE name = 'vec_version'`（加载前返回 `[]`，加载后返回 `[(1,)]`），无需吞掉 `OperationalError` 试探调用。`enable_load_extension(False)` 必须放在 `finally:`，确保 `sqlite_vec.load` 抛任何异常后权限立即闭锁。

### 4.5 `initialize_sqlite_database()` & `migrations.py` (`v0 -> v1`)

- **盘面与 Schema 真实性裁决（回应审计要求第 5 节）**：
  - 经对 `data/jarvis_studio.db` 与 `data/jarvis_vec.db` 的实际 `.schema` 核验：**Phase 1 的 `v0 -> v1` 不存在任何结构变更或字段新增**（既有库已拥有全部 4 张业务表、3 个应用索引及向量库全部表与索引）。因此 **Phase 1 不应制造任何破坏性或形式主义 `ALTER TABLE` 迁移 DDL**，`v0 -> v1` 的本质是**双库基线对齐（表与索引存在性幂等确认）+ `PRAGMA user_version = 1` 基础设施建立 + 备份校验链路落成**。
- **必须明确的 4 个关键实施边界**：
  1. **多 Store 共用 `jarvis_studio.db` 初始化顺序无关性**：`SessionService`、`MemoryStore`、`TraceStore` 默认共库。若首次全新库先构建 `SessionService`，其触发 `run_app_migrations()` 若尝试仅对 `ltm_memories` 建索引而不建表将直接报错 `no such table: ltm_memories`；若先置 `user_version = 1`，后续 `MemoryStore` 初始化将跳过 `v0 -> v1`。因此：**`run_app_migrations()` `v0 -> v1` 应在 `BEGIN IMMEDIATE` 内幂等执行 4 张基础表及索引的 `CREATE TABLE/INDEX IF NOT EXISTS`（零删改历史数据），同时保留各 Store 自身 `_init_db()` 对自身表的幂等建表（供 `:memory:` 独立库使用）**。
  2. **向量库禁用硬编码 `vec_chunks` 维度**：`tests/test_rag.py:55, 112` 传入 `dimension=32`，而生产默认 `1536`。`run_vec_migrations()` **严禁硬编码创建 `vec_chunks USING vec0(..., embedding float[1536])`**；`v0 -> v1` 仅应幂等确保 `doc_files`、`doc_chunks` 与 `idx_chunk_doc` 并推进 `user_version = 1`，动态 `vec_chunks` 虚拟表保留在 `SqliteVecStore._init_db()` 中按实例 `self.dimension` 创建。实测 `backup()` 后不加载 `sqlite_vec` 直接只读打开执行 `PRAGMA quick_check(1)` 仍返回 `('ok',)`，完整兼容带 `vec0` 的向量库备份校验。
  3. **跨线程与同秒同进程 `v0 -> v1` 竞态与命名冲突**：
     - 备份文件命名规则 `<db_stem>.bak.vX_to_vY.<YYYYMMDDTHHMMSSZ>_<pid>[_<seq>].db`（`L958`）中时间戳粒度仅 1 秒、同一 pytest 或服务进程内 `<pid>` 恒定，因此 **`<seq>` 必须强制生成（如全局线程安全自增序号）并按需冲突避让，绝不可省略为可选项**。
     - 并发多 Store 同时触发同一文件库迁移时，**进入 `BEGIN IMMEDIATE` 后必须再次读取 `PRAGMA user_version`**（Double-Check），若已被先行线程升至 `>= TARGET_VERSION` 则直接退出，避免重复跑 `v0 -> v1`；同进程按规范化路径加轻量锁亦可消除冗余双份备份。
     - 只读校验 URI 在 Windows 含空格/特殊字符路径下推荐 **`f"{Path(bak_path).resolve().as_uri()}?mode=ro"`**（已在 Python `3.12.14` 实测 `sub dir` 与 `dir#1` 路径均返回 `('ok',)`）。
  4. **":memory:" 跳过规则**：`:memory:` 数据库由设计 `L188, L732, L957` 规定跳过 `WAL` 与磁盘热备份。Store 应对 `:memory:` 仅在私有内存连接直接幂等建表，不对内存连接执行 `initialize_sqlite_database()` 的 WAL/目录逻辑及磁盘备份逻辑。

---

## 5. Test Compatibility Matrix

全面排查 `tests/` 下全部 20 个测试文件，涉及 4 个 Store 及 SQLite 连接的关键测试兼容性如下：

| 测试文件与位置 | 使用的 Store / 连接模式 | 当前行为与 Fixture 生命周期特征 | Phase 1 兼容风险与结论 |
| :--- | :--- | :--- | :--- |
| `tests/conftest.py` (`L1-L9`) | 全局环境变量预置 | 仅设 `CHECKPOINT_BACKEND="memory"`、`WORKSPACE_ROOT="./data/test_workspace"`；**未覆盖 `SQLITE_DB_PATH` 与 `VECTOR_DB_PATH`**。 | **关键关注点**：未隔离时跑全量测试会直击真实 `data/jarvis_studio.db` 与 `data/jarvis_vec.db`（触发首次 WAL 与 `.bak` 写入）。Phase 1 允许原样保留并在报告中明示（设计将该文件列入 Phase 2），但推荐确认是否提前隔离。 |
| `tests/test_memory.py` (`L18-L30`, `L83-L148`) | `MemoryStore(db_path=":memory:")` | 1. `memory_store` fixture teardown **仅调用 `store.clear()`，不调 `store.close()`**。<br>2. `memory_manager` (`L29`) 调 `get_memory_manager(store=memory_store)` 设置全局单例且**从未复原**。<br>3. `test_memory_api_endpoints` (`L100`) 与 `test_cli_memory_commands` (`L135`) 无 fixture 参数，完全靠执行顺序偶然复用前述 `:memory:` 单例！ | **潜在陷阱**：Phase 1 因不修改 `tests/test_memory.py` 且 Python 3.12 不触发 `sqlite3` `ResourceWarning`，全量跑 `124 passed` 通过。但**严禁**未来在 `memory_store` teardown 加 `store.close()` 而不复位 `_global_memory_manager`，否则 `L100`/`L135` 立即因连接已关闭失败；单跑 `-k test_cli_memory_commands` 时会命中真实 `data/jarvis_studio.db` 并残留测试记忆。 |
| `tests/test_rag.py` (`L51-L126`, `L128-L153`) | `SqliteVecStore(db_path=tmp_path/..., dimension=32)` 及无参 `SqliteVecStore()` | 前两个单测使用 `tmp_path` + `dimension=32`（`32` 维小向量）；后两个 API/CLI 用例 (`L128`, `L145`) 无参构造 `SqliteVecStore()`，直接连 **`data/jarvis_vec.db`**。 | 1. **必须**保持 `vec_chunks` 按实例 `dimension` 创建，否则 `L55`/`L112` 报错。<br>2. `L128`/`L145` 在 Phase 1 首跑会把当前 `DELETE` 模式的 `data/jarvis_vec.db` (`6.4 MB`) 切为 `WAL` 并在 `data/` 生成一份 `.bak.v0_to_v1.*.db` 备份。 |
| `tests/test_sessions_api.py` (`L9-L57`) | `TestClient(create_app())` + `get_session_service()` | 直接连默认 `data/jarvis_studio.db` 完成 Session 增删改查后删除该测试 Session。 | 功能兼容（无嵌套、单层事务）；首跑会触发真实 `data/jarvis_studio.db` 的 `v0 -> v1` 备份与升版。 |
| `tests/test_streaming.py` (`L30-L82`) | `TestClient(create_app())` & `AgentService(graph=...)` | 未隔离 `SessionService` 与 `TraceStore`，直接向真实 `data/jarvis_studio.db` 写入 `stream_test_01` 与 `test_stream_thread` 会话/Trace。 | 功能兼容，但持续向本地 `data/jarvis_studio.db` 写入测试数据。 |
| `tests/test_cli.py` (`L30-L44`) | `CliRunner` (`sessions`, `chat --no-mcp`) | 每次运行 `test_cli_chat_immediate_exit` 调用 `get_or_create_session(None)`，**在真实 `data/jarvis_studio.db` 永久新增 1 条 `'新会话'` 记录**（本轮审计已实测复现）。 | 功能兼容；Phase 1 需知悉该既有测试本身会修改真实 `data/jarvis_studio.db`。 |
| `tests/test_security_hardening_1a2.py` (`L59-L87`, 其余 Sprint 1A 测试) | `SessionService(db_path=tmp_path / ...)` + `MemorySaver` + `HITL` | `isolated_services` 隔离了 `_global_session_service` 与 `_global_agent_service`，但**未隔离 `_global_trace_store`**，Trace 仍落入真实 `data/jarvis_studio.db`。无任何直接访问 Store 私有连接属性的断言。 | 100% 兼容，**零破坏 Sprint 1A 冻结契约**（授权、lease、`finalize_execution`、线程隔离、Fail-Closed 均不受影响）。 |
| `tests/test_thinking_and_guard.py` (`L90-L124`) | `TraceStore(db_path=":memory:")` | 测试内构造 `:memory:` `TraceStore`，结束未显式 `close()`。 | 完全兼容，Python 3.12 `-W error::ResourceWarning` 下零告警；建议 Phase 1 新单测中按新规范验证 `store.close()` 幂等性。 |
| `tests/test_process_platform.py`, `test_process_runner.py`, `test_shell_runtime_integration.py`, `test_authorization_patch.py`, `test_hitl_security.py`, `test_file_path_security.py` | 进程运行时 / 安全测试 | 不直接依赖 4 个业务 SQLite Store（仅部分集成测试经由 Sprint 1A/1B）。 | 100% 兼容，**零破坏 Sprint 1B 进程树清理与 `CancelledError` 冻结契约**。 |

---

## 6. Dependency Compatibility

核验 `pyproject.toml`（`L1-L49`）、`uv.lock`（`L9-L16`, `L875-L920`, `L1716-L1726`）与 `.venv` 运行环境（通过 `uv lock --check` 与实际 Python 导入实测）：

| 依赖项 | 设计目标声明 | 仓库与锁文件当前状态 | 环境实测与平台兼容性结论 |
| :--- | :--- | :--- | :--- |
| **Python 版本** | `>=3.12` | `pyproject.toml:6` (`>=3.12`) | 本机 `.venv` 实测 `3.12.14` 正常；`sqlite3` 自带 SQLite `3.53.1`。 |
| **`sqlite-vec`** *(Phase 1 唯一允许新增)* | `pyproject.toml` 显式补充 **`sqlite-vec>=0.1.9`** | 当前仅作为 `langgraph-checkpoint-sqlite==3.1.1` 的传递依赖存在于 `uv.lock:1716-1726`（版本 **`0.1.9`**），`pyproject.toml` 尚未显式列出。 | 1. **兼容且对齐**：`0.1.9` 严格满足 `>=0.1.9`，`uv lock --check` 当前通过（92 packages）。<br>2. **多平台 Wheel 完备**：`uv.lock:1720-1725` 包含 `win_amd64`、`manylinux_2_17_x86_64`、`manylinux_2_17_aarch64`、`macosx_10_6_x86_64`、`macosx_11_0_arm64`。<br>3. **Windows API 实测正常**：`enable_load_extension(True) -> sqlite_vec.load(conn) -> enable_load_extension(False)` + `vec0` + `serialize_float32` + `backup()` + `quick_check(1)` 全部通过。<br>4. **待补验证**：Linux CI 上实际 SQLite C 库的 `enable_load_extension` 与 `manylinux` wheel 加载必须在 Linux CI 实机验证（Windows 环境无法代替）。 |
| **`aiosqlite`** *(Phase 2 范畴，Phase 1 严禁引入)* | `aiosqlite>=0.22.1`（延至 Phase 2 显式声明） | 当前作为 `langgraph-checkpoint-sqlite==3.1.1` 的传递依赖存在于 `uv.lock:9-16`（版本 **`0.22.1`**，`py3-none-any`）。 | **边界准确**：Phase 1 无任何异步 SQLite 调用，保持 `aiosqlite` 延迟至 Phase 2 引入完全合理，下限 `>=0.22.1` 与锁文件完全一致。 |
| **`langgraph-checkpoint-sqlite` / `langgraph`** *(Phase 2 预览审计)* | Phase 2 引入 `AsyncSqliteSaver` | `pyproject.toml:22` 声明 `langgraph-checkpoint-sqlite>=3.1.1`；锁文件与实测安装：`langgraph==1.2.12`、`langgraph-checkpoint==4.2.0`、`langgraph-checkpoint-sqlite==3.1.1`。 | 只读检查确认：`AsyncSqliteSaver.from_conn_string(conn_string: str) -> AsyncIterator[AsyncSqliteSaver]` 与 `__init__(self, conn: aiosqlite.Connection, *, serde=None)` 签名与设计第 3.2/12.2/15.2 节循环作用域生命周期设计兼容，Phase 1 冻结不动 `saver.py` 正确。 |

---

## 7. Phase 1 File Scope Audit

逐一核对设计文档第 15.1 节（`L858-L871`）列出的 Phase 1 Allowlist：

| 文件路径 | Allowlist 分类 | 是否确实需要修改 | 具体原因与实施范围约束 | 潜在风险 / 注意事项 |
| :--- | :---: | :---: | :--- | :--- |
| `src/jarvis_studio/core/persistence/__init__.py` | 允许新增 | **需要** | 统一导出 Phase 1 同步持久化原语与迁移入口，方便 4 个 Store 调用及后续 Phase 2/3/4 扩展。 | 只允许导出同步原语与异常类（严禁导入 `runtime`、`aiosqlite` 或 `core.observability`）。 |
| `src/jarvis_studio/core/persistence/sqlite.py` | 允许新增 | **需要** | 落地 `NestedTransactionError`、`open_sync_connection()`、`sync_transaction()`、`initialize_sqlite_database()`、`load_sqlite_vec()`。 | 必须保持**纯标准库 + `sqlite_vec` 叶子模块**（用 stdlib `logging`，严禁导入 `core.observability.logger`）；不得含 `aiosqlite` 或 `AsyncSqliteSaver` 引用。 |
| `src/jarvis_studio/core/persistence/migrations.py` | 允许新增 | **需要（但严格克制内容）** | 落地双库独立版本管理：`run_app_migrations()`、`run_vec_migrations()`、唯一命名备份（`conn.backup()` + 必带 `<seq>`）、`as_uri() + "?mode=ro"` `PRAGMA quick_check(1)` 校验。 | **严禁硬编码 `vec_chunks` 维度、严禁无表建索引、严禁在业务写事务内切 WAL、严禁新增 `agent_tasks` / `hitl_*` 表结构**。Phase 1 `v0 -> v1` 仅作基线版本对齐，不做结构改动。 |
| `tests/test_persistence_sqlite.py` | 允许新增 | **需要** | 验证双库 WAL 初始化与隔离 `user_version`、跨表基线建表、禁止嵌套事务抛 `NestedTransactionError` 不损外层、`sqlite_vec` 加载与异常闭锁、`:memory:` 隔离与 `close()`、唯一备份与损坏拦截、多线程并发短写。 | 必须全部基于 `tmp_path` 与 `:memory:`，不可触碰默认 `data/*.db`；需在最后显式 `close()`，并通过 `-W error::ResourceWarning` 与 `ruff check`。 |
| `src/jarvis_studio/service/session_service.py` | 允许修改 | **需要** | 委托 `sqlite.py` + `migrations.py`，统一 `:memory:` `RLock`、短读连接与单层 `sync_transaction`，增加幂等 `close()`（与可选上下文管理）。 | **严格禁止修改业务逻辑**（保持 `INSERT OR REPLACE` 与现有 `add_message` 两步事务边界，且保持 `get_or_create_session`/`get_session` 在 `sync_transaction` 外）。避免引入新 Ruff 问题（当前含 6 个历史 `BLE001`/`S110`）。 |
| `src/jarvis_studio/memory/store.py` | 允许修改 | **需要** | 委托 `sqlite.py` + `migrations.py`，**修复 `_init_db()` 缺少 `try/finally: conn.close()` 的资源泄漏**，补 `close()` 与 `:memory:` `RLock`。 | 保持 `if db_path is None:` 与既有 CRUD 签名不变；当前文件 Ruff 0 报错，修改后必须保持 Ruff 0 报错。 |
| `src/jarvis_studio/core/observability/trace_logger.py` | 允许修改 | **需要** | 委托 `sqlite.py` + `migrations.py`，移除 `_get_connection()` 每次磁盘调用 `Path.mkdir`，补 `close()` 与 `:memory:` `RLock`。 | 保持当前 `TraceStore.__init__` 对 `:memory:` 的可用性、不新增 `prune` 或 `task_id` 列；当前文件 Ruff 0 报错，修改后须保持 0 报错。 |
| `src/jarvis_studio/rag/store.py` | 允许修改 | **需要** | 委托 `sqlite.py` + `migrations.py`，改由 `enable_foreign_keys=False` + `enable_sqlite_vec=True` 瞬时加载并 `finally` 闭锁 `enable_load_extension(False)`；确保 `data/jarvis_vec.db` 初始化 `WAL`；补 `close()` 与 `:memory:` `RLock`。 | 保留 `vec_chunks USING vec0(... float[{self.dimension}])` 在 `_init_db()` 动态按实例创建；`_delete_doc_internal(conn, ...)` 保持接收外层 `conn` 的纯内部 helper 语义。 |
| `pyproject.toml` | 允许修改 | **需要** | 在 `[project].dependencies` 显式补充 **`"sqlite-vec>=0.1.9"`**。 | 严禁提前加入 `aiosqlite`。 |
| `uv.lock` | 允许修改（如有必要） | **条件需要** | 在改完 `pyproject.toml` 后运行 `uv lock --check`；若提示锁文件元数据需同步（因为直属依赖列表变化），仅更新项目自身 dependency 声明而不升级任何包版本（保持 `sqlite-vec==0.1.9`）。 | 严禁顺手升级无关传递依赖或改动 `aiosqlite` / `langgraph-*` 锁版本。 |
| `tests/conftest.py` *(Allowlist 之外，Phase 2 清单)* | 禁改（当前规划在 Phase 2） | **Phase 1 原则不改；建议架构确认** | 当前不隔离 `SQLITE_DB_PATH`/`VECTOR_DB_PATH` 会使首次 `pytest` 对本地真实 `data/*.db` 触发一次性 `WAL`+`v0->v1` 备份迁移。 | **若不允许在 Phase 1 修改 `tests/conftest.py`**，必须在实施前明示这一预期副作用（首次运行 `pytest` 会在本地 `data/` 生成 1 组 `.bak.v0_to_v1.*.db`）；如架构授权提前把 `tests/conftest.py` 临时库环境变量隔离纳入 Phase 1，则可彻底消除本地测试污染。 |

**已核实 Phase 1 禁改范围**（无需改动且不可提前触碰）：`agent/checkpoints/saver.py`、`agent/hitl/*`、`agent/nodes/*`、`service/agent_service.py`、`api/*`、`cli/*`、`core/persistence/runtime.py`（Phase 2）、`tests/test_security_hardening_1a2.py`、`tests/test_memory.py`、`tests/test_rag.py` 等所有既有测试。

---

## 8. Risk Register

按问题严重度分级，当前无 **P0** 与 **P1** 阻断项，存在 **5 项 P2 边界风险** 与 **3 项 P3 注意事项**：

| 编号 | 等级 | 风险描述 | 真实源码/实测证据 | 触发条件 | 影响 | 缓解/处理方式 |
| :--- | :---: | :--- | :--- | :--- | :--- | :--- |
| **R-01** | **P2** | **`core/persistence` 引入 `observability.logger` 导致循环导入崩溃** | [`observability/__init__.py:3-9`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/observability/__init__.py#L3-L9) 在导出 `logger` 的同时立即导入 `trace_logger.py`。 | `core/persistence/sqlite.py` 或 `migrations.py` 顶层 `from jarvis_studio.core.observability.logger import logger`，且任意外部模块（如 `MemoryStore`、`test_persistence_sqlite.py`）先于 `observability` 导入 `core.persistence`。 | 必然抛 `ImportError: cannot import name ... from partially initialized module`。 | **`core/persistence/` 严格做零内部业务依赖的叶子层**：仅使用标准库 `logging.getLogger(__name__)`（及 `sqlite_vec`），禁止顶层导入任何 `jarvis_studio.*` 其他包。 |
| **R-02** | **P2** | **共用 `jarvis_studio.db` 多 Store 首建顺序列失配、并发同秒备份撞名、及 `vec_chunks` 维度被迁移写死** | 3 个应用 Store 均默认 `data/jarvis_studio.db`；[`test_rag.py:55,112`](file:///d:/myProject/JarvisStudio/tests/test_rag.py#L55) 传入 `dimension=32` 而 `config/models.py:94` 默认 `1536`；设计 `L958` 将 `<seq>` 标为可选。 | 1. 空白库上 `SessionService` 先跑完 `run_app_migrations()` 若尝试建它表索引将报无表，或升完 `user_version=1` 后新 Store 若删去自建表逻辑会缺表；<br>2. 同进程 1 秒内跑多组迁移无 `<seq>` 会冲突；<br>3. `run_vec_migrations()` 若自带 `vec_chunks float[1536]` 会破坏 `dimension=32` 测试。 | 建表/建索引失败、`FileExistsError`/覆盖备份、或 RAG 维度测试报错。 | 1. `v0 -> v1` 在 `run_app_migrations` 内幂等建全 4 张应用基础表与索引（不增删字段），同时保留各 Store 自身 `_init_db()` 供 `:memory:` 与兜底；<br>2. `run_vec_migrations` 不得写死 `vec_chunks` 维度，保留在 `SqliteVecStore._init_db()`；<br>3. 强制 `<seq>` 递增且 `BEGIN IMMEDIATE` 内 Double-Check `user_version`；<br>4. 备份 URI 用 `Path.resolve().as_uri() + "?mode=ro"`。 |
| **R-03** | **P2** | **多线程访问同一个 `:memory:` Store 实例误触发 `NestedTransactionError` 与游标竞争** | 设计 `L716, L718` 规定 `:memory:` 使用单条 `check_same_thread=False` 连接并在 `sync_transaction` 首查 `if conn.in_transaction:` 抛 `NestedTransactionError`。 | 线程 A 持 `_in_memory_conn` 事务未提交时，线程 B 进入同一个 `:memory:` Store 的 `sync_transaction` 或只读查询。 | 线程 B 读到 `conn.in_transaction == True`，**非嵌套并发场景被误抛 `NestedTransactionError`** 或游标交叉破坏。 | 在 4 个 Store 内为 `:memory:` 实例加**可重入锁 `threading.RLock()`** 包裹读写及事务入口：跨线程排队等待不误报，同线程嵌套仍可重入进锁并稳定捕获 `conn.in_transaction == True` 抛出 `NestedTransactionError`。 |
| **R-04** | **P2** | **`open_sync_connection` / `sync_transaction` 异常清理边界与 `SessionService` 外层调用误包进事务** | [`session_service.py:258,282`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L258) 分别调用 `get_session` 与 `get_or_create_session`；`L718` 强调不回滚外层注入事务。 | 1. `open_sync_connection` 设 PRAGMA 或加载 `sqlite_vec` 抛错未关连接；<br>2. `sync_transaction` 因 `NestedTransactionError` 退出或 `ROLLBACK` 自身抛 `sqlite3.Error`；<br>3. 实施者顺手把 `add_message`/`update_session_title` 全函数包入 `sync_transaction`。 | 句柄泄漏、覆盖原异常、错误回滚外层事务、或在 `:memory:` 触发自嵌套崩溃。 | 1. `open_sync_connection` 初始化异常时 `conn.close()`；<br>2. `sync_transaction` 先检 `conn.in_transaction` 再进入受保护块，`began_tx` 标记保护 `ROLLBACK`，`except sqlite3.Error` 兜底后再 `finally: close` 磁盘连接；<br>3. 保持 `add_message`/`update_session_title` 的前置/后置查询在事务外；<br>4. 用 `pragma_function_list` 查 `vec_version` 防重复加载 `sqlite_vec`。 |
| **R-05** | **P2** | **既有未隔离测试在 Phase 1 首次运行时将自动迁移、WAL 化并备份本地 `data/*.db`** | 审计实测：执行一次 `pytest` 使真实 `data/jarvis_studio.db` 增加 6 条 traces、4 条 messages、1 条 session；[`test_rag.py:128-153`](file:///d:/myProject/JarvisStudio/tests/test_rag.py#L128-L153) 直连当前 `DELETE` 模式的 `data/jarvis_vec.db`（6.4 MB）；[`tests/conftest.py:1-9`](file:///d:/myProject/JarvisStudio/tests/conftest.py#L1-L9) 未隔离 DB 路径且被划归 Phase 2。 | Phase 1 接通 `initialize_sqlite_database` + `migrations` 后首次执行验收门禁 `pytest -q -W error::ResourceWarning`。 | 不会导致测试失败或损坏数据，但会将开发者本地真实 `data/jarvis_vec.db` 转为 `WAL`、提升两库 `user_version` 为 `1`，并在 `data/` 生成一份 ~6.6 MB `.bak.v0_to_v1.*.db` 快照及持续污染 `data/jarvis_studio.db`。 | 若严格保持 Phase 1 白名单不改 `tests/conftest.py`，请在执行验收测试前备份或心理预期首次测试会触发一次性迁移与 `.bak` 生成（仅发生一次）；或由架构特批将 `tests/conftest.py` 设置临时 `SQLITE_DB_PATH` 与 `VECTOR_DB_PATH`（及隔离全局单例）提前到 Phase 1（优先建议提前，无破坏性）。 |
| **R-06** | **P3** | **`tests/test_memory.py` 跨用例依赖全局 `_global_memory_manager` 泄漏** | [`tests/test_memory.py:18-30, 100-148`](file:///d:/myProject/JarvisStudio/tests/test_memory.py#L18-L30) teardown 只 `clear()` 不复位 `_global_memory_manager`，后续 2 个用例无 fixture 依赖前序残留。 | 未来若在 `memory_store` teardown 改为调 `store.close()` 而未重置 `_global_memory_manager`，或单跑 `-k test_cli_memory_commands`。 | 前者必炸 use-after-close，后者直写真实 `data/jarvis_studio.db`。 | Phase 1 严格不改动 `tests/test_memory.py`（维持 `clear()`）；将 fixture 隔离治理统一归入 Phase 2 测试清欠清单。 |
| **R-07** | **P3** | **Ruff 既有 29 项全仓错误与跨平台 Linux CI 缺口** | 本机实测 `ruff check src tests` 报 29 处旧警告（目标 Store 中 7 处）；本机为 Windows，无法本地实跑 POSIX 测试与 `sqlite-vec` manylinux wheel。 | 在 CI 直接以全仓 `ruff check src tests` 做强门禁，或仅依赖 Windows 本机测试即宣布跨平台通过。 | 非增量旧警告阻塞提交，或 Linux 上特定 SQLite 编译选项导致 `enable_load_extension` 行为差异未被及时发现。 | Phase 1 Ruff 检查先针对新增/修改文件范围确保 0 新增告警（尽量顺手维持目标文件不劣化）；**Phase 1 合并前必须在 Linux CI 实机跑通全量 `pytest` 与 `sqlite-vec` 扩展加载验证**（设计 `L987` 已有要求）。 |
| **R-08** | **P3** | **`:memory:` Store 显式 `close()` 后重复使用与 `MemoryStore` 空串入参语义微差** | `SessionService`/`TraceStore`/`SqliteVecStore` 用 `db_path or ...`，`MemoryStore` 用 `if db_path is None:`；当前无 `_closed` 标志。 | 调用方 `store.close()` 后再调查询方法，或误传 `db_path=""`。 | 关闭后静默重建空内存库并报迷惑性 `no such table`；或 `MemoryStore("")` 打开当前目录 `"."` 失败。 | `:memory:` Store 在 `close()` 置 `_closed = True` 并在使用时抛明确 `RuntimeError`；`open_sync_connection` 拒绝空白非 `:memory:` 路径抛 `ValueError`。 |

---

## 9. Recommended Phase 1 Implementation Plan

仅提出最小、稳健且无越界的实施建议（本轮审计未开始编码）：

1. **最小改动与精确文件范围**：
   - 新增：`src/jarvis_studio/core/persistence/__init__.py`、`sqlite.py`、`migrations.py`、`tests/test_persistence_sqlite.py`。
   - 修改：`session_service.py`、`memory/store.py`、`core/observability/trace_logger.py`、`rag/store.py`、`pyproject.toml`（仅加 `"sqlite-vec>=0.1.9"`）、按需 `uv.lock`（仅 `uv lock` 对齐，不升级包版本）。
   - *(可选待架构拍板)*：建议审慎评估是否在 Phase 1 授权同步将 `tests/conftest.py`（会话级 `tmp_path` 隔离 `SQLITE_DB_PATH` / `VECTOR_DB_PATH`）一并加入白名单，彻底防止测试污染本地真实 `data/*.db`。
2. **推荐实施顺序**：
   - **Step 1（依赖与无环基础设施）**：在 `pyproject.toml` 补 `"sqlite-vec>=0.1.9"` 并校验 `uv.lock`；编写纯叶子层 `core/persistence/sqlite.py`（仅依赖标准库 + `sqlite_vec`，使用 stdlib `logging`）。明确实现 `NestedTransactionError`、`open_sync_connection`（失败关闭、`busy_timeout=5000`、`synchronous=NORMAL`、按需 `foreign_keys`、`:memory:` 跳过 `synchronous`）、`load_sqlite_vec`（`pragma_function_list` 防重 + `try/finally: enable_load_extension(False)`）、`sync_transaction`（先查 `in_transaction` 防嵌套、外层嵌套禁触 `ROLLBACK`、`BEGIN IMMEDIATE`、`ROLLBACK` 异常保护）及 `initialize_sqlite_database`（无事务校验后切 `WAL` 并验证返回值 `'wal'`，`:memory:` 跳过）。
   - **Step 2（双库独立基线迁移与备份校验）**：编写 `core/persistence/migrations.py`。对磁盘库实现必带单调 `<seq>` 的唯一热备份（`conn.backup()` -> `Path.resolve().as_uri() + "?mode=ro"` -> `PRAGMA quick_check(1)` + `PRAGMA user_version`），备份/校验异常立即中止；随后进 `BEGIN IMMEDIATE` 后 Double-Check `user_version`，幂等补齐基线表/索引并推进 `PRAGMA user_version = 1`（`run_app_migrations` 覆盖 4 张核心表，避免先初始化任一 Store 后另两表索引缺失；`run_vec_migrations` 仅覆盖 `doc_files`/`doc_chunks`/`idx_chunk_doc`，绝不写死 `vec_chunks` 维度）。
   - **Step 3（逐一改造 4 个 Store，保持既有契约）**：先 `MemoryStore`（顺手修复 `_init_db` 泄漏）-> `TraceStore` -> `SessionService`（保留 `add_message` 与 `update_session_title` 的既有两段式边界于 `sync_transaction` 外）-> `SqliteVecStore`（`enable_foreign_keys=False`, `enable_sqlite_vec=True`，`_init_db` 动态建 `vec_chunks`，`_delete_doc_internal` 复用 `conn`）。4 者均接入 `:memory:` `RLock`、幂等 `close()`、`__enter__`/`__exit__` 及关闭后护栏。
   - **Step 4（专项测试与全量回归验证）**：编写 `tests/test_persistence_sqlite.py` 覆盖第 15.1 节全部 4 类验收标准及本审计识别的边界（多线程并发访问同 `:memory:` Store 不误报 `NestedTransactionError`、同线程必报 `NestedTransactionError`、同秒同进程多次迁移备份无重名冲突、先建 `SessionService` 再建 `MemoryStore` 共库正常、`vec0` 自定义 32 维正常且扩展权限必闭锁、损坏备份阻断迁移且不前移 `user_version`、`MemoryStore._init_db` 失败不泄漏句柄）。
3. **每一步验证命令与回滚边界**：
   - 每步后运行相关单测：`pytest -q -W error::ResourceWarning tests/test_persistence_sqlite.py tests/test_memory.py tests/test_rag.py tests/test_sessions_api.py tests/test_thinking_and_guard.py`。
   - 完成后跑全量：`pytest -q -W error::ResourceWarning` + 新增/修改文件 `ruff check` + `uv lock --check`；合并前务必在 Linux CI 验证 `sqlite-vec>=0.1.9` 与全部测试。
   - **回滚边界**：Phase 1 无结构变更、无 `aiosqlite`/Checkpointer 切换、无 HITL 或 Task 逻辑修改，单一功能提交可随时 `git revert` 无损回退（若本地磁盘库被升至 `v1`，因表结构与 `v0` 完全一致，旧代码同样向后兼容读取，必要时亦可凭备份还原）。

---

## 10. Final Verdict

**`APPROVED WITH NOTES`**

- **裁决理由**：现有源码的 4 个目标 Store 事务结构清晰、不存在嵌套事务冲突、`sqlite-vec==0.1.9` 运行时及双库备份/只读校验链路已在本机实测验证可用，Sprint 1A/1B 冻结契约与测试未发现结构性不兼容，**不存在 P0 或 P1 阻断问题**，可以立即推进 Phase 1 实施。
- **进入 Phase 1 的前置约定 Notes（对应 5 项 P2 边界）**：
  1. `core/persistence/` 禁止导入 `jarvis_studio.core.observability`（使用标准库 `logging`），彻底切断潜在循环导入；
  2. `v0 -> v1` 仅做双库基线表/索引幂等确认与版本号标记（不写死 `vec_chunks` 维度、强制 `<seq>` 序列与事务内 Double-Check `user_version`、使用 `as_uri() + "?mode=ro"` 校验备份）；
  3. 4 个 Store 的 `:memory:` 模式必须带 `threading.RLock` 与幂等 `close()`（含关闭后保护）；
  4. 严格落实 `open_sync_connection` / `sync_transaction` / `load_sqlite_vec` 的异常关闭、`ROLLBACK` 保护及 `SessionService` 非嵌套边界；
  5. 实施者与评审者须知悉现有未隔离测试首次运行会触发本地 `data/*.db` 一次性 `v0 -> v1` 迁移与 `.bak` 快照写入（或由架构授权将 `tests/conftest.py` 临时路径隔离提前纳入 Phase 1），且 Phase 1 最终合并前须补齐 Linux CI 实机验证。
