# Jarvis Studio - Human-in-the-Loop (HITL) Specification

## 1. Motivation & Philosophy

Jarvis Studio operates with direct access to local files, the terminal shell, and external APIs. Unconstrained autonomous tool invocation presents unacceptable risks of data corruption, unauthorized code execution, or unintended side effects.

**Core Rule**:
> High-risk operations must never execute without explicit, informed user confirmation.

---

## 2. Risk Classification Matrix

| Risk Level | Actions | Examples | Execution Policy |
| :--- | :--- | :--- | :--- |
| **SAFE** | Read-only operations | `read_file`, `list_directory`, `web_search` | Automated execution |
| **MEDIUM** | Non-destructive writes | Creating scratch files, saving temporary analysis | Policy-dependent (configurable) |
| **HIGH** | Mutation / Destructive | `execute_shell_command`, `write_file`, `delete_file`, `git_push` | **HITL Required (Mandatory Interrupt)** |

---

## 3. LangGraph Interrupt Mechanics

Jarvis Studio leverages LangGraph's native `interrupt()` feature. When a tool call is identified as high-risk:

1. **State Preservation**: The graph runtime calls `interrupt(payload)` inside the approval gate node.
2. **Persistence**: LangGraph automatically snapshots the full execution state to the configured Checkpointer (SQLite).
3. **Notification**: The API layer surfaces the pending approval with:
   - `approval_id`
   - `thread_id`
   - `tool_name`
   - `arguments` (e.g. bash command, file path, diff)
   - `risk_assessment`
4. **Resolution**: The user submits their decision via the API:
   - **APPROVE**: Execution resumes immediately; the tool is called with original parameters.
   - **REJECT**: The tool is NOT called; a rejection message is injected into the conversation so the LLM can re-plan.
   - **MODIFY**: The user alters the arguments (e.g. adjusts a shell flag or destination path), and execution resumes with the modified arguments.
5. **Resume**: The graph resumes execution from the exact checkpoint via `Command(resume=decision)`.

---

## 4. API Protocol

### Approval Request Payload (Returned when execution interrupts)
```json
{
  "status": "interrupted",
  "approval_id": "appr_9823f0a1",
  "thread_id": "session_001",
  "tool_name": "execute_shell_command",
  "arguments": {
    "command": "git push origin main"
  },
  "risk_level": "HIGH",
  "description": "Agent requested execution of shell command"
}
```

### Approval Decision Request (POST /api/v1/approvals/{approval_id}/decision)
```json
{
  "decision": "approve", // "approve" | "reject" | "modify"
  "reason": "Verified git commit history",
  "modified_arguments": null
}
```
