"""Developer Command Line Interface (CLI) for Jarvis Studio."""

import asyncio
import sys
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.base import get_tool_metadata
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.mcp.client import get_mcp_manager
from jarvis_studio.tools.registry import get_default_registry

console = Console()


@click.group()
def cli():
    """Jarvis Studio - Personal Production AI Assistant CLI."""
    pass


@cli.command()
def info():
    """Display system status, active provider, and registered tools."""
    settings = get_settings()
    registry = get_default_registry()
    register_builtin_tools(registry)
    mcp_mgr = get_mcp_manager()

    mcp_status = mcp_mgr.get_status()

    console.print(
        Panel.fit(
            f"[bold cyan]{settings.app.name}[/bold cyan] v{settings.app.version}\n"
            f"[dim]Environment:[/dim] {settings.app.environment}\n"
            f"[dim]LLM Provider:[/dim] [yellow]{settings.llm.default_provider.value}[/yellow] ({settings.llm.default_model})\n"
            f"[dim]HITL Security:[/dim] [{'green' if settings.security.hitl_enabled else 'red'}]{'ENABLED' if settings.security.hitl_enabled else 'DISABLED'}[/]\n"
            f"[dim]MCP Servers:[/dim] [cyan]{mcp_status['active_servers']}[/cyan] active / [dim]{mcp_status['total_servers']} configured[/dim]\n"
            f"[dim]Checkpointer:[/dim] {settings.storage.checkpoint_backend} ({settings.storage.sqlite_path})",
            title="Jarvis Studio Status",
            border_style="cyan",
        )
    )

    table = Table(title="Registered Tools & Safety Matrix")
    table.add_column("Tool Name", style="bold")
    table.add_column("Category", style="dim")
    table.add_column("Risk Level", justify="center")
    table.add_column("HITL Approval", justify="center")
    table.add_column("Description")

    for tool in registry.list_tools():
        meta = get_tool_metadata(tool)
        risk_color = (
            "red"
            if meta.risk_level.value == "HIGH"
            else ("yellow" if meta.risk_level.value == "MEDIUM" else "green")
        )
        approval_str = "[red]Required[/red]" if meta.requires_approval else "[green]Auto[/green]"
        table.add_row(
            meta.name,
            meta.category,
            f"[{risk_color}]{meta.risk_level.value}[/{risk_color}]",
            approval_str,
            meta.description[:60] + "..." if len(meta.description) > 60 else meta.description,
        )

    console.print(table)


@cli.group()
def mcp():
    """Manage Model Context Protocol (MCP) servers and external tools."""
    pass


@mcp.command("list")
def mcp_list():
    """List all configured MCP servers and their current status."""
    mcp_mgr = get_mcp_manager()
    status = mcp_mgr.get_status()

    table = Table(title="Model Context Protocol (MCP) Servers")
    table.add_column("Server Name", style="bold")
    table.add_column("Transport", justify="center")
    table.add_column("Enabled", justify="center")
    table.add_column("Connected", justify="center")
    table.add_column("Tools Count", justify="center")
    table.add_column("Description")

    for name, srv in status["servers"].items():
        enabled_str = "[green]Yes[/green]" if srv["enabled"] else "[dim]No[/dim]"
        conn_str = "[green]Connected[/green]" if srv["connected"] else "[yellow]Disconnected[/yellow]"
        tools_count = len(srv.get("registered_tools", []))
        desc = srv.get("description") or "-"
        table.add_row(name, srv.get("transport", "stdio"), enabled_str, conn_str, str(tools_count), desc)

    console.print(table)


@mcp.command("discover")
def mcp_discover():
    """Search workspace and standard paths for MCP configurations."""
    mcp_mgr = get_mcp_manager()
    discovered = mcp_mgr.auto_discover_configs()
    console.print(f"[bold green]Discovered {len(discovered)} MCP server definition(s):[/bold green]")
    for name, cfg in discovered.items():
        console.print(f"  - [cyan]{name}[/cyan] (transport: {cfg.transport}, enabled: {cfg.enabled})")


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host address to bind")
@click.option("--port", default=8000, type=int, help="Port to bind")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload")
def serve(host: str, port: int, reload: bool):
    """Start the FastAPI HTTP & SSE application server."""
    import uvicorn
    console.print(f"[bold green]Starting Jarvis Studio API Gateway on http://{host}:{port}...[/bold green]")
    uvicorn.run("jarvis_studio.api.app:create_app", host=host, port=port, reload=reload, factory=True)


@cli.command()
@click.option("--session", default=None, help="Specific session/thread ID to resume")
def chat(session: str):
    """Launch an interactive terminal chat session with Jarvis."""
    from jarvis_studio.service.agent_service import get_agent_service
    from jarvis_studio.service.session_service import get_session_service

    session_service = get_session_service()
    agent_service = get_agent_service()
    active_session = session_service.get_or_create_session(session)
    thread_id = active_session.session_id

    console.print(
        Panel(
            f"Chatting with Jarvis on session: [bold yellow]{thread_id}[/bold yellow]\nType 'exit' to quit.",
            title="Jarvis Terminal",
        )
    )

    async def _chat_loop():
        while True:
            try:
                user_input = console.input("\n[bold green]You > [/bold green]").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit"):
                    console.print("[yellow]Goodbye![/yellow]")
                    break

                with console.status("[cyan]Jarvis is thinking...[/cyan]"):
                    result = await agent_service.invoke_agent(thread_id, user_input)

                if result.status == "interrupted":
                    console.print(
                        Panel(
                            f"[bold red]HITL Approval Required![/bold red]\n{result.pending_approval}",
                            border_style="red",
                        )
                    )
                elif result.status == "error":
                    console.print(f"[bold red]Error:[/bold red] {result.error}")
                else:
                    console.print(f"[bold cyan]Jarvis > [/bold cyan]{result.output_text}")

            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Session terminated.[/yellow]")
                break

    asyncio.run(_chat_loop())


if __name__ == "__main__":
    cli()
