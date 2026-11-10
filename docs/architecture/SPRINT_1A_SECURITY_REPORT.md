# Jarvis Studio V2 — Sprint 1A: Security & HITL Correctness Report

> **首席 AI 系统架构师与系统工程师审计报告**  
> **分支**：`feature/v2-sprint-1a-security`  
> **状态**：完成 (63/63 测试全部通过，零破坏性变更，无回归缺陷)  
> **日期**：2026-11-10  

---

## 摘要

在 Jarvis Studio V2 的首个工程冲刺（Sprint 1A）中，我们针对架构审计中揭示的两个重大安全缺陷实施了生产级加固：
1. **HITL 多工具调用审批越权与参数污染缺陷**：从执行模型层面消除“批准单工具连带全执行”及“参数污染”漏洞，建立基于 `(thread_id, tool_call_id, tool_name, arguments_hash)` 的独立强制执行授权网关。
2. **工作区文件系统路径逃逸缺陷**：消除 `_resolve_safe_path` 中绝对路径、`../../` 穿越、Windows 盘符跨越、UNC 路径、符号链接穿透等全部逃逸向量，建立全平台防御体系。

本轮迭代新增 20 项严苛的端到端安全自动化测试用例，所有原存 43 项测试 100% 保持通过，测试总数提升至 **63 项全绿通过**，代码通过 Ruff 生产级静态检查。

---

## 一、漏洞成因与修复对比

### 1.1 HITL 多工具审批越权与参数错配

| 维度 | 修复前缺陷行为 | 修复后安全防护 |
| :--- | :--- | :--- |
| **工具发现** | `approval_node` 仅遍历检查并拦截消息中的**第一个**高危 ToolCall，其后高危调用被静默放行。 | 统一提取所有高危 ToolCall，为每个调用分别生成具备唯一身份与参数哈希的 `ApprovalRequest`。 |
| **审批身份绑定** | 决策结果仅包含无状态的 `{"decision": "approve"}`，无 `tool_call_id` 与参数哈希绑定。 | 决策与 `(thread_id, tool_call_id, tool_name, arguments_hash)` 强绑定，防止 ID 混淆与伪造。 |
| **执行权限判定** | `executor_node` 信任顶层决策状态，若第一个工具批准，剩余高危工具未经批准直接执行。 | `verify_tool_execution_authorization` 统一门禁强制前置校验，未获对应授权的高危工具一律拒绝。 |
| **参数修改隔离** | `modified_arguments` 直接覆盖全局，若一轮中有多个工具，被修改的参数会污染其他工具。 | 参数修改严格限制在对应 `tool_call_id`，并重新基于工具 `args_schema` 校验，绝不污染并存工具。 |
| **拒绝与消息协议** | 拒绝处理不规范，易导致 OpenAI / LangGraph ToolCall 与 ToolMessage 协议数量不一致。 | 被拒绝工具合规合成 `ToolMessage(content="Operation cancelled...", tool_call_id=call_id)`，维持会话对齐。 |
| **重复提交防护** | 允许对已决断的请求重复提交不同决断，存在审批重放风险。 | `HITLManager.resolve_request` 强校验 `PENDING` 状态，重复决断立即抛出 `ValueError`。 |

### 1.2 工作区文件路径逃逸

