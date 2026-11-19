# Jarvis Studio 现状代码与产品能力审计报告
## (Product Capability & Codebase Audit)

> **审计执行人**：AgentStudio 主力产品架构师 & 技术负责人  
> **审计基线**：Git 分支 `main` @ `69085fe` (Working tree clean)  
> **运行环境**：Python 3.12.14, pytest 9.1.1, Windows / POSIX 跨平台  
> **自动化测试现状**：213 项用例（199 Passed, 2 Resolved via root pythonpath, 12 Skipped，全绿通过）  
> **目标产品**：**AgentStudio (Personal AI Agent Workspace)**  
> **核心使命**：从“Agent Engineering 学习与底层加固项目”全面演进为“以任务完成与成果交付为中心、可观察、可控制的个人 AI Agent 工作空间”。

---

## 1. 审计执行背景与总体结论

### 1.1 审计方法与边界
本审计坚决贯彻“**代码真实行为优先**”原则，直接检查 `src/jarvis_studio/` 源码、SQLite V1/V2/V3 迁移脚本、FastAPI 路由、SPA 单页前端以及 `tests/` 目录下的 213 项单元与集成测试用例，拒绝仅凭历史设计文档（如 Sprint 1A/1B/1C 规划）推断功能完备性。

### 1.2 总体结论：工程地基极为扎实，但产品化缺口显著
当前 Jarvis Studio 经过 Sprint 1A（安全加固）、Sprint 1B（进程运行时）以及 Sprint 1C（持久化与生命周期治理 Phase 1-4），已经构建了**业界罕见的高可靠、单机单活持久化运行时基础设施**。
然而，从目标产品 **AgentStudio**（Personal AI Agent Workspace）的定位审视，系统呈现出显著的“**技术下沉充足、产品表面严重缺失**”的状态：

```
[目标产品层: AgentStudio] ─── 任务中心 (缺失) ── 交付物系统 (缺失) ── 专业智能体编排 (缺失)
                                      │
[产品服务层: Service/API]  ─── Task/Run API (缺失) ── 会话与任务 1:1 强绑定 ── 伪思考日志 (亟待重构)
                                      │
[核心基础设施: Runtime]    ─── SQLite WAL (已就绪) ── OS 单活锁 (已就绪) ── HITL CAS 租约 (已就绪) ── V3 状态机 (已就绪)
```

- **底层工程能力评分（88/100）**：数据持久化、WAL 单写者控制、OS 内核文件排他锁、HITL 授权状态与副作用分离、CAS 租约原子防重放、Startup Recovery 协调协议均具备极高可靠性。
- **产品体验完整度评分（35/100）**：UI 仍是简单的“Chat 终端 + 开发者 Traces 瀑布图 + 系统配置面板”，没有独立的 Task 概念，没有 Deliverables（交付物）展示与下载，没有多 Agent 体系，且在前端与 API 层存在将会话与任务 1:1 等同的严重产品硬编码。

---

## 2. 真实功能清单与成熟度矩阵

我们采用 5 级成熟度标准对现有系统各模块进行严格定级：
1. **Implemented & Verified (已实现且通过验证)**：核心代码完整，具备完备单元/集成测试，端到端可用。
2. **Partially Implemented (部分实现)**：核心逻辑存在，但缺少配套控制逻辑、边界处理或存在明显受限。
3. **Infrastructure Only (仅有基础设施/未产品化)**：底层数据库表、状态机或协调器已就绪，但上层无业务 API 或无前端界面，用户完全无法使用。
4. **Design Only (仅有设计文档)**：代码库中仅有设计文档、规划或占位说明，尚无实质实现。
5. **Not Implemented (未实现)**：产品目标所需，当前代码库完全空白。

### 2.1 模块能力清查全景表

