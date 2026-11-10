# Jarvis Studio — Sprint 1A.1 Authorization Security Patch Report

> **首席安全架构师与核心工程师**  
> **补丁分支**：`feature/v2-sprint-1a-security`  
> **补丁性质**：安全缺陷专项补丁（One-Time Consumption & Thread Isolation）  
> **测试状态**：73/73 PASS（全部通过，覆盖 10 项新增补丁测试）  
> **静态合规**：Ruff All checks passed!  
> **最终结论**：**`PATCH COMPLETE`**  
> **日期**：2026-11-10  

---

## 一、漏洞根因分析

在 Sprint 1A 独立安全审查中，发现了两个隐藏较深的授权管理缺陷：

### 1.1 授权缺少一次性消费机制（Authorization Replay & Double-Execution）
- **根因**：原 `ApprovalStatus` 仅定义了 `PENDING`, `APPROVED`, `REJECTED`, `MODIFIED` 四种状态。在工具执行完成之后，`ApprovalRequest` 依然永久保留在 `APPROVED` 状态，未作失效标记。
- **危害**：
  1. **历史授权重用**：在后续轮次（Turn 2+）中，若 Agent 产生相同 `tool_call_id` 的调用，回退逻辑会命中历史已决断记录并静默直接放行。
  2. **并发竞争执行**：若两个并发协程/任务使用相同的已批准 `tool_call_id` 触发执行器，两者均可通过授权校验，导致高危工具被重复并发执行两次。

### 1.2 授权查询缺少 `thread_id` 强制约束（Cross-Session Authorization Bleed）
- **根因**：
  1. `authorization.py` 第 141 行在调用 `hitl.get_by_tool_call_id(tool_call_id)` 时遗漏了 `thread_id` 参数。
  2. `manager.py` 的 `get_by_tool_call_id` 实现包含 `thread_id is None or ...` 的宽松逻辑，当调用方未传 `thread_id` 时退化为跨会话全局逆向搜索。
  3. `state["run_context"]["session_id"]` 缺失时未实行 Fail-Closed 阻断。
- **危害**：不同用户/会话（Thread A 与 Thread B）若存在相同的 `tool_call_id`，Thread B 可能越界复用 Thread A 的授权或参数哈希。

---

## 二、修复方案与状态机设计

### 2.1 强化状态机生命周期

我们在 `ApprovalStatus` 枚举中扩充了 `EXECUTING`, `CONSUMED`, `FAILED` 状态，建立了严格不可逆的单向流转链：

```
                    ┌──────────────┐
                    │   PENDING    │
                    └──────┬───────┘
           User Reject     │      User Approve / Modify
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │  REJECTED   │   │  APPROVED   │   │  MODIFIED   │
  └─────────────┘   └──────┬──────┘   └──────┬──────┘
       (End)               │                 │
                           └────────┬────────┘
                                    │ Atomic acquire_execution_lease()
                                    ▼
                            ┌───────────────┐
                            │   EXECUTING   │
                            └───────┬───────┘
                       Tool Success │ Tool Exception / Failure
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
             ┌─────────────┐                 ┌─────────────┐
             │  CONSUMED   │                 │   FAILED    │
             └─────────────┘                 └─────────────┘
          (Expended, No Replay)           (Terminated, Fail-Closed)
```

- **PENDING**：等待人类审批。
- **APPROVED / MODIFIED**：用户已决策授权，具备被执行资格。
- **EXECUTING**：已被执行器原子获取执行租约（In-Flight），**其他并发调用立即被阻断**。
- **CONSUMED**：工具执行成功完成，**租约永久消耗作废，不可再次执行或修改**。
- **FAILED**：工具执行抛出异常或工具未识别，记录 `execution_error` 并终结，**不可直接重用**。

---

## 三、并发授权获取与线程安全机制

为杜绝并发两个 Executor 同时通过校验，我们在 `HITLManager` 内部引入了互斥锁与原子状态跃迁：

