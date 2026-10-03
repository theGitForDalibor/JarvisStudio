# ADR-002: Strict 5-Tier Decoupling of API, Application Service, and Agent Runtime

- **Status**: Accepted
- **Date**: 2026-10-03
- **Deciders**: Principal Engineer / Tech Lead

---

## Context

Many AI agent projects tightly couple the agent execution loop directly with the web framework (e.g. instantiating LangGraph graphs inside FastAPI route handlers) or embedding UI concerns directly in the agent's prompts and tools. This makes it difficult to support multiple user interfaces (CLI, Web, Raycast, background cron jobs) and prevents clean automated testing.

---

## Decision

We enforce a strict 5-tier architecture:
```text
UI (CLI / Web) ──▶ Application API Gateway ──▶ Agent Service ──▶ LangGraph Runtime ──▶ Tools / MCP
```

- **API Layer (`src/jarvis_studio/api`)**: Only handles HTTP requests, schemas, validation, and error translation.
- **Service Layer (`src/jarvis_studio/service`)**: `AgentService` and `SessionService` manage execution orchestration, thread correlation, and resume signals.
- **Runtime Layer (`src/jarvis_studio/agent`)**: Pure LangGraph `StateGraph`, nodes, and checkpointers. Completely unaware of FastAPI or HTTP.

---

## Consequences

### Positive
- Both the developer CLI (`jarvis-studio chat`) and the web gateway (`POST /api/v1/chat`) share the exact same `AgentService` logic.
- Testing is straightforward: unit tests can invoke the graph directly with mock models, service tests can test orchestration, and API tests can test HTTP boundaries.
- Any future frontend (desktop, browser extension, mobile) can be built without changing the agent core.

### Negative / Trade-offs
- Requires lightweight mapping between layer objects (e.g. `ChatRequest` ➔ `AgentExecutionResult` ➔ `ChatResponse`).