| 序号 | 领域模块 | 源码文件路径 | 真实能力描述 | 成熟度定级 | 产品化处置建议 |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **1** | **OS 单活运行时锁** | `core/persistence/runtime_lock.py` | 基于 `msvcrt.locking` (Win) / `fcntl.flock` (POSIX) 的文件锁，支持多 DB 路径独立锁定，崩溃由 OS 自动释锁，防多进程撕裂。 | **Implemented & Verified** | **保留**。底层核心可靠性资产，严禁改动。 |
| **2** | **SQLite 事务与多库迁移** | `core/persistence/sqlite.py`, `migrations.py` | 支持 `BEGIN IMMEDIATE` 同步事务、WAL 模式、禁止嵌套事务、DB 备份校验（`PRAGMA quick_check`）、双库独立 `PRAGMA user_version` (App V3 / Vec V1)。 | **Implemented & Verified** | **保留**。演进至 Schema V4（新增 Deliverable 与 Agent 表）。 |
| **3** | **Checkpointer 持久化** | `core/persistence/async_sqlite.py`, `agent/checkpoints/` | 基于 `AsyncSqliteSaver`，异步 aiosqlite 连接，事件循环严格绑定，支持旧版本 Checkpoint 隔离检测与只读重试。 | **Implemented & Verified** | **保留**。保持 LangGraph 状态断点续跑能力。 |
| **4** | **Task & Run 状态机** | `core/tasks/coordinator.py`, `models.py` | 支持 `agent_tasks` (10态) 与 `agent_runs` (7态)；支持 `Task:Run = 1:N`；支持 CAS 抢占、触发源追踪（initial, hitl_resume, recovery_resume）；支持级联清理。 | **Infrastructure Only** | **产品化演进**。当前仅在内部调用，**API 网关与 UI 完全未暴露**，需封装 Task REST API 与任务中心。 |
| **5** | **HITL 审批与 CAS 租约** | `agent/hitl/manager.py`, `store.py`, `authorization.py` | 严格四元组校验；`ApprovalStatus` 与 `ExecutionOutcomeStatus` 严格正交分离；CAS 原子抢占防重放；实时取消落 `UNKNOWN`；启动恢复对账。 | **Implemented & Verified** | **产品化包装**。将底层审批状态流整合进 Task 执行流，支持计划确认与操作审批双轨制。 |
| **6** | **进程执行引擎** | `core/process/runner.py`, `platform.py` | 跨平台子进程治理；Windows JobObject 进程树绑定与终结；POSIX `killpg` 孤儿进程回收；实时标准流截断与 UTF-8 编码清洗。 | **Implemented & Verified** | **保留**。供 Shell 与脚本类工具安全调用。 |
| **7** | **工作区文件沙箱** | `tools/builtin/file_tools.py` | `resolve_safe_path` 严格校验 `is_relative_to(workspace)`；防目录穿越、防 UNC 路径、防多驱动器跳转、防符号链接逃逸与循环。 | **Implemented & Verified** | **保留**。作为 AgentStudio 默认工作区安全边界。 |
| **8** | **内置工具体系** | `tools/builtin/` (file, shell, rag, memory) | 提供 `read_file`, `write_file`, `list_directory`, `execute_shell_command`, `search_knowledge_base`, `save/recall_personal_memory`。带安全分级标签（SAFE / HIGH）。 | **Implemented & Verified** | **保留并扩充**。需增加 Deliverable 生成工具与结构化输出工具。 |
| **9** | **MCP 客户端长连接** | `tools/mcp/client.py`, `adapter.py` | 支持 stdio 与 SSE 传输协议；自动扫描配置文件；工具动态转换与注册；生命周期断开回收。 | **Implemented & Verified** | **产品化包装**。当前前端仅只读展示连接状态，需支持用户侧 MCP 启用/禁用交互。 |
| **10** | **向量知识库 (Local RAG)** | `rag/store.py`, `indexer.py`, `embeddings.py` | 基于 `sqlite-vec` 向量扩展；SHA-256 文件指纹增量索引；支持目录过滤；单机无外部向量服务依赖。 | **Implemented & Verified** | **保留**。作为第一阶段 Knowledge & Documents 场景的基础。 |
| **11** | **个人长期记忆 (LTM)** | `memory/manager.py`, `store.py`, `models.py` | 基础扁平 Key/Value 式记忆条目（Preference, Profile, Project, Fact）；支持关键词检索与分类过滤；对话前注入 System Prompt。 | **Partially Implemented** | **演进**。当前缺乏分层（Global vs Agent vs Task Context），无时效衰减与冲突消除机制。 |
| **12** | **意图守卫与规则直出** | `core/guard/route_guard.py` | 规则守卫前置快速通道；命中静态规则直接返回（0 Token，<50ms 延迟）；支持 Sticky Context 会话粘性上下文注入。 | **Implemented & Verified** | **保留**。作为轻量快速响应和系统硬规则防线。 |
| **13** | **追踪与指标日志** | `core/observability/trace_logger.py`, `tracer.py` | 记录 5 阶段耗时（TTFT, TTFA, TTLT）；记录各步骤输入输出 Payload；自动保留策略（200条/30天）。 | **Implemented & Verified** | **演进下沉**。从一级用户 Tab 降级为 Task 详情内的“高级诊断/技术 Trace”，避免普通用户认知过载。 |
| **14** | **智能体编排 StateGraph** | `agent/graph.py`, `nodes/` | 单 Agent ReAct 循环：`assistant` -> `approval_gate` -> `tools` -> `assistant`。 | **Partially Implemented** | **架构演进**。当前仅支持单 Agent ReAct，不支持 General Agent + Specialist Agents 委托交接，不支持多步规划。 |
| **15** | **思考过程生成引擎** | `core/thinking/engine.py` | 伪思考状态发生器：硬编码字符串（如“正在理解您的指令并分析上下文...”）、心跳脉冲。 | **Placeholder / Defective** | **重构/废弃**。违反“Observable by Design”和“禁止伪造思考过程”原则，应替换为真实执行计划与阶段流转事件。 |
| **16** | **Task API 与独立任务创建** | *无* | 当前无 `/api/v1/tasks` 路由，无法脱离 Chat 独立创建 Task，无任务查询、取消、暂停接口。 | **Not Implemented** | **MVP P0 必须新增**。 |
| **17** | **交付物系统 (Deliverables)** | *无* | 无交付物数据模型、无存储机制、无版本控制、无前端渲染与导出。任务完成仅有对话文本。 | **Not Implemented** | **MVP P0 必须新增**。核心产品支柱。 |
| **18** | **专业 Agent 管理与配置** | *无* | Agent 提示词与工具集固定在代码中，用户无法查看、创建、配置专业 Agent 或分配工具。 | **Not Implemented** | **MVP P1 实施**。支持内置专业 Agent 与自定义配置。 |
| **19** | **Web UI 任务中心与交付物展台** | `api/static/index.html` | 当前前端为典型单 Agent Chat UI（对话框、会话列表、审批弹窗、LTM 表格、RAG 测试台、Traces 瀑布图）。 | **Not Implemented** | **MVP P1 实施**。演进为包含 Dashboard、Chat、Task Center、Task Detail、Knowledge、Tools 的 Workspace。 |

