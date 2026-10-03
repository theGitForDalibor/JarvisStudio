# ADR-001: Adopt LangGraph and Model Context Protocol (MCP) as Production Foundation

- **Status**: Accepted
- **Date**: 2026-10-03
- **Deciders**: Principal Engineer / Tech Lead

---

## Context

Prior iterations of the personal assistant project served specific educational purposes:
- **Jarvis Agent**: Implemented a custom Python Agent Runtime from scratch to explore the internals of ReAct, memory management, and prompt engineering.
- **Jarvis Agent Plus**: Studied LangChain framework abstractions.
- **Jarvis Agent Pro**: Explored graph-based agent orchestration.

**Jarvis Studio** is created with a fundamentally different goal: to serve as a long-term, production-grade personal AI Operating Assistant for real daily work. Continuing to maintain and reinvent a custom agent loop, checkpointing mechanism, and tool registry introduces substantial technical debt and fragility.

---

## Decision

We decide to:
1. **Adopt LangGraph** as the primary runtime for agent state machines, cyclic workflows, checkpointing, and human-in-the-loop interruptions.
2. **Adopt LangChain Core** for standardized message schemas (`HumanMessage`, `AIMessage`, `ToolMessage`) and tool interfaces.
3. **Adopt Model Context Protocol (MCP)** as the primary standard for integrating external tools and local system services.

---

## Alternatives Considered

1. **Continue Maintaining the Custom Jarvis Agent Runtime**:
   - *Rejected*: Incurs immense maintenance overhead for edge cases (checkpoint serialization, token streaming, cyclic recovery, error propagation).
2. **Raw LangChain AgentExecutor**:
   - *Rejected*: Legacy `AgentExecutor` is rigid, lacks fine-grained checkpointing, and cannot natively handle human-in-the-loop pauses with state snapshots.
3. **Custom Tool Integration per External Service**:
   - *Rejected*: Leads to high coupling and fragmentation. MCP provides an open, vendor-neutral standard supported by a growing ecosystem of servers.

---

## Consequences

### Positive
- Built-in, battle-tested state graph execution with durable checkpointing.
- Native `interrupt()` and `Command(resume=...)` support for human review.
- Access to the MCP ecosystem (filesystem, database, GitHub, browser automation) without writing custom adapters for every service.

### Negative / Trade-offs
- Dependency on external framework release lifecycles.
- Learning curve for graph-based state reduction semantics (`add_messages`).
