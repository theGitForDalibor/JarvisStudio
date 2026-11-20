# AgentStudio MVP 1.0 — 产品差距盘点与实施规划

**基线版本**：`main` @ `d005094`  
**自动化回归基线**：`287 passed, 12 skipped, 0 failed`（Layer A，不依赖外网）  
**品牌定位**：**Personal AI Agent Workspace** — *Create · Orchestrate · Execute · Deliver*

> 本文档基于当前仓库实际源码（前端 `index.html`、API 路由、Service、Runtime 与测试）进行独立盘点，严格区分“后端能力已实现 / API 已实现 / UI 已接入 / UI 可正常操作 / 完整业务闭环已验证”五个层次，不将后端测试通过等同于产品可用。

---

## A. 当前产品现状

### 1. 当前前端页面与导航结构

当前前端为单页应用 [index.html](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L60-L89)，顶部包含 6 个一级 Tab：

| Tab | DOM 视图 | 实际调用的 API | 定位与现状 |
| :--- | :--- | :--- | :--- |
| **💬 对话终端** | `#view-chat` (L96) | `/api/v1/sessions/*`, `/api/v1/chat/stream` | Session 侧边栏 + 流式对话，仅支持普通聊天与审批内联卡片 |
| **🛡️ HITL 审批** | `#view-approvals` (L231) | `/api/v1/approvals/*` | 按当前 `thread_id` 查看待审批高危工具调用并提交 approve/reject |
| **🧠 长期记忆 (LTM)** | `#view-memory` (L245) | `/api/v1/memory/*` | 个人记忆条目列表、检索、新增、删除 |
| **📚 本地知识库 (RAG)** | `#view-rag` (L267) | `/api/v1/rag/*` | Sprint 3B 已升级为 Document Workspace、Retrieval Playground、Evaluation 三区 |
| **⚡ 链路追踪 (Traces)** | `#view-traces` (L423) | `/api/v1/system/traces*` | 偏开发者调试的白盒 5 阶段瀑布流（TTFT / TTFA / TTLT） |
| **⚙️ 系统工具** | `#view-system` (L388) | `/api/v1/system/status`, `/tools`, `/mcp` | 只读基础设施状态卡片与工具注册表 |

### 2. 已实现但 UI 完全未接入的核心后端能力

经过全量扫描 [index.html](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html) 对 `/api/v1/` 的调用，以下 Sprint 1C–3C 核心后端与 API **在前端没有任何入口或调用**：

1. **Task 领域与执行控制面（[tasks.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/tasks.py)）**：
   - `POST /api/v1/tasks`（创建 Task、绑定 `knowledge_scope`、`auto_start_run`）
   - `GET /api/v1/tasks` & `GET /api/v1/tasks/{task_id}`（Task 列表、Runs、Events、Deliverables 聚合详情）
   - `POST /api/v1/tasks/{task_id}/execute`（同步 / 后台异步执行）
   - `POST /api/v1/tasks/{task_id}/pause`、`resume`、`cancel`、`append-instruction`
   - `GET /api/v1/tasks/{task_id}/events` & `events/stream`（持久化事件重放与 SSE Live Handoff）
   - `DELETE /api/v1/tasks/{task_id}` & `/purge`
2. **Deliverables 交付物管理（[deliverables.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/deliverables.py)）**：
   - `GET /api/v1/tasks/{task_id}/deliverables`
   - `GET /api/v1/deliverables/{deliverable_id}` & `/download`
3. **Provenance 校验能力（[provenance.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/rag/provenance.py)）**：
   - 仅存在于 Python 核心模块与测试中，尚未通过 API/UI 暴露交付物来源核验结果。

---

## B. 产品差距分析

