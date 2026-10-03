# Configuration Reference

Jarvis Studio utilizes a layered configuration architecture managed by `pydantic-settings`:
1. **Environment Variables (`.env`)**: Highest priority. Used for secrets, endpoints, and deployment overrides.
2. **YAML Settings (`config/settings.yaml`)**: Medium priority. Used for runtime defaults and system thresholds.
3. **Pydantic Defaults (`src/jarvis_studio/config/models.py`)**: Fallback values.

---

## 1. Environment Variables (`.env`)

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | string | `development` | Environment name (`development`, `production`, `test`) |
| `APP_HOST` | string | `0.0.0.0` | Host IP for FastAPI gateway |
| `APP_PORT` | integer | `8000` | Port for FastAPI gateway |
| `LOG_LEVEL` | string | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DEFAULT_LLM_PROVIDER` | string | `openai` | Default provider: `openai`, `deepseek`, `ollama`, `custom`, `anthropic`, `gemini` |
| `DEFAULT_MODEL_NAME` | string | `gpt-4o-mini` | Default model name for the selected provider |
| `OPENAI_API_KEY` | string | `None` | API Key for OpenAI services |
| `OPENAI_BASE_URL` | string | `https://api.openai.com/v1` | OpenAI API Base URL |
| `DEEPSEEK_API_KEY` | string | `None` | API Key for DeepSeek services |
| `DEEPSEEK_BASE_URL` | string | `https://api.deepseek.com/v1` | DeepSeek API Base URL |
| `DEEPSEEK_MODEL_NAME` | string | `deepseek-chat` | Default DeepSeek model name |
| `OLLAMA_BASE_URL` | string | `http://localhost:11434/v1` | Local Ollama base endpoint |
| `OLLAMA_MODEL_NAME` | string | `llama3.1:8b` | Default Ollama model name |
| `ANTHROPIC_API_KEY` | string | `None` | API Key for Anthropic Claude |
| `GOOGLE_API_KEY` | string | `None` | API Key for Google Gemini |
| `CHECKPOINT_BACKEND` | string | `sqlite` | Checkpointer backend: `sqlite` or `memory` |
| `SQLITE_DB_PATH` | string | `data/jarvis_studio.db` | Path to SQLite database for state persistence |
| `WORKSPACE_ROOT` | string | `./data/workspace` | Base directory for safe file and shell operations |
| `HITL_ENABLED` | boolean | `true` | Enforce human confirmation for dangerous tools |
| `LANGSMITH_TRACING` | boolean | `false` | Enable LangSmith tracing export |

---

## 2. YAML Settings (`config/settings.yaml`)

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

## 3. MCP Server Configuration (`config/mcp_servers.json`)

Defines external Model Context Protocol servers to be managed by `MCPClientManager`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data/workspace"],
      "enabled": false,
      "description": "Local filesystem access via MCP"
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"],
      "enabled": false,
      "description": "Web fetch and content extraction via MCP"
    },
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./data/jarvis_studio.db"],
      "enabled": false,
      "description": "Direct database query access via MCP"
    }
  }
}
```
