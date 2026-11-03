"""API Request Schemas."""

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """User input message to the agent."""

    message: str = Field(description="The user's instruction or prompt")
    session_id: str | None = Field(
        default=None, description="Thread / Session identifier"
    )
    model: str | None = Field(
        default=None, description="Optional override for model name"
    )


class ApprovalDecisionRequest(BaseModel):
    """Payload to approve, reject, or modify an intercepted tool call."""

    decision: str = Field(description="'approve', 'reject', or 'modify'")
    thread_id: str = Field(description="The thread ID where the execution is paused")
    reason: str | None = Field(
        default=None, description="Optional feedback or rationale"
    )
    modified_arguments: dict[str, Any] | None = Field(
        default=None,
        description="Modified tool arguments if decision is 'modify'",
    )
