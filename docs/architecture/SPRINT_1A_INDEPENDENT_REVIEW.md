# Jarvis Studio — Sprint 1A Independent Security Review

> **角色**：Independent Principal Security Engineer  
> **审计对象**：Jarvis Studio V2 — Sprint 1A (`feature/v2-sprint-1a-security`)  
> **审查基准**：代码库实现源码、数据流图谱、执行时序与 63 项测试用例  
> **原则**：纯静态与逆向对抗审查，不编写或修改业务代码，不自动进入下一阶段  
> **日期**：2026-11-10  

---

## Executive Summary (执行摘要)

本报告由独立安全工程团队对刚刚提交的 `feature/v2-sprint-1a-security` 分支进行全面的逆向对抗审计。

Sprint 1A 的核心改造目标是解决 HITL 多工具调用的越权执行及工作区路径逃逸。审计确认：**该分支成功建立了统一授权网关及多维路径防御，彻底修复了原代码中“首个工具拦截后放行剩余高危工具”和“无包含关系检查的绝对路径直穿”两个致命漏洞，原有 43 项测试及新增 20 项测试全部绿灯通过（63/63 PASS）。**

然而，深入的代码级逆向推演发现了若干在深水区隐藏的架构与实现风险：
1. **授权缺少“消费失效（Consumed）”机制**：授权记录在执行后仍维持 `APPROVED` 状态，存在同 Session 后续轮次重放旧授权的潜在窗口。
2. **跨 Session 检索参数哈希时遗漏 `thread_id` 约束**：在特定回退路径下存在跨线程哈希污染可能。
3. **HITLManager 为单例纯内存存储**：未纳入持久化体系，多进程部署或服务重启将导致审批状态悬空。
4. **文件操作面临并发符号链接替换（TOCTOU）的物理局限**：应用层路径校验实现了 Level 1 & Level 2 防御，但未达到原子式文件描述符防护（Level 3）或内核沙箱隔离（Level 4）。

**最终审计结论：`APPROVED WITH CONDITIONS`（满足指定条件后建议合并）。**

---

## 一、A. 已确认修复的问题

经过对源码的逐行审计，以下原存致命安全漏洞已得到确凿修复：

### 1.1 HITL 多工具越权与放行缺陷彻底根除
- **证据文件**：[`src/jarvis_studio/agent/nodes/approval.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L112-L215)
- **分析**：原实现仅使用 `break` 拦截首个高危工具，后续高危工具未记录在案。重构后，`approval_node` 完整遍历 `last_message.tool_calls`，对所有匹配 `tool_registry.requires_approval(...)` 的工具均独立构建带有 `tool_call_id` 与 `arguments_hash` 的 `ApprovalRequest`，并维持中断循环直到所有待决高危操作全部决断。

### 1.2 执行器独立门禁防御（Fail-Closed 闭环）
- **证据文件**：[`src/jarvis_studio/agent/nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L35-L60) 与 [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L36-L233)
- **分析**：执行器在真正触发 `tool.invoke` 或 `tool.ainvoke` 之前，强制调用统一鉴权中心 `verify_tool_execution_authorization`。任何未明确获得 `approve` 决策的高危调用均被默认拒绝，并在执行器内部合成符合协议规范的 `ToolMessage(content="Operation cancelled...", tool_call_id=call_id)`，杜绝因审批节点故障绕过而引发的静默越权。

