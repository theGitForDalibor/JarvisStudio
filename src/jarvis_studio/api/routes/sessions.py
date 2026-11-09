"""Session management and conversational history API endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis_studio.service.session_service import (
    ChatMessageRecord,
    SessionMetadata,
    get_session_service,
)

router = APIRouter(prefix="/sessions", tags=["Sessions"])


class CreateSessionRequest(BaseModel):
    """Payload to create a new session."""

    title: str | None = Field(default=None, description="Optional custom session title")


class UpdateSessionTitleRequest(BaseModel):
    """Payload to update an existing session title."""

    title: str = Field(..., min_length=1, max_length=100, description="New title for the session")


class SessionDetailResponse(BaseModel):
    """Detailed session info including message history."""

    session: SessionMetadata
    messages: list[ChatMessageRecord]


@router.get("", response_model=list[SessionMetadata])
async def list_sessions() -> list[SessionMetadata]:
    """Retrieve all tracked sessions sorted by most recent activity."""
    session_service = get_session_service()
    return session_service.list_sessions()


@router.post("", response_model=SessionMetadata)
async def create_session(request: CreateSessionRequest | None = None) -> SessionMetadata:
    """Create a new session thread."""
    session_service = get_session_service()
    title = request.title if request else None
    return session_service.create_session(title=title)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(session_id: str) -> SessionDetailResponse:
    """Get metadata and chat history for a session."""
    session_service = get_session_service()
    sess = session_service.get_session(session_id)
    if not sess:
        # If it doesn't exist yet, instantiate it gracefully
        sess = session_service.get_or_create_session(session_id)

    messages = session_service.get_messages(session_id)
    return SessionDetailResponse(session=sess, messages=messages)


@router.patch("/{session_id}", response_model=SessionMetadata)
async def update_session_title(
    session_id: str, request: UpdateSessionTitleRequest
) -> SessionMetadata:
    """Rename an active session thread."""
    session_service = get_session_service()
    updated = session_service.update_session_title(session_id, request.title)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return updated


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    """Delete a session thread and its conversation history."""
    session_service = get_session_service()
    success = session_service.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"status": "deleted", "session_id": session_id}


@router.delete("/{session_id}/messages")
async def clear_session_messages(session_id: str) -> dict[str, Any]:
    """Clear message history of a session while keeping the session metadata."""
    session_service = get_session_service()
    sess = session_service.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    session_service.clear_session_messages(session_id)
    return {"status": "cleared", "session_id": session_id}
