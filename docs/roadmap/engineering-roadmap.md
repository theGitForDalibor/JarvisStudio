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
    M4 --> M5["M5: Web 界面与流式输出\n(计划中)"]
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
- [ ] Server-Sent Events (SSE) 逐 Token 与步骤事件流式传输。
- [ ] 双向控制 WebSocket 网关协议。
- [ ] 现代化 Web UI 前端 (Next.js / Tailwind CSS)：
  - [ ] 富文本 Markdown 与代码高亮对话界面。
  - [ ] 交互式 HITL 审批弹窗（命令高亮、入参 Diff 差异比对、批准/拒绝/编辑按钮）。
  - [ ] 已挂载工具与 MCP 服务状态卡片。
  - [ ] 长期记忆与知识库管理面板。
- [ ] 打包与部署：
  - [ ] 生产容器化镜像 (Dockerfile + Docker Compose)。
  - [ ] 个人系统开机常驻守护进程脚本。
