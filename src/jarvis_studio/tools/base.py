"""Tool metadata, risk definitions, and decorators."""

from enum import Enum

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    """Risk classification for tools."""

    SAFE = "SAFE"  # Read-only, no side effects
    MEDIUM = "MEDIUM"  # Non-destructive side effects (e.g. temporary files)
    HIGH = "HIGH"  # Destructive mutations, terminal commands, network writes


class ToolMetadata(BaseModel):
    """Metadata describing a tool and its safety profile."""

    name: str
    description: str
    risk_level: RiskLevel = RiskLevel.SAFE
    requires_approval: bool = False
    category: str = "general"
    tags: list[str] = Field(default_factory=list)


def tag_tool_safety(
    tool: BaseTool,
    risk_level: RiskLevel = RiskLevel.SAFE,
    requires_approval: bool | None = None,
    category: str = "general",
    name: str | None = None,
    description: str | None = None,
) -> BaseTool:
    """Attach safety and risk metadata directly to a LangChain tool instance."""
    should_approve = (
        requires_approval
        if requires_approval is not None
        else (risk_level == RiskLevel.HIGH)
    )
    meta = ToolMetadata(
        name=name or tool.name,
        description=description or tool.description,
        risk_level=risk_level,
        requires_approval=should_approve,
        category=category,
    )
    tool._jarvis_metadata = meta
    return tool


# Alias for backward and forward compatibility
mark_tool_metadata = tag_tool_safety


def get_tool_metadata(tool: BaseTool) -> ToolMetadata:
    """Extract Jarvis metadata from a tool or return default SAFE metadata."""
    if hasattr(tool, "_jarvis_metadata"):
        return tool._jarvis_metadata
    return ToolMetadata(
        name=tool.name,
        description=tool.description,
        risk_level=RiskLevel.SAFE,
        requires_approval=False,
    )
