"""Dynamic tool registry for Jarvis Studio."""

from typing import Dict, List, Optional
from langchain_core.tools import BaseTool

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel, ToolMetadata, get_tool_metadata, tag_tool_safety


class ToolRegistry:
    """Registry maintaining active agent tools and their safety policies."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(
        self,
        tool: BaseTool,
        risk_level: Optional[RiskLevel] = None,
        requires_approval: Optional[bool] = None,
        category: str = "general",
    ) -> BaseTool:
        """Register a LangChain tool with safety metadata."""
        if risk_level is not None or requires_approval is not None:
            tag_tool_safety(
                tool,
                risk_level=risk_level or RiskLevel.SAFE,
                requires_approval=requires_approval,
                category=category,
            )

        self._tools[tool.name] = tool
        meta = get_tool_metadata(tool)
        logger.info(
            f"Registered tool '{tool.name}' [Risk: {meta.risk_level.value}, Approval: {meta.requires_approval}]"
        )
        return tool

    def get(self, name: str) -> Optional[BaseTool]:
        """Look up a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def requires_approval(self, tool_name: str) -> bool:
        """Check whether invoking the given tool requires human confirmation."""
        tool = self._tools.get(tool_name)
        if not tool:
            return True  # Unknown tools default to requiring confirmation
        meta = get_tool_metadata(tool)
        return meta.requires_approval

    def get_metadata(self, tool_name: str) -> Optional[ToolMetadata]:
        """Retrieve safety and catalog metadata for a tool."""
        tool = self._tools.get(tool_name)
        if not tool:
            return None
        return get_tool_metadata(tool)


_global_registry: Optional[ToolRegistry] = None


def get_default_registry() -> ToolRegistry:
    """Retrieve the singleton ToolRegistry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry
