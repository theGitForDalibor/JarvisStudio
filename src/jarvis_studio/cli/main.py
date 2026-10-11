"""Developer Command Line Interface (CLI) for Jarvis Studio."""

import asyncio
import json
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jarvis_studio.agent.hitl.manager import ApprovalDecision
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


@cli.command("sessions")
def list_sessions_cmd():
    """Display all tracked conversation sessions and threads."""
    from jarvis_studio.service.session_service import get_session_service

    session_service = get_session_service()
    sessions = session_service.list_sessions()

    if not sessions:
        console.print("[dim]No active sessions found. Run 'jarvis-studio chat' to start one.[/dim]")
        return

    table = Table(title="Conversation Sessions")
    table.add_column("Session ID", style="bold yellow")
    table.add_column("Title")
    table.add_column("Last Updated", style="dim")
    table.add_column("Resume Command", style="cyan")

    for s in sessions:
        updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.updated_at))
        cmd = f"jarvis-studio chat --session {s.session_id}"
        table.add_row(s.session_id, s.title, updated_str, cmd)

    console.print(table)


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
@click.option("--mcp/--no-mcp", "connect_mcp", default=True, help="Auto-connect enabled MCP servers")
def chat(session: str, connect_mcp: bool):
    """Launch an interactive terminal chat session with Jarvis."""
    from jarvis_studio.service.agent_service import get_agent_service
    from jarvis_studio.service.session_service import get_session_service

    # 1. Wire tools and MCP infrastructure
    registry = get_default_registry()
    register_builtin_tools(registry)
    mcp_mgr = get_mcp_manager()

    session_service = get_session_service()
    agent_service = get_agent_service()
    active_session = session_service.get_or_create_session(session)
    thread_id = active_session.session_id

    settings = get_settings()

    async def _handle_approval_decision(pending_appr: dict) -> ApprovalDecision:
        """Render interactive prompt for user to approve, reject, or modify intercepted call."""
        tool_name = pending_appr.get("tool_name", "unknown")
        args = pending_appr.get("arguments", {})
        risk = pending_appr.get("risk_level", "HIGH")
        desc = pending_appr.get("description", "Action requires human approval")

        args_str = json.dumps(args, indent=2, ensure_ascii=False)
        box_text = (
            f"[bold red]Tool Name  :[/bold red] [bold yellow]{tool_name}[/bold yellow]\n"
            f"[bold red]Risk Level :[/bold red] [red]{risk}[/red]\n"
            f"[bold red]Description:[/bold red] {desc}\n"
            f"[bold cyan]Arguments  :[/bold cyan]\n[dim]{args_str}[/dim]"
        )
        console.print(Panel(box_text, title="[bold red]⚠️ HITL Approval Intercepted[/bold red]", border_style="red"))

        prompt_str = (
            "\n[bold yellow]Authorize execution?[/bold yellow] "
            "[[bold green]y[/bold green]]es / "
            "[[bold red]n[/bold red]]o / "
            "[[bold blue]m[/bold blue]]odify: "
        )

        while True:
            choice = console.input(prompt_str).strip().lower()
            if choice in ("y", "yes"):
                return ApprovalDecision(decision="approve")

            if choice in ("n", "no"):
                reason = console.input("[dim]Reason for rejection (optional): [/dim]").strip()
                return ApprovalDecision(
                    decision="reject",
                    reason=reason or "Rejected by developer in terminal",
                )

            if choice in ("m", "modify"):
                console.print(
                    "[dim]Enter modified arguments (JSON format or raw string if single parameter):[/dim]"
                )
                raw_mod = console.input("[bold blue]New Arguments > [/bold blue]").strip()
                if not raw_mod:
                    console.print("[yellow]Modification cancelled. Reverting to rejection.[/yellow]")
                    return ApprovalDecision(decision="reject", reason="Cancelled modification")

                try:
                    parsed_mod = json.loads(raw_mod)
                    return ApprovalDecision(decision="modify", modified_arguments=parsed_mod)
                except json.JSONDecodeError:
                    if len(args) == 1:
                        single_key = next(iter(args.keys()))
                        return ApprovalDecision(decision="modify", modified_arguments={single_key: raw_mod})
                    console.print("[red]Invalid JSON! Please try again.[/red]")
                    continue

            console.print("[yellow]Invalid option. Please enter 'y', 'n', or 'm'.[/yellow]")

    async def _chat_loop():
        if connect_mcp:
            with console.status("[dim]Connecting enabled MCP servers...[/dim]"):
                try:
                    await mcp_mgr.connect_and_register_all(registry)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Error initializing MCP servers: {e}")

        # Recompile graph with populated tools
        agent_service.rebuild_graph(registry=registry)

        tool_count = len(registry.list_tools())
        console.print(
            Panel(
                f"[bold cyan]Jarvis Studio Terminal Assistant[/bold cyan]\n"
                f"[dim]Session ID:[/dim] [yellow]{thread_id}[/yellow]\n"
                f"[dim]LLM Model :[/dim] {settings.llm.default_provider.value} ({settings.llm.default_model})\n"
                f"[dim]Active Tools:[/dim] {tool_count} registered (Builtin & MCP)\n"
                f"[dim]Commands:[/dim] Type '[bold green]/tools[/bold green]' to inspect, '[bold red]exit[/bold red]' to quit.",
                title="Interactive Console",
                border_style="cyan",
            )
        )

        try:
            while True:
                try:
                    user_input = console.input("\n[bold green]You > [/bold green]").strip()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Session terminated by user.[/yellow]")
                    break

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit", "/q"):
                    console.print("[yellow]Goodbye! Session state preserved in SQLite.[/yellow]")
                    break

                if user_input.lower() == "/tools":
                    t_table = Table(title="Currently Registered Tools")
                    t_table.add_column("Name", style="bold")
                    t_table.add_column("Risk", justify="center")
                    t_table.add_column("Description")
                    for t in registry.list_tools():
                        m = get_tool_metadata(t)
                        t_table.add_row(m.name, m.risk_level.value, m.description[:60])
                    console.print(t_table)
                    continue

                if user_input.lower() == "/clear":
                    console.clear()
                    continue

                if user_input.lower() == "/help":
                    console.print("[dim]Commands: /tools (list tools), /clear (clear screen), exit (quit)[/dim]")
                    continue

                with console.status("[cyan]Jarvis is thinking...[/cyan]"):
                    result = await agent_service.invoke_agent(thread_id, user_input)

                # Process any HITL approvals in a loop (supports chained tool calls)
                while result.status == "interrupted" and result.pending_approval:
                    decision = await _handle_approval_decision(result.pending_approval)
                    with console.status("[cyan]Resuming execution with decision...[/cyan]"):
                        result = await agent_service.resume_approval(
                            thread_id=thread_id,
                            approval_id=result.pending_approval.get("approval_id", ""),
                            decision=decision,
                        )

                if result.status == "error":
                    console.print(f"[bold red]Execution Error:[/bold red] {result.error}")
                else:
                    console.print(f"\n[bold cyan]Jarvis > [/bold cyan]{result.output_text}")

        finally:
            if connect_mcp:
                await mcp_mgr.disconnect_all(registry)

    asyncio.run(_chat_loop())


if __name__ == "__main__":
    cli()
