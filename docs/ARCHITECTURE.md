# Jarvis Studio - System Architecture Specification

## 1. Executive Summary & Positioning

**Jarvis Studio** is a personal production-grade AI Operating Assistant.

Unlike previous exploratory iterations:
- **Jarvis Agent** (custom Python runtime for learning internal mechanics)
- **Jarvis Agent Plus** (LangChain abstraction study)
- **Jarvis Agent Pro** (LangGraph graph orchestration exploration)

**Jarvis Studio** is engineered from the ground up for real-world personal utility:
- Built on top of official, battle-tested modern foundations: **LangGraph + LangChain + Model Context Protocol (MCP)**.
- Strict decoupling: UI ↔ Application API ↔ Agent Service ↔ LangGraph Runtime ↔ Tools/MCP.
- Zero vendor lock-in with a swappable LLM Adapter layer.
- First-class Human-in-the-loop (HITL) safety boundaries for dangerous operations.
- Deep observability with correlation IDs, token metrics, and run traces.

---

## 2. Layered Architecture Blueprint

```text
+-----------------------------------------------------------------------+
|                             USER INTERFACES                           |
|      Web UI (Next.js / Vue)  |  CLI Client  |  Desktop / Raycast      |
+-----------------------------------+-----------------------------------+
                                    | (HTTP / SSE / WebSocket)
                                    v
+-----------------------------------------------------------------------+
|                         APPLICATION API GATEWAY                       |
|           FastAPI Routes: /chat, /tasks, /approvals, /system          |
|      Auth / Session Middleware | Streaming Handlers | Error Mappers   |
+-----------------------------------+-----------------------------------+
                                    |
                                    v
+-----------------------------------------------------------------------+
|                         AGENT SERVICE LAYER                           |
|       AgentService (Execution, Resume, Interrupt Resolution)          |
|       SessionService (Thread state, Checkpointer persistence)        |
+-----------------------------------+-----------------------------------+
                                    |
                                    v
+-----------------------------------------------------------------------+
|                         LANGGRAPH RUNTIME                             |
|    StateGraph: [Assistant] -> [Approval Gate] -> [Tool Node] -> ...   |
|         State Checkpointer (SqliteSaver / MemorySaver)                |
|             LangGraph interrupt() & resume() Mechanics                |
+-----------------------------------+-----------------------------------+
                                    |
            +-----------------------+-----------------------+
            |                                               |
            v                                               v
+-----------------------+                       +-----------------------+
|   LLM PROVIDER LAYER  |                       |     TOOL SUBSYSTEM    |
| (Swappable Adapters)  |                       | Dynamic Tool Registry |
|  - OpenAI             |                       | Built-in Tools:       |
|  - DeepSeek           |                       |  - Filesystem         |
|  - Google Gemini      |                       |  - Shell (HITL)       |
|  - Anthropic          |                       |  - Web Fetch          |
|  - Ollama / Local     |                       +-----------+-----------+
+-----------------------+                                   |
                                                            v
                                                +-----------------------+
                                                |     MCP CLIENT HUB    |
                                                | Model Context Protocol|
                                                |  - Filesystem MCP     |
                                                |  - Database MCP       |
                                                |  - External API MCP   |
                                                +-----------------------+
```

---

## 3. Core Subsystems

### 3.1 LLM Provider Adapter (`core/llm`)
- **Factory Pattern**: `LLMFactory.create_model(config)` generates an initialized, tool-bindable `BaseChatModel`.
- **Protocol Neutrality**: All LLM calls pass through standardized parameters (`temperature`, `streaming`, `model_name`, `api_base`).
- **Seamless Provider Switch**: Switching from OpenAI to DeepSeek, Anthropic, or Ollama requires only an environment or runtime parameter change.

### 3.2 LangGraph Agent Engine (`agent/`)
- **State Definition (`AgentState`)**:
  - `messages`: Sequence of `BaseMessage` (User, AI, Tool, System).
  - `pending_approval`: Holds approval requests when a high-risk tool is intercepted.
  - `run_context`: Metadata tracking `session_id`, `run_id`, `step_count`, and user preferences.
- **Workflow Graph**:
  1. `assistant`: Invokes the LLM with bound tools.
  2. `router`: Analyzes whether tool calls were generated and whether any tool requires human confirmation.
  3. `approval_gate`: If high-risk tools are called, pauses execution using `langgraph.types.interrupt`.
  4. `tool_executor`: Runs authorized tools and formats `ToolMessage` outputs.

### 3.3 Human-in-the-Loop (HITL) Mechanism (`agent/hitl`)
- Operations such as `execute_shell_command`, destructive file modifications, and external write operations are annotated with `requires_approval = True`.
- When triggered:
  - Graph pauses execution at the interrupt checkpoint.
  - An approval event is recorded with execution parameters and diff summary.
  - The user can issue `APPROVE`, `REJECT`, or `MODIFY` via API `/api/v1/approvals/{task_id}/decision`.
  - The graph resumes with the user's decision applied.

### 3.4 MCP Integration Hub (`tools/mcp`)
- First-class client integration using the official `mcp` Python SDK.
- Reads `config/mcp_servers.json` to manage stdio and SSE MCP server processes.
- Converts MCP Tool schemas dynamically into standard LangChain `BaseTool` instances.

### 3.5 Observability & Structured Tracing (`core/observability`)
- Every execution request is stamped with a unique `run_id` and correlated with a `session_id`.
- Structured logging powered by `loguru` records node inputs, outputs, tool latency, and token consumption.
- Ready for native LangSmith and OpenTelemetry instrumentation.

---

## 4. Execution Lifecycle

```text
User Request
    │
    ▼
FastAPI Route (/chat)
    │
    ▼
AgentService.run_agent()
    │
    ├─> Checkpointer loads state for thread_id
    │
    ├─> Assistant Node (LLM produces ToolCall)
    │
    ├─> Router checks tool risk level
    │     ├── If Safe Tool:
    │     │     └─> ToolExecutor runs tool ──> Back to Assistant
    │     │
    │     └── If High-Risk Tool (HITL):
    │           └─> LangGraph interrupt() triggers
    │           └─> State persisted in SQLite
    │           └─> Returns PENDING_APPROVAL response to Client
    │
Client submits Approval (/approvals/decision)
    │
    ▼
AgentService.resume_approval()
    │
    ├─> Checkpointer restores state
    ├─> Approved: ToolExecutor runs tool ──> Assistant continues
    └─> Rejected: Feedback returned to Assistant ──> Assistant adjusts plan
```
