# ADR-001: 采用 LangGraph 与 Model Context Protocol (MCP) 作为生产基座

- **状态**: 已接受 (Accepted)
- **决策日期**: 2026-10-03
- **决策人**: Principal Engineer / Tech Lead

---

## 1. 背景与上下文 (Context)

在此前的探索阶段，团队先后完成了三代学习型项目：
- **Jarvis Agent**：从零自研纯 Python Agent Runtime，深入学习了 ReAct 循环、内存管理与工具执行机制。
- **Jarvis Agent Plus**：专注于学习 LangChain 框架抽象。
- **Jarvis Agent Pro**：专注于学习基于 LangGraph 的图循环编排。

**Jarvis Studio 的定位是生产级个人应用**，目标是长期稳定服务于日常开发工作，而非为了学习继续重复手写底层 Runtime。自研 Agent Loop 在断点快照、并发安全、异常重试、流式传输与跨进程恢复等复杂场景下需要付出巨大的边角维护成本。

---

## 2. 架构决策 (Decision)

我们决定：
1. **采用 LangGraph 作为核心图运行时**，负责 Agent 的状态机管理、条件路由、检查点持久化以及人机协同中断。
2. **采用 LangChain Core 基础组件**，统一抽象消息协议（`HumanMessage`, `AIMessage`, `ToolMessage`）与标准工具接口。
3. **采用 Model Context Protocol (MCP) 官方标准**，作为未来接入外部系统（文件、数据库、GitHub、浏览器）的首选扩展方式。

---

## 3. 备选方案权衡 (Alternatives Considered)

1. **继续维护自研的 Jarvis Agent Runtime**：
   - *否决理由*：维护自研状态机与持久化引擎耗费大量精力，且生态扩展性弱，违背“产品优先”原则。
2. **使用旧版 LangChain AgentExecutor**：
   - *否决理由*：旧版 `AgentExecutor` 过于黑盒、缺乏细粒度的单步快照，难以优雅实现多分支人机协同审查。
3. **为每个外部服务定制私有 Agent 逻辑**：
   - *否决理由*：造成高度耦合与代码膨胀，违背 MCP 开放生态理念。

---

## 4. 决策影响与后果 (Consequences)

### 正向收益
- 获得工业级稳定的图执行引擎与现成的 SQLite 检查点持久化。
- 获得原生的 `interrupt()` 与 `Command(resume=...)` 支持，优雅解决人机协同审批。
- 能够无缝兼容不断扩大的开源 MCP Server 生态。

### 权衡与代价
- 产生对 LangGraph / LangChain 外部依赖库版本迭代的追踪成本。
- 需要遵循状态图的增量归约范式（`add_messages`）。
