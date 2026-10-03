"""Human-in-the-Loop approval resolution endpoints."""

from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException

from jarvis_studio.agent.hitl.manager import ApprovalDecision, get_hitl_manager
from jarvis_studio.api.schemas.request import ApprovalDecisionRequest
from jarvis_studio.api.schemas.response import ChatResponse
from jarvis_studio.service.agent_service import get_agent_service

router = APIRouter(prefix="/approvals", tags=["Approvals (HITL)"])


@router.get("", response_model=List[Dict[str, Any]])
async def list_pending_approvals() -> List[Dict[str, Any]]:
    """List all currently pending human approval requests."""
    hitl_mgr = get_hitl_manager()
    requests = hitl_mgr.list_pending()
    return [req.model_dump() for req in requests]


@router.post("/{approval_id}/decision", response_model=ChatResponse)
async def submit_approval_decision(
    approval_id: str,
    payload: ApprovalDecisionRequest,
) -> ChatResponse:
    """Submit a decision (approve, reject, modify) and resume graph execution."""
    agent_service = get_agent_service()
    hitl_mgr = get_hitl_manager()

    req = hitl_mgr.get_request(approval_id)
    if not req:
        raise HTTPException(
            status_code=404,
            detail=f"Approval request '{approval_id}' not found",
        )

    decision = ApprovalDecision(
        decision=payload.decision,
        reason=payload.reason,
        modified_arguments=payload.modified_arguments,
    )

    result = await agent_service.resume_approval(
        thread_id=payload.thread_id,
        approval_id=approval_id,
        decision=decision,
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
