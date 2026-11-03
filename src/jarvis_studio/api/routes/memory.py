"""Long-Term Memory (LTM) API endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis_studio.memory.manager import get_memory_manager
from jarvis_studio.memory.models import MemoryCategory

router = APIRouter(prefix="/memory", tags=["Long-Term Memory"])


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., description="The content/fact/preference to store")
    category: str = Field(default="preference", description="Memory category")
    source: str | None = Field(default=None)
    importance: float = Field(default=1.0, ge=0.0, le=1.0)


class SearchMemoryRequest(BaseModel):
    query: str
    category: str | None = None
    limit: int = 5


@router.get("", response_model=list[dict[str, Any]])
async def list_memories(
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieve stored user memories."""
    mgr = get_memory_manager()
    cat_enum = None
    if category:
        try:
            cat_enum = MemoryCategory(category.lower())
        except ValueError:
            cat_enum = None

    items = mgr.list_all(category=cat_enum, limit=limit)
    return [i.model_dump() for i in items]


@router.post("", response_model=dict[str, Any])
async def create_memory(payload: CreateMemoryRequest) -> dict[str, Any]:
    """Record a new piece of persistent memory."""
    mgr = get_memory_manager()
    try:
        cat_enum = MemoryCategory(payload.category.lower())
    except ValueError:
        cat_enum = MemoryCategory.PREFERENCE

    item = mgr.remember(
        content=payload.content,
        category=cat_enum,
        source=payload.source,
        importance=payload.importance,
    )
    return item.model_dump()


@router.post("/search", response_model=list[dict[str, Any]])
async def search_memories(payload: SearchMemoryRequest) -> list[dict[str, Any]]:
    """Search stored memories by query string."""
    mgr = get_memory_manager()
    cat_enum = None
    if payload.category:
        try:
            cat_enum = MemoryCategory(payload.category.lower())
        except ValueError:
            cat_enum = None

    items = mgr.recall(query=payload.query, category=cat_enum, limit=payload.limit)
    return [i.model_dump() for i in items]


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str) -> dict[str, Any]:
    """Delete a memory record by ID."""
    mgr = get_memory_manager()
    success = mgr.forget(memory_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"status": "deleted", "id": memory_id}
