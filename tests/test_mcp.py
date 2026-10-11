"""Unit and integration tests for Model Context Protocol (MCP) subsystem."""

import sys
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from jarvis_studio.tools.base import RiskLevel, get_tool_metadata
from jarvis_studio.tools.mcp.adapter import (
    convert_mcp_tool_to_langchain,
    create_dynamic_args_schema,
)
from jarvis_studio.tools.mcp.client import (
    MCPClientManager,
    MCPConnection,
    MCPServerConfig,
)
from jarvis_studio.tools.registry import ToolRegistry

# Inline Python MCP Server script for live stdio testing
MOCK_MCP_SERVER_SCRIPT = """
import sys
from mcp.server.mcpserver import MCPServer

app = MCPServer("test-stdio-mcp")

@app.tool()
def calculate_sum(a: int, b: int) -> int:
    '''Calculate sum of two integers'''
    return a + b

@app.tool()
def dangerous_clean(target: str) -> str:
    '''Purge target directory'''
    return f"Purged {target}"

if __name__ == '__main__':
    app.run(transport='stdio')
"""


def test_create_dynamic_args_schema():
    """Verify conversion from JSON Schema to Pydantic model."""
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search term"},
            "limit": {"type": "integer", "description": "Max results"},
            "flag": {"type": "boolean"},
        },
        "required": ["query"],
    }

    model_cls = create_dynamic_args_schema("test_search", schema)
    assert model_cls is not None

    # Valid instantiation
    obj = model_cls(query="python", limit=10)
    assert obj.query == "python"
    assert obj.limit == 10
    assert obj.flag is None

    # Missing required field should raise validation error
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        model_cls(limit=5)


def test_convert_mcp_tool_to_langchain():
    """Verify tool conversion and safety metadata tagging."""
    from typing import ClassVar

    class FakeMCPTool:
        name: str = "mock_action"
        description: str = "A mock tool for testing"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"param": {"type": "string"}},
            "required": ["param"],
        }

    async def fake_call(name: str, args: dict):
        return f"Called {name} with {args}"

    lc_tool = convert_mcp_tool_to_langchain(
        mcp_tool=FakeMCPTool(),
        server_name="test_srv",
        call_func=fake_call,
        risk_level=RiskLevel.HIGH,
    )

    assert isinstance(lc_tool, BaseTool)
    assert lc_tool.name == "test_srv_mock_action"
    meta = get_tool_metadata(lc_tool)
    assert meta.risk_level == RiskLevel.HIGH
    assert meta.requires_approval is True
    assert meta.category == "mcp:test_srv"


def test_mcp_client_manager_load_and_discover(tmp_path: Path):
    """Verify config loading and auto-discovery."""
    cfg_file = tmp_path / "mcp_servers.json"
    cfg_file.write_text(
        '{"mcpServers": {"demo": {"command": "echo", "args": ["1"], "enabled": true}}}',
        encoding="utf-8",
    )

    manager = MCPClientManager(config_path=str(cfg_file))
    assert "demo" in manager.server_configs
    assert manager.server_configs["demo"].command == "echo"

    # Test auto-discovery with extra path
    extra_file = tmp_path / ".mcp.json"
    extra_file.write_text(
        '{"mcpServers": {"extra": {"command": "node", "enabled": false}}}',
        encoding="utf-8",
    )
    discovered = manager.auto_discover_configs(search_paths=[extra_file])
    assert "extra" in discovered
    assert discovered["extra"].command == "node"


@pytest.mark.asyncio
async def test_mcp_stdio_connection_lifecycle():
    """Verify real stdio subprocess connection, tool discovery, execution, and cleanup."""
    config = MCPServerConfig(
        command=sys.executable,
        args=["-c", MOCK_MCP_SERVER_SCRIPT],
        enabled=True,
        high_risk_tools=["dangerous_clean"],
    )

    conn = MCPConnection(name="test_srv", config=config)
    try:
        await conn.connect()
        assert conn.is_connected is True
        assert conn.session is not None

        # Verify tool discovery
        tools = await conn.list_tools()
        tool_names = [t.name for t in tools]
        assert "calculate_sum" in tool_names
        assert "dangerous_clean" in tool_names

        # Call calculate_sum
        output = await conn.call_tool("calculate_sum", {"a": 20, "b": 22})
        assert "42" in output

        # Call dangerous_clean
        output2 = await conn.call_tool("dangerous_clean", {"target": "/tmp/cache"})
        assert "Purged /tmp/cache" in output2

    finally:
        await conn.disconnect()
        assert conn.is_connected is False


@pytest.mark.asyncio
async def test_mcp_manager_register_and_unregister_tools():
    """Verify manager integrates with ToolRegistry and manages tool registration."""
    manager = MCPClientManager(config_path="nonexistent.json")
    registry = ToolRegistry()

    manager.server_configs["live_srv"] = MCPServerConfig(
        command=sys.executable,
        args=["-c", MOCK_MCP_SERVER_SCRIPT],
        enabled=True,
        default_risk_level=RiskLevel.SAFE,
        high_risk_tools=["dangerous_clean"],
    )

    try:
        await manager.connect_server("live_srv", registry=registry)
        registered = [t.name for t in registry.list_tools()]
        assert "live_srv_calculate_sum" in registered
        assert "live_srv_dangerous_clean" in registered

        # Check safety metadata
        sum_meta = registry.get_metadata("live_srv_calculate_sum")
        assert sum_meta.risk_level == RiskLevel.SAFE
        assert sum_meta.requires_approval is False

        clean_meta = registry.get_metadata("live_srv_dangerous_clean")
        assert clean_meta.risk_level == RiskLevel.HIGH
        assert clean_meta.requires_approval is True

        # Execute registered LangChain tool
        sum_tool = registry.get("live_srv_calculate_sum")
        res = await sum_tool.ainvoke({"a": 100, "b": 50})
        assert "150" in str(res)

        # Disconnect and verify unregistration
        await manager.disconnect_server("live_srv", registry=registry)
        remaining = [t.name for t in registry.list_tools()]
        assert "live_srv_calculate_sum" not in remaining
        assert "live_srv_dangerous_clean" not in remaining

    finally:
        await manager.disconnect_all(registry=registry)


@pytest.mark.asyncio
async def test_mcp_connection_failure_handling():
    """Verify robust error handling when an invalid command is configured."""
    config = MCPServerConfig(
        command="invalid_nonexistent_executable_12345",
        enabled=True,
    )
    conn = MCPConnection(name="broken_srv", config=config)

    with pytest.raises((OSError, RuntimeError, ValueError)):
        await conn.connect()

    assert conn.is_connected is False
    assert conn.error_message is not None
