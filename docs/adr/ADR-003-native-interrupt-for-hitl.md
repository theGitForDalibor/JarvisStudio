# ADR-003: Native LangGraph interrupt() with Persistent Checkpoints for Human-in-the-Loop

- **Status**: Accepted
- **Date**: 2026-10-03
- **Deciders**: Principal Engineer / Tech Lead

---

## Context

Jarvis Studio executes system-level operations such as terminal commands, file writing, and repository mutations. Unrestricted autonomous tool invocation risks data destruction or unintended system damage. We require a robust, crash-resilient mechanism to halt execution when high-risk tools are called, await human confirmation, and resume safely.

---

## Decision

We adopt LangGraph's native `interrupt()` function paired with persistent SQLite checkpointing (`SqliteSaver`):
1. The `approval_gate` node intercepts any tool marked with `requires_approval=True` or `RiskLevel.HIGH`.
2. The node invokes `interrupt(approval_payload)`, which instructs the LangGraph engine to save a state snapshot and pause execution.
3. The API surfaces this paused execution as `status="interrupted"` with a unique `approval_id`.
4. The client issues an approval, rejection, or argument modification via `POST /api/v1/approvals/{id}/decision`.
5. The graph is resumed via `graph.ainvoke(Command(resume=decision_payload))`.

---

## Alternatives Considered

1. **Blocking Synchronous Waiting (In-Memory Locks / Events)**:
   - *Rejected*: Keeps HTTP connections or async tasks suspended in memory. Fails completely if the server restarts or times out.
2. **External Message Queue (e.g. Celery / Temporal)**:
   - *Rejected*: Over-engineered for a personal AI operating assistant. Introduces unwanted infrastructure dependencies (Redis, RabbitMQ).
3. **Interactive Confirmation in Prompts Only**:
   - *Rejected*: Insecure. The LLM can hallucinate confirmation or bypass prompt-level restrictions.

---

## Consequences

### Positive
- **Durability**: The paused execution state is stored in SQLite; server restarts do not lose pending approvals.
- **Protocol Safety**: Handled rejections emit valid `ToolMessage` instances, preventing downstream LLM API failures.
- **Zero Overhead**: No worker processes or persistent connection locks while awaiting human review.

### Negative / Trade-offs
- Resumption requires the caller to know the active `thread_id`.
