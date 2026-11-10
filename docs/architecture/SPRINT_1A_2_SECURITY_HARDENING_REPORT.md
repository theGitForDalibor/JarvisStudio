# Jarvis Studio — Sprint 1A.2 Final Security Hardening Report

> **角色**：Security & Systems Implementation Engineer  
> **依据**：[`docs/architecture/SPRINT_1A_1_INDEPENDENT_REVIEW.md`](file:///d:/myProject/JarvisStudio/docs/architecture/SPRINT_1A_1_INDEPENDENT_REVIEW.md)  
> **当前分支**：`feature/v2-sprint-1a-security`  
> **最终状态**：**`PATCH COMPLETE`**  
> **测试结果**：**92/92 PASS**（含 Sprint 1A.2 新增 19 项安全与多线程竞争测试）  
> **日期**：2026-11-10  

---

## 一、修复概览与根因分析

针对第二轮独立审计指出的深水区阻断性隐患及纵深防御需求，Sprint 1A.2 严格采用 **TDD（先编写破坏性测试验证 Red -> 最小化精准修复 -> 验证全量 Green）** 流程，完成以下 5 项核心安全加固与真实多线程并发验证：

### Fix 1：禁止缺失 `approval_id` 时绕过执行租约

- **根因**：
  [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py) 原实现存在自动回退逻辑与非严格判空：`if not appr_id: req = hitl.get_by_tool_call_id(...)` 且 `if acquire_lease and appr_id:`。当调用方在 `AgentState["approval_decision"]` 中省略或传入空 `approval_id`、且内存中不存在记录（或绕过明确指定的 `approval_id`）时，系统会静默跳过 `acquire_execution_lease`，直接返回 `is_authorized=True`，导致高危工具绕过一次性租约消费机制。
- **修改位置**：
  - [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L171-L345)
  - [`src/jarvis_studio/agent/hitl/manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L111-L170)（将 `if req and thread_id:` 修复为 `if req and thread_id is not None:`，防止空字符串 `""` 绕过 `thread_id` 边界比较）
- **修复前后安全行为对比**：

| 场景 | 修复前 | 修复后 (Fail-Closed) |
| :--- | :--- | :--- |
| `acquire_lease=True` + 缺失 `approval_id`（无内存记录） | 跳过 `acquire_execution_lease`，直接放行执行 | **拦截**，返回 `unauthorized_unapproved` |
| `acquire_lease=True` + `approval_id` 为 `None`/`""`/`"   "`（有内存记录） | 自动回填并消费历史记录，或放行 | **拦截**，返回 `unauthorized_unapproved`，且绝不创建或消费记录 |
| `approval_id` 不存在或属于其他 Thread | 依赖局部校验，如缺失 hash 可能误退化 | **拦截**，返回 `unauthorized_lease_failed` / `unauthorized_unapproved`，原 Thread 记录保持 `APPROVED` |
| 篡改 `approval_decision` 将 `MODIFIED` 降级为 raw `approve` | 若保留原始 hash 可跳过修改后参数 | 检查 `bound_req.status` 与 `modified_arguments` 哈希一致性，篡改即阻断 |

---

### Fix 2：正确处理 `asyncio.CancelledError`

- **根因**：
  [`src/jarvis_studio/agent/nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py) 原 `try ... except Exception as exc:` 块在 Python 3.12 中无法捕获继承自 `BaseException` 的 `asyncio.CancelledError`。当外部 SSE 断连或任务超时取消时，工具协程抛出取消信号直接穿透 `except Exception`，导致 `finalize_execution` 从未执行，授权永久滞留在 `EXECUTING` 状态。
- **修改位置**：[`src/jarvis_studio/agent/nodes/executor.py:80-156`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L80-L156)
- **处理策略**：
  1. 显式增加 `except asyncio.CancelledError:` 分支，避免使用宽泛的 `except BaseException` 掩盖 `KeyboardInterrupt`/`SystemExit`。
  2. 在取消分支内对已持有租约的 `auth.approval_id` 调用一次 `hitl_mgr.finalize_execution(auth.approval_id, success=False, error="Task execution cancelled (asyncio.CancelledError)")`，将状态由 `EXECUTING` 终结为 `FAILED`。
  3. 用独立内部 `try ... except Exception` 保护清理调用：即便 `finalize_execution` 自身抛错，仅记录错误日志，随后**无条件执行 `raise` 重新向上抛出原始 `CancelledError`**，绝不吞没取消信号。
  4. 普通 `Exception` 仍维持原有错误工具输出语义（`EXECUTING -> FAILED`）。
  5. **边界声明**：本补丁仅实现协程授权租约状态清理，真正的多进程/子进程树（Shell Subprocess Tree）OS 级终止与回收按架构路线图归属 **Sprint 1B** 处理。

---

### Fix 3：审批 API 的 Thread 隔离与可信身份绑定

- **根因**：
  1. [`src/jarvis_studio/api/routes/approvals.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py) 的 `GET /api/v1/approvals` 全局返回所有会话待决请求，且缺少 `GET /api/v1/approvals/{approval_id}` 单条隔离接口。
  2. `POST /api/v1/approvals/{approval_id}/decision` 与 `/decision/stream` 调用 `hitl_mgr.get_request(approval_id)` 时未传入 `thread_id`，且直接信任客户端 JSON Body 提供的 `payload.thread_id`，甚至促使 `AgentService` 对伪造会话恢复或创建状态。
  3. [`src/jarvis_studio/service/agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py) 中的 `resume_approval` 与 `stream_resume_approval` 未核验 LangGraph Checkpoint 中断载荷，也未将 `approval_id` 写入 `Command(resume=...)`。
- **修改位置**：
  - [`src/jarvis_studio/api/routes/approvals.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py#L1-L190)
  - [`src/jarvis_studio/service/agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L430-L600)
  - [`src/jarvis_studio/api/static/index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L1107-L1196)（同步前端请求传递 `?thread_id=` 及 `X-Thread-ID` 请求头）
- **API Thread 隔离与可信度设计**：
  1. **多源一致性交叉核对（`_extract_consistent_thread_id`）**：提取并比对 `X-Thread-ID` / `X-Session-ID` 请求头、Query 参数 `thread_id` 与 Body `payload.thread_id`。只要任两方不一致，或出现空值/`"default"` 占位符，立即阻断。
  2. **服务端多重可信锚点核验**：
     - **SessionService 核验**：请求声明的 `thread_id` 必须在服务端 `chat_sessions` 数据库中真实存在。
     - **HITLManager 核验**：`hitl_mgr.get_request(approval_id, thread_id=trusted_tid)` 强制同时校验两项 ID。
     - **LangGraph Checkpoint 核验（`AgentService.verify_approval_context`）**：决议提交前校验 `graph.aget_state()` 存在未完结中断、`run_context.session_id == trusted_tid`、中断载荷绑定当前 `approval_id` 且请求当前仍处于 `PENDING` 状态。
  3. **统一无泄漏响应（防资源枚举）**：对于不存在的 `approval_id`、伪造的未注册 `thread_id`、请求头与请求体冲突、跨 Thread 访问他人审批，全部统一返回完全一致的 `HTTP 404` (`{"detail": "Approval request not found"}`)，彻底消除存在性侧信道泄漏。
- **API 兼容性影响说明**：
  - `POST /api/v1/approvals/{approval_id}/decision` 与 `/decision/stream` 的 JSON 请求体结构（[`ApprovalDecisionRequest`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/schemas/request.py#L20-L31)）保持 **100% 向后兼容**；可选 `X-Thread-ID`/`X-Session-ID` 请求头如提供则强制与体一致。
  - **必要安全变更（已同步前端 SPA）**：`GET /api/v1/approvals` 由“无参数全量跨会话列出”收紧为“必须通过 Query `thread_id` 或 `X-Thread-ID` 指定会话作用域（缺省返回 `400`）”；同步修改 [`index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L1159-L1196) 传入当前活动会话 ID，并将展示键名由错配的 `item.approval_id` 修正为模型字段 `item.id`。新增 Thread 隔离的 `GET /api/v1/approvals/{approval_id}`。

---

### Fix 4：移除 `default` Thread 身份兜底

- **根因**：
  [`src/jarvis_studio/agent/nodes/approval.py:126`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L126) 存在 `thread_id = run_context.get("session_id", "default")`，与 [`authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py) 的 Fail-Closed 规则不一致，存在将缺省身份的高危工具汇入共享 `"default"` 审批池的风险。
- **修改位置**：
  - 共享身份规则函数 [`extract_valid_thread_id`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L28-L44)（同时供 `approval.py` 与 `authorization.py` 调用，严格拒收缺失、空字符串及保留字 `"default"`）。
  - [`src/jarvis_studio/agent/nodes/approval.py:125-150`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L125-L150)
- **行为保证**：
  - 仅在检测到 `dangerous_calls` 时校验 `thread_id`；**普通对话及纯安全工具（如 `read_file`）流转零影响**。
  - 涉高危审批但缺少有效 `thread_id` 时，`approval_node` 直接 Fail-Closed 返回 `{"pending_approval": None, "approval_decision": None}`，不创建 `ApprovalRequest`、不触发 `interrupt`，下游 `executor` 将其全部阻断为 `unauthorized_missing_thread`。

---

### Fix 5：明确 `finalize_execution` 失败语义

- **根因**：
  原 `executor.py` 将 `hitl_mgr.finalize_execution(auth.approval_id, success=True)` 置于工具执行的 `try` 块内部。当工具物理执行成功但状态终结失败时，会误落入 `except Exception` 分支，不仅抹除真实输出伪装成 `Error executing ...`，还将状态覆写为 `FAILED`。
- **修改位置**：
  - [`src/jarvis_studio/agent/nodes/executor.py:114-155`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L114-L155)
  - [`src/jarvis_studio/agent/hitl/manager.py:296-322`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L296-L322)
- **失败处理策略**：
  1. 工具调用与授权终结彻底解耦：工具成功与否由独立标志位 `tool_succeeded` 与 `execution_output` 锁定，工具成功后不再回退进入工具异常分支。
  2. 当 `finalize_execution(success=True)` 抛出异常、或返回非 `CONSUMED` 状态时：
     - **严禁重放**：由于执行前已通过原子租约流转至 `EXECUTING`，且不会退回 `APPROVED` 或 `PENDING`，后续重放请求仍被 `acquire_execution_lease` 严格拦截，绝不会重复执行同一工具。
     - **输出不伪装**：`ToolMessage.content` 完整保留工具真实的 `execution_output`，并在尾部附加明确标记 `[AUTHORIZATION_FINALIZATION_ERROR] ... Re-execution remains blocked.`。
     - **结构化状态分离**：通过 `ToolMessage.additional_kwargs` 向调用方及观测链路显式暴露 `{"tool_succeeded": True, "authorization_finalization_error": str(fin_exc), "approval_id": auth.approval_id}`，同时打印 `CRITICAL` 级别日志。

---

## 二、状态机变化总结

状态枚举维持 7 个稳态/终态（`PENDING`、`APPROVED`、`MODIFIED`、`EXECUTING`、`CONSUMED`、`FAILED`、`REJECTED`），流转契约进一步收严：

1. `EXECUTING -> FAILED` 新增 **协程取消路径**（捕获 `asyncio.CancelledError` 后一次性终结并重抛）。
2. `EXECUTING` 在 `finalize_execution(success=True)` 失败时**锁定在 `EXECUTING`（非重放阻断态）**，不再被错误转回或重置为可执行态。
3. 任何路径均不得跳过 `acquire_execution_lease`：无有效 `approval_id` 的决策一律在准入阶段被拒绝。

---

## 三、新增测试与验证结果

### 3.1 TDD 测试验证

新增独立加固测试集 [`tests/test_security_hardening_1a2.py`](file:///d:/myProject/JarvisStudio/tests/test_security_hardening_1a2.py)（19 项），**先写测试确认 Red（9 failed on unpatched baseline，精准复现 API 跨线程创建伪造会话、缺失 `approval_id` 自动回填、`CancelledError` 卡死 `EXECUTING` 及输出覆写漏洞），再实施补丁实现全绿**：

| 分组 | 用例名称 | 验证目标 | 结果 |
| :--- | :--- | :--- | :---: |
| Fix 1 | `test_f1_01_missing_approval_id_fails_closed_even_if_request_in_manager` | 缺失 `approval_id`（即使管理器有记录）仍 Fail-Closed 且不消费 | **PASS** |
| Fix 1 | `test_f1_01b_missing_approval_id_without_manager_record_fails_closed` | 纯状态注入缺失 `approval_id` 时拒绝跳过租约 | **PASS** |
| Fix 1 | `test_f1_02_empty_or_whitespace_approval_id_fails_closed` (2 cases) | 空字符串 `""` 与空白 `"   "` 均 Fail-Closed 且不降级 | **PASS** |
| Fix 1 | `test_f1_03_nonexistent_approval_id_fails_closed` | 不存在的 `approval_id` Fail-Closed 且不创建记录 | **PASS** |
| Fix 1 | `test_f1_04_other_thread_approval_id_fails_closed` | 其他 Thread 的 `approval_id` 被阻断且原记录保留 `APPROVED` | **PASS** |
| Fix 1 | `test_f1_05_valid_authorization_acquires_lease_and_executes` | 完整四元组合法授权正常取得 `EXECUTING` 租约 | **PASS** |
| Fix 2 | `test_f2_01_cancelled_error_transitions_executing_to_failed_and_propagates` | `asyncio.CancelledError` 将租约终结为 `FAILED` 并向上传播 | **PASS** |
| Fix 2 | `test_f2_02_cancelled_error_propagates_even_if_finalize_execution_raises` | `finalize_execution` 自身失败时不吞掉 `CancelledError` | **PASS** |
| Fix 2 | `test_f2_03_normal_tool_exception_still_transitions_to_failed` | 普通工具异常继续正确终结为 `FAILED` | **PASS** |
| Fix 2 | `test_f2_04_normal_tool_execution_still_transitions_to_consumed` | 正常高危工具执行继续正确终结为 `CONSUMED` | **PASS** |
| Fix 5 | `test_f5_01_finalize_success_failure_preserves_output_and_blocks_replay` | 终结失败时保留成功输出、结构化区分状态、禁重放 | **PASS** |
| Fix 4 | `test_f4_01_approval_node_missing_or_default_thread_fails_closed` | 缺失、空、`default` 会话不创建请求、不触发中断 | **PASS** |
| Fix 4 | `test_f4_02_graph_missing_thread_blocks_dangerous_but_allows_safe` | 端到端全图无 `session_id` 放行安全工具、阻断高危工具 | **PASS** |
| Fix 3 | `test_f3_01_api_list_and_get_request_thread_isolation` | `GET /approvals` 与 `/{id}` 严格隔离且统一返回 404 防枚举 | **PASS** |
| Fix 3 | `test_f3_02_api_decision_approve_reject_modify_thread_isolation` | 跨 Thread/伪造 Thread/Header 冲突全拦截；approve/reject/modify 正常 | **PASS** |
| Fix 3 | `test_f3_03_api_stream_decision_with_matching_header_succeeds` | 流式审批 `/decision/stream` 在头部与服务会话匹配下正常执行 | **PASS** |
| 并发 | `test_mt_01_barrier_concurrent_lease_acquisition_single_winner` | 8 OS 线程经 `threading.Barrier` 并发争抢租约，严格 1 个成功 | **PASS** |
| 并发 | `test_mt_02_barrier_concurrent_real_executor_single_execution` | 4 OS 线程各自事件循环经 `threading.Barrier` 并发执行真实 Executor，工具仅执行 1 次 | **PASS** |

### 3.2 全量回归与静态检查

- **全量 Pytest**（最终代码修改后实测）：
  - 命令：`.venv\Scripts\pytest`
  - 结果：**`92 passed in 11.33s`**（73 项原有测试零回归 + 19 项新增 Sprint 1A.2 测试全通过，0 失败、0 跳过）。
- **Ruff 静态检查**（最终代码修改后实测）：
  - 针对改动及 HITL 安全测试路径：`.venv\Scripts\ruff check src\jarvis_studio\agent\hitl src\jarvis_studio\agent\nodes src\jarvis_studio\api\routes\approvals.py tests\test_security_hardening_1a2.py tests\test_authorization_patch.py tests\test_hitl_security.py` -> **`All checks passed!`**
  - 针对 [`agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py)：所有本次新增代码 0 警告；文件顶部原有导入顺序及既有 `BLE001` 等历史提示未擅自扩大修改范围（全仓库未触及的旧模块共保留 31 项既有提示，属基线存量问题）。

---

## 四、Git Diff 摘要

改动严格聚焦 Sprint 1A.2 五项安全加固，未触及 SQLite Checkpointer、Shell 进程架构、MCP、RAG 或 Memory 模块：

- [`src/jarvis_studio/agent/hitl/__init__.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/__init__.py) (+2): 导出共享校验函数 `extract_valid_thread_id`
- [`src/jarvis_studio/agent/hitl/authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py) (+112/-17): 提取 `extract_valid_thread_id`；`acquire_lease=True` 强制校验有效 `approval_id` 并移除静默回填；增强 `modify`/`approve` 状态一致性绑定
- [`src/jarvis_studio/agent/hitl/manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py) (+11/-2): `get_request` 与 `resolve_request` 使用 `thread_id is not None` 封堵空字符串绕过；`finalize_execution` 增补异常状态告警日志
- [`src/jarvis_studio/agent/nodes/approval.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py) (+23/-5): 移除 `"default"` 兜底，复用 `extract_valid_thread_id` 对高危调用实现 Fail-Closed
- [`src/jarvis_studio/agent/nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py) (+79/-10): 显式 `asyncio.CancelledError` 清理与重抛；隔离 `finalize_execution(success=True)` 与工具执行体并输出明确错误语义
- [`src/jarvis_studio/api/routes/approvals.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py) (+157/-32): 新增 `GET /{approval_id}`、收敛 `GET /approvals` 作用域、多源 `thread_id` 一致性与 `SessionService`/Checkpoint 双锚点校验、统一 404 防枚举
- [`src/jarvis_studio/service/agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py) (+86/-1): 新增 `verify_approval_context`，强制恢复链路注入 `approval_id` 与断点一致性核对
- [`src/jarvis_studio/api/static/index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html) (+23/-8): 前端按作用域传参并修正确认列表 ID 字段
- [`tests/test_security_hardening_1a2.py`](file:///d:/myProject/JarvisStudio/tests/test_security_hardening_1a2.py) (新增): 19 项 TDD 安全与 `threading.Barrier` 并发测试

---

## 五、已知残余风险（按路线图留待后续 Sprint 处理）

1. **进程崩溃与单机内存态局限（归属 Sprint 1C）**：
   `HITLManager` 仍采用进程内内存字典维护 `ApprovalRequest`。若服务遭遇 `SIGKILL` 崩溃、异常掉电或普通重启，尚未完成的 `PENDING`/`APPROVED`/`EXECUTING` 状态将在重启后清零（重启后系统按 Fail-Closed 拦截所有未消费调用，确保安全性，但无法自动断点续跑审批）。审批记录的 SQLite 持久化将在 Sprint 1C 完成。
2. **协程取消后的 OS 子进程树回收（归属 Sprint 1B）**：
   Fix 2 确保了 `asyncio.CancelledError` 发生时授权状态及时转为 `FAILED` 且取消信号不被吞没，但同步/阻塞型外部 Shell 子进程的实际进程组终止（`SIGTERM`/`CTRL_BREAK` 进程树清理）依赖 Sprint 1B 的统一任务运行时与 Shell 架构升级。
3. **无用户态认证体系下的 `Session` 模型前提**：
   目前 Jarvis Studio 定位为本地/受控单用户个人工作站（无多租户 JWT/Cookie 登录网关），因此服务端基于 `SessionService` + 请求上下文头部校验 + LangGraph 中断断点作为 Thread 信任锚点；若未来开放公网多用户共享部署，需额外叠加用户级认证凭据（如 Bearer Token）绑定会话归属。

---

## 六、最终结论

所有第二轮独立审计中发现的阻断性与高/中危问题已完成闭环修复与自动化对抗验证，所有修改已控制在最小合规边界内，不破坏既有能力。

**`PATCH COMPLETE`**

*(当前已完成提交前准备工作，未执行分支合并，亦未开启 Sprint 1B，等待最终独立复审指示。)*
