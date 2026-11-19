# AgentStudio MVP 实施路线图 (MVP Roadmap)
## Personal AI Agent Workspace

> **产品代号**：AgentStudio  
> **文档版本**：v1.0 (Roadmap Baseline)  
> **核心原则**：**以真实任务端到端完成能力作为验收核心，而非功能模块数量堆砌。**  
> **输出路径**：`docs/roadmap/AGENTSTUDIO_MVP_ROADMAP.md`

---

## 1. 第一阶段垂直场景选型与推进策略

系统第一阶段定义了三大核心业务场景：
1. **Research & Analysis (技术调研与方案分析)**
2. **Knowledge & Documents (文档分析与知识整理)**
3. **General Task Automation (多步任务与自动化)**

### 1.1 三大场景对比与评估矩阵

| 评估维度 | 场景 1：Research & Analysis | 场景 2：Knowledge & Documents | 场景 3：General Task Automation |
| :--- | :--- | :--- | :--- |
| **真实业务价值** | **极高**。直接产出结构完整、带引用论据的深度技术调研/选型报告，解决专业人士痛点。 | **高**。实现本地知识的高效检索与总结，但若无独立报告产出易退化为普通 RAG 问答。 | **高**。解放重复操作，但对用户定制化需求差异大，长尾操作多。 |
| **与目标原则契合度** | **100% 契合**。“Deliverable-centric”的最佳试金石（Markdown/HTML 报告即为独立可用交付成果）。 | **80% 契合**。更多偏向只读摘要，交付物形态偏单一。 | **70% 契合**。以外部副作用为主，交付物多为操作日志或变更结果。 |
| **现有工程复用度** | **极高**。直接复用已验证的 `SqliteVecStore`、`read_file`、网络检索工具与 LangGraph 运行时。 | **高**。强依赖已有 `sqlite-vec`，需新增更多文件格式解析器与 AST 切块。 | **中等**。虽有 `ProcessRunner`，但涉及复杂 MCP 协议适配与定时调度机制。 |
| **执行风险与安全度** | **极低 (低风险)**。以读取、检索和文本提炼为主，仅在最终落盘报告时触发一次受控写操作。 | **极低**。纯本地只读计算。 | **高风险**。涉及本地 Shell 命令与外部写操作，极易误伤本地开发环境。 |
| **用户体验震撼度** | **立竿见影**。用户下发复杂技术命题，Agent 在 2 分钟内端到端交出一份漂亮的专业报告。 | **平缓**。用户感知类似于高级本地搜索工具。 | **两极分化**。跑通很爽，但配置繁琐，极依赖外部环境就绪度。 |

### 1.2 推荐实施顺序与关键决策

> **首选第一条完备 Vertical Slice：【Research & Analysis (技术调研与方案分析)】**

**推荐路线顺序**：
$$\text{Sprint 2C: Research \& Analysis} \longrightarrow \text{Sprint 3: Knowledge \& Documents} \longrightarrow \text{Sprint 4: General Task Automation}$$

**决策理由**：
1. **成果最可见，最能体现 AgentStudio 与传统 Chat UI 的质的跨越**：传统 Chat 只能在对话气泡里吐出一串长文本，而 Research 场景直接将终态沉淀为 `agent_deliverables` 中的 Markdown/HTML 完整报告，带预览、图表与一键下载，完美闭环“Deliverable-Centric”理念。
2. **工程实现路径最平滑**：避开了复杂外部系统写操作的不确定性，充分发挥大模型在多步规划、信息整合与长文本结构化撰写上的优势。
3. **完美验证智能体分工交接**：General Agent 负责立项和调度，Research Specialist 负责检索和交叉核验，最终交付物生成器负责成文，这是 Multi-Agent 架构最自然落地的场景。

---

## 2. 优先级定义标准 (P0 / P1 / P2)

- **P0 (阻塞与可靠性级)**：安全底线、数据持久化完整性、并发撕裂风险、进程泄露、任务与会话领域解耦。任何 P0 问题未解决，严禁推进后续功能。
- **P1 (MVP 核心任务闭环级)**：用户可感知的端到端任务完成能力，包括 Task Center、Task Detail、Deliverables 展台、真实执行时间线与 Research 场景垂直切片。
- **P2 (增强与非必要体验级)**：自定义 Agent 配置抽屉、复杂甘特图渲染、暗黑主题精细调节、非必要格式导出。

---

## 3. Sprint 实施路线全景表

