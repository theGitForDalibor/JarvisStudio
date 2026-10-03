# Jarvis Studio - Engineering & Product Roadmap

## Milestone Overview

```text
  M0: Baseline Scaffolding ──▶ M1: Core LangGraph Agent ──▶ M2: Tool Registry & MCP
                                                                     │
  M5: Production & UI     ◀── M4: Memory & RAG         ◀── M3: HITL & Checkpointing
```

---

### Milestone 0: Architecture Baseline & Project Scaffolding (Current)
- [x] Establish architectural blueprint and layered design.
- [x] Configure modern Python 3.12 + `uv` environment.
- [x] Pin dependencies: `langgraph`, `langchain`, `fastapi`, `pydantic-settings`, `mcp`, `loguru`.
- [x] Design directory structure separating Application, Agent Service, Runtime, Tools, and Core.
- [x] Implement configuration system with `.env` and YAML fallback.
- [x] Set up unit testing scaffold with `pytest` and `pytest-asyncio`.

---

### Milestone 1: Multi-Provider LLM Engine & LangGraph Agent Runtime
- [ ] Implement `LLMFactory` supporting:
  - OpenAI API (`gpt-4o`, `gpt-4o-mini`)
  - DeepSeek (`deepseek-chat`, `deepseek-coder`)
  - Anthropic (`claude-3-5-sonnet`)
  - Google Gemini (`gemini-1.5-pro`, `gemini-1.5-flash`)
  - Local / Ollama (`llama3.1`, `qwen2.5`)
- [ ] Build base `AgentState` schema with `messages`, `run_context`, `pending_approval`.
- [ ] Implement core graph nodes (`assistant`, `router`, `tool_node`).
- [ ] Verify basic conversational and tool-calling agent loops.

---

### Milestone 2: Tool Subsystem & MCP Client Integration
- [ ] Implement `ToolRegistry` with risk level and category metadata.
- [ ] Implement production built-in tools:
  - Filesystem: read, write, search, diff
  - Shell / Terminal: secure command executor with timeout and output truncation
  - Web: HTTP fetch & search
- [ ] Integrate Model Context Protocol (MCP) Client Hub:
  - Stdio process lifecycle manager
  - SSE transport client
  - Dynamic tool schema converter (MCP Tool -> LangChain BaseTool)
- [ ] Auto-discovery of local and workspace MCP servers.

---

### Milestone 3: Human-in-the-Loop (HITL) & Persistent Checkpointing
- [ ] Implement SQLite-backed LangGraph Checkpointer (`SqliteSaver`).
- [ ] Implement LangGraph `interrupt()` protocol for dangerous actions.
- [ ] Build Approval State Machine:
  - `PENDING` -> `APPROVED` | `REJECTED` | `MODIFIED`
- [ ] Expose Approval API endpoints (`GET /approvals`, `POST /approvals/{id}/decision`).
- [ ] Test graph pause, serialization to disk, restart, and state resumption.

---

### Milestone 4: Personal Memory & Local Knowledge Base (RAG)
- [ ] Design layered memory system:
  - Short-term session memory (Thread state)
  - Long-term personal profile / preferences memory
  - Semantic knowledge base (notes, documentation, indexed codebases)
- [ ] Vector store integration (Chroma / SQLite-vec).
- [ ] Query rewrite and retrieval tools for the agent.

---

### Milestone 5: Application Gateway, Streaming & User Experience
- [ ] FastAPI streaming responses via Server-Sent Events (SSE).
- [ ] WebSocket gateway for bidirectional agent control.
- [ ] Interactive Developer CLI (`jarvis-studio chat`, `jarvis-studio run`).
- [ ] Web UI frontend integration (modern Chat + Task Board + HITL Review Center).
- [ ] Containerization (Dockerfile + Docker Compose) for 24/7 background operation.
