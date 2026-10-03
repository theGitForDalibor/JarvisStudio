"""Unit tests for ToolRegistry and safety tagging."""

from langchain_core.tools import tool
from jarvis_studio.tools.base import RiskLevel, get_tool_metadata, tag_tool_safety
from jarvis_studio.tools.builtin.file_tools import read_file, write_file
from jarvis_studio.tools.builtin.shell_tools import execute_shell_command
from jarvis_studio.tools.registry import ToolRegistry


@tool
def sample_read_tool(query: str) -> str:
    """Read information."""
    return f"Result: {query}"


@tool
def sample_delete_tool(target: str) -> str:
    """Delete a target resource."""
    return f"Deleted {target}"


def test_tool_safety_tagging():
    """Verify tool safety tags and metadata extraction."""
    tagged_safe = tag_tool_safety(sample_read_tool, risk_level=RiskLevel.SAFE)
    meta_safe = get_tool_metadata(tagged_safe)
    assert meta_safe.risk_level == RiskLevel.SAFE
    assert meta_safe.requires_approval is False

    tagged_high = tag_tool_safety(sample_delete_tool, risk_level=RiskLevel.HIGH)
    meta_high = get_tool_metadata(tagged_high)
    assert meta_high.risk_level == RiskLevel.HIGH
    assert meta_high.requires_approval is True


def test_tool_registry_registration():
    """Verify registry methods and lookup."""
    registry = ToolRegistry()
    registry.register(read_file)
    registry.register(write_file)
    registry.register(execute_shell_command)

    assert registry.get("read_file") is not None
    assert registry.get("write_file") is not None
    assert registry.get("execute_shell_command") is not None

    # Check approval requirement
    assert registry.requires_approval("read_file") is False
    assert registry.requires_approval("write_file") is True
    assert registry.requires_approval("execute_shell_command") is True
