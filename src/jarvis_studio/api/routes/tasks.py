"""Task management and execution monitoring API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
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


class PauseTaskRequest(BaseModel):
    """Payload to pause an active task."""

    reason: str = Field(default="Paused by user", description="Reason for pausing the task")


class ResumeTaskRequest(BaseModel):
    """Payload to resume a paused or interrupted task."""

    checkpoint_id: str | None = Field(default=None, description="Optional checkpoint to resume from")


class AppendInstructionRequest(BaseModel):
    """Payload to append follow-up user instruction to an existing task."""

    instruction: str = Field(..., min_length=1, description="Follow-up instruction for the task")
    checkpoint_id: str | None = Field(default=None, description="Optional checkpoint to build upon")


class ExecuteTaskRequest(BaseModel):
    """Payload to execute or retry a task through AgentRuntime."""

    user_message: str | None = Field(default=None, description="Optional override/follow-up input query")
    fresh_checkpoint: bool = Field(
        default=False,
        description="Whether to clear prior LangGraph thread checkpoints before executing",
    )


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
    include_archived: bool = Query(default=False, description="Include soft-deleted/archived tasks"),
    limit: int = Query(default=50, ge=1, le=200, description="Max items to return"),
) -> list[AgentTask]:
    """List tasks, optionally filtered by session, status, or archive state."""
    task_service = get_task_service()
    return task_service.list_tasks(
        session_id=session_id,
        status=status,
        include_archived=include_archived,
        limit=limit,
    )


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
        return await task_service.cancel_task_async(task_id, reason=reason)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@router.post("/{task_id}/pause", response_model=AgentTask)
async def pause_task(
    task_id: str,
    request: PauseTaskRequest | None = None,
) -> AgentTask:
    """Pause an active or queued task."""
    task_service = get_task_service()
    reason = request.reason if request else "Paused by user"
    try:
        return await task_service.pause_task(task_id, reason=reason)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except (ValueError, RuntimeError) as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.post("/{task_id}/resume", response_model=AgentRun)
async def resume_task(
    task_id: str,
    request: ResumeTaskRequest | None = None,
) -> AgentRun:
    """Resume a paused or interrupted task with a new run inheriting the checkpoint."""
    task_service = get_task_service()
    checkpoint_id = request.checkpoint_id if request else None
    try:
        return await task_service.resume_task(task_id, checkpoint_id=checkpoint_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except (ValueError, RuntimeError) as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.post("/{task_id}/append-instruction", response_model=AgentRun)
async def append_instruction(
    task_id: str,
    request: AppendInstructionRequest,
) -> AgentRun:
    """Append a follow-up instruction to an existing task and queue a new run."""
    task_service = get_task_service()
    try:
        return await task_service.append_instruction(
            task_id,
            request.instruction,
            checkpoint_id=request.checkpoint_id,
        )
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except (ValueError, RuntimeError) as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.post("/{task_id}/execute", response_model=dict[str, Any])
async def execute_task(
    task_id: str,
    request: ExecuteTaskRequest | None = None,
    background: bool = Query(default=False, description="Whether to execute asynchronously in background"),
) -> Any:
    """Execute a task directly via AgentRuntime / AgentService, or queue it for background processing."""
    task_service = get_task_service()
    payload = request or ExecuteTaskRequest()
    try:
        if background:
            run, _ = await task_service.submit_task(
                task_id,
                user_message=payload.user_message,
                fresh_checkpoint=payload.fresh_checkpoint,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "task_id": task_id,
                    "run_id": run.run_id,
                    "status": run.status.value,
                    "run_seq": run.run_seq,
                    "trigger_type": run.trigger_type.value,
                },
            )

        result = await task_service.execute_task(
            task_id,
            user_message=payload.user_message,
            fresh_checkpoint=payload.fresh_checkpoint,
        )
        return result.to_dict()
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except (ValueError, RuntimeError) as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    """Soft-delete (archive) a task while preserving its Runs and physical deliverables."""
    task_service = get_task_service()
    try:
        archived = task_service.archive_task(task_id)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    if archived is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {
        "status": "archived",
        "task_id": task_id,
        "archived_at": archived.archived_at,
    }


@router.delete("/{task_id}/purge")
async def purge_task(
    task_id: str,
    confirm: bool = Query(default=False, description="Explicit confirmation required for permanent purge"),
) -> dict[str, Any]:
    """Permanently purge a task, its DB records, and its physical deliverables directory."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Permanent task purge requires confirm=true",
        )
    task_service = get_task_service()
    try:
        purged = task_service.purge_task(task_id, confirm=True)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except OSError as err:
        raise HTTPException(
            status_code=500,
            detail=f"Failed purging task deliverables safely (database rolled back): {err}",
        ) from err

    if not purged:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "purged", "task_id": task_id}


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
