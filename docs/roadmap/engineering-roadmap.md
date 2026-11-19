# Jarvis Studio - 工程演进路线图 (Engineering Roadmap)

本文档系统规划并记录了 **Jarvis Studio** 的阶段性产品演进与工程里程碑。

状态图例：
- `[x] 已完成 (Completed)`：已在代码库中实现、通过自动化测试并可实际运行。
- `[~] 进行中 (In Progress)`：核心架构已就绪或部分实现，正在积极演进中。
- `[ ] 计划中 (Planned)`：已完成设计规划，已排期至后续阶段实现。

---

## 阶段全景 (Milestone Overview)

```mermaid
flowchart LR
    M0["M0: 架构基线与脚手架\n(已完成)"] --> M1["M1: 核心 Agent 与模型底座\n(已完成)"]
    M1 --> M2["M2: 工具子系统与 MCP\n(已完成)"]
    M2 --> M3["M3: HITL 与持久化检查点\n(已完成)"]
    M3 --> M4["M4: 长期记忆与 RAG\n(已完成)"]
    M4 --> M5["M5: Web 界面与流式输出\n(已完成)"]
    M5 --> M6["M6: 思考流与意图守卫\n(已完成)"]
    M6 --> M7["M7: 持久化调度与自愈\n(已完成)"]
```

---

### Milestone 0: 架构基线与工程脚手架 (Architecture Baseline & Scaffolding)
*目标：建立严格的 5 层解耦架构、现代包管理依赖基准与自动化测试底座。*

- [x] 确立 5 层解耦架构蓝图与边界设计规范 (`docs/architecture/system-architecture.md`)。
- [x] 基于 Python 3.12 虚拟环境并采用现代 `uv` 工具链进行全量依赖管理。
- [x] 锁定成熟官方依赖：`langgraph (1.2+)`, `langchain (1.4+)`, `fastapi (0.115+)`, `mcp (2.3+)`, `pydantic (2.13+)`。
- [x] 设计规范的项目目录树，清晰隔离 API、Service、Agent、Tools 与 Core。
- [x] 基于 `.env`、`settings.yaml` 与 Pydantic Settings 实现分级配置系统。
- [x] 搭建自动化测试套件 (`pytest` + `pytest-asyncio`)。

---

### Milestone 1: 多模型引擎与 LangGraph 核心运行时 (Multi-Provider Engine & Core Runtime)
*目标：保证模型热插拔零硬编码，构建基础 StateGraph 状态机并完成基础循环。*

- [x] 实现 `LLMFactory` 统一多模型工厂，支持：
  - [x] OpenAI (`gpt-4o`, `gpt-4o-mini`)
  - [x] DeepSeek (`deepseek-chat` 基于 OpenAI 兼容协议)
  - [x] 本地离线 Ollama (`llama3.1`, `qwen2.5`)
  - [x] 自定义兼容端点 (vLLM, SGLang)
- [~] 扩展供应商深度适配：
  - [~] Anthropic Claude (动态加载适配器已实现；待生产真实密钥验证)
  - [~] Google Gemini (动态加载适配器已实现；待生产真实密钥验证)
- [x] 建立 `AgentState` 强类型状态模式（基于 `add_messages` 消息增量归约）。
- [x] 实现核心图节点 (`assistant`, `approval_gate`, `tools`, `router`)。
- [x] 纯对话与基础工具调用闭环的全自动测试覆盖。

---

### Milestone 2: 工具子系统与 MCP 客户端集成 (Tool Subsystem & MCP Client Integration)
*目标：提供高频日常系统级工具，打通 Model Context Protocol 标准外部服务桥梁。*

- [x] 实现 `ToolRegistry` 注册中心与 `RiskLevel` 三级安全分级矩阵。
- [x] 内置高频文件操作工具：
  - [x] `read_file` (安全只读、上下文截断保护)
  - [x] `write_file` (高危写入、强制 HITL 确认)
  - [x] `list_directory` (安全只读)
- [x] 内置高频终端操作工具：
  - [x] `execute_shell_command` (高危执行、超时熔断保护、强制 HITL 确认)
- [x] 动态 MCP JSON Schema 强类型转换器 (`MCPToolAdapter`, `create_dynamic_args_schema`)。
- [x] Model Context Protocol (MCP) 客户端管理器：
  - [x] 配置文件加载与解析 (`config/mcp_servers.json`)
  - [x] stdio 长连接子进程生命周期管理 (`MCPConnection`)
  - [x] SSE 远程传输客户端支持
  - [x] 本地与工作区 MCP 服务自发现机制 (`auto_discover_configs`)