---

## 3. UI 已暴露能力 vs 底层真实能力错位分析

通过对比 `api/static/index.html` 与后端服务，发现当前系统存在严重的**能力错位**：

### 3.1 UI 暴露的 6 个标签页
1. **💬 对话终端 (`#view-chat`)**：纯单会话对话流，右侧显示消息历史，遇到敏感工具弹窗审批。
2. **🛡️ HITL 审批 (`#view-approvals`)**：列出待审批项，支持 Approve / Modify / Reject。
3. **🧠 长期记忆 (`#view-memory`)**：增删查个人偏好记忆。
4. **📚 本地知识库 (`#view-rag`)**：目录索引触发与向量检索实验台。
5. **⚡ 链路追踪 (`#view-traces`)**：展示全量底层技术日志、耗时指标瀑布流。
6. **⚙️ 系统工具 (`#view-system`)**：展示注册工具列表与 MCP 状态。

### 3.2 严重的“三无”缺口
1. **无 Task 概念**：虽然 `jarvis_studio.db` 拥有完备的 `agent_tasks`（10态）和 `agent_runs`（7态），但用户在 UI 上只能看到“会话”，每次在输入框按回车，后端在底层偷偷创建了一个 `task_id`，但用户完全不知道任务状态、执行时长、属于哪次 Run。
2. **无 Deliverable 概念**：Agent 搜索了 10 篇文档或执行了复杂分析后，最终产出被混杂在长篇聊天气泡中。用户无法另存为报告、无法查看独立交付文件、无法追溯交付物关联的 Run。
3. **暴露过多技术内部细节**：系统将开发者的“链路追踪 (Traces)”作为一级导航展示，普通用户被迫面对 TTFT/TTFA 指标、JSON Payload、Step Arguments，而真正关心的“任务目标是什么、完成了哪些步骤、最终交付了什么”反而没有承载页面。

---

## 4. 关键模块依赖与架构断层分析

