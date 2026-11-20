"""MVP 1.0 Sprint 2: Deliverables Center API, logical versioning, security boundaries, and provenance tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jarvis_studio.api.app import create_app
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.models import DeliverableType, EventType, RunTriggerType
from jarvis_studio.service.deliverable_manager import (
    get_deliverable_manager,
    reset_global_deliverable_manager,
)


@pytest.mark.asyncio
async def test_deliverables_global_list_logical_versions_provenance_and_security(
    tmp_path: Path,
) -> None:
    """Verify global list, logical_key version separation, Run-scoped provenance, and content security."""
    ws_dir = tmp_path / "ws_s2"
    ws_dir.mkdir(parents=True, exist_ok=True)
    app_db = str(tmp_path / "app_s2.db")
    vec_db = str(tmp_path / "vec_s2.db")

    from jarvis_studio.config.settings import get_settings

    s = get_settings()
    orig = (s.storage.workspace_root, s.storage.sqlite_path, s.rag.vector_db_path)
    s.storage.workspace_root = str(ws_dir)
    s.storage.sqlite_path = app_db
    s.rag.vector_db_path = vec_db
    reset_global_deliverable_manager()

    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=app_db,
            workspace_root=str(ws_dir),
        ),
        rag=RAGConfig(enabled=False, vector_db_path=vec_db),
    )

    try:
        async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
            coord = rt.task_coordinator
            deliv_mgr = get_deliverable_manager()

            # Create Task and Run #1 with a real fetch_web_page tool output
            task = coord.create_task(query="Multi-deliverable research task", thread_id="th_s2")
            run1 = coord.start_run(task.task_id, runtime_id=rt.instance_id, trigger_type=RunTriggerType.INITIAL)
            coord.record_event(
                task.task_id,
                EventType.TOOL_COMPLETED,
                run_id=run1.run_id,
                payload={
                    "tool_name": "fetch_web_page",
                    "tool_call_id": "call_f1",
                    "success": True,
                    "output_preview": "URL: https://www.example.com\nExample Domain",
                },
            )

            # Run 1 creates TWO distinct logical deliverables
            d_arch_v1 = deliv_mgr.create_deliverable(
                task_id=task.task_id,
                run_id=run1.run_id,
                run_seq=1,
                title="Architecture Report",
                content="# Architecture v1\n\nVerified source: https://www.example.com\n",
                deliverable_type=DeliverableType.MARKDOWN_REPORT,
            )
            d_sec_v1 = deliv_mgr.create_deliverable(
                task_id=task.task_id,
                run_id=run1.run_id,
                run_seq=1,
                title="Security Checklist",
                content="- Item 1: TLS verified\n",
                deliverable_type=DeliverableType.MARKDOWN_REPORT,
            )
            coord.complete_task_and_run(task.task_id, run1.run_id)

            # Run 2 updates ONLY Architecture Report (no retrieval tool outputs in Run 2)
            run2 = coord.start_run(
                task.task_id,
                runtime_id=rt.instance_id,
                trigger_type=RunTriggerType.USER_INPUT,
            )
            d_arch_v2 = deliv_mgr.create_deliverable(
                task_id=task.task_id,
                run_id=run2.run_id,
                run_seq=2,
                title="Architecture Report",
                content="# Architecture v2\n\nUnverified claim: https://forged.example.com\n",
                deliverable_type=DeliverableType.MARKDOWN_REPORT,
            )
            coord.complete_task_and_run(task.task_id, run2.run_id)

            # Binary deliverable for 415 test
            d_bin = deliv_mgr.create_deliverable(
                task_id=task.task_id,
                run_id=run2.run_id,
                run_seq=2,
                title="binary.dat",
                content=b"\xff\xfe\x00\x01binary",
                deliverable_type=DeliverableType.FILE,
                file_name="binary.dat",
                mime_type="application/octet-stream",
            )

            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # 1. HTML serves Deliverables Center
                html = (await client.get("/app")).text
                for marker in ("tab-btn-deliverables", "view-deliverables", "sanitizeMarkdownToSafeHtml"):
                    assert marker in html

                # 2. Global list + pagination + filtering
                list_res = await client.get("/api/v1/deliverables", params={"limit": 2, "offset": 0})
                assert list_res.status_code == 200
                list_data = list_res.json()
                assert list_data["total"] == 4
                assert len(list_data["items"]) == 2

                # 3. Logical version separation + Run-scoped provenance
                c_v1 = (await client.get(f"/api/v1/deliverables/{d_arch_v1.deliverable_id}/content")).json()
                assert len(c_v1["versions"]) == 2
                assert [v["run_seq"] for v in c_v1["versions"]] == [1, 2]
                assert c_v1["provenance"]["status"] == "verified"
                assert "https://www.example.com" in c_v1["provenance"]["verified_urls"]

                c_v2 = (await client.get(f"/api/v1/deliverables/{d_arch_v2.deliverable_id}/content")).json()
                assert len(c_v2["versions"]) == 2
                assert c_v2["provenance"]["status"] == "unverified"
                assert c_v2["provenance"]["verified"] is False

                c_sec = (await client.get(f"/api/v1/deliverables/{d_sec_v1.deliverable_id}/content")).json()
                assert len(c_sec["versions"]) == 1
                assert c_sec["provenance"]["status"] == "unverified"

                # 4. Download endpoint
                dl_res = await client.get(f"/api/v1/deliverables/{d_arch_v1.deliverable_id}/download")
                assert dl_res.status_code == 200
                assert "Architecture v1" in dl_res.text

                # 5. Security & Error boundaries: binary (415), size limit, missing file (404), traversal (403)
                bin_res = await client.get(f"/api/v1/deliverables/{d_bin.deliverable_id}/content")
                assert bin_res.status_code == 415

                with pytest.raises(ValueError, match="exceeds limit"):
                    deliv_mgr.read_deliverable_preview_text(d_arch_v1.deliverable_id, max_bytes=10)

                (ws_dir / d_sec_v1.file_path).unlink()
                miss_res = await client.get(f"/api/v1/deliverables/{d_sec_v1.deliverable_id}/content")
                assert miss_res.status_code == 404

                with deliv_mgr._tx() as conn:
                    conn.execute(
                        "UPDATE agent_deliverables SET file_path = ? WHERE deliverable_id = ?",
                        ("../outside.txt", d_arch_v2.deliverable_id),
                    )
                esc_res = await client.get(f"/api/v1/deliverables/{d_arch_v2.deliverable_id}/content")
                assert esc_res.status_code == 403
    finally:
        s.storage.workspace_root, s.storage.sqlite_path, s.rag.vector_db_path = orig
        reset_global_deliverable_manager()
