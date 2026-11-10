# Jarvis Studio — Sprint 1A.1 Independent Security Review

> **角色**：Independent Principal Security Engineer  
> **审计对象**：Jarvis Studio V2 — Sprint 1A.1 Authorization Security Patch (`feature/v2-sprint-1a-security`)  
> **审查性质**：第二轮独立反向安全审计（只读审查，严禁修改任何业务代码、测试代码与配置文件）  
> **基准代码**：Commit `2c8b192` (`HEAD -> feature/v2-sprint-1a-security`)  
> **验证基线**：全量测试 73/73 PASS，Ruff 静态检查专项通过  
> **日期**：2026-11-10  

---

## 一、最终审查结论 (Executive Verdict)

### 结论：`APPROVED WITH CONDITIONS` (条件通过，必须修复 2 个阻断性隐患后方可合入主干)

#### 综合评价：
Sprint 1A.1 安全补丁精准聚焦并修复了第一轮审查指出的两大核心缺陷：
1. **成功引入不可逆状态机**：新增 `EXECUTING`、`CONSUMED`、`FAILED` 状态，实现了单次授权执行后的消费闭环与并发租约互斥，阻断了已决断授权的无限期重放。
2. **系统化补齐 Thread 隔离**：在授权查询、决断处理、执行门禁与租约获取全链路强制注入 `thread_id`，杜绝了跨 Session 授权错配。
3. **工程质量与回归防护**：新增 10 项专项单元/集成测试，现有 73 项全量测试 100% 绿灯（73/73 PASS in 12.09s），且补丁严格限制在目标模块，无架构膨胀。

#### 条件限制（阻断性发现）：
尽管架构演进方向完全正确，逆向对抗审查在深水区捕获了 2 个高危与 2 个中危实现隐患：
- **【高危】`asyncio.CancelledError` 穿透导致 `EXECUTING` 租约永久挂起**（`executor.py:89` 使用 `except Exception`，在 Python 3.12 中无法捕获继承自 `BaseException` 的协程取消异常，导致 `finalize_execution` 被彻底跳过）。
- **【高危】无 `appr_id` 状态注入时租约获取被静默跳过**（`authorization.py:253` 中 `if acquire_lease and appr_id:` 缺少 Fail-Closed 兜底，当纯注入决策缺少 `appr_id` 时直接放行并不受租约控制）。
- **【中危】`finalize_execution(success=True)` 位于工具执行 `try` 块内**，状态决断自身抛出异常时会篡改已成功执行的工具输出为错误提示，并导致物理执行成功但业务状态被标记为 `FAILED`。
- **【中危】`manager.py` 的 `get_request` 与 `resolve_request` 的 `thread_id` 参数仍然为可选 (`None`)**，底层防御未达到编译期/调用层 100% 强制约束。

---

## 二、授权状态转换完整性审查 (State Machine Integrity)

### 2.1 状态转移矩阵 (State Transition Matrix)

系统当前定义了 7 种状态：`PENDING`、`APPROVED`、`REJECTED`、`MODIFIED`、`EXECUTING`、`CONSUMED`、`FAILED`。

| 当前状态 \ 目标状态 | PENDING | APPROVED | REJECTED | MODIFIED | EXECUTING | CONSUMED | FAILED |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **初始 (None)** | **允许**<br>(create_request) | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |
| **PENDING** | - | **允许**<br>(resolve: approve) | **允许**<br>(resolve: reject) | **允许**<br>(resolve: modify) | 禁止<br>(需先经决策) | 禁止 | 禁止 |
| **APPROVED** | 禁止 | 禁止 | 禁止 | 禁止 | **允许**<br>(acquire_lease) | 禁止 | 禁止 |
| **MODIFIED** | 禁止 | 禁止 | 禁止 | 禁止 | **允许**<br>(acquire_lease) | 禁止 | 禁止 |
| **EXECUTING** | 禁止 | 禁止 | 禁止 | 禁止 | 禁止<br>(并发被互斥) | **允许**<br>(finalize: True) | **允许**<br>(finalize: False) |
| **CONSUMED** | 禁止 | 禁止 | 禁止 | 禁止 | 禁止<br>(防重放阻断) | - | 禁止 |
| **FAILED** | 禁止 | 禁止 | 禁止 | 禁止 | 禁止<br>(异常阻断重放) | 禁止 | - |
| **REJECTED** | 禁止 | 禁止 | - | 禁止 | 禁止<br>(拒绝不可执行) | 禁止 | 禁止 |

### 2.2 专项合规检查细则

