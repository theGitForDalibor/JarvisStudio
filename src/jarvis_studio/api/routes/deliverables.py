"""Deliverable metadata and safe file download API endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from jarvis_studio.core.tasks.coordinator import get_task_coordinator
from jarvis_studio.core.tasks.models import DeliverableRecord
from jarvis_studio.rag.provenance import verify_report_provenance
from jarvis_studio.service.deliverable_manager import (
    _resolve_logical_key,
    get_deliverable_manager,
)

router = APIRouter(prefix="/deliverables", tags=["Deliverables"])


@router.get("", response_model=dict[str, Any])
async def list_deliverables(
    task_id: str | None = Query(default=None, description="Optional task_id filter"),
    deliverable_type: str | None = Query(default=None, description="Optional deliverable_type filter"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List deliverables globally across tasks with pagination and Task/Run metadata."""
    deliv_mgr = get_deliverable_manager()
    return deliv_mgr.list_all_deliverables(
        task_id=task_id,
        deliverable_type=deliverable_type,
        limit=limit,
        offset=offset,
    )


@router.get("/{deliverable_id}", response_model=DeliverableRecord)
async def get_deliverable(deliverable_id: str) -> DeliverableRecord:
    """Retrieve deliverable metadata, summary, and content preview."""
    deliv_mgr = get_deliverable_manager()
    record = deliv_mgr.get_deliverable(deliverable_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deliverable '{deliverable_id}' not found")
    return record


@router.get("/{deliverable_id}/download")
async def download_deliverable(deliverable_id: str) -> FileResponse:
    """Safely stream and download a deliverable file from disk."""
    deliv_mgr = get_deliverable_manager()
    record = deliv_mgr.get_deliverable(deliverable_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deliverable '{deliverable_id}' not found")

    try:
        abs_path = deliv_mgr.get_deliverable_file_path(deliverable_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except (PermissionError, ValueError) as err:
        raise HTTPException(status_code=403, detail=f"Access denied: {err}") from err
    except FileNotFoundError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    download_name = Path(record.file_path).name if record.file_path else f"{deliverable_id}.bin"
    return FileResponse(
        path=abs_path,
        filename=download_name,
        media_type=record.mime_type or "application/octet-stream",
    )


@router.get("/{deliverable_id}/content", response_model=dict[str, Any])
async def get_deliverable_content_and_provenance(deliverable_id: str) -> dict[str, Any]:
    """Return deliverable full text preview, logical versions, Task/Run context, and Run-scoped provenance."""
    deliv_mgr = get_deliverable_manager()
    try:
        record, text = deliv_mgr.read_deliverable_preview_text(deliverable_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except UnicodeDecodeError as err:
        raise HTTPException(
            status_code=415,
            detail="Binary or non-UTF-8 deliverable cannot be previewed as text; please download the file.",
        ) from err
    except (PermissionError, ValueError) as err:
        msg = str(err)
        if "exceeds limit" in msg:
            raise HTTPException(status_code=413, detail=msg) from err
        raise HTTPException(status_code=403, detail=f"Access denied: {err}") from err
    except FileNotFoundError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    coord = get_task_coordinator()
    task = coord.get_task(record.task_id)
    run = coord.get_run(record.run_id) if record.run_id else None
    events = coord.list_events_for_task(record.task_id)
    versions = deliv_mgr.list_logical_versions(deliverable_id)

    prov = verify_report_provenance(
        text,
        events,
        run_id=record.run_id,
        require_at_least_one_citation=True,
    )
    if prov.verified:
        prov_status = "verified"
    elif prov.unverified_citations:
        prov_status = "unverified"
    elif prov.retrieved_chunk_count == 0 and prov.retrieved_url_count == 0:
        prov_status = "unavailable"
    else:
        prov_status = "unverified"

    return {
        "deliverable": record.model_dump(),
        "logical_key": _resolve_logical_key(
            deliverable_type=record.deliverable_type,
            file_path=record.file_path,
            title=record.title,
            metadata_json=record.metadata_json,
        ),
        "task_query": task.query if task else "",
        "task_status": task.status.value if task else "",
        "run_seq": run.run_seq if run else None,
        "run_status": run.status.value if run else "",
        "content": text,
        "versions": versions,
        "provenance": {
            "status": prov_status,
            "verified": prov.verified,
            "run_id": prov.run_id,
            "retrieved_chunk_count": prov.retrieved_chunk_count,
            "retrieved_url_count": prov.retrieved_url_count,
            "verified_doc_ids": prov.verified_doc_ids,
            "verified_chunk_ids": prov.verified_chunk_ids,
            "verified_urls": prov.verified_urls,
            "verified_quotes": prov.verified_quotes,
            "unverified_citations": prov.unverified_citations,
        },
    }
