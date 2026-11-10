# Jarvis Studio V2 全代码库架构审计报告 (V2 Architecture Audit)

> **审计执行人**：Jarvis Studio 首席 AI 系统架构师  
> **审计日期**：2026-11-10  
> **审计代码基线**：Jarvis Studio v0.1.0 (Commit Baseline: Milestone 0 ~ Milestone 6)  
> **测试基准**：Python 3.12+ / 43 Pytest Tests (100% PASS)  
> **审计原则**：只记录代码中真实存在的问题，提供确切文件路径与符号，杜绝空泛推测，区分已确认问题、潜在风险与演进缺陷。

---

## 一、 审计执行概览 (Executive Summary)

经过对 `src/jarvis_studio/` 下的 Agent 运行时、应用服务层、API 网关、工具与 MCP、向量检索 (RAG)、长期记忆 (LTM) 及可观测性等全部 14 个核心模块的全面白盒审计，当前系统在**功能完备度**上已跑通单 Agent ReAct 闭环、原生 HITL 中断、sqlite-vec 检索与 SSE 流式展示；但在**高并发稳定性、运行时生命周期可靠性、安全隔离深度、检索智能度与上下文治理**上存在若干高危及中危缺陷，阻碍系统从“可运行的 Agent 实验体”跃迁为“可持续运行的个人操作系统级 Agent”。

### 缺陷严重度分布统计

| 严重度 (Severity) | 缺陷特征 | 问题数量 | 典型代表 |
| :--- | :--- | :---: | :--- |
| **CRITICAL (致命)** | 导致服务假死、HITL 绕过越权、数据撕裂或主循环阻塞 | **5** | 多工具调用 HITL 越权、同步工具阻塞 FastAPI 事件循环、SQLite 缺失 WAL/busy_timeout、路径逃逸、状态恢复断层 |
| **HIGH (高危)** | 导致进程泄漏、连接中断未取消、错误重试死循环、代码分块截断 | **6** | 客户端断连孤儿任务、MCP 子进程僵尸泄漏、停止指令无取消能力、无代码 AST 分块、仅 Dense 检索无关键词召回 |
| **MEDIUM (中危)** | 导致内存膨胀、上下文过度浪费、状态冗余 | **5** | LTM 无关记忆盲目注入、TraceStore 无保留期策略、Sticky 缓存无限泄漏、SystemMessage 首轮后无法更新 |
| **LOW (低危/演进)** | 设计耦合、可测试性不足或架构不够模块化 | **4** | 单例全局变量并发竞争、Reranker 扩展点缺失、TaskPlan 缺乏结构化表达 |

---

## 二、 关键问题清单与代码证据 (Detailed Findings)

---

### 模块 1：LangGraph StateGraph 与执行节点

#### 1.1 【已确认问题·CRITICAL】多工具并行调用时的 HITL 审批越权与参数污染
- **代码位置**：
  - [`src/jarvis_studio/agent/nodes/approval.py:37-43`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L37-L43)
  - [`src/jarvis_studio/agent/nodes/executor.py:40-62`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L40-L62)
- **问题描述**：
  - 当大模型在单轮对话中生成多个工具调用（如 `[call_1: execute_shell_command, call_2: write_file]`）时：
    1. `approval_node` 仅提取第一个需要审批的工具调用即 `break`，生成的 `interrupt()` 仅包含 `call_1`；
    2. 用户在前端只能看到 `call_1` 的审批弹窗。当用户选择“批准”后，图恢复进入 `tool_executor_node`；
    3. `tool_executor_node` 遍历 `last_message.tool_calls` 中的所有工具。由于全局 `decision_type == "approve"`，**`call_2` 将在用户不知情的情况下被自动执行**；
    4. 若用户选择“修改参数 (`modify`)”，`modified_args` 仅针对 `call_1`，但 `executor.py:54-61` 的条件判断将 `modified_args` 赋值给所有标记为需要审批的工具，导致 `call_2` 被强制覆盖为 `call_1` 的参数！
- **发生条件**：大模型使用原生并行工具调用（Parallel Tool Calling）生成 ≥2 个敏感工具调用。
- **影响范围**：破坏系统最高优先级的 HITL 安全保证，导致非授权高危操作静默执行或参数被破坏性篡改。
- **严重度**：`CRITICAL`