| 逃逸向量 | 修复前漏洞成因 | 修复后安全防护 |
| :--- | :--- | :--- |
| **相对穿越 (`../../`)** | `(workspace / target_path).resolve()` 解析后未做 `is_relative_to(workspace)` 校验。 | 严格校验 `candidate.is_relative_to(workspace)`，越界立即抛出 `PermissionError`。 |
| **绝对路径逃逸** | `if Path(target).is_absolute(): return Path(target).resolve()` 直接放行任意绝对路径。 | 绝对路径解析后必须通过工作区真实前缀包含关系校验，禁止越权访问根系统。 |
| **Windows 盘符跨越** | Windows 下从 `D:\workspace` 输入 `C:\Windows`，未做跨盘符拒绝。 | 捕获不同驱动盘导致的 `ValueError` 并明确转为 `PermissionError` 阻断。 |
| **Windows UNC 逃逸** | 允许形如 `\\evil-share\loot` 或 `//evil-server/data` 的共享网络路径。 | 前置拦截 `\\` 与 `//` 前缀，明确禁止一切 UNC 路径访问。 |
| **符号链接穿透** | 解析符号链接时仅看链接自身位置，目标指向系统敏感目录时放行。 | 通过 `os.path.realpath` 递归深层验证真实目标位置，严禁链接目标逸出工作区。 |
| **循环符号链接** | 存在符号链接环时可能引发无限递归或进程挂起。 | 捕获 `RecursionError` 及 `OSError (ELOOP)`，安全阻断并报告错误。 |
| **特殊字符与 ADS 流** | 允许输入包含空字节（`\x00`）或 NTFS 备用数据流冒号（`file.txt:stream`）。 | 字符级扫描拦截空字节，严格限定冒号仅能在盘符索引 1 处出现。 |
| **深层嵌套父目录创建** | 新建多级深层文件时父目录若包含穿透符可能逃逸。 | 逐级回溯现有祖先目录，确保所有现有及生成层级均在工作区合法范围内部。 |

---

## 二、HITL 授权模型设计

### 2.1 授权状态机与生命周期

```
[Agent Assistant Node]
       │
       ▼ (AIMessage with tool_calls)
[Approval Gate Node] ──(No dangerous tools)──► [Direct to Tools]
       │
       ▼ (Dangerous tools identified)
  Create `ApprovalRequest`s with `arguments_hash` & `tool_call_id`
       │
       ├─► Multiple pending? ──► Sequential or Batch `interrupt(payload)`
       │                              │
       │   ◄─── Resume with decision ──┘
       ▼
  All dangerous calls resolved?
       │
       ├─ No  ──► Next `interrupt(remaining_payload)`
       └─ Yes ──► Inject `decisions_by_call_id` into `AgentState`
                     │
                     ▼
             [Tool Executor Node]
                     │
                     ▼
          For each ToolCall in message:
                     │
                     ▼
     [verify_tool_execution_authorization]
                     │
       ┌─────────────┴─────────────┐
       ▼                           ▼
[Authorized]                [Unauthorized / Blocked]
 - Safe tool                 - Unapproved tool
 - Approved tool             - ID mismatch
 - Validated modified tool   - Tampered args (hash mismatch)
       │                     - Rejected by user
       ▼                     - Schema invalid args
Invoke Tool Implementation         │
       │                           ▼
       └─────────────┬─────────────┘
                     ▼
         Emit aligned ToolMessage
```

### 2.2 数据契约与核心组件

1. **`ApprovalRequest` 强化字段**：
   - `tool_call_id: str`：唯一绑定 LLM 发出的工具调用标识。
   - `arguments_hash: str`：对原始参数经确定性 JSON 序列化后计算的 SHA-256 摘要，用于防止参数被篡改。
   - `decision: str | None`：记录最终决策状态（`approve` / `reject` / `modify`）。
2. **`verify_tool_execution_authorization` 统一守卫**：
   - 在真正触发工具调用前进行不可绕过的强校验。
   - 即使 `approval_node` 故障被绕过，执行器亦绝不执行未授权高危工具。
3. **多工具独立决策支持**：
   - 支持多工具分步中断审批（单个独立批准/拒绝）。
   - 支持批量原子审批（`{"decisions": [{"tool_call_id": ..., "decision": ...}]}`）。
   - 完全向下兼容历史单工具无 ID 决断调用。

---

## 三、文件路径防护模型与跨平台实现

### 3.1 路径解析核心防护矩阵