1. **互斥锁保护**：
   在 [`src/jarvis_studio/agent/hitl/manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py) 中增加 `self._lock = threading.Lock()`，保护所有对 `self._requests` 的读写、查询与状态流转。
2. **原子租约获取方法 `acquire_execution_lease`**：
   ```python
   def acquire_execution_lease(
       self,
       approval_id: str,
       thread_id: str,
       tool_call_id: str,
       arguments_hash: str,
   ) -> tuple[bool, ApprovalRequest | None, str | None]:
   ```
   - 必须满足 `req.thread_id == thread_id`、`req.tool_call_id == tool_call_id`、`req.arguments_hash == arguments_hash`。
   - 必须处于 `APPROVED` 或 `MODIFIED` 状态。
   - 在锁内**原子跃迁至 `EXECUTING`**。
   - 若已被并发占有（`status == EXECUTING`）或已消费（`status == CONSUMED`），立即失败并返回明确阻断原因。
3. **执行闭环终结 `finalize_execution`**：
   在 [`src/jarvis_studio/agent/nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py) 执行完工具后：
   - 正常执行成功：原子流转至 `CONSUMED`。
   - 执行抛出异常：原子流转至 `FAILED` 并记录异常栈，绝不留置于 `EXECUTING` 挂起状态。

---

## 四、Thread 隔离严格实现

全面检查并加固授权生命周期的每一个环节：

1. **执行上下文强制要求 `thread_id`**：
   在 [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L60-L75)，若 `state["run_context"]["session_id"]` 缺失，执行器直接 Fail-Closed 阻断，返回状态 `unauthorized_missing_thread`。
