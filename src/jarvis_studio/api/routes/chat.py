"""Chat and agent execution API endpoints."""

import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from jarvis_studio.api.schemas.request import ChatRequest
from jarvis_studio.api.schemas.response import ChatResponse
from jarvis_studio.service.agent_service import get_agent_service
from jarvis_studio.service.session_service import get_session_service

router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse)
async def chat_with_agent(request: ChatRequest) -> ChatResponse:
    """Send an instruction to the Jarvis Agent and wait for complete execution."""
    session_service = get_session_service()
    agent_service = get_agent_service()

    session = session_service.get_or_create_session(request.session_id)
    thread_id = session.session_id

    result = await agent_service.invoke_agent(
        thread_id=thread_id,
        user_message=request.message,
    )

    if result.status == "error":
        raise HTTPException(status_code=500, detail=result.error)

    return ChatResponse(
        thread_id=result.thread_id,
        run_id=result.run_id,
        status=result.status,
        output_text=result.output_text,
        pending_approval=result.pending_approval,
        error=result.error,
    )


@router.post("/stream")
async def chat_stream_with_agent(request: ChatRequest):
    """Send an instruction and stream real-time Server-Sent Events (SSE) (tokens and steps)."""
    session_service = get_session_service()
    agent_service = get_agent_service()

    session = session_service.get_or_create_session(request.session_id)
    thread_id = session.session_id

    async def event_generator():
        async for event in agent_service.stream_agent(
            thread_id=thread_id,
            user_message=request.message,
        ):
            yield {
                "event": event.get("event", "message"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