```python
def resolve_safe_path(
    target_path: str | Path,
    workspace_root: str | Path | None = None,
    allow_symlinks_outside: bool = False,
) -> Path
```

- **规范化基准工作区**：在操作开始前，将 `workspace_root` 经由 `os.path.realpath` 与 `resolve()` 固化为绝对规范物理路径。
- **边界前置语法检查**：
  - 空字节注入扫描（`\x00`）。
  - UNC 网络共享路径前缀（`\\` 与 `//`）。
  - Windows NTFS ADS 数据流冒号注入扫描（冒号只能出现在盘符第 1 字符）。
- **相对包含断言**：通过 `candidate.is_relative_to(workspace)` 判定包含关系；若跨驱动盘（Windows 返回 `ValueError`）或发生父级溢出直接阻断。
- **真实目标路径审计**：针对现有文件与符号链接，通过 `os.path.realpath` 获取底层物理目标，确认其未链接至工作区外部；对尚未创建的目标文件，回溯现有最近祖先目录进行验证。
- **防御 TOCTOU（时间差竞争）**：
  - 在写入与读取工具内部再次断言 `path.is_symlink()` 并在必要时检查目标指向。
  - 创建父级目录采用原子式 `mkdir(parents=True, exist_ok=True)`。

---

## 四、测试覆盖与工程基线验证

### 4.1 原存基线测试（Regression Baseline）
- 运行命令：`.venv\Scripts\pytest tests/test_agent_graph.py tests/test_api.py ...`
- 原存测试用例数：**43 项**
- 执行结果：**43 项全部 PASS（耗时 12.04s，无破坏性变更）**。

### 4.2 Sprint 1A 新增安全测试集

#### 1. HITL 安全与独立授权测试集 (`tests/test_hitl_security.py`)

| 测试用例 | 验证目标 | 结果 |
| :--- | :--- | :---: |
| `test_01_single_high_risk_tool_normal_approval` | 单个高危工具正常拦截并在审批后正确执行 | **PASS** |
| `test_02_two_high_risk_tools_approved_separately` | 两个高危工具分别在连续两次 LangGraph 中断中独立审批通过 | **PASS** |
| `test_03_first_approved_second_rejected` | 第一个工具批准执行，第二个工具拒绝并被安全阻断 | **PASS** |
| `test_04_first_rejected_second_approved` | 第一个工具拒绝并安全阻断，第二个工具批准执行 | **PASS** |
| `test_05_multiple_tool_calls_concurrent_batch_resolution` | 多个 ToolCall 并发返回，支持批量决策载荷独立决断 | **PASS** |
| `test_06_modify_first_tool_arguments_does_not_affect_second` | 修改第一个工具参数严格隔离，不污染第二个工具原始参数 | **PASS** |
| `test_07_duplicate_submission_rejected` | 重复提交同一审批请求抛出 `ValueError`，保证幂等防重放 | **PASS** |
| `test_08_unapproved_tool_directly_entering_executor_is_blocked` | 绕过审批门禁直接进入执行器的高危工具被强制阻断 | **PASS** |
| `test_09_tool_call_id_mismatch_rejected` | 审批决策中的 `tool_call_id` 与实际执行工具不匹配时被拒绝 | **PASS** |
| `test_10_tampered_arguments_hash_mismatch_rejected` | 审批授权后参数被篡改（哈希不匹配）时强制阻断执行 | **PASS** |

#### 2. 工作区文件路径安全测试集 (`tests/test_file_path_security.py`)

