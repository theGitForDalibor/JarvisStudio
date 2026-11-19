"""Task management and execution monitoring API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from jarvis_studio.core.tasks.models import AgentRun, AgentTask, DeliverableRecord, EventRecord
from jarvis_studio.service.deliverable_manager import get_deliverable_manager
from jarvis_studio.service.task_service import get_task_service

router = APIRouter(prefix="/tasks", tags=["Tasks"])


class CreateTaskRequest(BaseModel):
    """Payload to create an independent or session-linked task."""

    query: str = Field(..., min_length=1, description="User goal or query for the task")
    session_id: str | None = Field(default=None, description="Optional associated chat session ID")
    assigned_agent_id: str = Field(default="general_agent", description="Assigned agent identifier")
    priority: str = Field(default="NORMAL", description="Task priority")
    plan_json: str | None = Field(default=None, description="Optional structured plan JSON")
    auto_start_run: bool = Field(default=False, description="Whether to start an initial execution run immediately")


class CancelTaskRequest(BaseModel):
    """Payload to cancel an active task."""

    reason: str = Field(default="Cancelled by user", description="Reason for cancelling the task")


class TaskDetailResponse(BaseModel):
    """Detailed task entity with execution runs, deliverables, and event logs."""

    task: AgentTask
    runs: list[AgentRun]
    deliverables: list[DeliverableRecord]
    events: list[EventRecord]


@router.post("", response_model=AgentTask, status_code=201)
async def create_task(request: CreateTaskRequest) -> AgentTask:
    """Create a new task, either stand-alone or linked to a session."""
    task_service = get_task_service()
    try:
        task, _ = task_service.create_task(
            query=request.query,
            session_id=request.session_id,
            assigned_agent_id=request.assigned_agent_id,
            priority=request.priority,
            plan_json=request.plan_json,
            auto_start_run=request.auto_start_run,
        )
        return task
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.get("", response_model=list[AgentTask])
async def list_tasks(
    session_id: str | None = Query(default=None, description="Filter by session ID"),
    status: str | None = Query(default=None, description="Filter by task status"),
    limit: int = Query(default=50, ge=1, le=200, description="Max items to return"),
) -> list[AgentTask]:
    """List tasks, optionally filtered by session or status."""
    task_service = get_task_service()
    return task_service.list_tasks(session_id=session_id, status=status, limit=limit)


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task_detail(task_id: str) -> TaskDetailResponse:
    """Retrieve complete task details including runs, deliverables, and events."""
    task_service = get_task_service()
    detail = task_service.get_task_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return TaskDetailResponse(
        task=detail["task"],
        runs=detail["runs"],
        deliverables=detail["deliverables"],
        events=detail["events"],
    )


@router.post("/{task_id}/cancel", response_model=AgentTask)
async def cancel_task(
    task_id: str,
    request: CancelTaskRequest | None = None,
) -> AgentTask:
    """Cancel an active or pending task."""
    task_service = get_task_service()
    reason = request.reason if request else "Cancelled by user"
    try:
        return task_service.cancel_task(task_id, reason=reason)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    """Permanently delete a task, its runs, database records, and physical deliverables."""
    task_service = get_task_service()
    try:
        deleted = task_service.delete_task(task_id)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "deleted", "task_id": task_id}


@router.get("/{task_id}/events", response_model=list[EventRecord])
async def list_task_events(
    task_id: str,
    since_seq: int = Query(default=0, ge=0, description="Return events strictly after this seq number"),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum events to return"),
) -> list[EventRecord]:
    """Retrieve incremental execution events for task observability."""
    task_service = get_task_service()
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task_service.list_events(task_id, since_seq=since_seq, limit=limit)


@router.get("/{task_id}/deliverables", response_model=list[DeliverableRecord])
async def list_task_deliverables(
    task_id: str,
    run_id: str | None = Query(default=None, description="Filter by run ID"),
) -> list[DeliverableRecord]:
    """Retrieve deliverables for a task."""
    task_service = get_task_service()
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    deliv_mgr = get_deliverable_manager()
    return deliv_mgr.list_deliverables_for_task(task_id, run_id=run_id)
