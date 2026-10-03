"""Bridge adapting Model Context Protocol (MCP) tools to LangChain BaseTool."""

import json
from typing import Any, Callable, Coroutine, Dict, Optional, Type
from pydantic import BaseModel, create_model
from langchain_core.tools import BaseTool

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety


def create_dynamic_args_schema(tool_name: str, input_schema: Dict[str, Any]) -> Type[BaseModel]:
    """Convert an MCP JSON Schema dict into a Pydantic model for LangChain argument validation."""
    properties = input_schema.get("properties", {})
    required_fields = set(input_schema.get("required", []))

    field_definitions: Dict[str, Any] = {}
    for prop_name, prop_data in properties.items():
        prop_type = str
        prop_desc = prop_data.get("description", "")
        # Basic JSON schema type mapping
        match prop_data.get("type"):
            case "integer":
                prop_type = int
            case "number":
                prop_type = float
            case "boolean":
                prop_type = bool
            case "array":
                prop_type = list
            case "object":
                prop_type = dict
            case _:
                prop_type = str

        if prop_name in required_fields:
            field_definitions[prop_name] = (prop_type, ...)
        else:
            field_definitions[prop_name] = (Optional[prop_type], None)

    schema_name = f"{tool_name.capitalize()}Input"
    return create_model(schema_name, **field_definitions)


class MCPToolAdapter(BaseTool):
    """LangChain tool wrapper executing an MCP tool via an active MCP client session."""

    name: str
    description: str
    server_name: str
    args_schema: Optional[Type[BaseModel]] = None
    call_mcp_func: Any = None

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """Synchronous invocation wrapper."""
        import asyncio
        return asyncio.run(self._arun(*args, **kwargs))

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """Execute the tool through the underlying MCP client."""
        logger.info(f"Calling MCP tool '{self.name}' on server '{self.server_name}'")
        try:
            if not self.call_mcp_func:
                return f"Error: No MCP client function bound for '{self.name}'"
            
            result = await self.call_mcp_func(self.name, kwargs)
            return str(result)
        except Exception as e:
            logger.error(f"Error calling MCP tool '{self.name}': {e}")
            return f"Error executing MCP tool '{self.name}': {e}"


def convert_mcp_tool_to_langchain(
    mcp_tool: Any,
    server_name: str,
    call_func: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, Any]],
    risk_level: RiskLevel = RiskLevel.SAFE,
) -> BaseTool:
    """Transform an MCP tool definition into a registered LangChain BaseTool."""
    tool_name = f"{server_name}_{mcp_tool.name}" if hasattr(mcp_tool, "name") else str(mcp_tool)
    tool_desc = getattr(mcp_tool, "description", "") or f"MCP Tool from {server_name}"
    input_schema = getattr(mcp_tool, "inputSchema", {}) or {}

    args_schema = None
    if isinstance(input_schema, dict) and "properties" in input_schema:
        args_schema = create_dynamic_args_schema(tool_name, input_schema)

    adapter = MCPToolAdapter(
        name=tool_name,
        description=tool_desc,
        server_name=server_name,
        args_schema=args_schema,
        call_mcp_func=call_func,
    )

    tag_tool_safety(
        adapter,
        risk_level=risk_level,
        requires_approval=(risk_level == RiskLevel.HIGH),
        category=f"mcp:{server_name}",
    )
    return adapter