| 功能模块 | 后端状态 | API 状态 | UI 状态 | 完整闭环 | 关键差距 | 优先级 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. 产品结构与导航** | 模块化单体完整 | 8 个路由模块就绪 | 6 个早期工程 Tab | 未闭环 | 缺少 **Tasks** 与 **Deliverables** 一级入口；`Traces` 与 `System` 偏开发调试视图 | **M0** |
| **2. Chat 与 Task 交互** | Session 与 Task 均可独立或关联 (`session_id`) | `/chat/stream` 与 `/tasks` 分离存在 | 仅 Chat 可用；无 Task 入口与“转任务”路径 | 未闭环 | 无法创建 Task、追加指令创建 Run、在 UI 中恢复刷新后的 Task 执行状态 | **M0** |
| **3. Agent 管理** | **未实现**（`AgentTask.assigned_agent_id` 仅为 `"general_agent"` 字符串） | **无** Agent CRUD / 配置端点 | **无** Agent 页面或选择器 | 未闭环 | 缺少内置 General Agent 展示及轻量 Agent 配置（名称、Prompt、模型、工具白名单、启用/停用） | **M0（默认 General Agent）/ M1（自定义）** |
| **4. Task 执行与控制体验** | `ExecutionEngine` + `TaskCoordinator` + `EventRecord` 完整 | `/execute`, `/pause`, `/resume`, `/cancel`, `/append-instruction`, `/events/stream` 完整 | **未接入** | 未闭环 | 用户无法在 UI 观察 QUEUED/RUNNING/PAUSED/WAITING_APPROVAL 状态、工具调用过程及多 Run | **M0** |
| **5. Deliverables 交付体验** | `DeliverableManager` 原子落盘 + SHA-256 + 多 Run 版本 | `/tasks/{id}/deliverables`, `/deliverables/{id}`, `/download`（缺全局列表与全文文本接口） | **未接入** | 未闭环 | 无法在 UI 预览 Markdown 报告、下载文件、对比多 Run 历史版本或查看来源关联 | **M0** |
| **6. Knowledge Workspace** | `SqliteVecStore` + FTS5 + `RAGEvaluator` + Server-side Scope 完整 | `/rag/*` 全套接口就绪 | `#view-rag` 已接入 3 个区域 | 部分闭环（受限于真实 Embedding） | UI 缺少在创建 Task 时选择 `KnowledgeScope` 的入口；本地网关默认无 `/v1/embeddings` 需明确引导 | **M0（Scope 选择）/ M1（引导）** |
| **7. Research & Analysis** | `search_web` + `fetch_web_page` + SSRF 防护 + `publish_research_report` + `verify_report_provenance` | 通过 Task 执行隐式触发 | 无研究任务快捷入口与来源核验展示 | 部分闭环 | 真实 LLM + `fetch_web_page` + 报告交付已实测跑通；`search_web` 在本机代理 TLS 受阻，UI 缺明确降级提示 | **M0（报告与错误呈现）** |
| **8. Settings 与首次体验** | `Settings` 支持环境变量与 `.env` | 仅只读 `GET /system/status` | `#view-system` 仅只读展示，无配置修改与引导 | 未闭环 | 首次进入直接落入空白对话页；缺模型/Base URL/API Key 脱敏配置、Embedding 诊断提示及空白页引导 | **M0** |

---

## C. MVP 产品结构建议

建议采用 **Linear / Vercel / Raycast** 风格的克制单层工作区导航（将现有开发调试感强的 Tab 收敛为 5 个清晰一级入口 + 1 个轻量系统/设置抽屉或入口）：

1. **Chat（对话）**：保留快速问答与 Session 历史；增加 **“转为 Task 执行”** 快捷动作，将当前对话目标带入 Task 创建。
2. **Tasks（任务中心 — MVP 核心工作台）**：
   - 左侧/顶部：Task 列表（按状态过滤：`RUNNING` / `WAITING_APPROVAL` / `PAUSED` / `COMPLETED` / `FAILED`）与“新建任务”表单（支持选择 Agent、绑定 `KnowledgeScope`）。
   - 主工作区：Run 切换器（Run #1, Run #2...）+ 实时 SSE 事件时间线（工具开始/完成、审批请求、状态流转）+ 控制栏（Pause / Resume / Cancel / HITL 审批）+ 底部 **Append Instruction** 输入框 + 右侧/Tab 关联 **Deliverables** 预览。
3. **Deliverables（交付物中心）**：聚合展示所有 Task 产出的正式交付物，支持 Markdown 在线预览、原文件下载、版本切换（按 `run_id`）与来源溯源标签。
4. **Knowledge（知识库）**：直接复用 Sprint 3B 已完成的 `#view-rag`（Document Workspace + Retrieval Playground + Evaluation）。
5. **Agents & Settings（Agent 与工作区设置）**：
   - **Agents**：默认内置可用 `General Agent`（及可选只读内置 `Research Agent` 预设），支持查看/编辑基础配置（Model、System Prompt、允许工具集、Knowledge Scope 默认值），M1 再开放新建/停用/删除自定义 Agent。
   - **Settings（合并原 `#view-system`）**：模型网关配置（Provider / Model / Base URL / 脱敏 API Key 状态）、Embedding 状态与缺失提示、MCP & 工具总览；将原有 `Traces`（白盒瀑布流）降级为 Task/System 内的 **“高级诊断”** 折叠入口，避免干扰普通用户。