```
┌────────────────────────────────────────────────────────────────────────┐
│ Sprint 2A: 任务领域模型与交付物管道建设 (Task & Deliverables Pipeline)    │
│ [P0/P1] 领域解耦 · Schema V4 · Task/Deliverable REST API · 交付物落盘   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ Sprint 2B: 真实事件可观测流与人机受控中心 (Real Observability & HITL)   │
│ [P1] 移除伪思考 · 真实 SSE Pipeline · 任务暂停/取消/恢复 · 参数修改审批 │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ Sprint 2C: 首个垂直切片闭环：技术调研场景 (Research & Analysis Slice)  │
│ [P1] 智能规划 · General+Research 专家协同 · 深度调研报告 Deliverable   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ Sprint 2D: 智能体中心与工作空间 UI 现代化 (Workspace UI & Agent Center)│
│ [P1/P2] 现代 SPA 工作空间 · 内置专家库 · 自定义 Agent 克隆与配置       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ Sprint 2E: 系统加固、跨平台验证与平滑品牌迁移 (Hardening & Branding)   │
│ [P0/P2] 213+ 测试加固 · Windows/POSIX 兼容 · 低风险 AgentStudio 品牌迁移│
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 各 Sprint 详细实施计划

---

### Sprint 2A：任务领域模型与交付物管道建设 (Task & Deliverable Domain)

> **实施详案参考**：👉 [docs/roadmap/SPRINT_2A_IMPLEMENTATION_PLAN.md](SPRINT_2A_IMPLEMENTATION_PLAN.md) 与 [docs/architecture/AGENTSTUDIO_DOMAIN_EVOLUTION.md](../architecture/AGENTSTUDIO_DOMAIN_EVOLUTION.md)

- **目标**：彻底打破 Chat 与 Task 的 1:1 强绑定，正式建立 `Deliverable` 实体和全套 API，完成从“聊天消息记录”到“任务与交付物沉淀”的架构底座转型。
- **优先级**：**P0 + P1**
- **用户可感知能力**：
  - 用户可在 API/CLI 层面独立创建、查询、查看 Task，不依赖先开 Chat 对话；
  - 会话被删除时，其关联的已立项 Task 完好保留，资产绝不丢失；
  - 任务执行完毕后，系统能够生成独立的交付物文件并在工作区按 `deliverables/{task_id}/run_{seq}/` 安全归档与下载。
- **需要新增的工程能力**：
  1. 数据库升级：在 `core/persistence/migrations.py` 落地 **Schema V4**（将 `agent_tasks.session_id` 改为 NULLABLE，外键改为 `ON DELETE SET NULL`；创建 `agent_deliverables` 表；创建 `agent_events` 表）；
  2. 领域服务：实现 `src/jarvis_studio/service/deliverable_manager.py`，支持文件原子落盘（.tmp -> replace）、元数据提取、哈希指纹与 2000 字符文本预览；
  3. 服务解耦：升级 `TaskService`，改写 `SessionService.delete_session`（软解绑而不删任务）；
  4. API 网关：新增 `src/jarvis_studio/api/routes/tasks.py` 与 `deliverables.py`，暴露标准 REST 接口；
  5. 工具准备：新增 `search_tools.py`（最小通用搜索适配器）与 `report_tools.py`（报告发布工具），打通 Research 垂直切片前置链；
  6. 修复 `pyproject.toml` 中的 `pythonpath = [".", "src"]`，彻底消除测试导入路径隐患。
- **复用的已有模块**：
  - `core/tasks/coordinator.py`（复用已有的 10 态 Task 与 7 态 Run 状态机）；
  - `core/persistence/sqlite.py`（复用同步事务与 WAL 机制）；
  - `tools/builtin/file_tools.py` 中的 `resolve_safe_path` 沙箱安全防线。
- **依赖关系**：无前置依赖，直接基于当前 `main` 分支启动。
- **潜在风险**：Task 与 Session 解耦时，需确保遗留历史会话的向后兼容读取。
- **验收标准**：
  - 自动化测试覆盖 `tasks.py` 与 `deliverables.py` 全量路由；
  - 独立创建 Task、从 Session 创建 Task、删除 Session 保留 Task 均通过断言；
  - 成功生成物理 Markdown 报告并在工作区安全归档；
  - 现有 213 项单元测试与新增测试全部保持全绿通过。

---

### Sprint 2B：真实事件可观测流与人机受控中心 (Real Observability & HITL Control)

- **目标**：全面落实“Observable by Design”和“Bounded Autonomy”原则，彻底废除伪造思考过程，打通真实的 SSE 事件管道与全周期用户控制能力。
- **优先级**：**P1**
- **用户可感知能力**：
  - 用户看到的每一个进度反馈都是真实的（工具开始、工具结束、耗时、状态流转），无虚假文本；
  - 用户可在任务运行期间随时点击“暂停”、“取消”；
  - 遇到写操作拦截时，用户不仅能点击批准或拒绝，还能在界面上直接修改工具参数（如改写目标文件路径或命令）并安全恢复执行。
- **需要新增的工程能力**：
  1. 彻底移除 `core/thinking/engine.py` 中的硬编码文本生成，重构为 `core/observability/event_emitter.py`；
  2. 在 `AgentService` 中捕获 `StateGraph.astream_events` 的原生节点与工具事件，规范化为 `task_status_changed`, `tool_start`, `tool_end` SSE 广播；
  3. 完善主动任务取消链路：调用 `TaskCoordinator.cancel_task_and_run`，触发 `ProcessRunner` 杀除关联子进程树；
  4. 适配前端 HITL 审批卡片支持在原位对 JSON 参数进行实时编辑并提交。
- **复用的已有模块**：
  - `agent/hitl/manager.py`（复用 CAS 租约、四元组鉴权与防重放）；
  - `core/process/runner.py`（复用 JobObject / killpg 进程治理）。
- **依赖关系**：依赖 Sprint 2A 的 Task 领域 API。
- **潜在风险**：SSE 长连接在网络波动或客户端异常刷新时的优雅断开与资源回收。
- **验收标准**：
  - 前端建立 SSE 连接后，能在 50ms 内收到真实 `tool_start` 与 `tool_end` 事件；
  - 点击“取消任务”，后台子进程在 1 秒内彻底终结，无孤儿进程残留，DB 状态准确记录为 `CANCELLED` 或 `UNKNOWN`；
  - 提交修改参数（Modify），工具以修改后的新入参执行并通过测试。

---

### Sprint 2C：首个垂直切片闭环：技术调研场景 (Research & Analysis Slice)

- **目标**：以 Technical Research & Analysis 为突破口，打造 AgentStudio 的首个端到端业务闭环，实现“自然语言目标输入 → 智能规划 → 检索与交叉核验 → 结构化交付物生成 → 用户预览下载”。
- **优先级**：**P1 (MVP 皇冠特性)**
- **用户可感知能力**：
  - 用户在界面输入：“请帮我调研 2026 年嵌入式向量数据库的最新选型，对比 sqlite-vec、Chromadb 与 pgvector 在本地单机场景的性能与依赖”；
  - Agent 展现 3-4 步结构化规划；
  - Agent 调用知识库与检索工具深入分析，必要时委托 Research Specialist 专业智能体；
  - 任务闭环时，Task 详情页自动弹出包含完整对比表格、架构优劣分析与参考文献的 Markdown/HTML 深度调研报告；
  - 用户可一键复制、全屏预览或下载此报告。
- **需要新增的工程能力**：
  1. 专业智能体图节点：在 `agent/` 下构建专用的 `ResearchSpecialistNode`，具备多轮检索、提炼对比与长文本报告组织能力；
  2. 渐进式规划决策：引入轻量规划器，识别任务目标复杂度，自动生成步骤清单并持久化至 `task.plan_json`；
  3. 报告交付物生成工具：实现 `generate_markdown_report` 内置工具，规范化报告大纲与引用格式。
- **复用的已有模块**：
  - `rag/store.py`（复用 sqlite-vec 本地向量检索）；
  - `tools/builtin/file_tools.py`（复用安全的 `write_file` 落盘报告）；
  - Sprint 2A 的 `DeliverableManager`。
- **依赖关系**：依赖 Sprint 2A（交付物模型）与 Sprint 2B（真实事件管道）。
- **潜在风险**：大模型生成长篇调研报告时可能出现单次 Token 上限截断，需采用分章节撰写或增量写入机制。
- **验收标准**：
  - 编写端到端集成测试 `test_e2e_research_slice.py`；
  - 测试用例自动触发调研任务，全程无报错，并在工作区产出字数 > 1500 字且包含标准 Markdown 标题和表格的可用报告文件；
  - `agent_deliverables` 成功记录该报告的完整元数据。

---

### Sprint 2D：智能体中心与工作空间 UI 现代化 (Workspace UI & Agent Center)

- **目标**：彻底告别简陋的单页调试界面，落地包含 Dashboard、Task Center、Task Detail、Chat、Agent Center 的现代化个人 Agent 工作空间。
- **优先级**：**P1 + P2**
- **用户可感知能力**：
  - 全新仪表盘：清晰掌握活跃任务态势、最新产出成果展台与待审批通知；
  - 任务中心：支持按状态筛选任务、全文搜索、进入 Task Detail；
  - 任务详情：三栏/双栏自适应工作台，左侧沉浸式阅读报告，右侧观察真实时间线；
  - 智能体中心：直观查看内置专业 Agent（General, Research Specialist, Document Specialist），并支持克隆自定义 Agent。
- **需要新增的工程能力**：
  1. 重塑前端架构：采用模块化组件设计（可采用 ESM 原生组件化或极轻量前端工程，零重量后端依赖，由 FastAPI 直接托管）；
  2. 实现交互式 Deliverable 预览组件（集成 `marked.js` 代码高亮与 `mermaid.js` 架构图渲染）；
  3. 实现 Task Timeline 组件与实时 SSE 状态绑定。
- **复用的已有模块**：
  - 后端全部 REST API 与 SSE 管道；
  - Tailwind CSS 样式系统与已验证的 Markdown/Highlight 解析管线。
- **依赖关系**：依赖 Sprint 2A、2B、2C 提供的完整后端支持。
- **潜在风险**：前端组件过多可能导致初次加载体积膨胀，需坚持原生轻量化，不引入无必要的重型前端框架依赖。
- **验收标准**：
  - 在 Chrome / Edge 浏览器中流畅运行，无控制台报错；
  - 能够流畅完成“从 Dashboard 浏览任务 -> 进入 Task Detail 验收交付物 -> 审批拦截弹窗决策”的全流程视觉交互闭环。

---

### Sprint 2E：系统加固、跨平台验证与平滑品牌迁移 (Hardening & Branding)

- **目标**：完成全面的代码健壮性加固、跨平台兼容性验证，并以零破坏、极低风险的方式完成向 **AgentStudio** 的品牌迁移。
- **优先级**：**P0 (加固) + P2 (品牌)**
- **用户可感知能力**：
  - 软件展示名称正式统一为 **AgentStudio (Personal AI Agent Workspace)**；
  - 品牌理念：“Create · Orchestrate · Execute · Deliver”贯彻于 UI 标题、CLI 终端与欢迎界面；
  - 无论在 Windows 还是 Linux/macOS 上均能丝滑、静默、单活稳定运行。
- **需要新增的工程能力**：
  1. 低风险品牌迁移：更新 `README.md`、UI 标题、网页 Meta、CLI 展示横幅、OpenAPI 文档标题，**坚决不变更 Python 包名 `jarvis_studio`、数据库表名与底层关键路径**；
  2. 跨平台兼容性测试矩阵：在 Windows 10/11 与 Linux 容器环境全面跑通测试套件；
  3. 编写完整的用户操作指南与架构文档归档。
- **复用的已有模块**：全系统所有模块。
- **依赖关系**：前序 Sprint 全部完成。
- **潜在风险**：若误修改包名或公开 API 路由，可能引发下游调用断裂。严格执行兼容性约束。
- **验收标准**：
  - `git grep -i "jarvis studio"` 仅出现在保留兼容性声明、历史 ADR 或包名定义处，所有面向用户的 UI、CLI 横幅和主文档均已优雅呈现为 **AgentStudio**；
  - 全量 pytest 测试套件 100% 通过（通过数 > 220 项）；
  - Windows 环境与 Linux 环境双绿。

---

## 5. 风险评估与应对预案

| 风险项 | 严重级别 | 触发条件 | 预防与缓解措施 |
| :--- | :---: | :--- | :--- |
| **1. 交付物生成截断或超时** | HIGH | 大模型在单轮生成超长深度报告（>4000 Tokens）时耗时过长或触发网关超时。 | 采用分步撰写模式：先生成结构化大纲，再逐章节填充，最后通过 `DeliverableManager` 合并落盘；设置合理的网关流式超时。 |
| **2. 并发写冲突 (SQLITE_BUSY)** | MEDIUM | 多个专业 Agent 或高频 SSE 状态更新瞬时竞争 SQLite 事务写锁。 | 严格恪守 WAL 模式下的单写者模型，所有写操作走统一的 `sync_transaction`，配置 `busy_timeout=5000`，对瞬时竞争引入退避重试。 |
| **3. 前端工程化臃肿化** | MEDIUM | 为了追求复杂交互而引入厚重的 Node/Webpack 工具链，破坏个人项目轻量即用性。 | 坚持轻量原则，采用现代浏览器原生 ESM 或单一轻量构建包，保持单文件/极简目录结构，确保与 FastAPI 静态服务完美契合。 |
| **4. 品牌更名破坏现有测试与导入** | HIGH | 盲目进行全局文本替换，导致 `from jarvis_studio import ...` 损坏。 | 严格执行**展示层与内部标识层分离**：代码包名、目录名、表名一律维持 `jarvis_studio`；仅更改 UI Header、HTML Title、CLI Print Banner 和用户文档。 |
