# Jarvis Studio - 技术文档中心 (Documentation Hub)

欢迎查阅 **Jarvis Studio** 技术文档中心。本文档库包含了完整的系统架构蓝图、设计规范、架构决策记录 (ADR)、开发指南与演进路线图。

---

## 目录索引 (Documentation Index)

```text
docs/
├── architecture/                     # 系统级技术蓝图与状态机规范
│   ├── system-architecture.md        # 5 层解耦架构、模块边界与全景拓扑图
│   └── execution-lifecycle.md        # 运行时请求生命周期、StateGraph 状态流转与断点恢复
├── design/                           # 核心子系统设计规范
│   ├── hitl-specification.md         # Human-in-the-Loop 人机协同规范、审批状态机与 API 契约
│   ├── tool-system-and-safety.md     # ToolRegistry、RiskLevel 安全分级矩阵与动态 MCP 适配器
│   └── llm-adapter-layer.md          # LLMFactory 多模型统一适配架构与热插拔配置
├── development/                      # 开发者手册与参数字典
│   ├── setup-and-development.md      # 环境初始化 (Python 3.12 + uv)、测试执行、CLI 命令与代码规范
│   └── configuration-reference.md    # 环境变量 (.env) 与 settings.yaml 全参数参考手册
├── adr/                              # 架构决策记录 (Architecture Decision Records)
│   ├── ADR-001-adopt-langgraph-and-mcp.md        # 决定采用 LangGraph 与 MCP 替代自研 Runtime
│   ├── ADR-002-decouple-application-and-agent.md # 决定实行严格的 5 层分层解耦体系
│   └── ADR-003-native-interrupt-for-hitl.md      # 决定采用原生 interrupt() 与 SQLite 检查点实现 HITL
└── roadmap/                          # 产品与工程演进
    └── engineering-roadmap.md        # M0 ~ M5 阶段性演进目标与当前进度
```

---

## 文档速览 (Document Summaries)

### 1. 系统架构 (Architecture)
- **[系统分层架构](architecture/system-architecture.md)**：深入阐述 UI ↔ API Gateway ↔ Application Service ↔ LangGraph Runtime ↔ Tools/MCP 的 5 层解耦规范，界定各层职责边界与禁止跨层规则。
- **[执行生命周期与状态机](architecture/execution-lifecycle.md)**：详细剖析普通对话、安全工具自主调用、高危工具中断确认三种场景下 `AgentState` 的流转与检查点落盘时序。

### 2. 设计规范 (Design Specifications)
- **[HITL 人机协同设计规范](design/hitl-specification.md)**：规范高危操作拦截机制、审批状态机（`PENDING`、`APPROVED`、`REJECTED`、`MODIFIED`）、合规拒绝处理与 REST API 契约。
- **[工具子系统与安全矩阵](design/tool-system-and-safety.md)**：规范 `ToolRegistry` 工具契约、三级风险矩阵（`SAFE`、`MEDIUM`、`HIGH`）、内置文件与 Shell 工具，以及 MCP JSON Schema 动态强类型转换原理。
- **[LLM 多模型适配层](design/llm-adapter-layer.md)**：规范 `LLMFactory` 统一接口实现、零硬编码多厂商适配（OpenAI、DeepSeek、Ollama 等）与运行时回退策略。

### 3. 开发与运维 (Development & Operations)
- **[环境搭建与开发指南](development/setup-and-development.md)**：提供 Python 3.12 与 `uv` 环境搭建步骤、Pytest 测试运行命令、Ruff 代码规范与开发守则。
- **[配置全参数参考手册](development/configuration-reference.md)**：系统整理 `.env` 环境变量、`config/settings.yaml` 与 `config/mcp_servers.json` 的全部配置参数字典与安全建议。

### 4. 架构决策记录 (ADRs)
- **[ADR-001: 采用 LangGraph 与 MCP](adr/ADR-001-adopt-langgraph-and-mcp.md)**：记录从自研 Runtime 转向成熟工业级框架的决策背景与利弊权衡。
- **[ADR-002: 应用与智能体运行时的 5 层解耦](adr/ADR-002-decouple-application-and-agent.md)**：记录 API、Service 与 Agent Core 解耦的设计考量与多端接入收益。
- **[ADR-003: 采用原生 interrupt() 实现 HITL](adr/ADR-003-native-interrupt-for-hitl.md)**：记录基于 LangGraph 原生断点机制与持久化 Checkpoint 实现安全拦截的权衡。

### 5. 演进路线图 (Roadmap)
- **[工程演进路线图](roadmap/engineering-roadmap.md)**：全量记录 M0（脚手架）到 M5（生产级 Web 界面）的各阶段目标，如实标记已完成、进行中与规划中的工程事项。
