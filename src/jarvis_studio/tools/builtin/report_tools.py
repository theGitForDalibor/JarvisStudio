"""Deliverable publishing tools for Research & Analysis and other agent deliverables."""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from jarvis_studio.core.tasks.models import DeliverableType
from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata

_logger = logging.getLogger(__name__)


@tool
def publish_research_report(
    task_id: str,
    title: str,
    markdown_content: str,
    summary: str = "",
    run_id: str | None = None,
) -> str:
    """Publish a completed research analysis as a formal durable Deliverable.

    The deliverable is atomically stored in the workspace filesystem under
    `workspace/deliverables/{task_id}/run_{run_seq}/{filename}.md` and indexed in the
    database with a SHA-256 fingerprint and content preview.

    Args:
        task_id: The ID of the task this report belongs to.
        title: Title of the research report (e.g. '2026 Edge Vector DB Benchmark').
        markdown_content: Complete markdown document content including citations and headers.
        summary: Optional high-level executive summary (if omitted, extracted automatically).
        run_id: Optional run ID associated with this delivery round.
    """
    clean_task_id = task_id.strip()
    clean_title = title.strip()
    clean_content = markdown_content.strip()

    if not clean_task_id:
        return "Error: task_id cannot be empty when publishing a report."
    if not clean_title:
        return "Error: report title cannot be empty."
    if not clean_content:
        return "Error: markdown_content cannot be empty."

    from jarvis_studio.service.deliverable_manager import get_deliverable_manager

    deliv_mgr = get_deliverable_manager()
    try:
        record = deliv_mgr.create_deliverable(
            task_id=clean_task_id,
            run_id=run_id,
            title=clean_title,
            content=clean_content,
            deliverable_type=DeliverableType.MARKDOWN_REPORT,
            metadata={"source": "publish_research_report", "custom_summary": summary} if summary else None,
        )
        return (
            f"Successfully published research deliverable!\n"
            f"- Deliverable ID: {record.deliverable_id}\n"
            f"- Title: {record.title}\n"
            f"- Type: {record.deliverable_type.value}\n"
            f"- File Path: {record.file_path}\n"
            f"- Size: {record.file_size_bytes} bytes\n"
            f"- SHA-256: {record.sha256_hash}\n"
            f"- Preview: {record.summary}"
        )
    except KeyError as err:
        return f"Error publishing deliverable: Task not found ({err})"
    except Exception as exc:
        _logger.error("Failed to publish research report for task %s: %s", clean_task_id, exc)
        return f"Error publishing deliverable: {exc}"


mark_tool_metadata(
    publish_research_report,
    risk_level=RiskLevel.MEDIUM,
    category="deliverables",
    description="Publish and persist formal research markdown deliverables into the task workspace.",
)