---

### Milestone 3: Human-in-the-Loop 与检查点持久化 (HITL & Persistent Checkpointing)
*目标：为危险操作构建绝对不可绕过的人机协同审批防线，保障状态断点持久落盘。*

- [x] 基于 SQLite 的 LangGraph 检查点持久化 (`SqliteSaver` 写入 `data/jarvis_studio.db`)。
- [x] 内存检查点 (`MemorySaver`) 用于单元测试隔离。
- [x] LangGraph 原生 `interrupt()` 协议与图执行暂停。
- [x] 审批状态机实现 (`PENDING`, `APPROVED`, `REJECTED`, `MODIFIED`)。
- [x] 基于 `Command(resume=decision)` 的断点唤醒与执行恢复。
- [x] 协议合规的拒绝处理：自动生成合法 `ToolMessage`，防止底层 LLM 报错。
- [x] 审批 REST API 契约 (`GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/decision`)。
- [ ] 审批超时自动取消策略与自定义规则过滤器。

---

### Milestone 4: 长期记忆与本地知识库 (Personal Memory & Local Knowledge Base / RAG)
*目标：为智能体赋予长期偏好适应能力与本地私有代码/文档检索能力。*

- [x] 基于会话 Thread 的短时上下文检查点持久化。
- [x] 长期个人记忆系统 (LTM)：
  - [x] 开发者习惯、编程风格与常用系统环境持久化 (`src/jarvis_studio/memory`)。
  - [x] 跨会话重要用户事实与偏好提取、存储与检索 (`MemoryStore` + `MemoryManager`)。
  - [x] Assistant 推理节点动态记忆上下文注入 (`[User Long-Term Memory & Preferences]`)。
  - [x] 内置长期记忆管理工具 (`save_personal_memory`, `recall_personal_memory`)。
  - [x] 记忆 REST API (`/api/v1/memory`) 与 CLI 终端命令 (`jarvis-studio memory add/list/delete`)。
- [x] 本地 RAG 知识库系统：
  - [x] 文本与代码语义切分器 (`DocumentSplitter`)。
  - [x] 多源 Embedding 统一工厂 (`EmbeddingsFactory`，支持 OpenAI 与零依赖确定性离线嵌入)。
  - [x] 基于 `sqlite-vec` 的本地高效向量 KNN 检索存储引擎 (`SqliteVecStore`)。
  - [x] 工作区文件哈希增量扫描与向量入库索引器 (`WorkspaceIndexer`)。
  - [x] 本地知识检索与索引工具 (`search_knowledge_base`, `index_workspace_directory`, `list_indexed_documents`)。
  - [x] 知识库 REST API (`/api/v1/rag`) 与 CLI 命令 (`jarvis-studio rag index/search/list`)。

---

### Milestone 5: 应用网关流式化与 Web 交互界面 (Application Gateway, Streaming & UI)
*目标：打造兼顾终端效率与现代可视化体验的全功能个人 AI 助手。*

- [x] FastAPI RESTful API 基础网关 (`/system`, `/chat`, `/approvals`, `/memory`, `/rag`)。
- [x] 开发者命令行交互终端 (`jarvis-studio info`, `serve`, `chat`, `memory`, `rag`, `sessions`, `mcp`)。
- [x] Server-Sent Events (SSE) 逐 Token 与步骤事件流式传输 (`/api/v1/chat/stream`, `/api/v1/approvals/{id}/decision/stream`)。
- [x] 现代化响应式 Web UI 控制台 (`/app`)：
  - [x] 逐 Token 实时流式响应与 Markdown/代码高亮展示。
  - [x] 工具执行动画气泡与状态指示器。
  - [x] 交互式 HITL 审批卡片（入参 JSON 检查、就地编辑修改参数、批准、拒绝）。
  - [x] 长期记忆管理面板（增删改查、加权展示与实时检索）。
  - [x] 本地私有知识库面板（文件切片统计、一键增量扫描入库、语义检索实验台）。
  - [x] 系统环境监控与已注册工具安全矩阵自省。
- [x] 生产级容器化与打包：
  - [x] 多阶段轻量生产镜像 (`Dockerfile`)。
  - [x] Docker Compose 一键挂载启动编排 (`docker-compose.yml`)。
  - [x] 跨平台系统常驻守护进程脚本 (`scripts/run_daemon.ps1`, `scripts/run_daemon.sh`)。

