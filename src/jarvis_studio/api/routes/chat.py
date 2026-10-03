"""Chat and agent execution API endpoints."""

from fastapi import APIRouter, HTTPException
from jarvis_studio.api.schemas.request import ChatRequest
from jarvis_studio.api.schemas.response import ChatResponse
from jarvis_studio.service.agent_service import get_agent_service
from jarvis_studio.service.session_service import get_session_service

router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse)
async def chat_with_agent(request: ChatRequest) -> ChatResponse:
    """Send an instruction to the Jarvis Agent."""
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
