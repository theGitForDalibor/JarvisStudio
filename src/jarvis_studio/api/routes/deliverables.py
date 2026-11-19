"""Deliverable metadata and safe file download API endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from jarvis_studio.core.tasks.models import DeliverableRecord
from jarvis_studio.service.deliverable_manager import get_deliverable_manager

router = APIRouter(prefix="/deliverables", tags=["Deliverables"])


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