---

### Milestone 6: 体验优化与工程化技能跃迁 (Experience & AI Engineering Skills)
*目标：打造首字响应 <50ms 的白盒渐进思考流与意图规则直出通道。*

- [x] 4 阶段白盒渐进式思考流引擎 (`ThinkingEngine`)：
  - [x] 意图分析 (`understand`) → 工具执行 (`tool`) → 答案整理 (`organize`) → 交付完成 (`completed`)。
  - [x] 工具白盒化动作翻译与心跳保活循环。
  - [x] 首字反馈响应时间（TTFT）压降至 <50ms。
- [x] 意图规则守卫与极速通道 (`IntentRouteGuard`)：
  - [x] 高频日常问候、系统查询 <50ms / 0 Token 消耗极速直出。
  - [x] 紧急制动安全熔断 (`emergency_halt`)。
  - [x] 多轮对话实体吸附锚定 (`sticky_anchor_enrichment`)。
- [x] 多会话原生 CRUD 存储 (`SessionService`)：
  - [x] 会话自动智能命名、全文快速检索过滤。
  - [x] 会话秒级恢复、重命名与消息历史持久化。
- [x] 5 阶段端到端全生命周期链路追踪 (`TraceStore`)：
  - [x] `输入规整` → `规则守卫` → `上下文预热` → `思考与工具瀑布` → `流式交付`。
  - [x] 毫秒级性能打标（TTFT, TTFA, TTLT）与 Web UI 瀑布流分析面板。

---

### Milestone 7: 生产级持久化架构与调度自愈基准 (Sprint 1C Engineering)
*目标：构建零数据丢失、零悬空竞态、具备单实例互斥锁与故障自愈的工业级状态调度引擎。*

- [x] SQLite Schema V3 版本化迁移体系 (`run_app_migrations`)：
  - [x] `_APP_V1`、`_APP_V2`、`_APP_V3` 线性升级与降级防范。
  - [x] 预升级物理文件原子备份 (`.bak.vX_to_vY`) 与只读完整性校验。
- [x] 8 表事务级外键级联清理 (`delete_session`)：
  - [x] 统一启用 `PRAGMA foreign_keys = ON;`。
  - [x] 会话级联删除彻底清理 `chat_sessions`、`chat_messages`、`agent_tasks`、`agent_runs`、`hitl_approval_requests`、`tool_execution_outcomes`、`agent_conversation_traces`、`ltm_memories`。
- [x] 持久化任务与运行协调器 (`TaskCoordinator`)：
  - [x] 状态闭包机管理 (`AgentTask` / `AgentRun`)。
  - [x] `(task_id, run_seq)` 唯一约束与事务防重。
  - [x] HITL 审批恢复生成独立 Run，分段追踪执行耗时。
- [x] 会话繁忙守卫 (`SessionBusyError` / HTTP 409 Conflict)：
  - [x] 覆盖 `CREATED`、`RUNNING`、`WAITING_APPROVAL`、`EXECUTING_TOOL` 任务与未决审批。
  - [x] 排他事务内原子校验，消除会话删除与任务执行竞态。
- [x] 高危操作在途阻断 (Option B 语义与 Fail-Closed)：
  - [x] 严格区分审批授权 (`ApprovalStatus`) 与物理副作用 (`ExecutionOutcomeStatus`)。
  - [x] 物理工具在途取消时 Run 标记为 `UNKNOWN`，彻底防御执行重放。
  - [x] 异步任务屏蔽保护 (`_execute_cancellation_cleanup_shielded`)，防止二次取消打断清理。
- [x] 单实例排他锁与自愈恢复 (`RuntimeInstanceLock` & `reconcile_on_startup`)：
  - [x] 跨平台排他锁守护，防多实例并发脑裂。
  - [x] 实例崩溃重启时自动将悬空任务收敛为 `INTERRUPTED` 或 `UNKNOWN`。
- [x] 实体吸附上下文实时写穿 (`Sticky Context Write-Through`)：
  - [x] 内存即时缓存与 `chat_sessions.metadata` 同步写穿，支持冷启动懒加载。
- [x] Trace 全生命周期归档与安全修剪 (`TraceStore.prune_old_traces`)：
  - [x] 超过 30 天或单会话超过 200 条的 Trace 记录定时非阻塞修剪。