```mermaid
classDiagram
    class AgentRuntime {
        +RuntimeInstanceLock lock
        +AsyncSqliteSaver checkpointer
        +HITLManager hitl_manager
        +TaskCoordinator task_coordinator
        +AgentService agent_service
        +start()
        +shutdown()
    }

    class TaskCoordinator {
        +create_task()
        +start_run()
        +set_task_status()
        +complete_task_and_run()
        +fail_task_and_run()
        +cancel_task_and_run()
        +pause_for_approval()
    }

    class AgentService {
        +invoke_agent(thread_id, msg)
        +stream_agent(thread_id, msg)
        +resume_approval(thread_id, appr_id, dec)
        -WeakKeyDictionary thread_locks
    }

    class HITLManager {
        +request_approval()
        +resolve_request()
        +acquire_execution_lease()
        +record_outcome()
    }

    class StateGraph {
        +assistant node
        +approval_gate node
        +tools node
    }

    AgentRuntime *-- TaskCoordinator
    AgentRuntime *-- HITLManager
    AgentRuntime *-- AgentService
    AgentService --> TaskCoordinator : 隐式强绑定 1:1
    AgentService --> StateGraph : ReAct Loop
    StateGraph --> HITLManager : interrupt()
```

### 4.1 架构断层 1：Chat Session 与 Task 的 1:1 强绑定
在 `src/jarvis_studio/service/agent_service.py` 的 `_invoke_agent_unlocked` 和 `_stream_agent_unlocked` 中：
```python
# 代码现状：直接使用 thread_id 作为 session_id，单次交互直接创建一个 task
task = self.task_coordinator.create_task(
    session_id=thread_id,
    thread_id=thread_id,
    query=user_message,
    runtime_id=runtime_id,
)
```
- **问题**：将用户在 Chat 中的一次发话直接等同于一个持久化 Task，导致任务无法跨多轮对话持续演进，无法支持“在对话中先讨论、再立项创建独立 Task”，也无法支持“脱离 Chat 独立创建长期运行 Task”。
- **架构解耦要求**：必须在 API 与领域层分离 `Session`（交互上下文容器）与 `Task`（独立目标与生命周期）。

### 4.2 架构断层 2：缺乏 Deliverable（交付物）领域实体
当前整个系统的数据流在 Agent 吐出最后一个 `AIMessage` 后即告终结：
```
User Message ──> Guard ──> LangGraph ReAct ──> AIMessage (Text) ──> chat_messages 表
```
- **问题**：文本输出没有结构化沉淀，生成的报告、图表、导出的文件或数据表没有独立元数据管理（文件类型、存储路径、大小、生成该文件的 run_id、引用来源）。
- **架构演进要求**：新增 `Deliverable` 实体和数据库表 `agent_deliverables`，挂载在 `agent_tasks` 和 `agent_runs` 之下。

### 4.3 架构断层 3：单 Agent 结构，缺乏专业 Agent 分工与交接
`agent/graph.py` 编译的图是一个单一的 `assistant` 节点配合 `ToolRegistry`。
- **问题**：所有已注册的工具（文件、Shell、RAG、Memory、MCP）一股脑塞进唯一的 `assistant` 模型上下文中。对于复杂任务（如调研报告），既需要网络检索，又需要代码执行，还需要深度综合撰写，单模型上下文混乱且容易发生工具误调。
- **架构演进要求**：保持 General Agent 作为交互与协调入口，引入明确的专业 Agent（如 Research Specialist, Code Specialist, Document Specialist），并通过显式上下文和结构化交接进行协作。

---

## 5. 冗余、过度设计与不良实现清单

1. **`core/thinking/engine.py` (伪思考发生器)**：
   - *现状*：通过固定 sleep 和写死的中文文本（“正在理解您的指令...”、“正在调用工具...”）向前端模拟思考进度。
   - *判定*：**有缺陷的实现**。与项目原则“禁止伪造思考过程，基于真实执行事件可观测”严重背离。真实思考应来自模型原生的 reasoning/thinking content，真实进度应来自 StateGraph 节点跃迁与 Tool Calling 真实生命周期事件。
   - *建议*：废弃伪思考生成，重构为基于真实执行事件驱动的 Event Bus。
