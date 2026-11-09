"""API routes package."""

from jarvis_studio.api.routes.approvals import router as approvals_router
from jarvis_studio.api.routes.chat import router as chat_router
from jarvis_studio.api.routes.memory import router as memory_router
from jarvis_studio.api.routes.rag import router as rag_router
from jarvis_studio.api.routes.sessions import router as sessions_router
from jarvis_studio.api.routes.system import router as system_router

__all__ = [
    "approvals_router",
    "chat_router",
    "memory_router",
    "rag_router",
    "sessions_router",
    "system_router",
]
