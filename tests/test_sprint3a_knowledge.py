"""Sprint 3A Integration & Acceptance Tests: Knowledge & Documents End-to-End."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from docx import Document as DocxDocument
from fastapi import FastAPI
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.api.routes.rag import router as rag_router
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings, get_settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.models import DeliverableType, EventType, TaskStatus
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.parser import parse_document
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.service.deliverable_manager import (
    DeliverableManager,
    reset_global_deliverable_manager,
)
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.registry import ToolRegistry


def _make_two_page_pdf(p1_text: str, p2_text: str) -> bytes:
    """Create a minimal valid 2-page PDF with Helvetica text streams."""
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 7 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R /Resources << /Font << /F1 7 0 R >> >> >>\nendobj\n",
    ]
    c1 = f"BT /F1 12 Tf 72 720 Td ({p1_text}) Tj ET".encode("ascii")
    c2 = f"BT /F1 12 Tf 72 720 Td ({p2_text}) Tj ET".encode("ascii")
    objs.append(
        f"5 0 obj\n<< /Length {len(c1)} >>\nstream\n".encode("ascii") + c1 + b"\nendstream\nendobj\n"
    )
    objs.append(
        f"6 0 obj\n<< /Length {len(c2)} >>\nstream\n".encode("ascii") + c2 + b"\nendstream\nendobj\n"
    )
    objs.append(b"7 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for o in objs:
        offsets.append(len(out))
        out.extend(o)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def _make_docx_bytes(heading: str, body: str, table_rows: list[tuple[str, str]]) -> bytes:
    doc = DocxDocument()
    doc.add_heading(heading, level=1)
    doc.add_paragraph(body)
    tbl = doc.add_table(rows=len(table_rows), cols=2)
    for idx, (k, v) in enumerate(table_rows):
        tbl.cell(idx, 0).text = k
        tbl.cell(idx, 1).text = v
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def knowledge_env(tmp_path: Path):
    settings = get_settings()
    orig_vec = settings.rag.vector_db_path
    orig_ws = settings.storage.workspace_root
    orig_dim = settings.rag.embedding_dimension
    orig_prov = settings.rag.embedding_provider

    vec_db = str(tmp_path / "vec_k.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    settings.rag.vector_db_path = vec_db
    settings.storage.workspace_root = str(ws_dir)
    settings.rag.embedding_dimension = 64
    settings.rag.embedding_provider = "local"

    yield {"vec_db": vec_db, "ws_dir": ws_dir, "tmp_path": tmp_path}

    settings.rag.vector_db_path = orig_vec
    settings.storage.workspace_root = orig_ws
    settings.rag.embedding_dimension = orig_dim
    settings.rag.embedding_provider = orig_prov


def test_multi_format_parsing_overlap_and_dimension_guard(knowledge_env) -> None:
    """Verify PDF page numbers, DOCX headings/tables, Markdown headings, overlap, and dimension mismatch guard."""
    tmp = knowledge_env["tmp_path"]

    # 1. PDF parsing preserves page numbers
    pdf_path = tmp / "architecture_spec.pdf"
    pdf_path.write_bytes(
        _make_two_page_pdf(
            "AgentStudio Architecture uses Modular Monolith",
            "Target P99 Latency SLA is 42ms for local vector lookup",
        )
    )
    parsed_pdf = parse_document(pdf_path)
    assert parsed_pdf.file_type == "pdf"
    assert len(parsed_pdf.sections) == 2
    assert parsed_pdf.sections[0].page_number == 1
    assert parsed_pdf.sections[1].page_number == 2
    assert "42ms" in parsed_pdf.sections[1].content

    # 2. DOCX parsing preserves headings and tables
    docx_path = tmp / "pricing_policy.docx"
    docx_path.write_bytes(
        _make_docx_bytes(
            "Enterprise Pricing Policy",
            "All enterprise tenants receive dedicated SQLite isolation.",
            [("Pro Tier", "$29/month"), ("Enterprise Tier", "$199/month")],
        )
    )
    parsed_docx = parse_document(docx_path)
    assert parsed_docx.file_type == "docx"
    assert any(s.section_title == "Enterprise Pricing Policy" for s in parsed_docx.sections)
    assert any("$199/month" in s.content for s in parsed_docx.sections)

    # 3. Markdown parsing preserves headings
    md_path = tmp / "security_notes.md"
    md_path.write_text(
        "# Security Overview\nZero trust.\n\n## SSRF Controls\nAll loopback IPs are blocked.\n",
        encoding="utf-8",
    )
    parsed_md = parse_document(md_path)
    assert parsed_md.title == "Security Overview"
    assert parsed_md.sections[1].section_title == "SSRF Controls"

    # 4. Splitter overlap check
    splitter = DocumentSplitter(chunk_size=60, chunk_overlap=15)
    para1 = "Alpha paragraph with enough characters to fill chunk one."
    para2 = "Beta paragraph with enough characters to become chunk two."
    chunks = splitter.split(f"{para1}\n\n{para2}", doc_path="overlap.txt")
    assert len(chunks) == 2
    assert para1[-15:].lstrip() in chunks[1].content

    # 5. Dimension mismatch guard
    store_64 = SqliteVecStore(db_path=knowledge_env["vec_db"], dimension=64)
    embedder = DeterministicLocalEmbeddings(dimension=64)
    c_list = splitter.split("Sample vector payload", doc_path="dim_test.txt")
    store_64.add_chunks(c_list, embedder.embed_documents([c.content for c in c_list]), file_hash="h1")

    with pytest.raises(ValueError, match="Vector store dimension mismatch"):
        SqliteVecStore(db_path=knowledge_env["vec_db"], dimension=128)


@pytest.mark.asyncio
async def test_knowledge_api_upload_hybrid_search_update_and_delete(knowledge_env) -> None:
    """Verify full document lifecycle via REST API: upload, status, hybrid search, update, and purge."""
    app = FastAPI()
    app.include_router(rag_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)

    pdf_bytes = _make_two_page_pdf(
        "Section 1: Jarvis Runtime uses RuntimeInstanceLock",
        "Section 2: Checkpoint GC cleans dangling threads safely",
    )
    docx_bytes = _make_docx_bytes(
        "Deployment Guide",
        "Deploy as a single modular monolith process with WAL mode.",
        [("Database", "SQLite 3.x + sqlite-vec"), ("Broker", "None (in-process)")],
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Upload PDF & DOCX into distinct collections
        res_pdf = await client.post(
            "/api/v1/rag/documents/upload",
            data={"collection": "specs", "title": "Runtime Spec PDF"},
            files={"file": ("runtime_spec.pdf", pdf_bytes, "application/pdf")},
        )
        assert res_pdf.status_code == 200
        pdf_doc_id = res_pdf.json()["doc_id"]
        assert res_pdf.json()["status"] == "indexed"
        assert res_pdf.json()["chunks"] == 2

        res_docx = await client.post(
            "/api/v1/rag/documents/upload",
            data={"collection": "ops", "title": "Deployment Guide DOCX"},
            files={
                "file": (
                    "deploy.docx",
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert res_docx.status_code == 200
        docx_doc_id = res_docx.json()["doc_id"]

        # 2. List documents & check status
        list_res = await client.get("/api/v1/rag/documents")
        assert list_res.status_code == 200
        assert len(list_res.json()) == 2

        status_res = await client.get(f"/api/v1/rag/documents/{pdf_doc_id}")
        assert status_res.status_code == 200
        assert status_res.json()["status"] == "READY"
        assert status_res.json()["title"] == "Runtime Spec PDF"

        # 3. Hybrid search with page_number provenance
        search_res = await client.post(
            "/api/v1/rag/search",
            json={"query": "Checkpoint GC dangling threads", "top_k": 3},
        )
        assert search_res.status_code == 200
        hits = search_res.json()
        assert len(hits) >= 1
        assert hits[0]["doc_id"] == pdf_doc_id
        assert hits[0]["metadata"]["page_number"] == 2
        assert "Checkpoint GC" in hits[0]["content"]

        # 4. Collection-scoped search
        scoped_res = await client.post(
            "/api/v1/rag/search",
            json={"query": "SQLite", "collection": "ops", "top_k": 3},
        )
        assert scoped_res.status_code == 200
        assert all(h["collection"] == "ops" for h in scoped_res.json())

        # 5. Update PDF with completely new content -> old content must NOT pollute search
        updated_pdf = _make_two_page_pdf(
            "Updated V2: Zero legacy tokens",
            "Updated V2: New SLA is 10ms",
        )
        upd_res = await client.post(
            "/api/v1/rag/documents/upload",
            data={"collection": "specs", "title": "Runtime Spec PDF", "force": "true"},
            files={"file": ("runtime_spec.pdf", updated_pdf, "application/pdf")},
        )
        assert upd_res.status_code == 200

        old_search = await client.post(
            "/api/v1/rag/search",
            json={"query": "RuntimeInstanceLock", "doc_id": pdf_doc_id, "top_k": 5},
        )
        assert all("RuntimeInstanceLock" not in h["content"] for h in old_search.json())

        # 6. Delete DOCX -> verify complete cleanup across doc_files, doc_chunks, vec_chunks, fts_chunks
        del_res = await client.delete(f"/api/v1/rag/documents/{docx_doc_id}")
        assert del_res.status_code == 200
        assert (await client.get(f"/api/v1/rag/documents/{docx_doc_id}")).status_code == 404

        with sqlite3.connect(knowledge_env["vec_db"]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM doc_files WHERE doc_id = ?", (docx_doc_id,)).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM doc_chunks WHERE doc_path LIKE '%deploy.docx'").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM fts_chunks WHERE doc_path LIKE '%deploy.docx'").fetchone()[0] == 0


class _KnowledgeAgentMockModel(BaseChatModel):
    """Scripted model simulating General Agent: list_indexed_documents -> search_knowledge_base -> publish_research_report."""

    turn: int = 0
    task_id_holder: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "mock_knowledge_agent"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _KnowledgeAgentMockModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if messages and isinstance(messages[0], SystemMessage):
            for line in str(messages[0].content).splitlines():
                if line.strip().startswith("- task_id:"):
                    tid = line.split(":", 1)[1].strip()
                    if tid not in self.task_id_holder:
                        self.task_id_holder.append(tid)

        step = self.turn
        self.turn += 1

        if step == 0:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Let me inspect available documents in the knowledge base first.",
                            tool_calls=[{"id": "k_list_1", "name": "list_indexed_documents", "args": {}}],
                        )
                    )
                ]
            )
        if step == 1:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Searching across indexed PDF and DOCX documents for SLA and pricing.",
                            tool_calls=[
                                {
                                    "id": "k_search_1",
                                    "name": "search_knowledge_base",
                                    "args": {"query": "Latency SLA 42ms", "top_k": 2},
                                },
                                {
                                    "id": "k_search_2",
                                    "name": "search_knowledge_base",
                                    "args": {"query": "Enterprise Tier pricing", "top_k": 2},
                                },
                            ],
                        )
                    )
                ]
            )
        if step == 2:
            tid = self.task_id_holder[0] if self.task_id_holder else "task_unknown"
            report_md = (
                "# 内部架构与定价知识库核查报告\n\n"
                "## 已验证事实（来自知识库检索）\n"
                "- **延迟指标**：根据 `sla_spec.pdf`（Page 2），本地向量检索目标 P99 Latency SLA 为 **42ms**。\n"
                "- **企业定价**：根据 `commercial.docx`（Section: Commercial Terms），Enterprise Tier 定价为 **$199/month**。\n\n"
                "## 推断与信息缺口\n"
                "- **信息缺口**：知识库文档未说明多租户并发上限，需补充压测报告。\n"
            )
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Publishing grounded knowledge report as deliverable.",
                            tool_calls=[
                                {
                                    "id": "k_pub_1",
                                    "name": "publish_research_report",
                                    "args": {
                                        "task_id": tid,
                                        "title": "内部架构与定价知识库核查报告",
                                        "markdown_content": report_md,
                                        "summary": "基于 PDF Page 2 与 DOCX 原文片段生成的知识核查报告。",
                                    },
                                }
                            ],
                        )
                    )
                ]
            )
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content="已完成知识库跨文档检索并生成核查报告交付物。"))
            ]
        )


@pytest.mark.asyncio
async def test_e2e_general_agent_knowledge_retrieval_and_deliverable(knowledge_env) -> None:
    """Verify General Agent retrieves multi-format knowledge with citations and publishes Markdown Deliverable."""
    tmp = knowledge_env["tmp_path"]
    ws_dir = knowledge_env["ws_dir"]
    app_db = tmp / "app_knowledge.db"

    # Ingest 1 PDF + 1 DOCX into knowledge base
    pdf_file = tmp / "sla_spec.pdf"
    pdf_file.write_bytes(
        _make_two_page_pdf(
            "System Overview: Modular Monolith Runtime",
            "Target P99 Latency SLA is 42ms for local vector lookup",
        )
    )
    docx_file = tmp / "commercial.docx"
    docx_file.write_bytes(
        _make_docx_bytes(
            "Commercial Terms",
            "Standard support included.",
            [("Enterprise Tier", "$199/month")],
        )
    )

    indexer = WorkspaceIndexer()
    indexer.index_file(pdf_file, collection="architecture")
    indexer.index_file(docx_file, collection="commercial")

    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(app_db),
            workspace_root=str(ws_dir),
        ),
        rag=RAGConfig(
            enabled=True,
            embedding_provider="local",
            embedding_dimension=64,
            vector_db_path=knowledge_env["vec_db"],
        ),
    )
    deliv_mgr = DeliverableManager(db_path=app_db, workspace_root=ws_dir)
    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    try:
        coord = rt.task_coordinator
        engine = rt.execution_engine
        reg = ToolRegistry()
        register_builtin_tools(reg)

        mock_llm = _KnowledgeAgentMockModel()
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

        with patch(
            "jarvis_studio.service.deliverable_manager.get_deliverable_manager",
            return_value=deliv_mgr,
        ):
            task = coord.create_task(query="结合知识库中的架构文档和商务文档，核查 SLA 与企业定价并生成报告。")
            _, fut = await engine.submit_task(task.task_id, return_future=True)
            assert fut is not None
            res = await asyncio.wait_for(fut, timeout=10.0)
            assert res.status == "completed"

        # Verify deliverable content has traceable citations
        deliverables = deliv_mgr.list_deliverables_for_task(task.task_id)
        assert len(deliverables) == 1
        d = deliverables[0]
        assert d.deliverable_type == DeliverableType.MARKDOWN_REPORT
        text = (ws_dir / d.file_path).read_text(encoding="utf-8")
        assert "sla_spec.pdf" in text
        assert "Page 2" in text
        assert "42ms" in text
        assert "commercial.docx" in text
        assert "$199/month" in text

        # Verify Tool & Deliverable Events
        events = coord.list_events_for_task(task.task_id)
        ev_types = [e.event_type for e in events]
        assert EventType.TOOL_STARTED in ev_types
        assert EventType.DELIVERABLE_CREATED in ev_types
        assert EventType.TASK_COMPLETED in ev_types

        # Verify search_knowledge_base tool outputs contained real page/section citations
        tool_comp_payloads = [
            json.loads(e.payload_json)
            for e in events
            if e.event_type == EventType.TOOL_COMPLETED
            and json.loads(e.payload_json).get("tool_name") == "search_knowledge_base"
        ]
        assert len(tool_comp_payloads) == 2
        assert any("Page: 2" in p.get("output_preview", "") for p in tool_comp_payloads)
    finally:
        reset_global_deliverable_manager()
        await rt.shutdown()