1. **`CONSUMED` 能否被重新批准？**  
   **严格阻断**。[`src/jarvis_studio/agent/hitl/manager.py:171-174`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L171-L174) 校验 `if req.status != ApprovalStatus.PENDING:`，任何非 PENDING 请求均抛出 `ValueError`。在 [`src/jarvis_studio/agent/nodes/approval.py:43`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L43) 中亦显式过滤 `if target_req.status != ApprovalStatus.PENDING: return`。
2. **`FAILED` 能否重新获取执行租约？**  
   **严格阻断**。[`src/jarvis_studio/agent/hitl/manager.py:242, 255-260`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L242-L260) 限制仅 `APPROVED` 与 `MODIFIED` 可获取租约，`FAILED` 请求直接命中错误分支并返回 `False, None, "... previously FAILED (replay blocked)"`。
3. **`EXECUTING` 能否被重复获取？**  
   **严格阻断**。在互斥锁保护下，第二个并发调用者检查到 `status == EXECUTING`，命中 [`manager.py:244-249`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L244-L249)，返回 `False, None, "... is already EXECUTING (concurrent execution blocked)"`。
4. **`REJECTED` 能否进入执行流程？**  
   **双重阻断**。首先在 [`authorization.py:194-205`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L194-L205) 中直接返回 `is_authorized=False, status="unauthorized_rejected"`；其次若绕过进入租约获取，在 [`manager.py:261-266`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L261-L266) 再次校验拦截。
5. **参数修改能否重新激活已消费授权？**  
   **严格阻断**。`resolve_request` 强制要求当前状态必须为 `PENDING`，已 `CONSUMED` 请求无法通过传入 `decision="modify"` 重置状态。测试用例 `test_06` 证明重新决断抛出异常。
6. **重复决断是否幂等且安全？**  
   **安全且防篡改**。`manager.py` 严格抛出 `ValueError`，`approval.py` 自动忽略已完成请求，防止同轮次多次重复决断互相踩踏。
7. **是否有任何未覆盖的状态转换路径？**  
   - 当 `finalize_execution` 对非 `EXECUTING` 状态调用时，[`manager.py:301`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L301) 采取静默忽略策略（`if req.status == ApprovalStatus.EXECUTING:`），不破坏当前状态，但缺少告警日志。

---

## 三、并发安全审查 (Concurrency Safety Review)

### 3.1 临界区与原子性分析

审查对象：[`src/jarvis_studio/agent/hitl/manager.py:200-283`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L200-L283) 中的 `acquire_execution_lease`。

```python
with self._lock:
    req = self._requests.get(approval_id)
    # 1. 查找授权与存在性检查
    if not req: return False, None, ...
    # 2. 身份、工具、参数哈希强校验
    if req.thread_id != thread_id: return False, None, ...
    if req.tool_call_id != tool_call_id: return False, None, ...
    if req.arguments_hash != arguments_hash: return False, None, ...
    # 3. 状态合法性校验
    if req.status not in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED):
        return False, None, ...
    # 4. 原子跃迁至 EXECUTING
    req.status = ApprovalStatus.EXECUTING
    req.executed_at = time.time()
    return True, req, None
```

- **临界区完整性**：查找、校验身份、比对哈希、检查前置状态、状态跃迁全部在 `self._lock` 保护范围内完成。
- **TOCTOU（Time-of-Check to Time-of-Use）消除**：检查与修改处于同一锁作用域，两个线程不可能在“检查通过后、状态变更前”发生交错。
- **多线程互斥**：在真实的 Python 多线程环境中，两个并发线程争抢相同授权，精确保证仅有一个线程获取成功，第二个线程必失败。
- **协程/事件循环安全性**：`acquire_execution_lease` 为同步方法且不包含任何 `await` 让渡点，在单线程 asyncio 事件循环中，其执行过程具备不可分割的原子性。
- **死锁风险**：锁内无任何 I/O 操作、无复杂依赖调用、无嵌套锁争用，不存在死锁可能。

### 3.2 并发维度的代码级盲区（Critical Finding）

- **盲区位置**：[`src/jarvis_studio/agent/hitl/authorization.py:253-271`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L253-L271)
- **代码事实**：
  ```python
  if not appr_id:
      req = hitl.get_by_tool_call_id(tool_call_id, thread_id=thread_id)
      if req:
          appr_id = req.id

  if acquire_lease and appr_id:
      success, _leased_req, err = hitl.acquire_execution_lease(...)
      ...
  ```