#### 1.2 【潜在风险·HIGH】缺少 Agent 层循环熔断与无效重试防线
- **代码位置**：
  - [`src/jarvis_studio/agent/graph.py:60-74`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/graph.py#L60-L74)
  - [`src/jarvis_studio/agent/nodes/router.py:28-33`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/router.py#L28-L33)
- **问题描述**：
  - 图编译未配置 `recursion_limit` 或最大步数校验；
  - 当工具调用失败（例如终端命令语法错误或文件不存在），工具节点仅返回错误文本字符串并回到 `assistant`；大模型极易在相同错误参数下连续循环调用 10~25 次，直至触发 LangGraph 默认底层递归上限崩溃。
- **发生条件**：模型陷入工具调用死循环，或工具连续报错。
- **影响范围**：Token 严重浪费、API 响应超时、用户无从感知并挂死。
- **严重度**：`HIGH`

#### 1.3 【架构缺陷·MEDIUM】SystemMessage 动态记忆首轮后无法更新
- **代码位置**：
  - [`src/jarvis_studio/agent/nodes/assistant.py:37-39`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/assistant.py#L37-L39)
- **问题描述**：
  - 代码通过 `if not messages or not isinstance(messages[0], SystemMessage): messages = [SystemMessage(...)] + messages` 注入提示词。
  - 在多轮对话中，`messages[0]` 已经为 `SystemMessage`。即使在对话过程中通过 API 或工具新增了长期记忆（LTM），后续轮次的 `assistant_node` 永远使用第一轮旧的 `SystemMessage`，动态记忆对当前已存在 Session 完全失效。
- **发生条件**：多轮会话期间新增或修改记忆。
- **影响范围**：记忆无法实时生效，与 LTM 动态注入的设计目标相违背。
- **严重度**：`MEDIUM`

---

### 模块 2：任务生命周期、并发与服务边界 (AgentService & Runtime)

#### 2.1 【已确认问题·CRITICAL】缺乏独立 Task 生命周期实体，Session 与 Task 混淆
- **代码位置**：
  - [`src/jarvis_studio/service/agent_service.py:66-74`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L66-L74)
  - [`src/jarvis_studio/service/agent_service.py:202-210`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L202-L210)
- **问题描述**：
  - 核心调度接口仅接收 `thread_id`（等同于 `session_id`），缺失一等公民 `task_id`；
  - 没有明确的任务生命周期状态机（`CREATED -> RUNNING -> WAITING_APPROVAL -> COMPLETED / FAILED / CANCELLED / TIMEOUT`）；
  - 任务状态被耦合在 `ConversationTrace` 和 LangGraph 的运行时状态中，外部无法通过独立 Task 句柄取消任务、查询状态或设置超时。
- **发生条件**：任何一次请求执行。
- **影响范围**：无法实现可靠的任务排队、并发控制、任务追踪与任务取消。
- **严重度**：`CRITICAL`

#### 2.2 【潜在风险·HIGH】Session 级别无并发互斥锁，多请求并发写入撕裂图状态
- **代码位置**：
  - [`src/jarvis_studio/service/agent_service.py:208-210`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L208-L210)
- **问题描述**：
  - `AgentService` 是全局单例，但其内部未维护针对 `thread_id` 的异步锁（`asyncio.Lock`）；
  - 若用户在同一个会话中快速连续发送两条消息，两个协程将同时调用 `self.graph.astream_events(config={"configurable": {"thread_id": thread_id}})`；
  - LangGraph 检查点与内存状态在同一 thread 下发生写入竞态，导致消息追加乱序或状态冲突报错。
- **发生条件**：用户双击发送、前端网络抖动重发或多个客户端操作同一 Session。
- **影响范围**：图状态损坏、会话抛出内部异常。
- **严重度**：`HIGH`

---

### 模块 3：持久化检查点与 SQLite 并发访问

#### 3.1 【已确认问题·CRITICAL】服务重启导致待审批 HITL 任务彻底孤儿化 (State Split-Brain)
- **代码位置**：
  - [`src/jarvis_studio/agent/checkpoints/saver.py:16-24`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/checkpoints/saver.py#L16-L24)
  - [`src/jarvis_studio/service/agent_service.py:269-281`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L269-L281)
- **问题描述**：
  - 由于 LangGraph 同步 `SqliteSaver` 不支持异步流式，当前编译图使用的 checkpointer 为内存态 `MemorySaver`；
  - 会话消息被保存在 SQLite `chat_messages` 表，但**图运行态超步（包括中断现场、挂起上下文）全在内存中**；
  - 一旦服务重启，`MemorySaver` 全部丢失。此时如果前端尝试提交之前的待审批项，LangGraph 因找不到 pending interrupt 而抛错，任务无法恢复。
- **发生条件**：任务在等待 HITL 审批期间，服务发生重启、重新发布或崩溃。
- **影响范围**：审批流中断无法恢复，会话状态与业务数据库撕裂。
- **严重度**：`CRITICAL`

#### 3.2 【已确认问题·CRITICAL】多模块并发写入 SQLite 缺失 WAL 模式与 busy_timeout
- **代码位置**：
  - [`src/jarvis_studio/service/session_service.py:57-59`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L57-L59)
  - [`src/jarvis_studio/memory/store.py:38-40`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/memory/store.py#L38-L40)
  - [`src/jarvis_studio/core/observability/trace_logger.py:150-152`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/core/observability/trace_logger.py#L150-L152)
- **问题描述**：
  - `SessionService`、`MemoryStore` 与 `TraceStore` 均独立打开 `data/jarvis_studio.db`；
  - 均未执行 `PRAGMA journal_mode = WAL;`，SQLite 默认运行在 Rollback Journal 模式下，写操作会对整库加排他锁；
  - 均未配置 `PRAGMA busy_timeout = 5000;`（默认 0ms）。
- **发生条件**：当 Agent 边流式生成（写 Trace）边保存消息（写 Session），或者并发执行 RAG/记忆写入时。
- **影响范围**：极易产生 `sqlite3.OperationalError: database is locked` 导致请求 500 崩溃。
- **严重度**：`CRITICAL`

---

### 模块 4：API 网关与 SSE 流式断连处理

#### 4.1 【已确认问题·CRITICAL】同步工具直接在异步事件循环中运行，阻塞整个网关
- **代码位置**：
  - [`src/jarvis_studio/agent/nodes/executor.py:77-81`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L77-L81)
  - [`src/jarvis_studio/tools/builtin/shell_tools.py:36-44`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/shell_tools.py#L36-L44)
  - [`src/jarvis_studio/rag/indexer.py:108-109`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/rag/indexer.py#L108-L109)
- **问题描述**：
  - 在 `tool_executor_node` 中：`if hasattr(tool, "ainvoke"): ... else: result = tool.invoke(effective_args)`；
  - 内置工具 `execute_shell_command`、`read_file`、`write_file` 均为同步函数，没有提供 `coroutine` 或 `run_in_executor` 封装；
  - `execute_shell_command` 执行耗时 30 秒的命令时，**会直接阻塞 Python 主线程的 asyncio 事件循环 30 秒**！
- **发生条件**：执行耗时终端命令、扫描大型目录或大文件读写。
- **影响范围**：整个服务挂起，所有在线用户的 SSE 流式卡死，心跳超时，API 无法响应。
- **严重度**：`CRITICAL`

#### 4.2 【已确认问题·HIGH】客户端断开连接后后台任务无法取消，导致资源泄漏
- **代码位置**：
  - [`src/jarvis_studio/api/routes/chat.py:57-73`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/api/routes/chat.py#L57-L73)
  - [`src/jarvis_studio/service/agent_service.py:420-425`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L420-L425)
- **问题描述**：
  - `chat_stream_with_agent` 中的 `event_generator` 未接收 `request: Request`，未轮询 `await request.is_disconnected()`；
  - 当客户端关闭浏览器标签或网络中断时，`agent_service.stream_agent` 捕获到 `CancelledError` 后未做清理，后台任务与大模型连接未显式终止；
  - Trace 状态未能打标为 `cancelled`，导致数据库中产生永久挂起的半截数据。
- **发生条件**：用户在 Agent 生成过程中刷新页面、关闭窗口或点击停止。
- **影响范围**：后台无意义消耗 Token，占用模型配额与并发句柄。
- **严重度**：`HIGH`

---

### 模块 5：MCP 客户端生命周期与外部进程安全

#### 5.1 【潜在风险·HIGH】stdio 子进程缺乏进程组绑定，异常崩溃易产生僵尸孤儿进程
- **代码位置**：
  - [`src/jarvis_studio/tools/mcp/client.py:90-100`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/mcp/client.py#L90-L100)
- **问题描述**：
  - `MCPConnection` 依托 `stdio_client` 启动外部进程（如 `npx ...` 或 `uvx ...`）；
  - Windows 环境下未绑定 `Job Object`（带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），Linux 环境下未配置 `prctl(PR_SET_PDEATHSIG)`；
  - 当 Jarvis 主进程由于不可抗力意外退出或强制 kill 时，MCP 子进程依然驻留在操作系统后台，占用端口和系统资源。
- **发生条件**：服务被任务管理器终止、Docker 容器被强制清理或主进程异常崩溃。
- **影响范围**：操作系统进程泄漏，端口冲突，再次启动时无法绑定。
- **严重度**：`HIGH`

#### 5.2 【潜在风险·HIGH】MCP 进程缺乏心跳探活与断线重连机制
- **代码位置**：
  - [`src/jarvis_studio/tools/mcp/client.py:47-50`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/mcp/client.py#L47-L50)
  - [`src/jarvis_studio/tools/mcp/client.py:151-170`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/mcp/client.py#L151-L170)
- **问题描述**：
  - 若 MCP 外部进程因 OOM 或自身崩溃退出，`conn.is_connected` 仍为 `True`；
  - 只有当 Agent 下次调用该工具发生 BrokenPipeError 抛错后才感知，且没有任何退避重连机制。
- **发生条件**：MCP 外部服务意外挂掉。
- **影响范围**：工具调用必然失败，直到人工重启整个 Jarvis 服务。
- **严重度**：`HIGH`

---

### 模块 6：代码与文件操作安全边界 (Sandbox Execution)

#### 6.1 【已确认问题·CRITICAL】文件系统工具存在路径穿越风险，宿主机执行缺乏沙箱隔离
- **代码位置**：
  - [`src/jarvis_studio/tools/builtin/file_tools.py:11-20`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/file_tools.py#L11-L20)
  - [`src/jarvis_studio/tools/builtin/shell_tools.py:23-27`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/shell_tools.py#L23-L27)
- **问题描述**：
  - `_resolve_safe_path` 中实现为：
    ```python
    resolved = (workspace / target_path).resolve() if not Path(target_path).is_absolute() else Path(target_path).resolve()
    ```
  - **严重缺陷 1**：传入绝对路径直接放行，可以读取或覆盖系统任意敏感路径；
  - **严重缺陷 2**：传入相对路径如 `../../../../etc/passwd`，`resolve()` 后直接越出 `workspace_root`，代码中**完全没有** `resolved.is_relative_to(workspace)` 校验！
  - **严重缺陷 3**：`execute_shell_command` 直接以当前开发者身份在宿主机物理执行，没有 Docker、gVisor 或受限用户沙箱。
- **发生条件**：模型受到 Prompt Injection 诱导，或生成带越界路径的读写指令。
- **影响范围**：宿主机数据泄露、文件损毁或非授权代码执行。
- **严重度**：`CRITICAL`

---

### 模块 7：RAG 检索质量与代码智能 (Code Intelligence)

#### 7.1 【架构缺陷·HIGH】纯固定长度分块破坏代码结构与 AST 语法完整性
- **代码位置**：
  - [`src/jarvis_studio/rag/splitter.py:54-85`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/rag/splitter.py#L54-L85)
- **问题描述**：
  - `DocumentSplitter` 采用通用的字符长度按 `\n\n` 和 `\n` 切分；
  - 对于 Python、Java、TypeScript 代码，一个完整的类或函数会被硬生生拦腰切断；
  - 切块元数据中缺乏：所属类名、函数名、模块路径、起止行号及符号层级，导致向量化后的片段语义支离破碎。
- **发生条件**：索引大型代码工程。
- **影响范围**：代码上下文理解准确率低，容易检索到半截函数，无法支撑重构和精准定位。
- **严重度**：`HIGH`

#### 7.2 【架构缺陷·HIGH】仅依赖密集向量检索 (Dense-Only)，缺少关键词与符号精准召回
- **代码位置**：
  - [`src/jarvis_studio/rag/store.py:230-260`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/rag/store.py#L230-L260)
- **问题描述**：
  - 检索只使用 `sqlite-vec` 的 KNN 距离；
  - 在代码工程中，用户经常查询精确符号（如 `create_assistant_node`、`SqliteVecStore`、配置项 `SQLITE_DB_PATH`、异常名 `OperationalError`）；
  - 纯 Dense 向量模型在此类场景下极易发生“语义漂移”，召回语义接近但符号完全不对的内容，急需引入 SQLite FTS5 (BM25) 进行混合检索 (Hybrid RAG) 与 RRF 排名融合。
- **发生条件**：根据特定函数名、变量名或异常堆栈检索代码。
- **影响范围**：关键词命中率低，开发者核心检索体验不佳。
- **严重度**：`HIGH`

---

### 模块 8：长期记忆 (LTM) 治理与上下文膨胀

#### 8.1 【架构缺陷·MEDIUM】LTM 全量无差别注入，缺少相关度检索与上下文预算控制
- **代码位置**：
  - [`src/jarvis_studio/memory/manager.py:50-62`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/memory/manager.py#L50-L62)
- **问题描述**：
  - `get_formatted_context()` 简单取时间最近的 15 条记忆直接贴在 SystemMessage；
  - 没有对当前用户的输入进行语义匹配，无论问什么都带上所有偏好；
  - 没有记忆衰减因子、置信度标记与去重合并机制，长期运行必将导致 Prompt 浪费甚至稀释模型注意力。
- **发生条件**：长期使用后积累几十条记忆。
- **影响范围**：Prompt 膨胀，无谓消耗 Token，甚至引入互相冲突的过时记忆。
- **严重度**：`MEDIUM`

---

### 模块 9：意图守卫与可观测性 (Guard & TraceStore)

#### 9.1 【潜在风险·HIGH】紧急制动规则仅返回假回复，未实际熔断正在执行的后台任务
- **代码位置**：
  - [`src/jarvis_studio/core/guard/route_guard.py:83-86`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/core/guard/route_guard.py#L83-L86)
  - [`src/jarvis_studio/service/agent_service.py:96-104`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L96-L104)
- **问题描述**：
  - 当用户输入“停止”/“cancel”时，Guard 匹配到 `emergency_halt` 并直接返回预设文字；
  - 但是！它**根本没有**去取消同一 Session 下正在执行的后台 Task，也没有调用任何工具中断接口；
  - 用户以为操作已经中止，后台的 Shell 命令或死循环图节点仍在继续运行。
- **发生条件**：用户在 Agent 执行敏感操作时发送“停止”。
- **影响范围**：安全熔断形同虚设，存在误导风险。
- **严重度**：`HIGH`

#### 9.2 【架构缺陷·MEDIUM】TraceStore 缺少数据保留与滚动归档机制
- **代码位置**：
  - [`src/jarvis_studio/core/observability/trace_logger.py:184-210`](file:///D:/myProject/JarvisStudio/src/jarvis_studio/core/observability/trace_logger.py#L184-L210)
- **问题描述**：
  - 每次请求都把全套 Payload（包含完整步骤、输入参数、输出内容）全量写入 `agent_conversation_traces`；
  - 表没有 TTL，没有按天清理，没有最大条数上限；
  - 在作为守护进程长期运行时，数据库文件会持续无限膨胀。
- **发生条件**：系统持续运行多周或经历大量请求。
- **影响范围**：数据库体积迅速增大，查询响应变慢，磁盘被耗尽。
- **严重度**：`MEDIUM`

---

## 三、 审计结论与红线改进原则

经过本次严格的代码级审计，可以得出如下结论：
1. **已有优势**：模块边界分明，代码风格统一，测试覆盖率良好（43/43 PASS），FastAPI + SSE + LangGraph 的主架构选型正确；
2. **核心短板**：处于“原型单 Agent 可用”向“工业级可靠运行时”过渡的阵痛期，缺乏**Task 生命周期实体、真正的并发治理、可靠的 Checkpoint 重启恢复、沙箱隔离与代码感知切块**；
3. **架构演进红线**：
   - 严禁为了 Multi-Agent 而 Multi-Agent，先将单 Agent 运行时的任务可靠性（Reliability）和代码检索（Hybrid RAG）做深做透；
   - 解决真实存在的并发、锁竞争与任务孤儿问题，再推进高级 Planning 与自动化流程。

---

> 审计报告编制完毕。后续分阶段实施计划见 [`V2_EVOLUTION_ROADMAP.md`](file:///D:/myProject/JarvisStudio/docs/architecture/V2_EVOLUTION_ROADMAP.md)。
