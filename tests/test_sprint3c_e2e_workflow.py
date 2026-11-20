"""Sprint 3C Layer A: Deterministic End-to-End Agent Workflow Validation.

Covers:
1. Knowledge Scope null / explicit-empty (deny-all) / collection / doc_id / AND intersection semantics.
2. Full multi-run workflow: Upload -> Scoped Task -> RAG Search -> Deliverable v1 -> Provenance Verifier
   -> Append Instruction -> Run 2 (inherited scope, Deliverable v2) -> Event/SSE traceability.
3. Deterministic Control Plane (Event/Barrier-synchronized Pause/Resume, Cancel, HITL Approve/Resume).
4. Failure boundary handling (FAILED status, RUN_FAILED event, no orphaned deliverable).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.app import create_app
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings, get_settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.models import EventType, TaskStatus
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings
from jarvis_studio.rag.parser import ParsedSection
from jarvis_studio.rag.provenance import verify_report_provenance
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.service.deliverable_manager import reset_global_deliverable_manager
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.builtin.rag_tools import (
    list_indexed_documents,
    scoped_knowledge_context,
    search_knowledge_base,
)
from jarvis_studio.tools.registry import ToolRegistry


def _configure_rag_settings(
    vec_db: str, ws_root: str, app_db: str | None = None
) -> tuple[str, int, str, str, str]:
    settings = get_settings()
    prev = (
        settings.rag.vector_db_path,
        settings.rag.embedding_dimension,
        settings.rag.embedding_provider,
        settings.storage.workspace_root,
        settings.storage.sqlite_path,
    )
    settings.rag.vector_db_path = vec_db
    settings.rag.embedding_dimension = 64
    settings.rag.embedding_provider = "local"
    settings.storage.workspace_root = ws_root
    if app_db is not None:
        settings.storage.sqlite_path = app_db
    reset_global_deliverable_manager()
    return prev


def _restore_rag_settings(prev: tuple[str, int, str, str, str]) -> None:
    settings = get_settings()
    settings.rag.vector_db_path = prev[0]
    settings.rag.embedding_dimension = prev[1]
    settings.rag.embedding_provider = prev[2]
    settings.storage.workspace_root = prev[3]
    settings.storage.sqlite_path = prev[4]
    reset_global_deliverable_manager()


def test_knowledge_scope_null_empty_and_intersection_semantics(tmp_path: Path) -> None:
    """Verify None (unrestricted), explicit empty (deny-all), single-axis, and AND intersection."""
    vec_db = str(tmp_path / "vec_sem.db")
    ws_dir = tmp_path / "ws_sem"
    ws_dir.mkdir(parents=True, exist_ok=True)
    prev = _configure_rag_settings(vec_db, str(ws_dir))

    try:
        store = SqliteVecStore(db_path=vec_db, dimension=64)
        embedder = DeterministicLocalEmbeddings(dimension=64)
        splitter = DocumentSplitter(chunk_size=300, chunk_overlap=20)

        docs = [
            ("alpha", "doc_a1", "a1.md", "Alpha one architecture token."),
            ("alpha", "doc_a2", "a2.md", "Alpha two deployment token."),
            ("beta", "doc_b1", "b1.md", "Beta one security token."),
        ]
        for col, did, path, text in docs:
            chunks = splitter.split_sections([ParsedSection(content=text, page_number=1)], doc_path=path)
            store.add_chunks(
                chunks,
                embedder.embed_documents([c.content for c in chunks]),
                file_hash=f"h_{did}",
                title=did,
                collection=col,
                doc_id=did,
            )
        store.close()

        # 1. Unspecified (None) -> full-library access
        with scoped_knowledge_context(None):
            res_none = search_knowledge_base.invoke({"query": "token", "top_k": 5})
            assert "doc_a1" in res_none and "doc_a2" in res_none and "doc_b1" in res_none

        # 2. Explicit empty scope -> deny-all (never degrades to full access)
        with scoped_knowledge_context({"collections": [], "doc_ids": []}):
            res_empty_search = search_knowledge_base.invoke({"query": "token", "top_k": 5})
            assert "Access denied" in res_empty_search
            assert "doc_a1" not in res_empty_search
            res_empty_list = list_indexed_documents.invoke({})
            assert "Access denied" in res_empty_list
            assert "doc_a1" not in res_empty_list

        # 3. Only collections
        with scoped_knowledge_context({"collections": ["alpha"], "doc_ids": []}):
            res_col = search_knowledge_base.invoke({"query": "token", "top_k": 5})
            assert "doc_a1" in res_col and "doc_a2" in res_col
            assert "doc_b1" not in res_col

        # 4. Only doc_ids
        with scoped_knowledge_context({"collections": [], "doc_ids": ["doc_b1"]}):
            res_doc = search_knowledge_base.invoke({"query": "token", "top_k": 5})
            assert "doc_b1" in res_doc
            assert "doc_a1" not in res_doc

        # 5. Both collections and doc_ids -> AND intersection
        with scoped_knowledge_context({"collections": ["alpha"], "doc_ids": ["doc_a2", "doc_b1"]}):
            res_and = search_knowledge_base.invoke({"query": "token", "top_k": 5})
            assert "doc_a2" in res_and
            assert "doc_a1" not in res_and
            assert "doc_b1" not in res_and
    finally:
        _restore_rag_settings(prev)


class _MultiRunWorkflowMockModel:
    """Protocol-level mock LLM that drives Run 1 (search + report v1) and Run 2 (search + report v2)."""

    def __init__(self) -> None:
        self.step = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> _MultiRunWorkflowMockModel:
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.step += 1
        human_texts = [str(getattr(m, "content", "")) for m in messages if getattr(m, "type", "") == "human"]
        last_human = human_texts[-1] if human_texts else ""

        # Extract task_id from [Execution Context] in system message
        sys_text = "\n".join(str(getattr(m, "content", "")) for m in messages if getattr(m, "type", "") == "system")
        tid_m = re.search(r"task_id:\s*(task_[a-zA-Z0-9]+)", sys_text)
        rid_m = re.search(r"run_id:\s*(run_[a-zA-Z0-9]+)", sys_text)
        task_id = tid_m.group(1) if tid_m else ""
        run_id = rid_m.group(1) if rid_m else None

        last_msg = messages[-1]
        is_tool = isinstance(last_msg, ToolMessage) or getattr(last_msg, "type", "") == "tool"

        if "Follow-up" not in last_human:
            # Run 1
            if not is_tool:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "r1_allowed",
                            "name": "search_knowledge_base",
                            "args": {"query": "SQLite hybrid retrieval latency", "top_k": 3},
                        },
                        {
                            "id": "r1_escape",
                            "name": "search_knowledge_base",
                            "args": {"query": "classified", "collection": "secret_col", "top_k": 3},
                        },
                    ],
                )
            # After tool results, publish Deliverable v1 if not yet published
            tool_names = [getattr(m, "name", "") for m in messages if getattr(m, "type", "") == "tool"]
            if "publish_research_report" not in tool_names:
                tool_text = "\n".join(str(getattr(m, "content", "")) for m in messages if getattr(m, "type", "") == "tool")
                doc_m = re.search(r"(doc_[a-zA-Z0-9]+)", tool_text)
                chk_m = re.search(r"(chk_[a-zA-Z0-9]+)", tool_text)
                did = doc_m.group(1) if doc_m else "doc_missing"
                cid = chk_m.group(1) if chk_m else "chk_missing"
                report = (
                    f"# Run 1 Architecture Report\n\n"
                    f"Source: `{did}` (`{cid}`)\n\n"
                    f"> AgentStudio uses SQLite with sqlite-vec and FTS5 for hybrid retrieval.\n"
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "r1_pub",
                            "name": "publish_research_report",
                            "args": {
                                "task_id": task_id,
                                "run_id": run_id,
                                "title": "Architecture Report v1",
                                "markdown_content": report,
                            },
                        }
                    ],
                )
            return AIMessage(content="Run 1 completed with Architecture Report v1.")

        # Run 2 (Follow-up instruction)
        if not is_tool or getattr(last_msg, "name", "") == "publish_research_report":
            # Need fresh retrieval in Run 2
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "r2_search",
                        "name": "search_knowledge_base",
                        "args": {"query": "retention SLA 99.9%", "top_k": 3},
                    }
                ],
            )
        if getattr(last_msg, "tool_call_id", "") == "r2_search":
            tool_text = str(getattr(last_msg, "content", ""))
            doc_m = re.search(r"(doc_[a-zA-Z0-9]+)", tool_text)
            chk_m = re.search(r"(chk_[a-zA-Z0-9]+)", tool_text)
            did = doc_m.group(1) if doc_m else "doc_missing"
            cid = chk_m.group(1) if chk_m else "chk_missing"
            report_v2 = (
                f"# Run 2 Updated Architecture Report\n\n"
                f"Source: `{did}` (`{cid}`)\n\n"
                f"> The target retention SLA is 99.9% with local SQLite persistence.\n"
            )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "r2_pub",
                        "name": "publish_research_report",
                        "args": {
                            "task_id": task_id,
                            "run_id": run_id,
                            "title": "Architecture Report v2",
                            "markdown_content": report_v2,
                        },
                    }
                ],
            )
        return AIMessage(content="Run 2 completed with Architecture Report v2.")


@pytest.mark.asyncio
async def test_e2e_upload_scoped_research_deliverable_provenance_and_append_instruction(
    tmp_path: Path,
) -> None:
    """Full Layer-A E2E: upload -> scoped task -> Run 1 Deliverable v1 -> Provenance -> Run 2 Deliverable v2 -> SSE."""
    ws_dir = tmp_path / "ws_e2e"
    ws_dir.mkdir(parents=True, exist_ok=True)
    vec_db = str(tmp_path / "vec_e2e.db")
    app_db = str(tmp_path / "app_e2e.db")
    prev = _configure_rag_settings(vec_db, str(ws_dir), app_db=app_db)

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

    try:
        async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
            reg = ToolRegistry()
            register_builtin_tools(reg)
            rt.agent_service.registry = reg
            rt.agent_service.graph = build_agent_graph(
                model=_MultiRunWorkflowMockModel(),
                registry=reg,
                checkpointer=rt.checkpointer,
                hitl_mgr=rt.hitl_manager,
            )

            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # 1. Upload allowed & secret docs
                allowed_md = (
                    "# System Design\n\n"
                    "AgentStudio uses SQLite with sqlite-vec and FTS5 for hybrid retrieval.\n\n"
                    "The target retention SLA is 99.9% with local SQLite persistence.\n"
                ).encode("utf-8")
                secret_md = b"# Secret\n\nCLASSIFIED_PASSWORD=do-not-leak\n"

                up1 = await client.post(
                    "/api/v1/rag/documents/upload",
                    files={"file": ("system_design.md", allowed_md, "text/markdown")},
                    data={"collection": "allowed_col", "title": "System Design"},
                )
                assert up1.status_code == 200
                allowed_doc_id = up1.json()["doc_id"]

                up2 = await client.post(
                    "/api/v1/rag/documents/upload",
                    files={"file": ("secret.md", secret_md, "text/markdown")},
                    data={"collection": "secret_col", "title": "Secret Doc"},
                )
                assert up2.status_code == 200

                # 2. Create scoped Task
                t_res = await client.post(
                    "/api/v1/tasks",
                    json={
                        "query": "Analyze architecture from allowed_col and publish report",
                        "knowledge_scope": {"collections": ["allowed_col"]},
                    },
                )
                assert t_res.status_code == 201
                task_id = t_res.json()["task_id"]

                # 3. Execute Run 1 via ExecutionEngine
                _, fut1 = await rt.execution_engine.submit_task(task_id, return_future=True)
                assert fut1 is not None
                res1 = await asyncio.wait_for(fut1, timeout=10.0)
                assert res1.status == "completed"

                # Verify Run 1 deliverables & provenance
                delivs1 = (await client.get(f"/api/v1/tasks/{task_id}/deliverables")).json()
                assert len(delivs1) == 1
                d1 = delivs1[0]
                assert d1["title"] == "Architecture Report v1"
                d1_text = (ws_dir / d1["file_path"]).read_text(encoding="utf-8")
                assert allowed_doc_id in d1_text

                # Collect full ToolMessage contents from LangGraph checkpoint state
                snap1 = await rt.agent_service.graph.aget_state({"configurable": {"thread_id": task_id}})
                msg_map = {
                    getattr(m, "tool_call_id", ""): str(getattr(m, "content", ""))
                    for m in (snap1.values or {}).get("messages", [])
                    if getattr(m, "type", "") == "tool"
                }
                events1 = rt.task_coordinator.list_events_for_task(task_id)
                prov1 = verify_report_provenance(
                    d1_text,
                    events1,
                    run_id=d1["run_id"],
                    tool_messages_by_call_id=msg_map,
                )
                assert prov1.verified is True
                assert allowed_doc_id in prov1.verified_doc_ids
                assert len(prov1.verified_chunk_ids) == 1
                assert len(prov1.verified_quotes) == 1

                # Negative provenance checks: fabricated doc_id, chunk_id, quote, and URL MUST fail
                bad_report = (
                    f"# Forged Report\n\n"
                    f"Ref: doc_deadbeef99 chk_deadbeef99 https://evil.example.com/fake\n\n"
                    f"> This sentence was never retrieved from any chunk in this Run.\n"
                )
                bad_prov = verify_report_provenance(
                    bad_report,
                    events1,
                    run_id=d1["run_id"],
                    tool_messages_by_call_id=msg_map,
                )
                assert bad_prov.verified is False
                bad_types = {u["type"] for u in bad_prov.unverified_citations}
                assert {"doc_id", "chunk_id", "url", "quote"}.issubset(bad_types)

                # 4. Append Instruction -> Run 2 (inherits scope, produces Deliverable v2)
                app_res = await client.post(
                    f"/api/v1/tasks/{task_id}/append-instruction",
                    json={"instruction": "Follow-up: document the retention SLA and publish v2."},
                )
                assert app_res.status_code == 200
                run2_id = app_res.json()["run_id"]

                # Wait deterministically via ExecutionEngine active task completion or run status
                for _ in range(100):
                    detail = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                    if len(detail["runs"]) == 2 and detail["runs"][1]["status"] == "COMPLETED":
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("Run 2 did not complete in time")

                delivs2 = (await client.get(f"/api/v1/tasks/{task_id}/deliverables")).json()
                assert len(delivs2) == 2
                d2 = next(d for d in delivs2 if d["run_id"] == run2_id)
                assert d2["title"] == "Architecture Report v2"
                d2_text = (ws_dir / d2["file_path"]).read_text(encoding="utf-8")

                snap2 = await rt.agent_service.graph.aget_state({"configurable": {"thread_id": task_id}})
                msg_map2 = {
                    getattr(m, "tool_call_id", ""): str(getattr(m, "content", ""))
                    for m in (snap2.values or {}).get("messages", [])
                    if getattr(m, "type", "") == "tool"
                }
                events_all = rt.task_coordinator.list_events_for_task(task_id)
                prov2 = verify_report_provenance(
                    d2_text,
                    events_all,
                    run_id=run2_id,
                    tool_messages_by_call_id=msg_map2,
                )
                assert prov2.verified is True

                # Verify SSE replay returns ordered lifecycle events including both deliverables
                sse_res = await client.get(f"/api/v1/tasks/{task_id}/events/stream?once=true")
                assert sse_res.status_code == 200
                sse_body = sse_res.text
                assert "task_created" in sse_body
                assert "deliverable_created" in sse_body
                assert "instruction_appended" in sse_body
                assert "task_completed" in sse_body
    finally:
        _restore_rag_settings(prev)


class _BarrierControlModel:
    """Barrier-synchronized mock LLM for deterministic Pause/Resume/Cancel/HITL testing."""

    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> _BarrierControlModel:
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.calls += 1
        if self.mode == "pause_resume":
            if self.calls == 1:
                # Signal test harness we are mid-run, wait until test calls /pause, then return tool call
                self.entered.set()
                await self.release.wait()
                return AIMessage(
                    content="",
                    tool_calls=[{"id": "p_call", "name": "list_indexed_documents", "args": {}}],
                )
            return AIMessage(content="Resumed after pause cleanly.")

        if self.mode == "cancel":
            self.entered.set()
            await self.release.wait()
            return AIMessage(content="Should not reach completion after cancel.")

        if self.mode == "hitl":
            last_msg = messages[-1]
            if getattr(last_msg, "type", "") != "tool":
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "hitl_write_1",
                            "name": "write_file",
                            "args": {"file_path": "scoped_note.txt", "content": "approved output"},
                        }
                    ],
                )
            return AIMessage(content="HITL tool approved and executed.")

        raise RuntimeError("Simulated LLM failure")


@pytest.mark.asyncio
async def test_e2e_deterministic_pause_resume_cancel_and_hitl_with_scope_inheritance(
    tmp_path: Path,
) -> None:
    """Verify Pause/Resume, Cancel, and HITL state consistency using asyncio.Event barriers."""
    ws_dir = tmp_path / "ws_ctrl"
    ws_dir.mkdir(parents=True, exist_ok=True)
    app_db = str(tmp_path / "app_ctrl.db")
    vec_db = str(tmp_path / "vec_ctrl.db")
    prev = _configure_rag_settings(vec_db, str(ws_dir), app_db=app_db)

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

    try:
        async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
            reg = ToolRegistry()
            register_builtin_tools(reg)
            rt.agent_service.registry = reg
            app = create_app()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # --- A. Pause -> Resume with scope inheritance ---
                pause_llm = _BarrierControlModel(mode="pause_resume")
                rt.agent_service.graph = build_agent_graph(
                    model=pause_llm,
                    registry=reg,
                    checkpointer=rt.checkpointer,
                    hitl_mgr=rt.hitl_manager,
                )
                t_p = (
                    await client.post(
                        "/api/v1/tasks",
                        json={"query": "Pause test", "knowledge_scope": {"collections": ["allowed_col"]}},
                    )
                ).json()
                tid_p = t_p["task_id"]

                _, fut_p = await rt.execution_engine.submit_task(tid_p, return_future=True)
                await asyncio.wait_for(pause_llm.entered.wait(), timeout=5.0)

                p_resp = await client.post(f"/api/v1/tasks/{tid_p}/pause", json={"reason": "User pause"})
                assert p_resp.status_code == 200
                pause_llm.release.set()

                res_p = await asyncio.wait_for(fut_p, timeout=5.0)
                assert res_p.status == "paused"
                assert rt.task_coordinator.get_task(tid_p).status == TaskStatus.PAUSED

                r_resp = await client.post(f"/api/v1/tasks/{tid_p}/resume", json={})
                assert r_resp.status_code == 200
                for _ in range(100):
                    t_cur = rt.task_coordinator.get_task(tid_p)
                    if t_cur.status == TaskStatus.COMPLETED and not await rt.execution_engine.is_task_active(tid_p):
                        break
                    await asyncio.sleep(0.02)
                assert rt.task_coordinator.get_task(tid_p).status == TaskStatus.COMPLETED
                assert rt.task_coordinator.get_task(tid_p).get_knowledge_scope() == {
                    "collections": ["allowed_col"],
                    "doc_ids": [],
                }
                ev_types_p = [e.event_type for e in rt.task_coordinator.list_events_for_task(tid_p)]
                assert EventType.TASK_PAUSED in ev_types_p
                assert EventType.TASK_RESUMED in ev_types_p

                # --- B. Cancel ---
                cancel_llm = _BarrierControlModel(mode="cancel")
                rt.agent_service.graph = build_agent_graph(
                    model=cancel_llm,
                    registry=reg,
                    checkpointer=rt.checkpointer,
                    hitl_mgr=rt.hitl_manager,
                )
                tid_c = (await client.post("/api/v1/tasks", json={"query": "Cancel test"})).json()["task_id"]
                _, fut_c = await rt.execution_engine.submit_task(tid_c, return_future=True)
                await asyncio.wait_for(cancel_llm.entered.wait(), timeout=5.0)

                c_resp = await client.post(f"/api/v1/tasks/{tid_c}/cancel", json={"reason": "Abort"})
                assert c_resp.status_code == 200
                cancel_llm.release.set()

                res_c = await asyncio.wait_for(fut_c, timeout=5.0)
                assert res_c.status == "cancelled"
                assert rt.task_coordinator.get_task(tid_c).status == TaskStatus.CANCELLED
                ev_types_c = [e.event_type for e in rt.task_coordinator.list_events_for_task(tid_c)]
                assert EventType.TASK_CANCELLED in ev_types_c

                # --- C. HITL Interrupt -> Approve -> Resume ---
                hitl_llm = _BarrierControlModel(mode="hitl")
                rt.agent_service.graph = build_agent_graph(
                    model=hitl_llm,
                    registry=reg,
                    checkpointer=rt.checkpointer,
                    hitl_mgr=rt.hitl_manager,
                )
                tid_h = (
                    await client.post(
                        "/api/v1/tasks",
                        json={"query": "Write file with HITL", "knowledge_scope": {"collections": ["allowed_col"]}},
                    )
                ).json()["task_id"]
                _, fut_h1 = await rt.execution_engine.submit_task(tid_h, return_future=True)
                res_h1 = await asyncio.wait_for(fut_h1, timeout=5.0)
                assert res_h1.status == "waiting_approval"
                assert rt.task_coordinator.get_task(tid_h).status == TaskStatus.WAITING_APPROVAL

                pending = rt.hitl_manager.get_pending_by_thread(tid_h)
                assert len(pending) == 1
                approval_id = pending[0].approval_id

                # Approve and resume via AgentService
                rt.hitl_manager.resolve_approval(approval_id, approved=True, resolved_by="e2e_user")
                _, fut_h2 = await rt.execution_engine.submit_task(
                    tid_h,
                    resume_payload={"decisions": [{"approval_id": approval_id, "approved": True}]},
                    return_future=True,
                )
                res_h2 = await asyncio.wait_for(fut_h2, timeout=5.0)
                assert res_h2.status == "completed"
                assert (ws_dir / "scoped_note.txt").read_text(encoding="utf-8") == "approved output"
                assert rt.task_coordinator.get_task(tid_h).get_knowledge_scope() == {
                    "collections": ["allowed_col"],
                    "doc_ids": [],
                }
    finally:
        _restore_rag_settings(prev)


@pytest.mark.asyncio
async def test_e2e_failure_boundary_and_no_orphaned_deliverable(tmp_path: Path) -> None:
    """Verify LLM/tool failure marks Task/Run FAILED, emits RUN_FAILED, and leaves no deliverables."""
    ws_dir = tmp_path / "ws_fail"
    ws_dir.mkdir(parents=True, exist_ok=True)
    app_db = str(tmp_path / "app_fail.db")
    vec_db = str(tmp_path / "vec_fail.db")
    prev = _configure_rag_settings(vec_db, str(ws_dir), app_db=app_db)

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

    try:
        async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
            rt.agent_service.graph = build_agent_graph(
                model=_BarrierControlModel(mode="fail"),
                checkpointer=rt.checkpointer,
                hitl_mgr=rt.hitl_manager,
            )
            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                tid = (await client.post("/api/v1/tasks", json={"query": "Trigger failure"})).json()["task_id"]
                _, fut = await rt.execution_engine.submit_task(tid, return_future=True)
                res = await asyncio.wait_for(fut, timeout=5.0)
                assert res.status == "error"

                detail = (await client.get(f"/api/v1/tasks/{tid}")).json()
                assert detail["task"]["status"] == "FAILED"
                assert detail["runs"][0]["status"] == "FAILED"
                assert detail["deliverables"] == []
                ev_types = [e["event_type"] for e in detail["events"]]
                assert "run_failed" in ev_types
    finally:
        _restore_rag_settings(prev)
