"""API Response Schemas."""

from typing import Any

from pydantic import BaseModel


class ChatResponse(BaseModel):
    """Standardized response from an agent invocation."""

    thread_id: str
    run_id: str
    status: str  # "completed" | "interrupted" | "error"
    output_text: str = ""
    pending_approval: dict[str, Any] | None = None
    error: str | None = None


class SystemStatusResponse(BaseModel):
    """System health and runtime status."""

    status: str = "ok"
    version: str
    default_provider: str
    default_model: str
    hitl_enabled: bool
    registered_tools_count: int
    active_mcp_servers_count: int
