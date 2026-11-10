# Jarvis Studio — Sprint 1A Final Independent Security Audit

> **角色**：Independent Principal Security Engineer  
> **审计性质**：最终独立只读审查（未修改任何业务代码、测试代码与配置文件，未改动 `main`）  
> **审计对象**：`feature/v2-sprint-1a-security`（涵盖 Sprint 1A、Sprint 1A.1、Sprint 1A.2 全部演进与相对 `main` 的完整 Diff）  
> **基准版本**：Commit `f9b0def` (`HEAD -> feature/v2-sprint-1a-security`)，含工作区未提交文件仅限于安全报告文档  
> **独立实测基线**：Pytest **92/92 PASS (13.56s)**；专项 Ruff Check **All checks passed!**  
> **审计日期**：2026-11-10  

---

## 一、最终审查结论与核心决策回答 (Final Verdict)

### 最终结论：**`APPROVED`（批准合并 Sprint 1A 并冻结安全基线）**

#### 针对 6 项核心决策问题的明确回答：

| 审计核心问题 | 明确结论 |
| :--- | :--- |
| **1. Sprint 1A 是否允许合并？** | **允许合并**。`feature/v2-sprint-1a-security` 已解决既定全部高危缺陷（多工具越权放行、路径逃逸、授权重放、`CancelledError` 状态悬挂、缺失 `approval_id` 绕过租约、`default` 身份混用及 API 跨 Thread 探针），建议合并至 `main` 并冻结当前单进程安全基线。 |
| **2. 是否还有 P0/P1 阻断问题？** | **无 P0/P1 阻断问题**。在当前单用户工作站模型边界内，所有工具执行、审批、取消、重放与路径防穿越控制均已实现 Fail-Closed。 |
| **3. API Session 隔离是否满足当前项目的安全模型？** | **满足当前单用户/受信任本机个人工作站安全模型，但不等于多租户用户身份认证**。系统目前**没有**用户登录认证体系（无 JWT/Cookie/Bearer Token/ACL 归属字段）。API 层的 `thread_id`、请求头与 `SessionService` 核验防止了会话错配、客户端捏造与跨资源存在性枚举，但**不能防止知道目标 `session_id` 的第三方未认证客户端冒用相同字符串**（详述见第三节）。 |
| **4. 授权状态机是否具备单进程环境下的可靠性？** | **具备**。`HITLManager` 通过单一 `threading.Lock` 保护全状态跃迁与校验，经 8 OS 线程 `threading.Barrier` 实测精确保证单一获胜者；`CONSUMED` 与 `FAILED` 严格不可逆；协程取消（`CancelledError`）与 `finalize_execution` 失败均不会造成重放或虚假成功状态覆写。 |
| **5. 当前有哪些明确不保证的能力？** | **① 多用户强身份认证与访问控制**（无 Auth/ACL）；**② 进程崩溃/重启/多 Worker 跨进程审批一致性**（`HITLManager` 与 `MemorySaver` 仍为单进程内存态）；**③ 异步取消后的 OS 子进程树强制终止**（阻塞型 `subprocess` 进程组回收待 Sprint 1B 实现）；**④ OS 级并发符号链接替换（TOCTOU）与容器沙箱**（文件系统目前为 Level 1/Level 2 静态隔离）；**⑤ Shell 命令 AST 深度语义审计**（依赖人类确认）。 |
| **6. 进入 Sprint 1B 前是否还需要安全补丁？** | **不需要阻塞合并的前置补丁**。已观察到的低风险代码细节（见第八节残余微瑕疵，如 `MODIFIED` 状态下 `bound_req.modified_arguments is None` 极端检查、Web UI 页面刷新后的只读审批页缺操作按钮等）可作为工程改进纳入后续 Sprint 顺带消化。 |

---

## 二、审计范围与 Git Diff 核查

### 2.1 分支状态与相对 `main` 的改动边界