---

## D. MVP 功能清单（M0 / M1 / M2 / M3）

### M0：MVP 必须具备（核心闭环缺一不可）
1. **Tasks 工作台（创建、列表、详情、状态恢复）**：
   - *验收标准*：用户 1 分钟内可输入目标创建 Task 并后台启动；刷新页面后列表与当前 Task 状态从 SQLite 完整恢复。
2. **SSE 实时执行流与状态控制（Pause / Resume / Cancel / HITL）**：
   - *验收标准*：UI 通过 `/api/v1/tasks/{id}/events/stream` 重放历史并实时接收新事件；点击 Pause/Resume/Cancel 后按钮状态与后端 `TaskStatus` 严格同步；遇到 `WAITING_APPROVAL` 时直接在任务流内完成审批并恢复执行。
3. **多 Run 追加指令（Append Instruction）**：
   - *验收标准*：任务完成或中断后，用户在任务详情内追加指令，触发 Run 2，UI 可切换查看 Run 1 与 Run 2 的事件流，且自动继承原 `KnowledgeScope`。
4. **Deliverables 预览、下载与版本保留**：
   - *验收标准*：调用 `publish_research_report` 后 Task 详情与 Deliverables 页面均可直接渲染 Markdown 全文预览与元数据（标题、大小、SHA-256、所属 Run），支持点击下载；Run 2 生成新版本时不覆盖 Run 1 产物；文件缺失时展示友好 404 提示而非报错崩溃。
5. **Task Knowledge Scope 绑定与 Knowledge 基础闭环**：
   - *验收标准*：创建 Task 时可选 Collection / Document 范围；复用 `#view-rag` 完成上传、列表、检索实验。
6. **基础 Settings 与首次使用引导（Onboarding）**：
   - *验收标准*：首次打开无配置或 LLM/Embedding 不可用时显示清晰的诊断提示与配置指引；提供可用的默认 `General Agent`；API Key 仅展示脱敏状态或安全写入，绝不在响应中明文回显。

### M1：MVP 应当具备（显著提升日常体验）
- 用户自定义 Agent（创建/编辑/启用/禁用/删除轻量 Agent Profile，配置专属 Prompt、模型与工具子集）。
- Chat 对话一键转为 Task（继承上下文生成 Task）。
- Deliverable 页面的 Provenance 来源核验徽章（直接调用 `verify_report_provenance` 展示已核验的 `doc_id` / URL）。
- 全局 Deliverables 聚合列表接口（`GET /api/v1/deliverables`）与搜索过滤。

### M2：后续迭代
- 长期记忆（LTM）与 Task 的自动关联推荐。
- RAG Evaluation 数据集批量导入导出与多模型对比。
- 定时/周期任务触发。

### M3：暂不考虑
- 多 Agent 编排器 / 可视化 Workflow Designer。
- 多用户 / RBAC / 团队权限体系。
- 分布式消息队列或独立向量数据库。

---

## E. MVP 开发路线（分阶段推进）

| 阶段 | 目标 | 实际修改模块与预计文件 | 验收标准 | Schema 变更 |
| :--- | :--- | :--- | :--- | :--- |
| **MVP-1：Deliverable & Settings API 补齐** | 补齐全局 Deliverable 列表、正文预览接口，以及安全配置读写与连通性自检 API | `src/jarvis_studio/api/routes/deliverables.py`、`src/jarvis_studio/api/routes/system.py`（或新增 `settings.py`）、`src/jarvis_studio/config/`、`tests/` | `GET /deliverables` 列表、`GET /deliverables/{id}/content` 预览、`GET/PATCH /system/settings`（Key 脱敏）及 `/system/preflight` 全部通过 API 测试 | **否** |
| **MVP-2：Tasks & Deliverables 前端工作台** | 接入 `/api/v1/tasks*` 与 `/api/v1/deliverables*`，打通创建 → SSE 观察 → 控制 → 追加指令 → 交付预览/下载闭环 | `src/jarvis_studio/api/static/index.html`（新增 `#view-tasks`、`#view-deliverables` 视图与 SSE 客户端） | 创建 Task、绑定 `KnowledgeScope`、SSE 实时刷新、Pause/Resume/Cancel、HITL 内联审批、Append Instruction 多 Run 切换、Markdown 预览与下载均可在浏览器实操 | **否** |
| **MVP-3：轻量 Agent Profile 与 Settings 引导** | 实现单用户轻量 Agent Profile 管理（内置 `general_agent` + 可配置模型/Prompt/工具）与首次引导/错误提示 UI | `src/jarvis_studio/core/agents/`（轻量 SQLite 表或 JSON 持久化）、`src/jarvis_studio/api/routes/agents.py`、`src/jarvis_studio/service/agent_service.py`、`index.html` | 用户可在 UI 选择/配置 Agent 并绑定到新 Task；空白页与未配置 Embedding/LLM 时有明确可执行提示 | **是（可选：新增 `agent_profiles` 表，或存配置表）** |
| **MVP-4：导航收敛、Chat 转 Task 与发布验收** | 合并开发调试入口，增加 Chat 一键转 Task 与 Provenance 徽章，执行全量与手动端到端发布验收 | `src/jarvis_studio/api/static/index.html`、`tests/` | 完整走通下方《G. MVP 发布验收清单》所有条目 | **否** |

