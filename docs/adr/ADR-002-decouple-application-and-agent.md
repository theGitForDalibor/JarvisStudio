# ADR-002: 应用层、服务层与 Agent 运行时的严格 5 层解耦架构

- **状态**: 已接受 (Accepted)
- **决策日期**: 2026-10-03
- **决策人**: Principal Engineer / Tech Lead

---

## 1. 背景与上下文 (Context)

常见的简易智能体开源项目通常存在严重的强耦合问题：例如直接在 FastAPI 路由函数内构建图、在 Prompt 内硬编码网络协议或直接在命令行 REPL 中调用大模型底层 API。这种耦合导致一旦需要增加 Web 前端、桌面端（如 Raycast）、CLI 或执行后台定时自动化任务时，业务逻辑无法复用，单元测试难以编写。

---

## 2. 架构决策 (Decision)

我们决定推行严格的 5 层分层解耦体系：
```text
UI (CLI / Web) ──▶ Application API 网关 ──▶ 业务应用服务层 ──▶ LangGraph 运行时 ──▶ 工具与 MCP 生态
```

- **API 层 (`src/jarvis_studio/api`)**：只负责 HTTP 接入、Pydantic 参数校验与错误映射，**严禁触碰 LangGraph 图节点与 Checkpointer**。
- **服务层 (`src/jarvis_studio/service`)**：`AgentService` 与 `SessionService` 充当外观层（Facade），负责线程管理、图调用与中断信号转换。
- **运行时层 (`src/jarvis_studio/agent`)**：纯净的 LangGraph `StateGraph`，完全不知晓 FastAPI、CLI 或 HTTP 请求的存在。

---

## 3. 决策影响与后果 (Consequences)

### 正向收益
- **多端零冗余共享**：开发者 CLI 终端与 FastAPI Web 网关共享同一套底层 `AgentService`，保证多端行为绝对一致。
- **自动化测试极其简便**：可以单独对 Graph、Service 或 API 端点分层编写测试，无需启动完整 Web 容器即可完成全流程状态机验证。
- **演进无侵入**：未来若新增 WebSocket 双向通信或 Electron 桌面端，核心智能体逻辑无需变更一行代码。

### 权衡与代价
- 跨层之间需要少量数据映射对象（如 `ChatRequest` ➔ `AgentExecutionResult` ➔ `ChatResponse`）。