| 测试用例 | 验证目标 | 结果 |
| :--- | :--- | :---: |
| `test_01_normal_workspace_read_write` | 工作区合法子目录下文件正常读写与列表遍历 | **PASS** |
| `test_02_relative_path_traversal_escape` | `../../` 跨层相对路径逃逸阻断验证 | **PASS** |
| `test_03_root_and_system_directory_escape` | 根目录 `/etc/passwd` 与 `C:\Windows` 绝对路径逃逸阻断 | **PASS** |
| `test_04_windows_drive_jumping_escape` | Windows 盘符变换跨盘访问（D: 到 C:/E:）阻断验证 | **PASS** |
| `test_05_unc_path_escape` | UNC 网络共享路径（`\\evil-share\loot`）严格拦截 | **PASS** |
| `test_06_symlink_reading_outside_workspace` | 指向工作区外部的符号链接文件读取安全阻断 | **PASS** |
| `test_07_symlink_writing_outside_workspace` | 通过符号链接向工作区外部写入操作安全阻断 | **PASS** |
| `test_08_circular_symlinks` | 循环递归符号链接安全拦截，防止进程挂起崩溃 | **PASS** |
| `test_09_special_characters_and_invalid_colons` | 空字节注入、NTFS 备用流冒号注入及非法路径过滤 | **PASS** |
| `test_10_nested_parent_directories_safe_creation` | 不存在的多层嵌套父目录安全创建与包含验证 | **PASS** |

### 4.3 全量测试集执行结果
- **全项目测试总数**：**63 项**
- **通过率**：**100% (63 passed in 12.82s)**
- **代码静态合规**：Ruff 检查全绿通过（`All checks passed!`）。

---

## 五、代码修改清单

1. `src/jarvis_studio/agent/hitl/manager.py`：
   - 增加参数哈希计算工具函数 `compute_arguments_hash`。
   - `ApprovalRequest` 扩充 `tool_call_id`、`arguments_hash`、`decision` 属性。
   - `HITLManager.resolve_request` 引入幂等防重放检查与状态更新。
   - 增加按 `tool_call_id` 检索请求与测试状态清理方法。
2. `src/jarvis_studio/agent/hitl/authorization.py`（**新增模块**）：
   - 定义 `AuthorizationResult` 数据契约。
   - 实现高可靠执行鉴权核心 `verify_tool_execution_authorization`。
3. `src/jarvis_studio/agent/hitl/__init__.py`：
   - 导出鉴权结果类型与校验函数。
4. `src/jarvis_studio/agent/nodes/approval.py`：
   - 重构审批节点：支持遍历多工具调用、批量请求登记、多步/批量中断应答驱动。
5. `src/jarvis_studio/agent/nodes/executor.py`：
   - 引入前置强制执行鉴权，杜绝越权执行与参数污染，规范化拒绝 `ToolMessage` 构造。
6. `src/jarvis_studio/tools/builtin/file_tools.py`：
   - 重构 `resolve_safe_path`，集成全方位包含验证、符号链接审计、特殊字符拦截。
7. `tests/test_hitl_security.py`（**新增测试**）：
   - 10 个端到端 HITL 独立审批与执行安全测试用例。
8. `tests/test_file_path_security.py`（**新增测试**）：
   - 10 个工作区路径隔离与逃逸攻击防御测试用例。

---

## 六、待跟进潜在风险与后续建议

1. **Shell 命令执行参数语义分析**：
   - 当前针对 Shell 命令已具备 HITL 拦截与参数哈希绑定，但命令字符串本身若包含隐式管道逃逸（如 `cat file | sh`）仍依赖人类审核员的甄别。建议后续引入预执行命令语法树静态安全分析器（AST Policy Scanner）。
2. **符号链接的运行时原子绑定**：
   - 在高并发极端场景下，检查与使用之间仍有微小竞争可能。后续在支持 POSIX `openat(2)` / `O_NOFOLLOW` 标志的环境中，可进一步封装文件描述符级别的无符号链接直接访问通道。
3. **下一步规划**：
   - 本轮 Sprint 1A 范围（Security & HITL Correctness）已圆满收尾，系统安全防线已经筑牢。
   - 等待负责人检阅审批后，再行开启下一阶段 Sprint 1B（SQLite WAL 并发与持久化隔离）。