2. **查询接口严禁全局退化**：
   在 [`src/jarvis_studio/agent/hitl/manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L110-L125) 中，`get_by_tool_call_id` 将 `thread_id` 设为必选；若缺少 `thread_id`，立即拒绝并返回 `None`，杜绝 cross-session 跨会话匹配。
3. **授权校验所有查询全传 `thread_id`**：
   - 修复第 141 行，改为 `hitl.get_by_tool_call_id(tool_call_id, thread_id=thread_id)`。
   - `hitl.get_request(appr_id, thread_id=thread_id)` 显式传入 `thread_id`。
4. **决策处理绑定 Thread**：
   `resolve_request` 与 `_apply_user_response` 均显式传入并校验 `thread_id`。非本 Thread 发起的决策无法 resolve 他人请求。

---

## 五、异常恢复策略

- **执行中异常 (Runtime Crash / Exception)**：
  若工具执行过程中发生异常（如网络断开、系统命令非零退出、权限错误）：
  - `executor.py` 捕获异常，调用 `hitl_mgr.finalize_execution(appr_id, success=False, error=str(exc))`。
  - 请求状态明确转为 `FAILED`，释放执行锁并终止，避免死锁或留在 `EXECUTING` 悬空。
  - 授权被关闭，**不允许自动重试绕过**。LLM 接收到标准 `ToolMessage` 报告错误，必须在下一轮根据错误信息发起新的调用并重新申请用户审批。
- **幂等保护**：
  已决断、已消费或已失败的请求，任何后续的 `resolve_request` 尝试均抛出 `ValueError`。

---

## 六、测试结果与覆盖度

新增专用测试集 [`tests/test_authorization_patch.py`](file:///d:/myProject/JarvisStudio/tests/test_authorization_patch.py)，采用严格 TDD（Red -> Green）模式开发。

### 6.1 新增 10 项安全补丁测试

| 序号 | 测试用例 | 验证目标 | 结果 |
| :---: | :--- | :--- | :---: |
| 1 | `test_01_normal_approval_executes_once_and_consumes` | 正常审批后成功执行一次，状态从 APPROVED 推进至 CONSUMED | **PASS** |
| 2 | `test_02_replay_consumed_authorization_is_blocked` | 重复使用已 CONSUMED 授权，第二次执行被严格拒绝 | **PASS** |
| 3 | `test_03_concurrent_executor_race_only_one_acquires` | 两个并发执行器争抢同一授权，精确保证仅一个获取执行权，另一个被阻断 | **PASS** |
| 4 | `test_04_two_different_tool_calls_have_independent_leases` | 同一轮内两个不同 ToolCall 拥有独立租约并各自独立消费 | **PASS** |
| 5 | `test_05_execution_exception_transitions_to_failed_and_blocks_replay` | 工具抛出异常后状态明确转为 FAILED，且后续重放被严格阻断 | **PASS** |
| 6 | `test_06_consumed_authorization_cannot_be_reactivated_via_modify` | 已消费授权禁止通过 modify 决策被重新激活或修改 | **PASS** |
| 7 | `test_07_different_threads_same_tool_call_id_strictly_isolated` | 两个不同 Thread 拥有相同 tool_call_id，Thread B 无法窃取 Thread A 的授权 | **PASS** |
| 8 | `test_08_same_thread_same_call_id_different_hash_blocked` | 相同 Thread 和 Call ID 下，篡改参数导致哈希不匹配时被阻断 | **PASS** |
| 9 | `test_09_missing_thread_id_fails_closed` | 上下文缺失 thread_id 时 Fail-Closed，拒绝执行高危工具 | **PASS** |
| 10 | `test_10_full_lifecycle_thread_binding` | 创建、查询、决断、租约获取、终结全生命周期严格绑定 Thread | **PASS** |

### 6.2 全量回归验证
- 运行命令：`.venv\Scripts\pytest`
- 测试总数：**73 项**（43 项基础测试 + 10 项路径安全测试 + 10 项 HITL 安全测试 + 10 项本次补丁测试）
- 结果：**73 passed in 8.76s (100% 通过，零回归，零破坏)**。
- Ruff 静态检查：**All checks passed!**

---

## 七、Git Diff 摘要

修改严格限制在授权核心链路内，无任何无关改动：
- [`src/jarvis_studio/agent/hitl/manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py)：引入 `EXECUTING/CONSUMED/FAILED` 状态、`_lock` 线程锁、`acquire_execution_lease` 与 `finalize_execution` 原子操作、强制 `thread_id` 过滤。
- [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py)：强化 `thread_id` 存在性校验、原子租约获取调用、`thread_id` 全链路传递。
- [`src/jarvis_studio/agent/nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py)：引入执行成功与失败后的 `finalize_execution` 闭环。
- [`src/jarvis_studio/agent/nodes/approval.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py)：传递 `thread_id` 至决断处理函数。
- [`tests/test_authorization_patch.py`](file:///d:/myProject/JarvisStudio/tests/test_authorization_patch.py)：新增 10 个专项安全测试。
- [`tests/test_hitl_security.py`](file:///d:/myProject/JarvisStudio/tests/test_hitl_security.py)：为纯单元测试用例补齐 `run_context` 中的 `session_id`。

---

## 八、已知残余风险（按既定规划消化）

1. **HITLManager 纯内存持久化（归属 Sprint 1C）**：
   - 当前 `HITLManager` 借助 `threading.Lock` 实现了单进程内高并发与原子性保护。但如独立审查所指出，多进程（Uvicorn 多 Worker）部署与服务崩溃恢复仍需依赖 SQLite 持久化表。该项工作已按产品路线图正式排入 **Sprint 1C**，不在本补丁范围内。
2. **操作系统级 TOCTOU 并发符号链接替换**：
   - 依赖文件系统 Level 3 (原子文件描述符) 及 Level 4 (Docker 沙箱) 的长期演进，不影响当前应用层授权与路径隔离。

---

## 九、结论

所有目标缺陷已通过原子状态机与严格 Thread 隔离彻底根治，全量 73 项测试完全绿灯。

**`PATCH COMPLETE`**
