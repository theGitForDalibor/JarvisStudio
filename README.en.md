# Jarvis Studio

<div align="center">

**Personal Production AI Operating Assistant**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2+-blueviolet.svg)](https://github.com/langchain-ai/langgraph)
[![LangChain](https://img.shields.io/badge/LangChain-1.4+-1C3C3C.svg)](https://github.com/langchain-ai/langchain)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-2.3+-orange.svg)](https://modelcontextprotocol.io/)
[![sqlite-vec](https://img.shields.io/badge/sqlite--vec-0.1.9-purple.svg)](https://github.com/asg017/sqlite-vec)
[![Tests](https://img.shields.io/badge/Tests-35%20Passed-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<p align="center">
  <a href="README.md">简体中文</a> | <b>English</b>
</p>

</div>

---

## 1. Overview

### What is Jarvis Studio?
**Jarvis Studio** is a personal AI Operating Assistant designed for daily engineering, file management, system automation, and knowledge retrieval. 

It runs locally or as a personal background service, executing instructions through modular tools, connected [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers, local vector retrieval (`sqlite-vec`), and a persistent cross-session memory system.

### Why does it exist?
Jarvis Studio represents the evolution from educational prototypes to a reliable production assistant:

| Project | Foundation | Purpose | Status |
| :--- | :--- | :--- | :--- |
| **Jarvis Agent** | Custom Python Runtime | Deep exploration of ReAct and loop internals | Archived |
| **Jarvis Agent Plus** | LangChain Core | Framework abstraction study | Separate Project |
| **Jarvis Agent Pro** | LangGraph | Graph-based state machine orchestration | Separate Project |
| **Jarvis Studio** | **LangGraph + LangChain + MCP + sqlite-vec** | **Reliable, personal AI assistant for daily work** | **Active Project** |

> **Design Tenet**: Do not reinvent the agent runtime. Leverage mature, battle-tested frameworks (**LangGraph**, **LangChain**, and **MCP**) to focus on real utility, safety, and personal extensibility.

---

## 2. Core Capabilities (Implemented)

- **Decoupled 5-Tier Architecture**: Clean separation across API Gateway, Application Service, Agent Runtime, Tools/MCP, and LLM Adapters.
- **Swappable LLM Adapters**: Zero vendor lock-in. Switch between OpenAI, DeepSeek, local Ollama, and custom endpoints via configuration without altering graph code.
- **Human-in-the-Loop (HITL) Safety**: Dangerous tools (`execute_shell_command`, `write_file`) automatically trigger LangGraph `interrupt()`, snapshotting state to SQLite for human review (`approve`, `reject`, `modify`).
- **Long-Term Memory (LTM)**: Persistent storage for developer preferences, profiles, and key facts across sessions, dynamically injected into the Assistant prompt.
- **Local RAG Knowledge Base**: High-performance vector storage powered by `sqlite-vec` (zero external service dependencies), with incremental workspace scanning, semantic splitting, and KNN search.
- **Resilient Rejection Protocol**: User rejections generate valid `ToolMessage` payloads, allowing the LLM to replan gracefully without breaking API schema contracts.
- **Tool System & Safety Matrix**: Central `ToolRegistry` enforces `RiskLevel` boundaries (`SAFE`, `MEDIUM`, `HIGH`).
- **Dynamic MCP Bridge**: Converts external MCP tool JSON Schemas dynamically into strongly-typed LangChain `BaseTool` models at runtime.
- **Durable Checkpointing**: Powered by `langgraph-checkpoint-sqlite` for state serialization and crash-resilient session resumption.
- **End-to-End Tracing**: Contextual `session_id` and `run_id` correlation, execution latency measurement (`timed_step`), and structured logging with Loguru.
- **Dual Interfaces**: Native developer CLI (`jarvis-studio info`, `serve`, `chat`, `memory`, `rag`, `sessions`) and FastAPI REST gateway (`/chat`, `/approvals`, `/system`, `/memory`, `/rag`).

---

## 3. Architecture Snapshot

```mermaid
flowchart TD
    subgraph UI ["User Interfaces"]
        CLI["Developer CLI (jarvis-studio)"]
        CLIENT["REST / Web Client"]
    end

    subgraph API ["1. Application API Gateway (FastAPI)"]
        ENDPOINT_CHAT["/api/v1/chat"]
        ENDPOINT_APPR["/api/v1/approvals"]
        ENDPOINT_SYS["/api/v1/system"]
        ENDPOINT_MEM["/api/v1/memory"]
        ENDPOINT_RAG["/api/v1/rag"]
    end

    subgraph SVC ["2. Application Service Layer"]
        AGENT_SVC["AgentService"]
        SESS_SVC["SessionService"]
        MEM_MGR["MemoryManager (LTM)"]
    end

    subgraph RUNTIME ["3. LangGraph Runtime (StateGraph)"]
        ASST["Assistant Node"]
        GATE["Approval Gate (HITL interrupt)"]
        EXEC["Tool Executor Node"]
        CHECKPOINTER[("SqliteSaver (data/jarvis_studio.db)")]
    end

    subgraph CORE ["4. Multi-Provider LLM & 5. Tools/RAG"]
        LLM["LLMFactory (OpenAI / DeepSeek / Ollama)"]
        REGISTRY["ToolRegistry (Built-in + MCP + Memory/RAG)"]
        VECSTORE[("SqliteVecStore (sqlite-vec)")]
    end

    CLI --> AGENT_SVC
    CLIENT --> API --> AGENT_SVC
    AGENT_SVC --> SESS_SVC
    AGENT_SVC --> RUNTIME
    MEM_MGR -. Memory Injection .-> ASST
    ASST --> GATE --> EXEC --> ASST
    RUNTIME -. Checkpoint Persistence .-> CHECKPOINTER
    ASST --> LLM
    GATE & EXEC --> REGISTRY
    REGISTRY --> VECSTORE
```

---

## 4. Project Structure

```text
JarvisStudio/
├── docs/                        # Complete technical documentation hub
│   ├── architecture/            # 5-tier architecture & state machine execution flow
│   ├── design/                  # HITL specs, safety classification & LLM adapter designs
│   ├── development/             # Environment setup, testing standards & config reference
│   ├── adr/                     # Architectural Decision Records (ADR-001 ~ ADR-003)
│   └── roadmap/                 # Engineering roadmap from M0 to M5
├── config/                      # YAML runtime config & MCP server registry
├── src/jarvis_studio/
│   ├── api/                     # FastAPI application, routers & schemas
│   ├── service/                 # AgentService & SessionService orchestrators
│   ├── agent/                   # LangGraph StateGraph, nodes, HITL & checkpointing
│   ├── memory/                  # Long-Term Memory (LTM: Store, Manager, Models)
│   ├── rag/                     # Local RAG (sqlite-vec Store, Embeddings, Indexer, Splitter)
│   ├── tools/                   # ToolRegistry, file/shell/memory/RAG tools, MCP adapter
│   ├── core/                    # LLMFactory, Loguru logging, tracing & errors
│   ├── config/                  # Pydantic Settings configuration loader & models
│   └── cli/                     # Click developer terminal commands
├── tests/                       # Automated test suite (35 integration & unit tests)
├── .env.example                 # Environment configuration template
└── pyproject.toml               # uv / PEP 621 dependencies & project metadata
```

---

## 5. Quickstart

### Prerequisites
- Python `3.12+`
- [`uv`](https://docs.astral.sh/uv/) modern Python package manager

### 1. Installation
```bash
# Clone the repository
git clone <repo-url>
cd JarvisStudio

# Install all dependencies with uv
uv sync --extra dev

# Copy environment template
cp .env.example .env
```

### 2. Configure LLM Credentials
Edit `.env` and set your credentials:
```bash
DEFAULT_LLM_PROVIDER=openai
OPENAI_API_KEY=your_api_key_here

# Or use DeepSeek
# DEFAULT_LLM_PROVIDER=deepseek
# DEEPSEEK_API_KEY=your_deepseek_key

# Or use local Ollama
# DEFAULT_LLM_PROVIDER=ollama
# OLLAMA_MODEL_NAME=llama3.1:8b
```

### 3. Run Automated Tests
```bash
uv run --extra dev pytest
```

### 4. Run Interfaces
```bash
# Inspect system status and registered tools
uv run jarvis-studio info

# Start FastAPI API Gateway (Swagger docs at http://localhost:8000/docs)
uv run jarvis-studio serve --port 8000 --reload

# Start developer terminal chat session
uv run jarvis-studio chat

# Manage Long-Term Memory (LTM)
uv run jarvis-studio memory list
uv run jarvis-studio memory add "Prefer Python 3.12 with uv" --category preference

# Local RAG Knowledge Base
uv run jarvis-studio rag index ./docs
uv run jarvis-studio rag search "HITL approval workflow"
```

---

## 6. Documentation Hub

Detailed technical documentation is available under [`docs/`](docs/README.md):

| Document | Key Topics |
| :--- | :--- |
| **[System Architecture](docs/architecture/system-architecture.md)** | 5-tier decoupled design, module boundaries & topology |
| **[Execution Lifecycle](docs/architecture/execution-lifecycle.md)** | StateGraph lifecycle, checkpoint snapshots & state transitions |
| **[HITL Specification](docs/design/hitl-specification.md)** | Tool interception, approval state machine & REST API contracts |
| **[Tool System & Safety](docs/design/tool-system-and-safety.md)** | `RiskLevel` matrix, built-in tools & dynamic MCP bridge |
| **[LLM Adapter Layer](docs/design/llm-adapter-layer.md)** | `LLMFactory`, provider hot-swapping & fallback strategies |
| **[Configuration Reference](docs/development/configuration-reference.md)** | Full parameter dictionary for `.env`, `settings.yaml`, and MCP |
| **[Setup & Development](docs/development/setup-and-development.md)** | Developer onboarding, testing rules & code quality standards |
| **[Architecture Decisions (ADRs)](docs/adr/)** | Accepted architectural decisions (LangGraph, Decoupling, HITL) |
| **[Engineering Roadmap](docs/roadmap/engineering-roadmap.md)** | Milestone schedule from M0 (Scaffolding) to M5 (Production UI) |

---

## 7. Engineering Roadmap Summary

- **Milestone 0: Architecture Baseline & Scaffolding** `[Completed]`  
  5-tier decoupled architecture, Python 3.12 + `uv` environment, automated test scaffold.
- **Milestone 1: Core LangGraph Agent & Multi-Provider Engine** `[Completed]`  
  `LLMFactory` (OpenAI, DeepSeek, Ollama), `AgentState` schema, basic reasoning loop.
- **Milestone 2: Tool Subsystem & MCP Client Integration** `[Completed]`  
  `ToolRegistry`, built-in file/shell tools, dynamic MCP schema converter, stdio/SSE sub-process manager.
- **Milestone 3: Human-in-the-Loop & Execution Checkpoints** `[Completed]`  
  LangGraph `interrupt()`, SQLite checkpointing, approval REST API, interactive terminal prompts.
- **Milestone 4: Personal Memory & Local Knowledge Base (RAG)** `[Completed]`  
  Long-Term Memory (LTM), dynamic context injection, local vector store with `sqlite-vec`, incremental workspace file indexer.
- **Milestone 5: Application Gateway, Streaming & User Interface** `[Planned]`  
  Server-Sent Events (SSE) token streaming, Web UI frontend (Chat + HITL Review Center), containerization.

---

## 8. License

This project is licensed under the [MIT License](LICENSE).