- 当前分支：`feature/v2-sprint-1a-security`；工作树干净（`main` 分支未发生任何变更或自动合并）。
- 相对 `main` 共涉及 20 个文件：
  - **HITL 核心安全链路**：[`src/jarvis_studio/agent/hitl/__init__.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/__init__.py)、[`manager.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py)、[`authorization.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py)、[`nodes/approval.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py)、[`nodes/executor.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py)。
  - **API 与前端联调隔离**：[`src/jarvis_studio/api/routes/approvals.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py)、[`src/jarvis_studio/service/agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py)、[`src/jarvis_studio/api/static/index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html)。
  - **文件系统边界防护**：[`src/jarvis_studio/tools/builtin/file_tools.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/tools/builtin/file_tools.py)。
  - **安全测试与架构审计文档**：4 组专项安全测试文件及 `docs/architecture/` 审计文档。
- **范围纯净性确认**：严格核查 Diff，**未修改** `src/jarvis_studio/rag/`、`src/jarvis_studio/memory/`、`src/jarvis_studio/tools/mcp/`、`config/`、`pyproject.toml` 或 LangGraph Checkpointer 底盘。

---

## 三、最高优先级：API 身份与 Session 隔离审查

重点审查 [`src/jarvis_studio/api/routes/approvals.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py)、[`AgentService.verify_approval_context`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L431-L489) 及前端 [`index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html)。

### 3.1 必须回答的 10 项核心安全质询

1. **`X-Thread-ID` 和 `X-Session-ID` 是否由服务端可信上下文验证？**  
   **部分验证（上下文一致性 + 服务端注册状态绑定，非密码学身份认证）**。  
   在 [`approvals.py:25-80`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py#L25-L80)，`_extract_consistent_thread_id` 将两个请求头与 Query/Body 互相比对（任何冲突直接拒绝）；随后 `_resolve_verified_session_thread_id` 与 [`verify_approval_context`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L431-L489) 核对 `SessionService` 存在性、`HITLManager` 归属以及 LangGraph Checkpoint 中断载荷。但**必须如实指出**：`X-Thread-ID` 与 `X-Session-ID` 均属于客户端自声明 HTTP Header，项目当前**没有登录认证中间件或服务端签名凭据**，因此服务端无法证实发送请求头的 HTTP 客户端即为该会话的创建者本尊。

2. **是否可能仅凭客户端自行指定 `thread_id` 访问其他 Session？**  
   - **不知道目标 `thread_id` 或混用自己 `thread_id` 时**：**绝对不可能**。任何捏造的不存在 `thread_id`、混用 Session A 的 `approval_id` 与 Session B 的 `thread_id`、或头体不一致的请求，均被拦截并返回统一 404。
   - **已获悉目标 `thread_id` 字符串时**：**存在无认证风险**。若攻击者获取了他人合法的 `sess_xxx` 字符串（例如通过未鉴权的 `GET /api/v1/sessions`），由于系统无用户认证机制，攻击者可在 Query/Header/Body 中同时填入该 `sess_xxx`，进而从 `GET /api/v1/approvals?thread_id=sess_xxx` 读取待决 `approval_id` 并发起决断请求。

3. **`SessionService` 的 Session 注册记录是否具有真实的访问控制语义？**  
   **不具有**。[`src/jarvis_studio/service/session_service.py:67-74`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/session_service.py#L67-L74) 中的 `chat_sessions` 表仅记录 `session_id`、标题、时间戳和元信息，没有任何用户标识（`owner_id`/`user_id`）或密钥哈希字段。`session_service.get_session(thread_id)` 仅确认“该线程是服务端真实注册过的会话（防止任意捏造不存在的线程）”，并不代表所有权授权。

4. **是否存在不同用户或客户端之间的 Session 越权访问？**  
   **当前模型边界说明**：Jarvis Studio 现阶段定位为**本机/单用户个人 AI 操作系统**（无多租户系统）。在“同机单人使用”模型下，Session 隔离有效阻断了前端多标签页并发误操作、审批串台和参数跨会话注入。**一旦部署为多人共享服务**，多个客户端只要提供相同的 `thread_id` 即可共享操作同一会话——当前系统**绝对不能作为多用户认证隔离系统使用**。

5. **请求头、Query、Body 出现不一致时是否统一拒绝？**  
   **是**。[`_extract_consistent_thread_id`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py#L25-L58) 收集非空的 `body_thread_id`、`query_thread_id`、`x-thread-id`、`x-session-id`，剔除空白与保留字 `"default"` 后要求 `len(set(normalized)) == 1`，任何两处不匹配均返回 `None` 并触发统一拒绝（404）。

6. **`approval_id` 是否同时绑定正确的 `thread_id`？**  
   **是，三重校验绑定**。① `hitl_mgr.get_request(approval_id, thread_id=trusted_tid)`（[`approvals.py:124`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/approvals.py#L124) & [`agent_service.py:451`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L451)）；② `verify_approval_context` 校验 LangGraph 状态快照 `run_context["session_id"] == clean_tid` 及 `tasks[*].interrupts[*]` 中的 `thread_id` 与 `approval_id`（[`agent_service.py:468-488`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py#L468-L488)）；③ 将 `approval_id` 显式写入 `resume_payload` 并流转至 `acquire_execution_lease`。

7. **`GET`、`POST approve`、`reject`、`modify` 是否都经过同一套身份校验？**  
   **是**。`GET /approvals/{approval_id}`、`POST /approvals/{approval_id}/decision`（涵盖 `approve`/`reject`/`modify`）及 `POST /approvals/{approval_id}/decision/stream` 均统一先执行 `_resolve_verified_session_thread_id`，且所有写决断入口先执行 `agent_service.verify_approval_context`（要求状态必须为 `PENDING` 且匹配活动断点），随后才做决策字段检查（`approve`/`reject`/`modify`）。`GET /approvals` 使用 `_extract_consistent_thread_id` 并按 `thread_id` 过滤。

8. **`404` 是否真正消除了跨 Session 资源存在性侧信道？**  
   - **针对单条审批接口 (`/{approval_id}` 与 `/decision*`)**：**是**。无论 `approval_id` 不存在、属于其他 Session、`thread_id` 未注册、头体冲突、或请求已被消费，均返回字节级完全一致的 `HTTP 404` (`{"detail": "Approval request not found"}`)。
   - **针对列表接口 (`GET /api/v1/approvals?thread_id=...`)**：对合法空会话与未注册 `thread_id` 均返回 `200 []`，消除了基于 `/approvals` 的会话存在性探测（缺少 `thread_id` 时统一返回 `400`；头/参数冲突统一返回 `404`）。
   - **前置端点旁注（Sprint 1A 范围外）**：预存的 `GET /api/v1/sessions`（[`sessions.py:37`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/sessions.py#L37)）和 `GET /api/v1/system/traces`（[`system.py:67`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/routes/system.py#L67)）尚未引入 Auth 鉴权，直接调用可枚举全局会话 ID（不泄漏待审批 `approval_id` 详情，但会暴露 `session_id`）。

9. **是否存在未受保护的旧 API 路由或兼容接口？**  
   核查 `src/jarvis_studio/api/routes/` 全部 6 个路由文件（`approvals.py`、`chat.py`、`sessions.py`、`memory.py`、`rag.py`、`system.py`）：不存在第二套审批兼容入口，也不存在绕过 `executor.py` 直接调用高危工具（`execute_shell_command` / `write_file`）的 API 路由。

10. **前端修改后，页面刷新、恢复待审批请求、提交审批是否仍正常？**  
    审查 [`src/jarvis_studio/api/static/index.html`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L432-L1206)：
    - **审批提交**：[`resolveApproval`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L1080-L1157) 同步携带了 `'X-Thread-ID': currentThreadId` 请求头与 Body `thread_id: currentThreadId`，与后端严格一致检查完全兼容；且 `loadApprovals()` 修复了历史键名错配（`item.approval_id` -> `item.id`），列表可正常展示审批 ID。
    - **页面加载与刷新边界**：第 434 行初始 `let currentThreadId = 'default';`，页面首次加载时（[`index.html:1761-1765`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L1761-L1765)）先异步 `loadSessions()` 又同步调用一次 `checkPendingApprovals()`，会以 `'default'` 触发一次 404（已被 `if (!res.ok) return;` 静默兜底，随后 `loadSessions()` 切换为真实 `sess_...` 恢复正常）。此外，`#approvals-list` 视图（第 1193–1202 行）本身为只读展示列表（无交互恢复按钮，系 Sprint 1A 之前的原生 UI 限制，并非本次修改退化），在未刷新离开当前会话流时弹窗审批完全正常。

---

## 四、HITL 授权执行闭环审查

完整追踪执行链路：**高危 ToolCall → `approval_node` → `ApprovalRequest` → 用户决策 → `verify_tool_execution_authorization` → `acquire_execution_lease` → `tool_executor_node` → `finalize_execution`**。

| 核查项 | 审计代码证据 | 判定 |
| :--- | :--- | :---: |
| **1. 所有高危工具强制授权** | [`router.py:30`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/router.py#L30) 前置分流 + [`executor.py:39`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L39) 强制内门禁；覆盖内置 `execute_shell_command`、`write_file` 及高危 MCP 工具（[`adapter.py:137`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/tools/mcp/adapter.py#L137)）。 | **安全** |
| **2. 缺失 `approval_id` 绕过路径** | [`authorization.py:309-322`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L309-L322) 在 `acquire_lease=True` 时强制校验非空 `appr_id`，已移除缺失回填；即使内存中恰好有匹配记录，State 中缺 `approval_id` 也直接 Fail-Closed 且绝不消费记录。 | **安全** |
| **3. 跨 Session 借用授权** | [`authorization.py:81`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L81) 与 `get_by_tool_call_id` / `acquire_execution_lease` 全链路强制 `thread_id`，且 [`manager.py:117, 165`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L117) 采用 `thread_id is not None` 防止 `""` 绕过。 | **安全** |
| **4. 重复获取 `EXECUTING` 租约** | [`manager.py:216-282`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L216-L282) 在单把 `threading.Lock` 临界区内完成四要素核对与 `APPROVED/MODIFIED -> EXECUTING` 原子跃迁，第二个线程/协程必被阻断。 | **安全** |
| **5. `CONSUMED` / `FAILED` 不可逆性** | `resolve_request` 仅接受 `PENDING`（[`manager.py:171`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/manager.py#L171)）；`acquire_execution_lease` 仅接受 `APPROVED/MODIFIED`；`finalize_execution` 仅处理 `EXECUTING`。全代码无任何逆向重置路径。 | **安全** |
| **6. 参数修改绑定校验** | [`authorization.py:197-211, 242-282`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L197-L282) 对原始参数哈希防篡改 + `args_schema.model_validate` + 校验 `bound_req.status == MODIFIED` 与 `bound_req.modified_arguments` 哈希一致性，防止 `State` 篡改降级或偷换参数。 | **安全**（注一处极低风险 P3 细节：第 254 行当 `bound_req.modified_arguments` 本身为 `None` 时未比对，`None != dict`，可日后顺带补全） |
| **7. `tool_call_id` 碰撞安全性** | **跨线程碰撞**：完全由 `thread_id` 隔离；**同线程后续轮次碰撞**：[`approval.py:161-173`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/approval.py#L161-L173) 查到已有记录且状态为 `CONSUMED`/`FAILED` 时跳过中断并进入 `executor`，随后被 `acquire_execution_lease` 严格拦截（Fail-Closed，绝不重放）。 | **安全** |
| **8. 绕过 Executor 直接调用高危工具** | 全库 `git grep` 确认 `execute_shell_command`、`write_file` 与 `subprocess.run` 仅注册于 ToolRegistry 并由 `tool_executor_node` 统一调度，无其他直接调用入口。 | **安全** |

---

## 五、取消与异常安全审查 (`executor.py`)

核查 [`src/jarvis_studio/agent/nodes/executor.py:80-169`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/nodes/executor.py#L80-L169) 在全部 6 种故障模型下的真实行为：

| 故障模型 | 实际执行路径（代码实证） | 工具执行结果 vs 授权状态结果 | 判定 |
| :--- | :--- | :--- | :---: |
| **1. 普通 `Exception`** | 行 109 `except Exception` 捕获；调用一次 `finalize_execution(..., success=False)`；返回 `"Error executing '{tool_name}': {exc}"`。 | 工具失败 (`Error executing...`)；授权状态转为 `FAILED`，不可重放。 | **安全** |
| **2. `asyncio.CancelledError`** | 行 92 `except asyncio.CancelledError` 显式捕获（未使用 `BaseException`）；记录警告并调一次 `finalize_execution(..., success=False)`；行 108 原样 `raise` 向上传播。 | 工具被取消；授权状态脱离 `EXECUTING` 转为 `FAILED`（记录取消原因），取消信号不被吞没。 | **安全** |
| **3. `finalize_execution(success=False)` 失败** | 行 104 / 行 117 内部捕获 `fin_exc` 并打日志；CancelledError 仍旧 `raise`，Exception 仍返回工具错误消息；状态保持 `EXECUTING`。 | 不吞没取消信号；虽停留在 `EXECUTING` 但继续阻断一切重放。 | **安全** |
| **4. `finalize_execution(success=True)` 失败** | 行 127 独立于工具 `try` 块之外；捕获异常或非 `CONSUMED` 状态后打印 `CRITICAL` 日志（含 `tool_name`、`call_id`、`approval_id`、`fin_exc`）；不改写为 `FAILED`。 | **精确区分**：`ToolMessage.content` 保留真实成功输出并附加 `[AUTHORIZATION_FINALIZATION_ERROR]`，`additional_kwargs` 置 `tool_succeeded=True` 与 `authorization_finalization_error`；租约锁定 `EXECUTING` 严禁重放。 | **安全** |
| **5. 工具成功但终结异常对后续流影响** | `tool_succeeded = True` 已锁定事实，绝不再回退执行 `finalize_execution(success=False)`，也绝不会重复触发同一工具调用。 | 不误报工具本身执行失败，不触发重放。 | **安全** |
| **6. 取消与终结并发** | `tool.ainvoke` 结束后同步调用无 `await` 的 `finalize_execution`，事件循环不会在 `finalize_execution` 内部插入协程取消；多线程竞争则由 `HITLManager._lock` 串行化互斥。 | 不可能发生重复 finalize 或半状态撕裂。 | **安全** |

---

## 六、全链路 Session 与默认身份检查

核查项目全入口，确认移除 `"default"` 兜底后的真实身份边界：

1. **Web API (`approvals.py`, `chat.py`)**：
   - `approvals.py` 全端点禁止空值与 `"default"`。
   - `chat.py` 允许用户显式传入或省略 `session_id`（省略则生成 `sess_<hex>`；若客户端显式传入 `"default"`，虽会在 SQLite 生成记录但下游 HITL 节点强制 Fail-Closed 拒绝任何高危审批）。
2. **Web UI (`index.html`)**：
   - 仅页面脚本启动瞬间初始化占位变量 `'default'`，随后由 `loadSessions()` 切换至真实 `sess_...`；期间同步触发的轮询会被后端 404 安全拦截。
3. **CLI (`src/jarvis_studio/cli/main.py`)**：
   - 第 387–388 行通过 `session_service.get_or_create_session(session)` 获取真实 `sess_...`（仅当用户显式以 `--session default` 启动时因命中禁用规则被 HITL 阻断，无自动隐式回退）。
4. **`AgentService` & LangGraph Runtime**：
   - `invoke_agent` / `stream_agent` 强制注入 `run_context={"session_id": thread_id}`；`verify_approval_context` 拒绝 `"default"`。
   - `assistant.py:41` 保留 `session_id = run_context.get("session_id", "default")` **仅用于观测链路 `timed_step` 打点**，不涉及任何工具鉴权或数据隔离；**普通无 HITL 对话与只读安全工具（`read_file`、`list_directory`）完全不受影响**（已由 `test_f4_02` 端到端全图验证）。
5. **HITL Approval (`approval.py`, `authorization.py`, `manager.py`)**：
   - `approval_node` 与 `verify_tool_execution_authorization` 共享完全一致的 `extract_valid_thread_id(state)`（均拒绝缺失、`None`、空串、空白及 `"default"`），彻底消除默认池风险。

---

## 七、测试质量评价

审查 [`tests/test_security_hardening_1a2.py`](file:///d:/myProject/JarvisStudio/tests/test_security_hardening_1a2.py)（19 项）、[`tests/test_authorization_patch.py`](file:///d:/myProject/JarvisStudio/tests/test_authorization_patch.py)（10 项）、[`tests/test_hitl_security.py`](file:///d:/myProject/JarvisStudio/tests/test_hitl_security.py)（10 项）及 [`tests/test_file_path_security.py`](file:///d:/myProject/JarvisStudio/tests/test_file_path_security.py)（10 项）：

1. **19 项新增测试真实覆盖度**：逐项核实全部覆盖 Fix 1–5 要求（missing/empty/nonexistent/cross-thread `approval_id`、`CancelledError` 清理与传播、`finalize_execution` 失败语义保留与防重放、API 跨 Thread/伪造 Thread/Header 冲突/统一 404、`approval_node` 禁用 `"default"` 及安全流兼容）。
2. **多线程 Barrier 并发真实性**：`test_mt_01`（8 线程 `threading.Barrier(8)`）与 `test_mt_02`（4 线程各自运行 `asyncio.run(executor(...))` 跨 OS 线程经 `Barrier(4)`）均直接作用于**全局共享 `get_hitl_manager()` 单例**（无 sleep 脆弱延时），精确验证单次获胜与物理工具仅执行 1 次（`exec_counter == 1`）。
3. **API 与头/参数冲突覆盖**：使用 FastAPI `TestClient` + 隔离临时 SQLite DB + 真实 `MemorySaver` 检查点完成了 `GET /approvals`、`GET /approvals/{id}`、`POST /decision`（approve/reject/modify）及 `POST /decision/stream` 的跨 Thread 越权实测；`r_header_conflict` 与 `bad_header` 验证了 Header/Body/Query 冲突拒绝。
4. **Mock 使用边界评估**：除以下明确必须场景外均驱动真实 Graph/Executor/API 运行：
   - LLM 响应使用 `_MockToolCallingChatModel`（模拟工具调用指令）。
   - `test_f2_02` / `test_f5_01` 使用 `patch.object(hitl, "finalize_execution")`（专门注入存储终结失败故障，同时真实执行 `@tool` 函数体）。
   - `test_file_path_security.py` 对无权创建真实符号链接的受限 Windows 环境降级使用 `patch("os.path.realpath")`。
5. **尚存未覆盖测试空白（低风险/非阻断）**：
   - 未单独编写 `X-Session-ID` Header（代码已测 `X-Thread-ID`，两者同走 `_extract_consistent_thread_id`）及 `POST /decision/stream` 单独 Header 冲突的用例。
   - API 集成测试基于 `MemorySaver` 检查点，未在多轮重启恢复场景（依赖 Sprint 1C SQLite 持久化后才可完整端到端测试）验证断点恢复。
   - Windows NTFS 物理真实 `os.symlink` 未在提权环境下无 Mock 跑通。

---

## 八、工程质量、测试与 Lint 审计

1. **全量 pytest（审计过程独立实测）**：  
   执行 `.venv\Scripts\pytest`：**`92 passed in 13.56s`**（0 失败，0 跳过）。
2. **Ruff 静态分析（严格区分新增改动与历史基线）**：  
   - 针对 Sprint 1A/1A.1/1A.2 新增与修改的安全核心文件及测试文件（`src/jarvis_studio/agent/hitl`、`src/jarvis_studio/agent/nodes`、`src/jarvis_studio/api/routes/approvals.py`、`tests/test_security_hardening_1a2.py`、`tests/test_authorization_patch.py`、`tests/test_hitl_security.py`）：**`All checks passed!`**
   - [`src/jarvis_studio/service/agent_service.py`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/service/agent_service.py)：本次新增方法 `verify_approval_context` 与校验逻辑**0 警告**；保留了原文件顶部既有 `I001` 及原有的 4 处 `BLE001`（属历史基线，未顺手扩散修改范围）。
   - 全仓库 `.venv\Scripts\ruff check .` 共报告 31 项错误，全部集中在未触及的历史遗留模块（`api/app.py`、`chat.py`、`rag/*`、`session_service.py`、`shell_tools.py`、`mcp/*`、`test_agent_graph.py` 等）。
3. **残余微瑕记录（P3 级，非阻断）**：
   - [`authorization.py:254`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/agent/hitl/authorization.py#L254)：`if bound_req.modified_arguments is not None and ...` 可收紧为直接比对（即使为 `None` 时也拒绝对被外部篡改注入 dict 的 `AgentState` 放行，可在后续顺带清理）。
   - [`index.html:1765`](file:///d:/myProject/JarvisStudio/src/jarvis_studio/api/static/index.html#L1765) 初次加载在 `loadSessions()` 返回前以 `'default'` 轮询一次产生可忽略的 404，以及 `#approvals-list` 视图尚缺页面刷新后的交互审批按钮。

---

## 九、合并建议与后续 Sprint 交接清单

1. **正式决策**：批准将 `feature/v2-sprint-1a-security` 合并至 `main`，并冻结 **Jarvis Studio V2 Sprint 1A 单进程安全基线**。
2. **明确进入 Sprint 1B / 1C 的安全边界交接**：
   - **Sprint 1B（Task Lifecycle & Shell 架构演进）**：必须在 `execute_shell_command` 及后台 Task Runtime 中引入异步子进程控制与 OS 进程组终止机制（`CancelledError` 触发后真正杀死子进程树，弥补当前仅回收授权状态的限制）。
   - **Sprint 1C（持久化 & 存储演进）**：将 `HITLManager._requests` 迁移至 SQLite WAL 持久化表（与 Session/Task 同事务或同级恢复），解决进程重启/崩溃与多 Worker 状态丢失；如计划对外网或多租户暴露 API，必须独立规划用户认证与会话 ACL 鉴权中间件。

---
*独立安全审计结束。当前严格遵守只读规则，未改动任何源码、测试与 `main` 分支，等待负责人统筹合并。*