- **漏洞机理**：
  如果传入的 `AgentState` 中包含外部直接注入或伪造的 `approval_decision`（例如仅包含 `{"decision": "approve", "arguments_hash": "..."}` 而无 `approval_id`），且在 `HITLManager` 内存中找不到对应 `ApprovalRequest` 时：
  - `appr_id` 保持为 `None`。
  - `if acquire_lease and appr_id:` 判定为 **False**！
  - **租约获取被完全跳过**！
  - 函数直接返回 `is_authorized=True, status="authorized_approved", approval_id=None`。
  - 后续 `executor.py` 成功执行工具，且由于 `auth.approval_id is None`，**永远不会调用 `finalize_execution`**。
- **安全评级**：**High**。必须实现 Fail-Closed：若工具需要审批且要求获取租约（`acquire_lease=True`），但未能解析出有效的 `appr_id`，必须阻断执行，返回 `unauthorized_unapproved`。

---

## 四、Executor 异常与取消安全审查 (Exception & Cancellation Safety)

这是本次审计最关键的核心部分。

审查对象：[`src/jarvis_studio/agent/nodes/executor.py:80-96`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L80-L96)。

```python
with timed_step(f"tool_exec_{tool_name}", tool_name=tool_name):
    try:
        if hasattr(tool, "ainvoke"):
            result = await tool.ainvoke(effective_args)
        else:
            result = tool.invoke(effective_args)
        result_str = str(result)
        if auth.approval_id:
            hitl_mgr.finalize_execution(auth.approval_id, success=True)
    except Exception as exc:  # noqa: BLE001
        if auth.approval_id:
            hitl_mgr.finalize_execution(
                auth.approval_id, success=False, error=str(exc)
            )
        logger.error(f"Error executing tool '{tool_name}': {exc}")
        result_str = f"Error executing '{tool_name}': {exc}"
```

### 4.1 四种运行态故障行为深度解构

| 故障类型 | 发生场景 | 当前代码响应 | 状态机实际终态 | 安全与可用性影响 |
| :--- | :--- | :--- | :---: | :--- |
| **1. 工具失败 (Tool Failure)** | 工具内部抛出 `RuntimeError`、`ValueError`、Shell 非零退出 | 被 `except Exception` 捕获，调用 `finalize_execution(..., success=False)` | **FAILED** | **安全闭环**。租约终结并记录错误，后续重放被彻底阻断，Agent 获取包含错误信息的 `ToolMessage`。 |
| **2. 任务取消 (Task Cancellation)** | 客户端主动断开 SSE 连接、网关超时触发 `asyncio.wait_for` 超时、协程被取消 | **未被捕获！**<br>`CancelledError` 直接向外抛出 | **永久挂起在 EXECUTING** | **高危缺陷**。在 Python 3.12 中，`asyncio.CancelledError` 继承自 `BaseException`，`except Exception` 无法捕获！`finalize_execution` 从未被执行，状态永久悬空在 `EXECUTING`。下次复用该请求时被报“already EXECUTING”。 |
| **3. 进程崩溃 (Process Kill)** | OS 级 `SIGKILL`、任务管理器强制结束、OOM Killer 杀死 | 进程内存被操作系统直接释放 | **内存消失**<br>(下轮重启为 None) | **Fail-Closed 闭环，但任务中断**。重启后 `HITLManager` 内存清空，由于缺少 `appr_id`，校验直接判定未授权并拒绝执行。 |
| **4. 服务崩溃 (Service Crash)** | 宿主异常退出或普通重启 | 同上 | **内存消失** | 同上。归属于 Sprint 1C 的 SQLite 持久化改造范围。 |

### 4.2 `finalize_execution(success=True)` 放置位置隐患

- **位置**：`executor.py:88`
- **问题**：`hitl_mgr.finalize_execution(auth.approval_id, success=True)` 被置于 `try` 块内工具执行语句之后。
- **反向对抗推演**：
  若工具执行（`tool.ainvoke`）已产生真实的物理副作用（如创建了外部文件、执行了 Shell 脚本），但随后的 `finalize_execution` 因极端原因抛出异常（或未来引入数据库写锁超时）：
  1. 外层 `except Exception as exc:` 捕获该异常；
  2. 错误地再次调用 `finalize_execution(auth.approval_id, success=False, error=str(exc))`，将本已物理执行成功的操作标记为 `FAILED`；
  3. `result_str` 被重写为 `Error executing '{tool_name}': ...`，向 Agent 和前端掩盖了真实的物理执行事实。
- **修复方案**：将工具执行的 `try/except` 与授权状态终结解耦，或使用两段式确认。

---

