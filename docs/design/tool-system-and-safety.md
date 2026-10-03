# 工具子系统与安全分类矩阵 (Tool System & Safety Matrix)

## 1. 设计概述

在 Jarvis Studio 中，工具是扩展智能体核心能力的首要途径（**原则 5：Tool First**）。我们坚决避免把所有业务处理逻辑全部塞进冗长脆弱的 Prompt，而是将每一项能力封装为类型安全、具备独立单元测试的高内聚工具。

所有工具由全局 `ToolRegistry` 统一注册与管理，并在绑定给大模型前强制附加显式的安全元数据。

---

## 2. 工具安全元数据契约 (Safety Contract)

Jarvis Studio 内的每一个工具必须通过 `tag_tool_safety` 标注安全元数据：

```python
class RiskLevel(str, Enum):
    SAFE = "SAFE"        # 只读操作，零副作用，不改变任何系统状态
    MEDIUM = "MEDIUM"    # 局部、临时或可逆的写入变动
    HIGH = "HIGH"        # 破坏性修改、文件覆盖/删除、终端执行、网络写请求

class ToolMetadata(BaseModel):
    name: str
    description: str
    risk_level: RiskLevel = RiskLevel.SAFE
    requires_approval: bool = False
    category: str = "general"
    tags: list[str] = []
```

### 拦截策略
- 任何标记为 `RiskLevel.HIGH` 或 `requires_approval=True` 的工具调用，都会被 `approval_gate` 节点强制中断并持久化挂起。
- 动态加载或未被安全标注的未知工具，默认按高危策略对待，强制触发人工确认。

---

## 3. 内置高频工具清单

### 3.1 文件系统工具 (`src/jarvis_studio/tools/builtin/file_tools.py`)

#### `read_file` (读取文件)
- **业务分类**: `filesystem`
- **风险等级**: `RiskLevel.SAFE`（自主自动执行）
- **入参规范**:
  - `file_path` (str, 必填): 目标文件路径（支持绝对路径或相对于工作区的相对路径）。
  - `max_chars` (int, 默认 20000): 上下文安全截断上限，防止大文件爆掉 LLM 上下文。
- **安全沙箱**: 路径基于 `settings.storage.workspace_root` 自动校验解析。

#### `write_file` (写入文件)
- **业务分类**: `filesystem`
- **风险等级**: `RiskLevel.HIGH`（**强制触发 HITL 人机协同审查**）
- **入参规范**:
  - `file_path` (str, 必填): 写入目标路径。
  - `content` (str, 必填): 待写入的文本内容。
  - `overwrite` (bool, 默认 True): 是否允许覆盖已存在的文件。

#### `list_directory` (列出目录)
- **业务分类**: `filesystem`
- **风险等级**: `RiskLevel.SAFE`（自主自动执行）
- **入参规范**:
  - `directory_path` (str, 默认 "."): 目标目录路径。
- **输出格式**: 结构化显示子条目类型（`[DIR]` 或 `[FILE]`）及文件字节大小。

---

### 3.2 系统与终端工具 (`src/jarvis_studio/tools/builtin/shell_tools.py`)

#### `execute_shell_command` (执行终端命令)
- **业务分类**: `system`
- **风险等级**: `RiskLevel.HIGH`（**强制触发 HITL 人机协同审查**）
- **入参规范**:
  - `command` (str, 必填): 待执行的终端命令字符串。
  - `cwd` (str, 默认 "."): 命令执行的工作目录。
  - `timeout_seconds` (int, 默认 60): 超时熔断秒数，防止命令无限阻塞。
  - `max_output_chars` (int, 默认 15000): 标准输出与标准错误的合并截断上限。
- **跨平台适配**: Windows 平台自动采用 PowerShell 执行，Linux / macOS 平台采用 `/bin/bash`。

---

## 4. Model Context Protocol (MCP) 动态适配桥接

Jarvis Studio 将 MCP 视作一等公民工具集成标准（**原则 6：MCP First-Class**）。

### 4.1 协议动态映射架构
外部 MCP Server（无论是 stdio 还是 SSE）均通过 JSON Schema 对外暴露工具规范。`tools/mcp` 模块实现了全自动的动态类型转换：

```mermaid
flowchart LR
    MCP_SRV["外部 MCP Server"] -->|JSON Schema 描述| MCP_CLIENT["MCPClientManager"]
    MCP_CLIENT -->|提取工具定义| ADAPTER["MCPToolAdapter"]
    ADAPTER -->|create_dynamic_args_schema| PYDANTIC["动态强类型 Pydantic 模型"]
    PYDANTIC --> LANGCHAIN_TOOL["LangChain BaseTool 实例"]
    LANGCHAIN_TOOL --> REGISTRY["ToolRegistry"]
    REGISTRY --> AGENT["LangGraph StateGraph"]
```

### 4.2 强类型自动构建原理
`create_dynamic_args_schema` 函数在运行时解析 MCP 工具的入参模式：
- 将 JSON Schema 中的 `string`, `integer`, `number`, `boolean`, `array`, `object` 动态映射为 Python 类型。
- 提取 `required` 字段设定必填约束。
- 动态调用 Pydantic `create_model` 生成参数模型，使 MCP 工具能够原生支持 LangChain 的结构化参数绑定与大模型 Function Calling。
