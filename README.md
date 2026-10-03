# Jarvis Studio

> **Personal Production AI Operating Assistant**  
> Built for long-term real-world utility, continuous extensibility, and daily engineering/knowledge workflows.

---

## 1. Project Background & Evolution

Jarvis Studio is the culmination of iterative learning and exploration across personal agent architectures:

| Project | Core Tech | Objective | Status |
| :--- | :--- | :--- | :--- |
| **Jarvis Agent** | Custom Python Runtime | Deep understanding of Agent Loop & ReAct internals | Complete (Archived) |
| **Jarvis Agent Plus** | LangChain Core | Framework abstraction and tool binding mechanics | Separate Project |
| **Jarvis Agent Pro** | LangGraph | Graph-based state machine orchestration | Separate Project |
| **Jarvis Studio** | **LangGraph + LangChain + MCP** | **Production personal AI assistant for daily engineering use** | **Active Project** |

Jarvis Studio does **not** reinvent the runtime. It leverages mature, production-grade frameworks to deliver reliability, safety, and extensibility.

---

## 2. Core Architectural Principles

1. **Product First**: Every feature solves a real problem in daily work, coding, research, or system automation.
2. **Don't Reinvent the Runtime**: Uses official LangGraph, LangChain, and MCP protocols.
3. **Decoupled Architecture**: Strict 5-tier separation:
   ```text
   UI  ──▶  Application API  ──▶  Agent Service  ──▶  LangGraph Runtime  ──▶  Tools / MCP
   ```
4. **Swappable LLM Adapters**: First-class support for OpenAI-compatible APIs, DeepSeek, Anthropic Claude, Google Gemini, and local Ollama without hardcoded provider dependencies.
5. **Tool First**: Agent capabilities are extended through modular tools, not endless prompt bloat.
6. **MCP First-Class**: Standardized integration with external tools and ecosystems via Model Context Protocol (MCP).
7. **Human-in-the-Loop (HITL)**: High-risk operations (shell execution, destructive writes, git push) pause via LangGraph checkpoints for user approval/modification.
8. **End-to-End Observability**: Tracing across Session ID, Run ID, Graph Node, Tool Execution, Latency, and Token Consumption.

---

## 3. Directory Layout

```text
JarvisStudio/
├── docs/                   # System design, architecture, HITL, and roadmaps
├── config/                 # YAML and MCP server configurations
├── src/jarvis_studio/
│   ├── config/             # Pydantic Settings & environment validation
│   ├── core/
│   │   ├── llm/            # Multi-provider LLM adapter factory
│   │   ├── observability/  # Tracing, run IDs, and structured logging
│   │   └── errors.py       # Custom typed domain exceptions
│   ├── tools/
│   │   ├── base.py         # Tool metadata, risk classification
│   │   ├── registry.py     # Dynamic Tool Registry
│   │   ├── builtin/        # Shell, File, and Web tools
│   │   └── mcp/            # Model Context Protocol client manager & adapter
│   ├── agent/
│   │   ├── state.py        # LangGraph AgentState definitions
│   │   ├── graph.py        # StateGraph builder & orchestrator
│   │   ├── nodes/          # Assistant, Router, Approval, and Tool nodes
│   │   ├── checkpoints/    # SQLite and Memory checkpointers
│   │   └── hitl/           # Human-in-the-loop approval management
│   ├── service/            # Application service layer (AgentService, SessionService)
│   ├── api/                # FastAPI routes, schemas, and SSE streaming
│   └── cli/                # Interactive developer CLI
├── tests/                  # Unit and integration test suite
├── .env.example            # Environment configuration template
└── pyproject.toml          # uv / PEP 621 project configuration
```

---

## 4. Quickstart

### Prerequisites
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) package manager

### Installation
```bash
# Clone the repository
git clone <repo-url>
cd JarvisStudio

# Install all dependencies (including dev tools)
uv sync --extra dev

# Copy environment configuration
cp .env.example .env
```

### Running Tests
```bash
uv run pytest
```

### Starting the Server
```bash
uv run uvicorn jarvis_studio.api.app:create_app --factory --reload --port 8000
```