2. **`pyproject.toml` 中的 `pythonpath` 配置遗漏根路径**：
   - *现状*：`tests/test_phase2_shutdown.py` 中有两处 `from tests.test_mcp import ...`，而 `pyproject.toml` 的 `pythonpath = ["src"]` 未包含 `.`，导致直接运行 `pytest` 时报错 `ModuleNotFoundError: No module named 'tests'`。
   - *判定*：工程配置疏漏。测试依赖根目录。
   - *建议*：在 `pyproject.toml` 中配置 `pythonpath = [".", "src"]` 或将公共测试辅助工具收敛至 `tests/fixtures/`。
3. **前端对技术 Trace 的一级强依赖**：
   - *现状*：前端主导航中给 `⚡ 链路追踪 (Traces)` 单独开辟一个与对话并列的顶级 Tab，里面充斥着毫秒级 TTFT 统计和复杂 Waterfall。
   - *判定*：**产品形态过度工程化**。对个人工作空间用户而言，这是开发者调试工具，而非日常生产工作流界面。
   - *建议*：将 Trace 下沉为 Task 详情中的“执行诊断/事件流 (Advanced Inspector)”。

---

## 6. 保留、演进、延后与废弃决策矩阵

| 模块 / 组件 | 决策 | 处置细节与理由 |
| :--- | :---: | :--- |
| **`core/persistence/runtime_lock.py`** | **保留** | OS 内核文件排他锁保证单活，严防 SQLite 数据库损坏，完全符合目标产品高可靠要求。 |
| **`core/persistence/sqlite.py` & `migrations.py`** | **保留 & 演进** | WAL 模式、连接治理、事务控制均极为成熟；仅需平滑追加 Schema V4 迁移脚本。 |
| **`core/persistence/recovery.py`** | **保留** | 启动对账、清理死锁、收敛 UNKNOWN 状态的鲁棒机制，是保证 AgentStudio 不死锁的关键。 |
| **`core/tasks/coordinator.py` & `models.py`** | **演进** | 完整复用已建好的 10 态 Task 与 7 态 Run 状态机；对外新增 REST API 与 WebSocket/SSE 广播。 |
| **`agent/hitl/` (Manager, Store, Auth)** | **保留 & 演进** | 四元组鉴权与 CAS 租约机制保留；在前端补充“计划确认”与“参数修改审批”的双轨视觉区分。 |
| **`core/process/` (Runner, Platform)** | **保留** | 跨平台 JobObject 与 POSIX 进程树治理稳定，直接复用。 |
| **`tools/builtin/file_tools.py`** | **保留** | `resolve_safe_path` 严格的工作区沙箱保护是 Bounded Autonomy 原则的底线，直接复用。 |
| **`tools/mcp/` (Client, Adapter)** | **演进** | 复用 stdio/SSE 客户端长连接能力；在 Web 端增加可视化的 MCP 管理面板。 |
| **`rag/` (Store, Indexer, Embeddings)** | **保留 & 演进** | 复用 sqlite-vec 与增量索引；为第一阶段 Knowledge 场景提供基础检索能力。 |
| **`memory/` (Manager, Store)** | **演进** | 现有 LTM 仅作为 Global Memory；需要补充 Agent 级记忆与 Task 临时上下文的生命周期。 |
| **`core/thinking/engine.py`** | **废弃/替换** | 彻底移除模拟思考与硬编码文本；替换为捕获原生 LLM 推理流与 StateGraph 节点事件的真实事件管道。 |
| **`api/static/index.html` (现有 SPA)** | **废弃/全面重构** | 现有前端是单一的 Chat+Traces 界面，不符合 AgentStudio 的 Workspace 信息架构。需重塑为 Task-Centric 交互界面。 |

---

## 7. 结论与下一步

当前 Jarvis Studio **并非玩具 Demo，而是具备了工业级持久化底座的半成品**。
演进为 AgentStudio 的关键，**不在于重写底层，而在于打通从“底层 Task/Run”到“用户交付物”之间的“产品最后一公里”**：
1. 立即补充 Task 领域 API，打破 Chat 与 Task 的 1:1 强绑定；
2. 建立 Deliverable（交付物）领域模型与文件管线；
3. 将 General Agent 与专业 Agent（Research/Knowledge）通过清晰的消息规范串联；
4. 构建以任务中心（Task Center）和任务详情（Task Detail）为核心的全新现代化 Workspace 界面。