## 五、Thread 隔离生命周期审查 (Thread Isolation Audit)

检查整个生命周期涉及的 7 个核心入口：

| 接口名称 | 源码位置 | `thread_id` 约束机制 | 隔离强度 | 审计判定 |
| :--- | :--- | :--- | :---: | :---: |
| **1. `create_request`** | [`manager.py:81-110`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L81-L110) | 必选参数，强制写入 `req.thread_id` | **强** | **合规** |
| **2. `get_request`** | [`manager.py:112-120`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L112-L120) | 可选参数 `thread_id: str \| None = None`，提供时校验 `req.thread_id != thread_id` | **中** | **存遗漏风险** (见下文) |
| **3. `get_by_tool_call_id`** | [`manager.py:121-137`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L121-L137) | `if not thread_id: return None`，强制禁止无会话全局扫描 | **强** | **合规 (已彻底修复)** |
| **4. `list_pending`** | [`manager.py:138-147`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L138-L147) | 可选参数；当传入时严格按 `r.thread_id == thread_id` 过滤 | **良** | **合规 (满足管理接口需求)** |
| **5. `resolve_request`** | [`manager.py:148-175`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L148-L175) | 可选参数；不匹配时抛出 `PermissionError` | **中** | **存遗漏风险** (见下文) |
| **6. `acquire_execution_lease`** | [`manager.py:200-227`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L200-L227) | 必选参数；`req.thread_id != thread_id` 拒绝租约 | **强** | **合规** |
| **7. `finalize_execution`** | [`manager.py:284-312`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L284-L312) | 仅传 `approval_id`；因该 ID 为高熵且租约已在前置校验，无越权风险 | **良** | **合规** |

### 5.1 跨模块 Thread 隔离隐患发现

