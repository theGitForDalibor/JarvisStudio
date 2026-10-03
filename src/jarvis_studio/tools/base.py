"""Tool metadata, risk definitions, and decorators."""

from enum import Enum
from typing import Any, Callable, Dict, Optional
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool


class RiskLevel(str, Enum):
    """Risk classification for tools."""
    SAFE = "SAFE"          # Read-only, no side effects
    MEDIUM = "MEDIUM"      # Non-destructive side effects (e.g. temporary files)
    HIGH = "HIGH"          # Destructive mutations, terminal commands, network writes


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
    requires_approval: Optional[bool] = None,
    category: str = "general",
) -> BaseTool:
    """Attach safety and risk metadata directly to a LangChain tool instance."""
    should_approve = requires_approval if requires_approval is not None else (risk_level == RiskLevel.HIGH)
    meta = ToolMetadata(
        name=tool.name,
        description=tool.description,
        risk_level=risk_level,
        requires_approval=should_approve,
        category=category,
    )
    setattr(tool, "_jarvis_metadata", meta)
    return tool


def get_tool_metadata(tool: BaseTool) -> ToolMetadata:
    """Extract Jarvis metadata from a tool or return default SAFE metadata."""
    if hasattr(tool, "_jarvis_metadata"):
        return getattr(tool, "_jarvis_metadata")
    return ToolMetadata(
        name=tool.name,
        description=tool.description,
        risk_level=RiskLevel.SAFE,
        requires_approval=False,
    )