### 1.3 参数修改严格隔离与 Schema 校验
- **证据文件**：[`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L177-L212)
- **分析**：修改后的参数仅在对应 `tool_call_id` 鉴权时生效，绝不影响同轮次的其他调用。同时强制调用 `tool.args_schema.model_validate(modified_args)`，参数格式或字段不合法时直接阻断并拒绝执行。

### 1.4 路径穿越与逃逸静态向量全数封死
- **证据文件**：[`src/jarvis_studio/tools/builtin/file_tools.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/file_tools.py#L12-L127)
- **分析**：`resolve_safe_path` 替换了原脆弱的路径拼接。通过 `candidate.is_relative_to(ws)` 与 `os.path.realpath(candidate).is_relative_to(ws)`，全面阻断了：
  - `../../` 跨级目录穿越。
  - 绝对路径直穿（如 `/etc/passwd`、`C:\Windows`）。
  - Windows 跨盘符跳跃（如工作区在 D:，请求访问 C: 或 E:）。
  - Windows UNC 共享路径（`\\` 与 `//`）。
  - 静态外部符号链接与递归循环符号链接。
  - 特殊字符注入（空字节 `\x00` 与 NTFS ADS 冒号流）。

---

## 二、B. 仍然存在的安全风险 (Residual Risks)

以下是在逆向审计中发现的代码级真实隐患，按严重等级排序：

### 2.1 【Medium-High】授权记录无“已消费（Consumed）”状态，同 Session 存在授权重用漏洞
- **位置**：[`src/jarvis_studio/agent/hitl/authorization.py:107-120`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L107-L120) 及 [`src/jarvis_studio/agent/hitl/manager.py:14-21`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L14-L21)
- **代码事实**：
  ```python
  # authorization.py:107-120
  if not decision_data:
      run_context = state.get("run_context", {})
      thread_id = run_context.get("session_id")
      req = hitl.get_by_tool_call_id(tool_call_id, thread_id)
      if req and req.status != ApprovalStatus.PENDING:
          decision_data = {
              "decision": req.decision or req.status.value.lower(),
              ...
          }
  ```
  以及 `ApprovalStatus` 枚举仅有：
  ```python
  class ApprovalStatus(str, Enum):
      PENDING = "PENDING"
      APPROVED = "APPROVED"
      REJECTED = "REJECTED"
      MODIFIED = "MODIFIED"
  ```
- **危害分析**：
  当一个审批请求被用户批准后，其状态变为 `APPROVED` 并永远驻留在 `HITLManager._requests` 中。在当前轮次执行完毕后，系统将 `state["approval_decision"]` 重置为 `None`，但**并没有将 `HITLManager` 中的该条记录标记为已失效/已消费**。
  如果后续轮次（Turn 2 或 Turn 3）中，模型由于某些重试策略或确定的 mock ID 重新生成了相同的 `tool_call_id`，`authorization.py` 的回退逻辑将命中历史已批准记录，**无需用户再次确认直接予以放行**！
- **修复建议**：
  在 `ApprovalStatus` 中增加 `CONSUMED` 状态。在 `executor.py` 成功执行工具后，显式将对应请求状态推进至 `CONSUMED`；或在一次 StateGraph 运转步进结束后清除该请求的有效性。

---

### 2.2 【Medium】跨 Session/Thread 参数哈希检索丢失 `thread_id` 过滤
- **位置**：[`src/jarvis_studio/agent/hitl/authorization.py:140-143`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L140-L143)
- **代码事实**：
  ```python
  # authorization.py:140-143
  if not expected_hash:
      req = hitl.get_by_tool_call_id(tool_call_id)  # <--- 注意：未传入 thread_id！
      if req:
          expected_hash = req.arguments_hash
  ```
  对比 `manager.py:106-114` 的函数签名：
  ```python
  def get_by_tool_call_id(self, tool_call_id: str, thread_id: str | None = None) -> ApprovalRequest | None:
  ```
- **危害分析**：
  当 `thread_id` 缺省为 `None` 时，`get_by_tool_call_id` 会全局扫描所有会话的审批记录。
  如果不同会话（Thread A 和 Thread B）偶然具有相同的 `tool_call_id`（如某些 LLM 格式输出 `call_0`），Thread B 在尝试校验参数哈希时，可能错误获取到 Thread A 的 `arguments_hash`，从而引发跨会话授权错配或校验异常。
- **修复建议**：
  修改为 `req = hitl.get_by_tool_call_id(tool_call_id, thread_id=thread_id)`，严格限定在当前会话作用域内。

---

### 2.3 【Medium】`HITLManager` 纯内存驻留与状态孤岛风险
- **位置**：[`src/jarvis_studio/agent/hitl/manager.py:53-54, 122-130`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L53-L54)
- **代码事实**：
  ```python
  class HITLManager:
      def __init__(self) -> None:
          self._requests: dict[str, ApprovalRequest] = {}
  ```
- **危害分析**：
  虽然 LangGraph 状态通过 `SqliteSaver` 实现了持久化，但 `HITLManager` 是纯 Python 进程内存单例。
  1. 如果 Uvicorn 在等待用户审批期间重启，内存中的 `_requests` 全部丢失，用户调用 `POST /approvals/{approval_id}/decision` 时将直接返回 404 Not Found，而底层的 LangGraph 线程将永久卡在中断状态。
  2. 若后端启用多进程 Worker（如 `uvicorn --workers 4`），Worker A 生成的中断在 Worker B 上完全不可见。
- **修复建议**：
  在 Sprint 1B（SQLite WAL 与持久化）中，将 `ApprovalRequest` 映射为 SQLite 表结构存储，使审批记录与 Checkpoint 拥有同等级的生命周期。

---

### 2.4 【Medium】符号链接并发替换竞态（TOCTOU）的物理局限
- **位置**：[`src/jarvis_studio/tools/builtin/file_tools.py:153-160`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/file_tools.py#L153-L160)
- **代码事实**：
  ```python
  path = resolve_safe_path(file_path)
  ...
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content, encoding="utf-8")
  ```
- **危害分析**：
  `resolve_safe_path` 检查的是调用瞬间的路径状态；随后 `path.write_text` 通过 Python 内建 `open(str(path), 'w')` 打开文件。
  如果宿主机存在受损的本地进程，在该极小时间差内，将原本安全的目标文件替换为指向系统关键文件的符号链接，`write_text` 会顺着符号链接穿透写入。
- **概念澄清**：
  - **Level 1 (普通路径校验)**：静态 `../../` 过滤。（当前具备）
  - **Level 2 (符号链接检查)**：静态解析目标真实路径。（当前具备）
  - **Level 3 (抵御并发替换的安全打开机制)**：利用 `O_NOFOLLOW` / `openat2(RESOLVE_BENEATH)` 或 Windows `FILE_FLAG_OPEN_REPARSE_POINT` 进行文件描述符级原子打开。（当前**不具备**）
  - **Level 4 (真正的执行隔离)**：OS 级容器/沙箱虚拟化隔离。（当前**不具备**）
  结论：当前实现能抵御 LLM 生成的越权路径，但**无法抵御操作系统层面的并发竞态攻击**。

---

## 三、C. 潜在但尚未验证的问题

1. **Shell 工具无语法语义 AST 分析**：
   `execute_shell_command` 依赖人类审批，但未对 Shell 命令本身做管道逃逸、Base64 混淆或别名展开的静态分析。
2. **API 接口无并发锁控制**：
   `POST /approvals/{approval_id}/decision` 并发发起时，`HITLManager.resolve_request` 缺少 `asyncio.Lock` 保护，极端高频并发可能触发状态竞争。
3. **SHA-256 哈希截断为 16 位字符**：
   `compute_arguments_hash` 使用了 `[:16]`（64-bit 熵值）。虽然对于会话级防误判已足够，但密码学意义上完整 64 字符（256-bit）更具碰撞抵御力。

---

## 四、D. 测试质量评估

| 审计维度 | 评级 | 审计发现 |
| :--- | :---: | :--- |
| **中断与恢复真实验证** | **优 (High)** | 6 个集成测试真实驱动了 StateGraph，从 `START -> assistant -> approval_gate` 拦截，并通过 `Command(resume=...)` 恢复流转至 `tools`。 |
| **工具执行副作用验证** | **优 (High)** | 测试不仅验证了返回值，且实际验证了 Shell echo 输出与物理文件内容，确认执行与阻断均产生真实物理效果。 |
| **Checkpointer 生产贴近度** | **中 (Medium)** | 当前所有 HITL 安全测试均使用 `MemorySaver()`，未在生产级的 `SqliteSaver` 上检验二进制反序列化与多事务锁行为。 |
| **跨会话边界覆盖** | **良 (Low-Med)** | 虽测试了同会话内的不同 ID 错配（`call_authorized_AAA` vs `call_victim_BBB`），但未编写跨 `thread_id` 争用相同调用 ID 的对抗测试。 |
| **Windows 符号链接测试真实度**| **中 (Medium)** | 由于 Windows 默认受限用户权限无法直接创建符号链接，测试用例降级使用了 `unittest.mock.patch("os.path.realpath")`，未在原生 Windows NTFS 上完成非 Mock 的实地验证。 |

---

## 五、E. 是否建议合并 (Final Verdict)

### 结论：`APPROVED WITH CONDITIONS` (满足指定条件后建议合并)

#### 决策依据：
Sprint 1A 严格受限于安全正确性边界，成功将前一阶段审计出的两大致命漏洞（多工具越权放行、文件路径绝对逃逸）彻底降服，代码架构整洁，且保持了原有 43 项测试的零回归破坏，综合质量达到较高工程水准。

#### 建议合并的前提条件（可在后续 Sprint 1B 中立即消化）：
1. **立即修复一行代码隐患**：将 [`src/jarvis_studio/agent/hitl/authorization.py:141`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L141) 中的 `hitl.get_by_tool_call_id(tool_call_id)` 显式补充 `thread_id` 参数。
2. **在 Sprint 1B 规划中立项“授权生命周期消费”**：在 SQLite 持久化改造时，为审批实体加入 `CONSUMED` 状态，防止同一个线程内的过期授权被重放。
3. **安全边界诚实告知**：在系统安全规范文档中明确标注文档系统防护目前达到 Level 2（应用层静态包含），非 Docker/OS 级 Level 4 沙箱。

本轮审查工作至此全部结束，不修改任何业务代码，等待负责人进一步统筹指令。