1. **API 层 `submit_approval_decision` 遗漏 Thread 传递**：  
   在 [`src/jarvis_studio/api/routes/approvals.py:34`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py#L34)：
   ```python
   req = hitl_mgr.get_request(approval_id)  # <--- 未传入 payload.thread_id！
   ```
   尽管 `payload.thread_id` 已在请求体中，但 API 预校验未传递该参数，导致客户端可用合法的 `approval_id` 探针跨会话探测其他会话的审批请求是否存在。
2. **`approval.py` 与 `authorization.py` 的缺省策略不一致**：  
   - [`approval.py:126`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L126)：`thread_id = run_context.get("session_id", "default")`（静默退化为 `"default"`）。
   - [`authorization.py:63`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L63)：`if not thread_id: return unauthorized_missing_thread`（严格 Fail-Closed）。  
   建议将 `approval.py` 同步改造为无 `session_id` 时直接告警并阻断，杜绝 `"default"` 占位会话。

---

## 六、测试质量评价 (Test Suite Quality Evaluation)

审查针对 [`tests/test_authorization_patch.py`](file:///d:/myProject/JarvisStudio/tests/test_authorization_patch.py) 与 [`tests/test_hitl_security.py`](file:///d:/myProject/JarvisStudio/tests/test_hitl_security.py)：

### 6.1 优点
1. **真实执行器驱动**：`test_01` 至 `test_05`、`test_07` 均调用了 `create_tool_executor_node` 真实驱动了工具（包括真实的 Shell echo、真实的 `exploding_tool` 异常抛出），而非简单 mock。
2. **会话边界对抗**：`test_07` 真实构造了 Thread A 与 Thread B 拥有相同 `tool_call_id` 的场景，确证 Thread B 无法窃取或消耗 Thread A 的授权。
3. **全局状态隔离良好**：测试夹具均设置了 `clean_hitl_state`（`autouse=True`），每个测试前后强制 `mgr.clear()`，无残留测试污染。

### 6.2 严重不足与盲区
1. **并发争抢测试未涉及多线程**：  
   `test_03` 采用 `asyncio.gather(executor(state), executor(state))`，由于 `verify_tool_execution_authorization` 为纯同步调用，协程 1 在让渡前即已同步完成状态设置，未能有效模拟真实多线程（多 OS 线程）争抢 `self._lock` 的物理场景。
2. **任务取消（CancelledError）测试覆盖率为 0%**：  
   未设计任何模拟 `asyncio.CancelledError` 发生时的测试用例，导致前述第 4.1 节中的严重缺陷在现有 73 项测试中完全未被暴露。
3. **`finalize_execution` 失败测试覆盖率为 0%**：  
   未测试终结函数故障时的异常传播行为。
4. **全图持久化未纳入**：  
   测试均直接调用 `executor(state)`，未配合 `SqliteSaver` 测试中途崩溃重启后的持久化状态。

---

## 七、Git Diff 与工程规范审查 (Git Diff & Engineering Quality)

1. **改动范围控制**：  
   Commit `2c8b192` 仅触及 `agent/hitl/*`、`agent/nodes/approval.py`、`agent/nodes/executor.py` 与两个测试文件及文档，严格限定在 Sprint 1A.1 补丁要求范围内，无越界引入新框架或重构。
2. **无残留调试痕迹**：  
   源码中无残留 `print`、`breakpoint()`、临时注释代码或调试文件。
3. **自动化测试执行结果**：  
   - 运行环境：Python 3.12.14 / pytest-9.1.1。
   - 结果：**73 passed in 12.09s**（100% 通过）。
4. **代码风格与 Lint 检查**：  
   - 补丁模块 `src/jarvis_studio/agent/hitl`、`src/jarvis_studio/agent/nodes` 以及两组安全测试运行 `ruff check`，报告：**`All checks passed!`**。

---

## 八、已知残余风险 (Residual Risks)

以下风险不属于本次授权状态补丁引入，但需在后续 Sprint 中按计划处置：
1. **纯内存单例持久化限制 (排期: Sprint 1C)**：  
   `HITLManager` 仍为内存驻留，多 Worker 部署或进程重启审批状态丢失问题，已规划在 Sprint 1C 的 SQLite 持久化改造中处理。
2. **NTFS / Linux 文件并发替换 (TOCTOU) (长期演进)**：  
   文件系统路径安全仍处于 Level 2（静态路径包含），未实现文件描述符级的原子打开防御（Level 3）。

---

## 九、必须修复的问题及其精确代码位置 (Actionable Fixes)

在允许合并到主干前，实施团队**必须**修复以下两项问题（预计改动不超过 15 行）：

### 9.1 【必须修复 1】补齐 `asyncio.CancelledError` 终结处理
- **文件位置**：[`src/jarvis_studio/agent/nodes/executor.py:80-96`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L80-L96)
- **缺陷代码**：
  ```python
  try:
      ...
  except Exception as exc:  # noqa: BLE001
      if auth.approval_id:
          hitl_mgr.finalize_execution(auth.approval_id, success=False, error=str(exc))
  ```
- **修复方案**：
  必须在 `try...finally` 块中确保终结，或者显式捕获 `asyncio.CancelledError`：
  ```python
  except (Exception, asyncio.CancelledError) as exc:
      if auth.approval_id:
          hitl_mgr.finalize_execution(
              auth.approval_id, success=False, error=str(exc) or "Cancelled"
          )
      if isinstance(exc, asyncio.CancelledError):
          raise
      ...
  ```

### 9.2 【必须修复 2】无 `appr_id` 状态注入时实行 Fail-Closed 阻断
- **文件位置**：[`src/jarvis_studio/agent/hitl/authorization.py:253-271`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L253-L271)
- **缺陷代码**：
  ```python
  if acquire_lease and appr_id:
      success, _leased_req, err = hitl.acquire_execution_lease(...)
  ```
- **修复方案**：
  若 `acquire_lease` 为 `True` 但 `appr_id` 不存在，严禁默许放行：
  ```python
  if acquire_lease:
      if not appr_id:
          return AuthorizationResult(
              is_authorized=False,
              effective_args=effective_args,
              status="unauthorized_unapproved",
              rejection_reason="Execution blocked: No active approval lease record found to acquire.",
          )
      success, _leased_req, err = hitl.acquire_execution_lease(...)
  ```

---

## 十、是否允许合并 Sprint 1A (Merge Recommendation)

### 判定结论：**`APPROVED WITH CONDITIONS` (条件通过)**

1. **当前分支状态**：`feature/v2-sprint-1a-security` 基础架构设计优良，已从根本上消除了 Sprint 1A 最初的多工具越权放行与路径逃逸两大致命漏洞。
2. **合并准入条件**：
   - 修复第 9.1 节：`executor.py` 补充 `asyncio.CancelledError` 异常终结保证；
   - 修复第 9.2 节：`authorization.py` 消除无 `appr_id` 绕过租约获取的漏洞；
   - 新增针对上述两项修复的自动化测试用例，确保全量测试通过。
3. **完成上述修复后，批准将 `feature/v2-sprint-1a-security` 分支合入 `main`。**
4. 合入完成后，Jarvis Studio V2 可正式开启 Sprint 1B。

---
*独立安全审查报告完毕。当前状态保持只读，未修改任何代码。*