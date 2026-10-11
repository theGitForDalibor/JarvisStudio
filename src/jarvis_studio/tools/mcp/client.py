"""Model Context Protocol (MCP) Client Manager and Connection Lifecycle."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import RiskLevel
from jarvis_studio.tools.mcp.adapter import convert_mcp_tool_to_langchain
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


class MCPServerConfig(BaseModel):
    """Specification for launching or connecting to an external MCP server."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    description: str | None = None
    transport: str = "stdio"  # "stdio" or "sse"
    url: str | None = None
    cwd: str | None = None
    default_risk_level: RiskLevel = RiskLevel.SAFE
    high_risk_tools: list[str] = Field(default_factory=list)
    medium_risk_tools: list[str] = Field(default_factory=list)
    timeout_seconds: float = 60.0


class MCPConnection:
    """Manages an active long-running connection and ClientSession to an MCP server."""

    def __init__(self, name: str, config: MCPServerConfig):
        self.name = name
        self.config = config
        self.session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self.is_connected = False
        self.registered_tools: list[str] = []
        self.server_info: Any | None = None
        self.error_message: str | None = None

    async def connect(self) -> None:
        """Establish connection to the server process/endpoint and initialize MCP session."""
        async with self._lock:
            if self.is_connected:
                return

            self._exit_stack = AsyncExitStack()
            try:
                transport_type = self.config.transport.lower()
                if transport_type == "sse" or (self.config.url and not self.config.command):
                    if not self.config.url:
                        raise ValueError(f"SSE transport requires 'url' for server '{self.name}'")
                    logger.info(f"Connecting to MCP SSE endpoint: {self.config.url} ({self.name})")
                    read_stream, write_stream = await self._exit_stack.enter_async_context(
                        sse_client(self.config.url)
                    )
                else:
                    if not self.config.command:
                        raise ValueError(f"stdio transport requires 'command' for server '{self.name}'")

                    merged_env = os.environ.copy()
                    if self.config.env:
                        merged_env.update(self.config.env)

                    logger.info(
                        f"Spawning MCP stdio server process '{self.name}': {self.config.command} {' '.join(self.config.args)}"
                    )
                    server_params = StdioServerParameters(
                        command=self.config.command,
                        args=self.config.args,
                        env=merged_env,
                        cwd=self.config.cwd,
                    )
                    read_stream, write_stream = await self._exit_stack.enter_async_context(
                        stdio_client(server_params)
                    )

                self.session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )

                init_result = await self.session.initialize()
                self.server_info = getattr(init_result, "server_info", None)
                self.is_connected = True
                self.error_message = None
                logger.info(f"Successfully initialized MCP server session for '{self.name}'")

            except Exception as e:
                self.error_message = str(e)
                logger.error(f"Failed to connect to MCP server '{self.name}': {e}")
                await self._cleanup()
                raise

    async def _cleanup(self) -> None:
        """Internal cleanup of exit stack resources."""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning(f"Error while closing MCP connection stack '{self.name}': {e}")
            finally:
                self._exit_stack = None
                self.session = None
                self.is_connected = False

    async def disconnect(self) -> None:
        """Safely terminate the connection and shutdown child process/streams."""
        async with self._lock:
            if not self.is_connected and not self._exit_stack:
                return
            logger.info(f"Disconnecting MCP server '{self.name}'...")
            await self._cleanup()
            logger.info(f"MCP server '{self.name}' disconnected.")

    async def list_tools(self) -> list[Any]:
        """Query available tools from the connected MCP server."""
        if not self.is_connected or not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected.")
        res = await self.session.list_tools()
        return list(res.tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool on the remote server and format result."""
        if not self.is_connected or not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected.")

        # Strip server name prefix if passed
        target_name = tool_name
        prefix = f"{self.name}_"
        if tool_name.startswith(prefix):
            target_name = tool_name[len(prefix):]

        logger.debug(f"Calling MCP tool '{target_name}' on server '{self.name}' with args: {arguments}")
        result = await self.session.call_tool(
            target_name,
            arguments=arguments,
            read_timeout_seconds=self.config.timeout_seconds,
        )
        return self._format_tool_result(result)

    def _format_tool_result(self, result: Any) -> str:
        """Format an MCP CallToolResult or response into string text."""
        if result is None:
            return "Success"

        if hasattr(result, "content") and isinstance(result.content, list):
            pieces = []
            for item in result.content:
                if hasattr(item, "text"):
                    pieces.append(item.text)
                elif hasattr(item, "data"):
                    pieces.append(str(item.data))
                else:
                    pieces.append(str(item))

            output = "\n".join(pieces)
            if getattr(result, "is_error", False):
                return f"Tool Error: {output}"
            return output

        return str(result)


class MCPClientManager:
    """Manages connections to external MCP servers and registers their tools."""

    def __init__(self, config_path: str = "config/mcp_servers.json"):
        self.config_path = Path(config_path)
        self.server_configs: dict[str, MCPServerConfig] = {}
        self.connections: dict[str, MCPConnection] = {}
        self.load_configs()

    def load_configs(self, path: Path | str | None = None) -> None:
        """Load configured MCP servers from a JSON file."""
        target_path = Path(path) if path else self.config_path
        if not target_path.exists():
            logger.warning(f"MCP configuration file not found at {target_path}")
            return

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                servers = data.get("mcpServers", {})
                for name, srv in servers.items():
                    self.server_configs[name] = MCPServerConfig(**srv)
            logger.info(f"Loaded {len(self.server_configs)} MCP server definition(s) from {target_path}")
        except Exception as e:
            logger.error(f"Failed to load MCP config from {target_path}: {e}")

    def auto_discover_configs(self, search_paths: list[str | Path] | None = None) -> dict[str, MCPServerConfig]:
        """Discover and load MCP configurations from standard workspace locations."""
        default_search = [
            Path("config/mcp_servers.json"),
            Path(".mcp.json"),
            Path("mcp_servers.json"),
            Path("data/workspace/.mcp.json"),
            Path(".jarvis/mcp_servers.json"),
        ]
        candidates = [Path(p) for p in search_paths] if search_paths else default_search

        discovered_count = 0
        for cand in candidates:
            if cand.exists() and cand != self.config_path:
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        servers = data.get("mcpServers", {})
                        for name, srv in servers.items():
                            if name not in self.server_configs:
                                self.server_configs[name] = MCPServerConfig(**srv)
                                discovered_count += 1
                    logger.info(f"Auto-discovered MCP configs from {cand}")
                except Exception as e:
                    logger.debug(f"Skipping auto-discovery from {cand}: {e}")

        if discovered_count > 0:
            logger.info(f"Auto-discovered {discovered_count} additional MCP server definition(s)")

        return self.server_configs

    def list_servers(self) -> dict[str, MCPServerConfig]:
        """Return all discovered MCP server configurations."""
        return self.server_configs

    def get_connection(self, name: str) -> MCPConnection | None:
        """Retrieve connection instance by server name."""
        return self.connections.get(name)

    async def connect_server(
        self,
        name: str,
        registry: ToolRegistry | None = None,
    ) -> MCPConnection:
        """Connect to a single MCP server and register its tools."""
        config = self.server_configs.get(name)
        if not config:
            raise ValueError(f"No configuration found for MCP server '{name}'")

        target_registry = registry or get_default_registry()
        conn = self.connections.get(name)
        if not conn:
            conn = MCPConnection(name=name, config=config)
            self.connections[name] = conn

        if not conn.is_connected:
            await conn.connect()

        # Discover and register tools
        tools = await conn.list_tools()
        logger.info(f"Discovered {len(tools)} tool(s) on MCP server '{name}'")

        for tool in tools:
            tool_raw_name = getattr(tool, "name", str(tool))
            # Determine safety level
            if tool_raw_name in config.high_risk_tools:
                risk = RiskLevel.HIGH
            elif tool_raw_name in config.medium_risk_tools:
                risk = RiskLevel.MEDIUM
            else:
                risk = config.default_risk_level

            lc_tool = convert_mcp_tool_to_langchain(
                mcp_tool=tool,
                server_name=name,
                call_func=conn.call_tool,
                risk_level=risk,
            )
            target_registry.register(lc_tool)
            if lc_tool.name not in conn.registered_tools:
                conn.registered_tools.append(lc_tool.name)

        return conn

    async def disconnect_server(
        self,
        name: str,
        registry: ToolRegistry | None = None,
    ) -> None:
        """Disconnect an MCP server and unregister all its registered tools."""
        conn = self.connections.get(name)
        if not conn:
            return

        target_registry = registry or get_default_registry()
        for tool_name in list(conn.registered_tools):
            target_registry.unregister(tool_name)
        conn.registered_tools.clear()

        await conn.disconnect()

    async def connect_and_register_all(
        self,
        registry: ToolRegistry | None = None,
    ) -> dict[str, MCPConnection]:
        """Connect to all enabled MCP servers and register their tools."""
        target_registry = registry or get_default_registry()
        enabled_servers = {k: v for k, v in self.server_configs.items() if v.enabled}

        if not enabled_servers:
            logger.info("No enabled MCP servers to connect to.")
            return {}

        logger.info(f"Initializing {len(enabled_servers)} enabled MCP server(s)...")

        for name in enabled_servers:
            try:
                await self.connect_server(name, registry=target_registry)
            except Exception as e:
                logger.error(f"Failed to initialize enabled MCP server '{name}': {e}")

        return self.connections

    async def disconnect_all(self, registry: ToolRegistry | None = None) -> None:
        """Disconnect all active MCP servers."""
        for name in list(self.connections.keys()):
            try:
                await self.disconnect_server(name, registry=registry)
            except Exception as e:
                logger.error(f"Error disconnecting MCP server '{name}': {e}")

    def get_status(self) -> dict[str, Any]:
        """Return runtime status summary of all configured MCP servers."""
        status_info: dict[str, Any] = {}
        for name, config in self.server_configs.items():
            conn = self.connections.get(name)
            status_info[name] = {
                "enabled": config.enabled,
                "transport": config.transport,
                "description": config.description,
                "connected": conn.is_connected if conn else False,
                "registered_tools": conn.registered_tools if conn else [],
                "error": conn.error_message if conn else None,
            }
        return {
            "total_servers": len(self.server_configs),
            "active_servers": len([c for c in self.connections.values() if c.is_connected]),
            "servers": status_info,
        }


_global_mcp_manager: MCPClientManager | None = None


def get_mcp_manager() -> MCPClientManager:
    """Retrieve the singleton MCPClientManager instance."""
    global _global_mcp_manager
    if _global_mcp_manager is None:
        _global_mcp_manager = MCPClientManager()
    return _global_mcp_manager
