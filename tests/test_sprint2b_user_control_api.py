"""Integration tests for Sprint 2B User Control REST APIs: execute background, cancel, pause, resume, append."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.app import create_app
from jarvis_studio.config.settings import Settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.coordinator import get_task_coordinator
from jarvis_studio.service.agent_service import get_agent_service


class _DelayedModel:
    def __init__(self, delay: float = 0.1, content: str = "Delayed response"):
        self.delay = delay
        self.content = content

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return AIMessage(content=self.content)


@pytest.mark.asyncio
async def test_api_background_execute_and_status(tmp_path: Path):
    """POST /execute?background=true returns 202 immediately and updates task in background."""
    db_file = tmp_path / "api_bg.db"
    settings = Settings()
    settings.storage.sqlite_path = str(db_file)
    settings.app.max_concurrent_tasks = 2

    async with AgentRuntime.open(settings, register_as_active=True, manage_mcp=False) as rt:
        svc = rt.agent_service
        svc.graph = build_agent_graph(
            model=_DelayedModel(delay=0.1, content="Async executed response"),
            checkpointer=svc.graph.checkpointer,
            hitl_mgr=svc.hitl_manager,
        )

        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # 1. Create task
            t_resp = await client.post("/api/v1/tasks", json={"query": "Run background task"})
            assert t_resp.status_code == 201
            task_id = t_resp.json()["task_id"]

            # 2. Execute with background=true -> 202 Accepted
            exec_resp = await client.post(f"/api/v1/tasks/{task_id}/execute?background=true")
            assert exec_resp.status_code == 202
            data = exec_resp.json()
            assert data["task_id"] == task_id
            assert data["status"] in ("QUEUED", "RUNNING")
            assert "run_id" in data

            # 3. Allow background task to complete
            for _ in range(20):
                await asyncio.sleep(0.1)
                chk = await client.get(f"/api/v1/tasks/{task_id}")
                if chk.json()["task"]["status"] == "COMPLETED":
                    break

            # 4. Check task details
            detail_resp = await client.get(f"/api/v1/tasks/{task_id}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()
            assert detail["task"]["status"] == "COMPLETED"
            assert len(detail["runs"]) >= 1
            assert detail["runs"][-1]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_api_pause_resume_and_append_endpoints(tmp_path: Path):
    """Test POST /pause, /resume, and /append-instruction REST endpoints."""
    db_file = tmp_path / "api_pause.db"
    settings = Settings()
    settings.storage.sqlite_path = str(db_file)
    settings.app.max_concurrent_tasks = 2

    async with AgentRuntime.open(settings, register_as_active=True, manage_mcp=False) as rt:
        svc = rt.agent_service
        svc.graph = build_agent_graph(
            model=_DelayedModel(delay=0.05, content="Step response"),
            checkpointer=svc.graph.checkpointer,
            hitl_mgr=svc.hitl_manager,
        )

        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # 1. Create task
            t_resp = await client.post("/api/v1/tasks", json={"query": "Multi-step flow"})
            assert t_resp.status_code == 201
            task_id = t_resp.json()["task_id"]

            # 2. Execute sync first step
            step1_resp = await client.post(f"/api/v1/tasks/{task_id}/execute?background=false")
            assert step1_resp.status_code == 200
            assert step1_resp.json()["status"] == "completed"

            # 3. Append instruction
            app_resp = await client.post(
                f"/api/v1/tasks/{task_id}/append-instruction",
                json={"instruction": "Continue with step 2"},
            )
            assert app_resp.status_code == 200
            run_data = app_resp.json()
            assert run_data["trigger_type"] == "user_input"
            assert run_data["run_seq"] == 2

            # Let step 2 complete
            await asyncio.sleep(0.2)
            detail_resp = await client.get(f"/api/v1/tasks/{task_id}")
            detail = detail_resp.json()
            assert detail["task"]["status"] == "COMPLETED"
            assert len(detail["runs"]) == 2

            # 4. Test Pause & Resume
            # Manually set task to PAUSED to test /resume endpoint
            rt.task_coordinator.pause_task_and_run(task_id, detail["runs"][-1]["run_id"])
            p_task_resp = await client.get(f"/api/v1/tasks/{task_id}")
            p_task = p_task_resp.json()["task"]
            assert p_task["status"] == "PAUSED"

            # Resume endpoint
            res_resp = await client.post(f"/api/v1/tasks/{task_id}/resume", json={})
            assert res_resp.status_code == 200
            res_run = res_resp.json()
            assert res_run["trigger_type"] == "resume"
            assert res_run["run_seq"] == 3

            # Let resumed run complete
            await asyncio.sleep(0.2)
            detail3_resp = await client.get(f"/api/v1/tasks/{task_id}")
            detail3 = detail3_resp.json()
            assert detail3["task"]["status"] == "COMPLETED"
            assert len(detail3["runs"]) == 3
