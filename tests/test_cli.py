"""Automated tests for Jarvis Studio Developer CLI."""

from click.testing import CliRunner
from jarvis_studio.cli.main import cli


def test_cli_info():
    """Verify `jarvis-studio info` displays system information."""
    runner = CliRunner()
    result = runner.invoke(cli, ["info"])
    assert result.exit_code == 0
    assert "Jarvis Studio Status" in result.output
    assert "Registered Tools & Safety Matrix" in result.output


def test_cli_mcp_commands():
    """Verify `jarvis-studio mcp list` and `mcp discover`."""
    runner = CliRunner()

    res_list = runner.invoke(cli, ["mcp", "list"])
    assert res_list.exit_code == 0
    assert "Model Context Protocol (MCP) Servers" in res_list.output

    res_disc = runner.invoke(cli, ["mcp", "discover"])
    assert res_disc.exit_code == 0
    assert "Discovered" in res_disc.output


def test_cli_sessions():
    """Verify `jarvis-studio sessions` command."""
    runner = CliRunner()
    result = runner.invoke(cli, ["sessions"])
    assert result.exit_code == 0


def test_cli_chat_immediate_exit():
    """Verify `jarvis-studio chat` initializes and exits cleanly."""
    runner = CliRunner()
    # Provide 'exit' as simulated user input and --no-mcp to prevent spawning external MCP processes during test
    result = runner.invoke(cli, ["chat", "--no-mcp"], input="exit\n")
    assert result.exit_code == 0
    assert "Jarvis Studio Terminal Assistant" in result.output
    assert "Goodbye" in result.output
