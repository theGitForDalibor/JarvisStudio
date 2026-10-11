"""System information, health check, and MCP inspection endpoints."""

from typing import Any, Dict, List
from fastapi import APIRouter

from jarvis_studio.api.schemas.response import SystemStatusResponse
from jarvis_studio.config.settings import get_settings
from jarvis_studio.tools.base import get_tool_metadata
from jarvis_studio.tools.mcp.client import get_mcp_manager
from jarvis_studio.tools.registry import get_default_registry

router = APIRouter(prefix="/system", tags=["System"])


@router.get("/health")
async def health_check() -> Dict[str, str]:
    """Basic liveness probe."""
    return {"status": "ok"}


@router.get("/status", response_model=SystemStatusResponse)
async def system_status() -> SystemStatusResponse:
    """Retrieve runtime configuration and component health."""
    settings = get_settings()
    registry = get_default_registry()
    mcp_mgr = get_mcp_manager()

    active_count = len([c for c in mcp_mgr.connections.values() if c.is_connected])

    return SystemStatusResponse(
        version=settings.app.version,
        default_provider=settings.llm.default_provider.value,
        default_model=settings.llm.default_model,
        hitl_enabled=settings.security.hitl_enabled,
        registered_tools_count=len(registry.list_tools()),
        active_mcp_servers_count=active_count,
    )


@router.get("/tools")
async def list_registered_tools() -> List[Dict[str, Any]]:
    """Inspect all active tools and their safety profiles."""
    registry = get_default_registry()
    tools_info = []
    for t in registry.list_tools():
        meta = get_tool_metadata(t)
        tools_info.append(
            {
                "name": meta.name,
                "description": meta.description,
                "risk_level": meta.risk_level.value,
                "requires_approval": meta.requires_approval,
                "category": meta.category,
            }
        )
    return tools_info


@router.get("/mcp")
async def get_mcp_status() -> Dict[str, Any]:
    """Inspect configured MCP servers and active connections."""
    return get_mcp_manager().get_status()
