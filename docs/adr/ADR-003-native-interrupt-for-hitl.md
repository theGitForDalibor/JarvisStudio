# ADR-003: 基于 LangGraph 原生 interrupt() 与持久化检查点实现人机协同 (HITL)

- **状态**: 已接受 (Accepted)
- **决策日期**: 2026-10-03
- **决策人**: Principal Engineer / Tech Lead

---

## 1. 背景与上下文 (Context)

Jarvis Studio 拥有执行系统终端命令、写入/删除本地工作区文件以及调用外部网络服务的深层权限。必须建立可靠的安全机制，使得智能体在尝试执行破坏性动作时能够自动挂起，通知人工审批，并在获得批准或参数订正后恢复执行。

---

## 2. 架构决策 (Decision)

我们决定采用 LangGraph 原生 `interrupt()` 协议搭配持久化 SQLite 检查点（`SqliteSaver`）：
1. 在图拓扑中设立独立的 `approval_gate` 节点，审查最新 Assistant 消息的 `tool_calls`。
2. 凡命中高危名单（`RiskLevel.HIGH`）的工具，节点直接调用 `interrupt(approval_payload)`。
3. LangGraph 引擎自动在检查点数据库记录当前现场，并退出当前执行栈。
4. API 层返回 `status="interrupted"` 响应，等待人工交互。
5. 用户通过 REST API 提交决定后，系统通过 `graph.ainvoke(Command(resume=decision_payload))` 唤醒图执行。

---

## 3. 备选方案权衡 (Alternatives Considered)

1. **内存同步锁 / 协程挂起等待 (In-Memory Locking)**：
   - *否决理由*：长时间占用 HTTP 连接与内存句柄。一旦服务重启或网络闪断，挂起的任务现场全部丢失。
2. **重型消息队列驱动 (Celery / Temporal)**：
   - *否决理由*：对于个人单用户常驻助手而言架构过重，引入了 Redis、RabbitMQ 等不必要的基础设施复杂度。
3. **纯 Prompt 层面的口头确认**：
   - *否决理由*：极其危险且脆弱。大模型存在幻觉欺骗风险，可能自主伪造“用户已确认”而强行执行。

---

## 4. 决策影响与后果 (Consequences)

### 正向收益
- **故障自愈与崩溃安全**：等待用户审批期间，即使关闭终端或重启系统，状态依然完好保存在 SQLite 中，随时可恢复。
- **协议兼容性保护**：当用户拒绝执行时，系统自动生成合规的 `ToolMessage`，使大模型理解指令被否决，避免违反底层 LLM API 协议规范。
- **零额外资源消耗**：等待期间不占用任何计算线程与连接池。

### 权衡与代价
- 恢复执行时，调用端必须持有对应的 `thread_id` 会话标识。
