"""Bridge adapting Model Context Protocol (MCP) tools to LangChain BaseTool."""

from collections.abc import Callable, Coroutine
from typing import Any, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel, tag_tool_safety


def create_dynamic_args_schema(tool_name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """Convert an MCP JSON Schema dict into a Pydantic model for LangChain argument validation."""
    properties = input_schema.get("properties", {})
    required_fields = set(input_schema.get("required", []))

    field_definitions: dict[str, Any] = {}
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
            field_definitions[prop_name] = (prop_type, Field(description=prop_desc))
        else:
            field_definitions[prop_name] = (prop_type | None, Field(default=None, description=prop_desc))

    sanitized_name = "".join(c if c.isalnum() else "_" for c in tool_name).capitalize()
    schema_name = f"{sanitized_name}Input"
    return create_model(schema_name, **field_definitions)


class MCPToolAdapter(BaseTool):
    """LangChain tool wrapper executing an MCP tool via an active MCP client session."""

    name: str
    raw_name: str = ""
    description: str
    server_name: str
    args_schema: type[BaseModel] | None = None
    call_mcp_func: Any = None

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """Synchronous invocation wrapper."""
        import asyncio
        import concurrent.futures

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(self._arun(*args, **kwargs))).result()
        return asyncio.run(self._arun(*args, **kwargs))

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """Execute the tool through the underlying MCP client."""
        target_name = self.raw_name or self.name
        logger.info(f"Calling MCP tool '{target_name}' (registered as '{self.name}') on server '{self.server_name}'")
        try:
            if not self.call_mcp_func:
                return f"Error: No MCP client function bound for '{self.name}'"

            result = await self.call_mcp_func(target_name, kwargs)
            return str(result)
        except Exception as e:
            logger.error(f"Error calling MCP tool '{self.name}': {e}")
            return f"Error executing MCP tool '{self.name}': {e}"


def convert_mcp_tool_to_langchain(
    mcp_tool: Any,
    server_name: str,
    call_func: Callable[[str, dict[str, Any]], Coroutine[Any, Any, Any]],
    risk_level: RiskLevel = RiskLevel.SAFE,
) -> BaseTool:
    """Transform an MCP tool definition into a registered LangChain BaseTool."""
    raw_name = (
        getattr(mcp_tool, "name", None)
        or (mcp_tool.get("name") if isinstance(mcp_tool, dict) else str(mcp_tool))
    )
    tool_name = f"{server_name}_{raw_name}"
    tool_desc = (
        getattr(mcp_tool, "description", None)
        or (mcp_tool.get("description") if isinstance(mcp_tool, dict) else None)
        or f"MCP Tool from {server_name}"
    )

    input_schema = (
        getattr(mcp_tool, "input_schema", None)
        or getattr(mcp_tool, "inputSchema", None)
        or (mcp_tool.get("input_schema") if isinstance(mcp_tool, dict) else None)
        or (mcp_tool.get("inputSchema") if isinstance(mcp_tool, dict) else None)
        or {}
    )

    args_schema = None
    if isinstance(input_schema, dict) and "properties" in input_schema:
        args_schema = create_dynamic_args_schema(tool_name, input_schema)

    adapter = MCPToolAdapter(
        name=tool_name,
        raw_name=raw_name,
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
