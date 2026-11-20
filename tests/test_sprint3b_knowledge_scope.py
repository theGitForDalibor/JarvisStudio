"""Phase 3B-2 tests: Schema V6 and Server-Side Enforced Agent Knowledge Scope."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.routes.tasks import router as tasks_router
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings, get_settings
from jarvis_studio.core.persistence.migrations import APP_SCHEMA_VERSION, run_app_migrations
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.models import EventType, TaskStatus
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings
from jarvis_studio.rag.parser import ParsedSection
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.registry import ToolRegistry


class _ScopeProbeMockModel(BaseChatModel):
    """Simulates an Agent attempting to widen/escape its Knowledge Scope across Runs."""

    turn: int = 0

    @property
    def _llm_type(self) -> str:
        return "mock_scope_probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScopeProbeMockModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        step = self.turn
        self.turn += 1

        if step == 0:
            # Attempt 1: explicitly pass forbidden collection 'secret_col'
            # Attempt 2: omit collection completely while querying secret keyword
            # Attempt 3: list_indexed_documents to enumerate out-of-scope docs
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Probing knowledge base",
                            tool_calls=[
                                {
                                    "id": "call_escape_1",
                                    "name": "search_knowledge_base",
                                    "args": {"query": "TOP_SECRET_KEY", "collection": "secret_col"},
                                },
                                {
                                    "id": "call_escape_2",
                                    "name": "search_knowledge_base",
                                    "args": {"query": "TOP_SECRET_KEY"},
                                },
                                {
                                    "id": "call_list_1",
                                    "name": "list_indexed_documents",
                                    "args": {},
                                },
                            ],
                        )
                    )
                ]
            )
        if step == 1:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="Run 1 completed within scope."))]
            )
        if step == 2:
            # Run 2 (after append_instruction): try escaping again!
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Run 2 probing",
                            tool_calls=[
                                {
                                    "id": "call_escape_3",
                                    "name": "search_knowledge_base",
                                    "args": {"query": "TOP_SECRET_KEY", "doc_id": "doc_secret_1"},
                                }
                            ],
                        )
                    )
                ]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="Run 2 completed within scope."))]
        )


def test_schema_v6_migration_adds_knowledge_scope_column(tmp_path: Path) -> None:
    """Verify Schema V6 adds knowledge_scope_json idempotently."""
    db_path = tmp_path / "v6_test.db"
    ver = run_app_migrations(db_path)
    assert ver == APP_SCHEMA_VERSION == 6

    with sqlite3.connect(str(db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()}
        assert "knowledge_scope_json" in cols


@pytest.mark.asyncio
async def test_task_api_knowledge_scope_and_server_side_enforcement(tmp_path: Path) -> None:
    """Verify Task knowledge_scope persists via API, blocks out-of-scope access, and inherits on append_instruction."""
    settings = get_settings()
    orig_vec = settings.rag.vector_db_path
    orig_dim = settings.rag.embedding_dimension
    orig_prov = settings.rag.embedding_provider

    vec_db = str(tmp_path / "vec_scope.db")
    app_db = str(tmp_path / "app_scope.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    settings.rag.vector_db_path = vec_db
    settings.rag.embedding_dimension = 64
    settings.rag.embedding_provider = "local"

    # Seed two collections in vector DB: 'allowed_col' and 'secret_col'
    store = SqliteVecStore(db_path=vec_db, dimension=64)
    embedder = DeterministicLocalEmbeddings(dimension=64)
    splitter = DocumentSplitter(chunk_size=300, chunk_overlap=30)

    c_allowed = splitter.split_sections(
        [ParsedSection(content="Public architecture guide for allowed collection.", page_number=1)],
        doc_path="allowed/guide.md",
    )
    store.add_chunks(
        c_allowed,
        embedder.embed_documents([c.content for c in c_allowed]),
        file_hash="h_allow",
        title="Allowed Guide",
        collection="allowed_col",
        doc_id="doc_allowed_1",
    )

    c_secret = splitter.split_sections(
        [ParsedSection(content="TOP_SECRET_KEY=sk-live-do-not-leak", page_number=1)],
        doc_path="secret/keys.md",
    )
    store.add_chunks(
        c_secret,
        embedder.embed_documents([c.content for c in c_secret]),
        file_hash="h_secret",
        title="Secret Keys",
        collection="secret_col",
        doc_id="doc_secret_1",
    )
    store.close()

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
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

    try:
        coord = rt.task_coordinator
        engine = rt.execution_engine
        reg = ToolRegistry()
        register_builtin_tools(reg)

        mock_llm = _ScopeProbeMockModel()
        graph = build_agent_graph(
            model=mock_llm,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )
        svc = AgentService(
            graph=graph,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_manager=rt.hitl_manager,
            task_coordinator=coord,
            session_service=rt.agent_service.session_service,
        )
        engine.set_agent_service(svc)

        # 1. Create scoped Task via REST API
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            create_res = await client.post(
                "/api/v1/tasks",
                json={
                    "query": "Check allowed docs",
                    "knowledge_scope": {"collections": ["allowed_col"]},
                },
            )
            assert create_res.status_code == 201
            task_id = create_res.json()["task_id"]
            assert create_res.json()["knowledge_scope_json"] is not None

        # 2. Run 1: Agent attempts to bypass scope
        _, fut1 = await engine.submit_task(task_id, return_future=True)
        assert fut1 is not None
        res1 = await asyncio.wait_for(fut1, timeout=10.0)
        assert res1.status == "completed"

        events1 = coord.list_events_for_task(task_id)
        tool_outputs1 = [
            json.loads(e.payload_json)
            for e in events1
            if e.event_type == EventType.TOOL_COMPLETED
        ]
        # Verify 'secret_col' request was explicitly denied
        escape1 = next(p for p in tool_outputs1 if p["tool_call_id"] == "call_escape_1")
        assert "Access denied" in escape1["output_preview"]
        assert "TOP_SECRET_KEY" not in escape1["output_preview"]

        # Verify unscoped search did NOT leak secret_col
        escape2 = next(p for p in tool_outputs1 if p["tool_call_id"] == "call_escape_2")
        assert "sk-live-do-not-leak" not in escape2["output_preview"]
        assert "doc_secret_1" not in escape2["output_preview"]

        # Verify list_indexed_documents only showed allowed_col
        list1 = next(p for p in tool_outputs1 if p["tool_call_id"] == "call_list_1")
        assert "Allowed Guide" in list1["output_preview"]
        assert "Secret Keys" not in list1["output_preview"]

        # 3. Append Instruction (Run 2): verify scope is STILL inherited & enforced
        await engine.append_instruction(task_id, "Now try fetching doc_secret_1")
        for _ in range(60):
            runs = coord.list_runs(task_id)
            if len(runs) == 2 and runs[1].status.value == "COMPLETED":
                break
            await asyncio.sleep(0.05)

        events2 = coord.list_events_for_task(task_id)
        escape3 = next(
            json.loads(e.payload_json)
            for e in events2
            if e.event_type == EventType.TOOL_COMPLETED
            and json.loads(e.payload_json).get("tool_call_id") == "call_escape_3"
        )
        assert (
            "Access denied" in escape3["output_preview"]
            or "No relevant documents" in escape3["output_preview"]
        )
        assert "sk-live-do-not-leak" not in escape3["output_preview"]
        assert "Secret Keys" not in escape3["output_preview"]
    finally:
        await rt.shutdown()
        settings.rag.vector_db_path = orig_vec
        settings.rag.embedding_dimension = orig_dim
        settings.rag.embedding_provider = orig_prov
