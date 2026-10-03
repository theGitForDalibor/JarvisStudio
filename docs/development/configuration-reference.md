# 配置全参数参考手册 (Configuration Reference)

Jarvis Studio 采用基于 `pydantic-settings` 的分层配置管理架构：
1. **环境变量 (`.env`)**：最高优先级，用于存储密钥、端口与环境覆盖项。
2. **YAML 配置文件 (`config/settings.yaml`)**：中优先级，用于系统默认运行时参数。
3. **Pydantic 模型默认值 (`src/jarvis_studio/config/models.py`)**：底层兜底缺省值。

---

## 1. 环境变量参数字典 (`.env`)

| 变量名称 | 类型 | 默认值 | 详细说明 |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | string | `development` | 运行环境标识 (`development`, `production`, `test`) |
| `APP_HOST` | string | `0.0.0.0` | FastAPI 应用网关监听 IP |
| `APP_PORT` | integer | `8000` | FastAPI 应用网关监听端口 |
| `LOG_LEVEL` | string | `INFO` | 全局日志级别 (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DEFAULT_LLM_PROVIDER` | string | `openai` | 默认模型提供商 (`openai`, `deepseek`, `ollama`, `custom`, `anthropic`, `gemini`) |
| `DEFAULT_MODEL_NAME` | string | `gpt-4o-mini` | 默认使用的模型名称 |
| `OPENAI_API_KEY` | string | `None` | OpenAI API 鉴权密钥 |
| `OPENAI_BASE_URL` | string | `https://api.openai.com/v1` | OpenAI API 请求端点基地址 |
| `DEEPSEEK_API_KEY` | string | `None` | DeepSeek API 鉴权密钥 |
| `DEEPSEEK_BASE_URL` | string | `https://api.deepseek.com/v1` | DeepSeek API 请求端点基地址 |
| `DEEPSEEK_MODEL_NAME` | string | `deepseek-chat` | DeepSeek 模型标识符 |
| `OLLAMA_BASE_URL` | string | `http://localhost:11434/v1` | 本地 Ollama OpenAI 兼容端点 |
| `OLLAMA_MODEL_NAME` | string | `llama3.1:8b` | 本地 Ollama 模型标识符 |
| `ANTHROPIC_API_KEY` | string | `None` | Anthropic Claude API 鉴权密钥 |
| `GOOGLE_API_KEY` | string | `None` | Google Gemini API 鉴权密钥 |
| `CHECKPOINT_BACKEND` | string | `sqlite` | 状态检查点后端 (`sqlite` 持久化，或 `memory` 内存隔离) |
| `SQLITE_DB_PATH` | string | `data/jarvis_studio.db` | SQLite 数据库文件落盘路径 |
| `WORKSPACE_ROOT` | string | `./data/workspace` | 文件与终端工具操作的安全基准根目录 |
| `HITL_ENABLED` | boolean | `true` | 是否对高危工具启用强制人工审查 |
| `LANGSMITH_TRACING` | boolean | `false` | 是否开启 LangSmith 远程分布式追踪导出 |

---

## 2. 默认 YAML 配置清单 (`config/settings.yaml`)

```yaml
app:
  name: "Jarvis Studio"
  version: "0.1.0"
  description: "Personal Production AI Operating Assistant"

llm:
  default_provider: "openai"
  default_model: "gpt-4o-mini"
  temperature: 0.2
  streaming: true

security:
  hitl_enabled: true
  dangerous_tools:
    - "execute_shell_command"
    - "write_file"
    - "delete_file"
    - "git_push"

storage:
  checkpoint_backend: "sqlite"
  sqlite_path: "data/jarvis_studio.db"
  workspace_root: "./data/workspace"

observability:
  enable_rich_logging: true
  log_level: "INFO"
```

---

## 3. 外部 MCP 服务注册表 (`config/mcp_servers.json`)

用于定义由 `MCPClientManager` 统一管理的外部 Model Context Protocol 服务的配置规范：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data/workspace"],
      "enabled": false,
      "description": "通过 MCP 协议访问本地文件系统"
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"],
      "enabled": false,
      "description": "通过 MCP 协议实现网页抓取与提取"
    },
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./data/jarvis_studio.db"],
      "enabled": false,
      "description": "通过 MCP 协议直接执行数据库查询"
    }
  }
}
```
