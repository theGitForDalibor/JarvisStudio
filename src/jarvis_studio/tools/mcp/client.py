"""Model Context Protocol (MCP) Client Manager."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.registry import ToolRegistry, get_default_registry


class MCPServerConfig(BaseModel):
    """Specification for launching an external MCP server."""
    command: str
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    description: Optional[str] = None


class MCPClientManager:
    """Manages connections to external MCP servers and registers their tools."""

    def __init__(self, config_path: str = "config/mcp_servers.json"):
        self.config_path = Path(config_path)
        self.server_configs: Dict[str, MCPServerConfig] = {}
        self.load_configs()

    def load_configs(self) -> None:
        """Load configured MCP servers from JSON file."""
        if not self.config_path.exists():
            logger.warning(f"MCP configuration file not found at {self.config_path}")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                servers = data.get("mcpServers", {})
                for name, srv in servers.items():
                    self.server_configs[name] = MCPServerConfig(**srv)
            logger.info(f"Loaded {len(self.server_configs)} MCP server definition(s)")
        except Exception as e:
            logger.error(f"Failed to load MCP config from {self.config_path}: {e}")

    def list_servers(self) -> Dict[str, MCPServerConfig]:
        """Return all discovered MCP server configurations."""
        return self.server_configs

    async def connect_and_register_all(self, registry: Optional[ToolRegistry] = None) -> None:
        """Connect to all enabled MCP servers and register their tools."""
        target_registry = registry or get_default_registry()
        enabled_servers = {k: v for k, v in self.server_configs.items() if v.enabled}
        
        if not enabled_servers:
            logger.info("No enabled MCP servers to connect to.")
            return

        logger.info(f"Initializing {len(enabled_servers)} enabled MCP server(s)...")
        # In production M2, this connects via stdio_client or SSE client sessions
        # and converts tool specs into LangChain tools using convert_mcp_tool_to_langchain.
