"""FastAPI Application factory for Jarvis Studio."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jarvis_studio.api.routes.approvals import router as approvals_router
from jarvis_studio.api.routes.chat import router as chat_router
from jarvis_studio.api.routes.system import router as system_router
from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.registry import get_default_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle hook."""
    settings = get_settings()
    logger.info(f"Starting {settings.app.name} v{settings.app.version} ({settings.app.environment})")
    
    # Auto-register builtin tools
    register_builtin_tools(get_default_registry())
    
    yield

    logger.info(f"Shutting down {settings.app.name}")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description="Personal Production AI Operating Assistant API Gateway",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register Routers
    api_prefix = "/api/v1"
    app.include_router(system_router, prefix=api_prefix)
    app.include_router(chat_router, prefix=api_prefix)
    app.include_router(approvals_router, prefix=api_prefix)

    @app.get("/")
    async def root():
        return {
            "name": settings.app.name,
            "version": settings.app.version,
            "status": "online",
            "docs_url": "/docs",
        }

    return app
