"""Human-in-the-Loop approval resolution endpoints."""

import json
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ExecutionOutcomeStatus,
    get_hitl_manager,
)
from jarvis_studio.api.schemas.request import (
    ApprovalDecisionRequest,
    OutcomeAuditRequest,
)
from jarvis_studio.api.schemas.response import ChatResponse
from jarvis_studio.service.agent_service import get_agent_service
from jarvis_studio.service.session_service import get_session_service

router = APIRouter(prefix="/approvals", tags=["Approvals (HITL)"])

_NOT_FOUND_DETAIL = "Approval request not found"


def _raise_not_found() -> NoReturn:
    """Raise a uniform non-leaking 404 error for any missing or cross-thread resource."""
    raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)


def _extract_consistent_thread_id(
    request: Request,
    *,
    body_thread_id: str | None = None,
    query_thread_id: str | None = None,
) -> tuple[str | None, bool]:
    """Validate and reconcile thread_id across request body, query params, and headers.

    Returns:
        (thread_id, any_source_present)
        thread_id is None if sources conflict, are blank, or equal reserved 'default'.
    """
    header_tid = request.headers.get("x-thread-id")
    header_sid = request.headers.get("x-session-id")

    raw_sources = (body_thread_id, query_thread_id, header_tid, header_sid)
    any_present = any(s is not None for s in raw_sources)
    if not any_present:
        return None, False

    normalized: list[str] = []
    for raw in raw_sources:
        if raw is None:
            continue
        cleaned = raw.strip()
        if not cleaned or cleaned.lower() == "default":
            return None, True
        normalized.append(cleaned)

    if len(set(normalized)) != 1:
        # Conflict between caller context (header/query) and payload body
        return None, True

    return normalized[0], True


def _resolve_verified_session_thread_id(
    request: Request,
    *,
    body_thread_id: str | None = None,
    query_thread_id: str | None = None,
) -> str:
    """Resolve thread_id and verify it corresponds to a real server-side session."""
    thread_id, _ = _extract_consistent_thread_id(
        request,
        body_thread_id=body_thread_id,
        query_thread_id=query_thread_id,
    )
    if not thread_id:
        _raise_not_found()

    session_service = get_session_service()
    if session_service.get_session(thread_id) is None:
        _raise_not_found()

    return thread_id


@router.get("", response_model=list[dict[str, Any]])
async def list_pending_approvals(
    request: Request,
    thread_id: str | None = Query(
        default=None, description="Session / Thread identifier owning pending approvals"
    ),
) -> list[dict[str, Any]]:
    """List pending approval requests strictly scoped to the caller's thread_id."""
    resolved_tid, any_present = _extract_consistent_thread_id(
        request, query_thread_id=thread_id
    )
    if not any_present:
        raise HTTPException(
            status_code=400,
            detail="thread_id query parameter or X-Thread-ID header is required",
        )
    if not resolved_tid:
        _raise_not_found()

    session_service = get_session_service()
    if session_service.get_session(resolved_tid) is None:
        return []

    hitl_mgr = get_hitl_manager()
    requests = hitl_mgr.list_pending(thread_id=resolved_tid)
    return [req.model_dump() for req in requests]


@router.get("/{approval_id}", response_model=dict[str, Any])
async def get_approval_request(
    approval_id: str,
    request: Request,
    thread_id: str | None = Query(
        default=None, description="Session / Thread identifier owning the approval"
    ),
) -> dict[str, Any]:
    """Retrieve a single approval request strictly within its owning thread boundary."""
    trusted_tid = _resolve_verified_session_thread_id(
        request, query_thread_id=thread_id
    )
    hitl_mgr = get_hitl_manager()
    req = hitl_mgr.get_request(approval_id, thread_id=trusted_tid)
    if not req:
        _raise_not_found()
    return req.model_dump()