---

## F. 已知风险与技术债（仅列出影响 MVP 的实际事项）

1. **P1 — `AgentRuntime` 与 `DeliverableManager` 全局 Settings 回退不一致**：
   - *位置*：[agent_runtime.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/core/runtime/agent_runtime.py#L110)、[report_tools.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/report_tools.py#L49)。
   - *影响*：当 `AgentRuntime` 使用非默认 `sqlite_path` 时，`get_deliverable_manager()` 仍读取全局 `get_settings().storage.sqlite_path`，可导致 `Task not found`。MVP-1/3 前需确保 `AgentRuntime.start()` 同步绑定 `_default_deliverable_manager` 到当前 `cfg`。
2. **P1 — Deliverable 缺少全文预览与全局列表接口**：
   - *位置*：[deliverables.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/deliverables.py)。
   - *影响*：`DeliverableRecord` 仅含 `content_preview`（截断摘要），UI 无法直接渲染完整 Markdown 报告，且无法跨 Task 浏览所有产出物。
3. **P2 — 真实 Embedding 网关依赖与无服务降级提示**：
   - *现状*：当前本地反代网关不支持 `/v1/embeddings`（404）。UI 需明确提示当前处于“仅文本/未启用真实向量检索”或引导配置可用 Embedding 端点，避免用户误解检索效果。
4. **P2 — 外部 Web Search 代理 TLS 异常呈现**：
   - *现状*：本机代理下 DuckDuckGo 可能发生 SSL EOF / 超时。Agent 工具返回错误时，UI 事件流需清晰高亮外部搜索受限提示，而不是静默吞掉。
5. **P2 — SSE 断线重连与页面刷新恢复**：
   - *位置*：[tasks.py](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/tasks.py#L303) `/events/stream`。
   - *影响*：前端接入时必须传递 `last_seq` 或基于 `GET /tasks/{task_id}` 先拉取全量状态再订阅增量 SSE，防止刷新页面后丢失执行进度。

---

## G. MVP 发布验收清单（Release Acceptance Checklist）

- [ ] **新用户首次使用**：打开首页有清晰引导；未配置或配置错误时给出脱敏可操作的诊断提示。
- [ ] **创建 Chat**：新建 Session、流式对话、刷新页面后会话与历史消息不丢失。
- [ ] **创建 Task**：可在 UI 创建带（或不带）`KnowledgeScope` 的 Task，并正确进入后台排队执行。
- [ ] **真实 LLM 执行与工具调用**：使用真实可用模型执行 Task，UI 事件流实时显示 `TOOL_STARTED` / `TOOL_COMPLETED`。
- [ ] **执行控制（Pause / Resume / Cancel / HITL）**：各控制动作后 UI 状态、后端 API 与 SQLite 状态一致；高危工具可在任务内审批恢复。
- [ ] **Append Instruction / 多 Run**：对已完成任务追加指令生成 Run 2，可切换查看 Run 1 与 Run 2 历史记录且 Scope 继承不变。
- [ ] **Deliverable 预览与下载**：UI 可完整预览 Markdown 报告、查看元数据与所属 Run、成功下载文件；Run 2 产物不覆盖 Run 1；文件缺失时展示友好提示。
- [ ] **Knowledge 上传与检索**：在 Knowledge 页面上传文档显示 `READY`，Playground 可检索且 Task 绑定 Scope 后不会越权泄露。
- [ ] **Research 报告生成**：真实 LLM 抓取网页并生成带来源引用的报告；当 `search_web` 因代理网络不可用时，UI 清晰呈现错误原因。
- [ ] **页面刷新恢复**：在任务执行中或完成后刷新浏览器，重新进入即可恢复完整状态、事件流与交付物。
