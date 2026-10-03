"""API Response Schemas."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ChatResponse(BaseModel):
    """Standardized response from an agent invocation."""
    thread_id: str
    run_id: str
    status: str  # "completed" | "interrupted" | "error"
    output_text: str = ""
    pending_approval: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class SystemStatusResponse(BaseModel):
    """System health and runtime status."""
    status: str = "ok"
    version: str
    default_provider: str
    default_model: str
    hitl_enabled: bool
    registered_tools_count: int
    active_mcp_servers_count: int