@router.post("/{approval_id}/decision", response_model=ChatResponse)
async def submit_approval_decision(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    thread_id: str | None = Query(default=None),
) -> ChatResponse:
    """Submit a decision (approve, reject, modify) and resume graph execution."""
    trusted_tid = _resolve_verified_session_thread_id(
        request,
        body_thread_id=payload.thread_id,
        query_thread_id=thread_id,
    )
    agent_service = get_agent_service()

    if not await agent_service.verify_approval_context(trusted_tid, approval_id):
        _raise_not_found()

    if payload.decision.lower() not in ("approve", "reject", "modify"):
        raise HTTPException(
            status_code=400,
            detail="Invalid decision; expected 'approve', 'reject', or 'modify'",
        )

    decision = ApprovalDecision(
        decision=payload.decision,
        reason=payload.reason,
        modified_arguments=payload.modified_arguments,
    )

    result = await agent_service.resume_approval(
        thread_id=trusted_tid,
        approval_id=approval_id,
        decision=decision,
    )

    if result.status == "error":
        if result.error == _NOT_FOUND_DETAIL:
            _raise_not_found()
        raise HTTPException(status_code=500, detail=result.error)

    return ChatResponse(
        thread_id=result.thread_id,
        run_id=result.run_id,
        status=result.status,
        output_text=result.output_text,
        pending_approval=result.pending_approval,
        error=result.error,
    )


@router.post("/{approval_id}/decision/stream")
async def submit_approval_decision_stream(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    thread_id: str | None = Query(default=None),
):
    """Submit a decision and resume execution with real-time SSE event streaming."""
    trusted_tid = _resolve_verified_session_thread_id(
        request,
        body_thread_id=payload.thread_id,
        query_thread_id=thread_id,
    )
    agent_service = get_agent_service()

    if not await agent_service.verify_approval_context(trusted_tid, approval_id):
        _raise_not_found()

    if payload.decision.lower() not in ("approve", "reject", "modify"):
        raise HTTPException(
            status_code=400,
            detail="Invalid decision; expected 'approve', 'reject', or 'modify'",
        )

    decision = ApprovalDecision(
        decision=payload.decision,
        reason=payload.reason,
        modified_arguments=payload.modified_arguments,
    )

    async def event_generator():
        async for event in agent_service.stream_resume_approval(
            thread_id=trusted_tid,
            approval_id=approval_id,
            decision=decision,
        ):
            yield {
                "event": event.get("event", "message"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())

@router.get("/{approval_id}/outcome", response_model=dict[str, Any])
async def get_approval_outcome(
    approval_id: str,
    request: Request,
    thread_id: str | None = Query(
        default=None, description="Session / Thread identifier owning the approval"
    ),
) -> dict[str, Any]:
    """Retrieve the execution outcome linked to an approval request within its thread."""
    trusted_tid = _resolve_verified_session_thread_id(
        request, query_thread_id=thread_id
    )
    hitl_mgr = get_hitl_manager()
    outcome = hitl_mgr.get_outcome_by_approval(approval_id, thread_id=trusted_tid)
    if outcome is None:
        _raise_not_found()
    return outcome.model_dump()


@router.post("/{approval_id}/audit", response_model=dict[str, Any])
async def audit_approval_outcome(
    approval_id: str,
    payload: OutcomeAuditRequest,
    request: Request,
    thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Record human audit verification for an uncertain outcome without reactivating approval."""
    trusted_tid = _resolve_verified_session_thread_id(
        request,
        body_thread_id=payload.thread_id,
        query_thread_id=thread_id,
    )
    try:
        target_status = ExecutionOutcomeStatus(payload.verified_status.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid verified_status; expected 'SUCCEEDED', 'FAILED', or 'PARTIAL'",
        ) from exc
    if target_status not in (
        ExecutionOutcomeStatus.SUCCEEDED,
        ExecutionOutcomeStatus.FAILED,
        ExecutionOutcomeStatus.PARTIAL,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid verified_status; expected 'SUCCEEDED', 'FAILED', or 'PARTIAL'",
        )
    hitl_mgr = get_hitl_manager()
    outcome = hitl_mgr.audit_uncertain_outcome(
        thread_id=trusted_tid,
        approval_id=approval_id,
        verified_status=target_status,
        actor=payload.actor,
        note=payload.note,
    )
    if outcome is None:
        _raise_not_found()
    return outcome.model_dump()
