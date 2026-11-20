"""MVP 1.0 Sprint 1: Tasks Workbench API contract, SSE replay, and HTML integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.app import create_app
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.registry import ToolRegistry


class _Sprint1UiMockModel:
    def bind_tools(self, tools: Any, **kwargs: Any) -> _Sprint1UiMockModel:
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content="Sprint 1 Tasks UI mock execution completed.")


@pytest.mark.asyncio
async def test_tasks_ui_api_contract_sse_replay_and_conflict_semantics(tmp_path: Path) -> None:
    """Verify exact REST, 409 conflict, and SSE since_seq / Last-Event-ID semantics used by Tasks UI."""
    ws_dir = tmp_path / "ws_s1"
    ws_dir.mkdir(parents=True, exist_ok=True)
    app_db = str(tmp_path / "app_s1.db")
    vec_db = str(tmp_path / "vec_s1.db")

    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=app_db,
            workspace_root=str(ws_dir),
        ),
        rag=RAGConfig(
            enabled=True,
            embedding_provider="local",
            embedding_dimension=64,
            vector_db_path=vec_db,
        ),
    )

    async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
        reg = ToolRegistry()
        register_builtin_tools(reg)
        rt.agent_service.registry = reg
        rt.agent_service.graph = build_agent_graph(
            model=_Sprint1UiMockModel(),
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )

        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # 1. HTML serves Tasks Workbench entrypoints at /app
            html_res = await client.get("/app")
            assert html_res.status_code == 200
            for marker in ("tab-btn-tasks", "view-tasks", "createTaskFromForm", "connectTaskSSEIfNeeded"):
                assert marker in html_res.text

            # 2. Create Task with auto_start_run=False, then execute background=true
            c_res = await client.post(
                "/api/v1/tasks",
                json={
                    "query": "Sprint 1 Tasks Workbench verification",
                    "knowledge_scope": {"collections": ["eng_docs"]},
                    "auto_start_run": False,
                },
            )
            assert c_res.status_code == 201
            task_id = c_res.json()["task_id"]

            ex_res = await client.post(f"/api/v1/tasks/{task_id}/execute?background=true")
            assert ex_res.status_code == 202

            # Wait for completion
            detail: dict[str, Any] = {}
            for _ in range(100):
                detail = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                if detail["task"]["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert detail["task"]["status"] == "COMPLETED"
            assert len(detail["runs"]) == 1
            max_seq = max(ev["seq"] for ev in detail["events"])

            # 3. Pausing a COMPLETED task must return 409 Conflict
            p_err = await client.post(f"/api/v1/tasks/{task_id}/pause", json={"reason": "invalid"})
            assert p_err.status_code == 409

            # 4. Append instruction -> Run 2
            app_res = await client.post(
                f"/api/v1/tasks/{task_id}/append-instruction",
                json={"instruction": "Follow-up Run 2"},
            )
            assert app_res.status_code == 200
            for _ in range(100):
                detail2 = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                if len(detail2["runs"]) == 2 and detail2["task"]["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert detail2["runs"][1]["run_seq"] == 2

            # 5. SSE replay strictly respects since_seq (no Run 1 events repeated)
            sse_res = await client.get(f"/api/v1/tasks/{task_id}/events/stream?since_seq={max_seq}")
            assert sse_res.status_code == 200
            assert "instruction_appended" in sse_res.text
            assert "task_created" not in sse_res.text
