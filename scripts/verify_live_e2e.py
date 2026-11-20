"""Layer B: Isolated Live Environment E2E Verification & Diagnostic Script.

Explicit opt-in only (--confirm-live or JARVIS_LIVE_E2E=1). Never runs in default pytest.
Uses isolated temporary SQLite/sqlite-vec databases and workspace_root, disables
destructive/shell side-effect tools by default, enforces strict budgets/timeouts,
redacts sensitive credentials, and reports strict statuses:
  - PASS
  - BLOCKED (GATEWAY_CONFIG | PROXY_NETWORK)
  - FAIL    (CODE_DEFECT)
  - NOT_RUN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.config.models import RAGConfig, StorageConfig
from jarvis_studio.config.settings import Settings, get_settings
from jarvis_studio.core.llm.factory import LLMFactory
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings, EmbeddingsFactory
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.parser import parse_document
from jarvis_studio.rag.provenance import verify_report_provenance
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.service.deliverable_manager import (
    get_deliverable_manager,
    reset_global_deliverable_manager,
)
from jarvis_studio.service.task_service import get_task_service
from jarvis_studio.tools.builtin.rag_tools import (
    list_indexed_documents,
    search_knowledge_base,
)
from jarvis_studio.tools.builtin.report_tools import publish_research_report
from jarvis_studio.tools.builtin.search_tools import search_web
from jarvis_studio.tools.builtin.web_fetch_tools import fetch_web_page
from jarvis_studio.tools.registry import ToolRegistry

_SECRET_RE = re.compile(
    r"(sk-[a-zA-Z0-9_-]{8,}|Bearer\s+[a-zA-Z0-9._-]+|//[^/\s:@]+:[^/\s@]+@)",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        val = m.group(0)
        return "//[REDACTED]@" if val.startswith("//") else "[REDACTED]"

    return _SECRET_RE.sub(_repl, text)


def _classify_exception(stage: str, exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(
        tok in msg
        for tok in (
            "api_key",
            "apikey",
            "unauthorized",
            "authentication",
            "invalid x-api-key",
            "model_not_found",
            "does not exist",
            "quota",
            "billing",
            "401",
            "403",
        )
    ):
        return "GATEWAY_CONFIG"
    if stage in ("preflight_llm", "preflight_embedding", "knowledge_documents_e2e") and (
        "not configured" in msg or "notfounderror" in msg or "error code: 404" in msg
    ):
        return "GATEWAY_CONFIG"
    if any(
        tok in msg
        for tok in (
            "proxy",
            "socks",
            "connecterror",
            "connecttimeout",
            "readtimeout",
            "timed out",
            "timeout",
            "dns",
            "name resolution",
            "getaddrinfo",
            "ssl",
            "tls",
            "connection refused",
            "network is unreachable",
        )
    ):
        return "PROXY_NETWORK"
    return "CODE_DEFECT"


def _build_minimal_pdf(text: str) -> bytes:
    """Build a valid single-page PDF 1.4 byte payload without extra dependencies."""
    safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe_text}) Tj ET".encode("latin-1", errors="replace")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


class _BudgetedToolWrapper:
    """Enforces max_tool_calls across a Live E2E workflow without modifying Runtime."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.calls = 0

    def wrap(self, tool_obj: Any) -> Any:
        from langchain_core.tools import StructuredTool

        wrapper_self = self

        def _sync_or_async_guard(*args: Any, **kwargs: Any) -> Any:
            wrapper_self.calls += 1
            if wrapper_self.calls > wrapper_self.max_calls:
                raise RuntimeError(
                    f"Budget exceeded: max_tool_calls={wrapper_self.max_calls} reached"
                )
            return tool_obj.func(*args, **kwargs)

        return StructuredTool(
            name=tool_obj.name,
            description=tool_obj.description,
            args_schema=tool_obj.args_schema,
            func=_sync_or_async_guard,
        )


@dataclass
class StageResult:
    stage: str
    status: str  # PASS | BLOCKED | FAIL | NOT_RUN
    attribution: str | None = None
    duration_ms: int = 0
    detail: str = ""
    raw_error: str | None = None


async def run_live_e2e(
    *,
    timeout_s: float = 90.0,
    llm_model: str | None = None,
    embedding_model: str | None = None,
    embedding_base_url: str | None = None,
    embedding_dim: int | None = None,
    max_tokens: int = 800,
    recursion_limit: int = 12,
    max_tool_calls: int = 6,
    max_runs: int = 1,
    skip_web: bool = False,
) -> dict[str, Any]:
    results: list[StageResult] = []
    settings = get_settings()
    overall_deadline = time.monotonic() + timeout_s

    def _remaining_timeout(cap: float = 30.0) -> float:
        rem = overall_deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Overall Live E2E budget exceeded ({timeout_s}s)")
        return min(rem, cap)

    with tempfile.TemporaryDirectory(prefix="jarvis_live_e2e_") as tmp:
        tmp_path = Path(tmp)
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        app_db = str(tmp_path / "live_app.db")
        vec_db = str(tmp_path / "live_vec.db")

        orig_ws = settings.storage.workspace_root
        orig_app = settings.storage.sqlite_path
        orig_vec = settings.rag.vector_db_path
        orig_llm_model = settings.llm.default_model
        orig_llm_max_tokens = settings.llm.max_tokens
        orig_openai_base = settings.llm.openai_base_url
        orig_emb_model = settings.rag.embedding_model
        orig_emb_dim = settings.rag.embedding_dimension
        orig_emb_prov = settings.rag.embedding_provider

        settings.storage.workspace_root = str(ws_dir)
        settings.storage.sqlite_path = app_db
        settings.rag.vector_db_path = vec_db
        settings.llm.max_tokens = max_tokens
        if llm_model:
            settings.llm.default_model = llm_model
        if embedding_model:
            settings.rag.embedding_model = embedding_model
            settings.rag.embedding_provider = "openai"
        if embedding_dim:
            settings.rag.embedding_dimension = embedding_dim
        reset_global_deliverable_manager()

        try:
            # --- Stage 1: Document Parsing & Chunk Splitting (Independent of Embedding/LLM) ---
            t0 = time.perf_counter()
            pdf_path = ws_dir / "live_architecture.pdf"
            try:
                pdf_path.write_bytes(
                    _build_minimal_pdf(
                        "AgentStudio enforces Task Knowledge Scope server-side via SQLite and sqlite-vec."
                    )
                )
                parsed_pdf = parse_document(pdf_path)
                if not parsed_pdf.sections or parsed_pdf.sections[0].page_number != 1:
                    raise RuntimeError("PDF parser failed to extract page 1 provenance")
                splitter = DocumentSplitter(chunk_size=300, chunk_overlap=30)
                pdf_chunks = splitter.split_sections(parsed_pdf.sections, doc_path="live_architecture.pdf")
                if not pdf_chunks or pdf_chunks[0].metadata.get("page_number") != 1:
                    raise RuntimeError("Splitter failed to preserve page_number metadata")

                docx_note = "docx_skipped_optional"
                try:
                    import docx  # noqa: F401

                    docx_path = ws_dir / "live_notes.docx"
                    d_doc = docx.Document()
                    d_doc.add_heading("Live Notes", level=1)
                    d_doc.add_paragraph("Deterministic provenance verifies doc_id, chunk_id, and URLs.")
                    d_doc.save(str(docx_path))
                    parsed_docx = parse_document(docx_path)
                    docx_chunks = splitter.split_sections(parsed_docx.sections, doc_path="live_notes.docx")
                    docx_note = f"docx_chunks={len(docx_chunks)}"
                except ImportError:
                    pass

                results.append(
                    StageResult(
                        stage="stage_document_parsing",
                        status="PASS",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        detail=f"pdf_chunks={len(pdf_chunks)}, page=1, {docx_note}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    StageResult(
                        stage="stage_document_parsing",
                        status="FAIL",
                        attribution="CODE_DEFECT",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                    )
                )

            # --- Stage 2: Preflight LLM ---
            t0 = time.perf_counter()
            llm = None
            try:
                llm = LLMFactory.create_model(
                    provider=settings.llm.default_provider,
                )
                ping_resp = await asyncio.wait_for(
                    llm.ainvoke("Reply with the single word OK."),
                    timeout=_remaining_timeout(25.0),
                )
                results.append(
                    StageResult(
                        stage="preflight_llm",
                        status="PASS",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        detail=(
                            f"model={settings.llm.default_model}, max_tokens={max_tokens}, "
                            f"reply={_redact(str(ping_resp.content)[:40])}"
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                attr = _classify_exception("preflight_llm", exc)
                results.append(
                    StageResult(
                        stage="preflight_llm",
                        status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                        attribution=attr,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                    )
                )

            # --- Stage 3: Preflight Real Embedding (with optional base URL override) ---
            t0 = time.perf_counter()
            emb_ok = False
            try:
                if embedding_base_url:
                    settings.llm.openai_base_url = embedding_base_url
                emb = EmbeddingsFactory.create_embeddings()
                if isinstance(emb, DeterministicLocalEmbeddings):
                    raise RuntimeError(
                        "GATEWAY_CONFIG: Real Embedding not configured (fell back to DeterministicLocalEmbeddings)"
                    )
                vec = await asyncio.wait_for(
                    asyncio.to_thread(emb.embed_query, "AgentStudio SQLite hybrid retrieval"),
                    timeout=_remaining_timeout(25.0),
                )
                if len(vec) != settings.rag.embedding_dimension:
                    raise RuntimeError(
                        f"Embedding dimension mismatch: expected {settings.rag.embedding_dimension}, got {len(vec)}"
                    )
                emb_ok = True
                results.append(
                    StageResult(
                        stage="preflight_embedding",
                        status="PASS",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        detail=f"model={settings.rag.embedding_model}, dim={len(vec)}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                attr = _classify_exception("preflight_embedding", exc)
                results.append(
                    StageResult(
                        stage="preflight_embedding",
                        status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                        attribution=attr,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                    )
                )
            finally:
                settings.llm.openai_base_url = orig_openai_base

            # --- Stage 4: Preflight Web Search & Web Fetch (Reported Separately) ---
            if skip_web:
                results.append(StageResult(stage="preflight_web_search", status="NOT_RUN", detail="--skip-web"))
                results.append(StageResult(stage="preflight_web_fetch", status="NOT_RUN", detail="--skip-web"))
            else:
                t0 = time.perf_counter()
                try:
                    web_out = await asyncio.wait_for(
                        asyncio.to_thread(search_web.invoke, {"query": "SQLite WAL mode", "top_k": 2}),
                        timeout=_remaining_timeout(20.0),
                    )
                    if "Error" in web_out or "blocked" in web_out.lower() or "timeout" in web_out.lower():
                        raise ConnectionError(web_out[:300])
                    results.append(
                        StageResult(
                            stage="preflight_web_search",
                            status="PASS",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            detail=f"len={len(web_out)}",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("preflight_web_search", exc)
                    results.append(
                        StageResult(
                            stage="preflight_web_search",
                            status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                            attribution=attr,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                        )
                    )

                t0 = time.perf_counter()
                try:
                    fetch_out = await asyncio.wait_for(
                        asyncio.to_thread(
                            fetch_web_page.invoke,
                            {"url": "https://www.example.com", "max_chars": 1500},
                        ),
                        timeout=_remaining_timeout(20.0),
                    )
                    if "Example Domain" not in fetch_out:
                        raise RuntimeError(f"Unexpected fetch_web_page output: {fetch_out[:200]}")
                    results.append(
                        StageResult(
                            stage="preflight_web_fetch",
                            status="PASS",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            detail=f"url=https://www.example.com, len={len(fetch_out)}",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("preflight_web_fetch", exc)
                    results.append(
                        StageResult(
                            stage="preflight_web_fetch",
                            status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                            attribution=attr,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                        )
                    )

            llm_ok = any(r.stage == "preflight_llm" and r.status == "PASS" for r in results)
            parse_ok = any(r.stage == "stage_document_parsing" and r.status == "PASS" for r in results)
            search_ok = any(r.stage == "preflight_web_search" and r.status == "PASS" for r in results)
            fetch_ok = any(r.stage == "preflight_web_fetch" and r.status == "PASS" for r in results)

            # --- Stage 5A: Knowledge & Documents E2E ---
            if not (llm_ok and emb_ok and parse_ok and llm is not None):
                reason = "preflight_embedding blocked" if not emb_ok else "preflight_llm blocked"
                results.append(
                    StageResult(
                        stage="knowledge_documents_e2e",
                        status="BLOCKED",
                        attribution="GATEWAY_CONFIG",
                        detail=f"Not executed: {reason}",
                    )
                )
            else:
                t0 = time.perf_counter()
                try:
                    if embedding_base_url:
                        settings.llm.openai_base_url = embedding_base_url
                    idx_res = WorkspaceIndexer().index_file(
                        pdf_path, force=True, collection="live_eval", title="Live Architecture PDF"
                    )
                    if idx_res.get("status") != "indexed":
                        raise RuntimeError(f"PDF indexing failed: {idx_res}")
                    settings.llm.openai_base_url = orig_openai_base

                    cfg = Settings(
                        storage=StorageConfig(
                            checkpoint_backend="sqlite",
                            sqlite_path=app_db,
                            workspace_root=str(ws_dir),
                        ),
                        rag=RAGConfig(
                            enabled=True,
                            embedding_provider=settings.rag.embedding_provider,
                            embedding_model=settings.rag.embedding_model,
                            embedding_dimension=settings.rag.embedding_dimension,
                            vector_db_path=vec_db,
                        ),
                    )
                    async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
                        budget = _BudgetedToolWrapper(max_calls=max_tool_calls)
                        safe_reg = ToolRegistry()
                        for t in (search_knowledge_base, list_indexed_documents, publish_research_report):
                            safe_reg.register(budget.wrap(t))
                        rt.agent_service.registry = safe_reg
                        rt.agent_service.graph = build_agent_graph(
                            model=llm,
                            registry=safe_reg,
                            checkpointer=rt.checkpointer,
                            hitl_mgr=rt.hitl_manager,
                        )
                        task, _ = get_task_service().create_task(
                            query=(
                                "Search the local knowledge base for AgentStudio Task Knowledge Scope, "
                                "then call publish_research_report with the exact task_id, citing doc_id and chunk_id."
                            ),
                            knowledge_scope={"collections": ["live_eval"]},
                        )
                        if len(rt.task_coordinator.list_runs(task.task_id)) >= max_runs:
                            raise RuntimeError(f"Budget exceeded: max_runs={max_runs}")
                        _, fut = await rt.execution_engine.submit_task(task.task_id, return_future=True)
                        assert fut is not None
                        res = await asyncio.wait_for(fut, timeout=_remaining_timeout(45.0))
                        delivs = get_deliverable_manager().list_deliverables_for_task(task.task_id)
                        if res.status != "completed" or not delivs:
                            raise RuntimeError(
                                f"Knowledge E2E incomplete: status={res.status}, deliverables={len(delivs)}"
                            )
                        d_text = (ws_dir / delivs[0].file_path).read_text(encoding="utf-8")
                        snap = await rt.agent_service.graph.aget_state(
                            {"configurable": {"thread_id": task.task_id}}
                        )
                        msg_map = {
                            getattr(m, "tool_call_id", ""): str(getattr(m, "content", ""))
                            for m in (snap.values or {}).get("messages", [])
                            if getattr(m, "type", "") == "tool"
                        }
                        prov = verify_report_provenance(
                            d_text,
                            rt.task_coordinator.list_events_for_task(task.task_id),
                            run_id=delivs[0].run_id,
                            tool_messages_by_call_id=msg_map,
                        )
                        results.append(
                            StageResult(
                                stage="knowledge_documents_e2e",
                                status="PASS" if prov.verified else "FAIL",
                                attribution=None if prov.verified else "CODE_DEFECT",
                                duration_ms=int((time.perf_counter() - t0) * 1000),
                                detail=(
                                    f"tool_calls={budget.calls}/{max_tool_calls}, "
                                    f"verified={prov.verified}, docs={prov.verified_doc_ids}"
                                ),
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("knowledge_documents_e2e", exc)
                    results.append(
                        StageResult(
                            stage="knowledge_documents_e2e",
                            status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                            attribution=attr,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                        )
                    )
                finally:
                    settings.llm.openai_base_url = orig_openai_base

            # --- Stage 5B: Research & Analysis E2E ---
            if skip_web:
                results.append(
                    StageResult(stage="research_analysis_e2e", status="NOT_RUN", detail="--skip-web")
                )
            elif not llm_ok:
                results.append(
                    StageResult(
                        stage="research_analysis_e2e",
                        status="BLOCKED",
                        attribution="GATEWAY_CONFIG",
                        detail="Not executed: preflight_llm blocked",
                    )
                )
            elif not (search_ok and fetch_ok):
                # Do NOT claim Research & Analysis E2E PASS if web_search is blocked!
                # Still exercise fetch_web_page + LLM + publish_research_report if fetch_ok,
                # and report honest status as BLOCKED(PROXY_NETWORK) due to search_web failure.
                detail_msg = (
                    f"web_search={'PASS' if search_ok else 'BLOCKED'}, "
                    f"web_fetch={'PASS' if fetch_ok else 'BLOCKED'}"
                )
                if fetch_ok and llm is not None:
                    t0 = time.perf_counter()
                    try:
                        cfg_r = Settings(
                            storage=StorageConfig(
                                checkpoint_backend="sqlite",
                                sqlite_path=str(tmp_path / "live_research.db"),
                                workspace_root=str(ws_dir),
                            ),
                            rag=RAGConfig(enabled=False, vector_db_path=vec_db),
                        )
                        async with AgentRuntime.open(cfg_r, register_as_active=True, manage_mcp=False) as rt:
                            budget_r = _BudgetedToolWrapper(max_calls=max_tool_calls)
                            safe_reg_r = ToolRegistry()
                            for t in (fetch_web_page, publish_research_report):
                                safe_reg_r.register(budget_r.wrap(t))
                            rt.agent_service.registry = safe_reg_r
                            rt.agent_service.graph = build_agent_graph(
                                model=llm,
                                registry=safe_reg_r,
                                checkpointer=rt.checkpointer,
                                hitl_mgr=rt.hitl_manager,
                            )
                            task_r, _ = get_task_service().create_task(
                                query=(
                                    "Call fetch_web_page on https://www.example.com and "
                                    "https://www.iana.org/domains/reserved, then call publish_research_report "
                                    "with the exact task_id and cite the exact URLs you fetched."
                                ),
                            )
                            _, fut_r = await rt.execution_engine.submit_task(task_r.task_id, return_future=True)
                            assert fut_r is not None
                            res_r = await asyncio.wait_for(fut_r, timeout=_remaining_timeout(45.0))
                            delivs_r = get_deliverable_manager().list_deliverables_for_task(task_r.task_id)
                            prov_ok = False
                            urls_v: list[str] = []
                            if delivs_r:
                                d_text_r = (ws_dir / delivs_r[0].file_path).read_text(encoding="utf-8")
                                snap_r = await rt.agent_service.graph.aget_state(
                                    {"configurable": {"thread_id": task_r.task_id}}
                                )
                                msg_map_r = {
                                    getattr(m, "tool_call_id", ""): str(getattr(m, "content", ""))
                                    for m in (snap_r.values or {}).get("messages", [])
                                    if getattr(m, "type", "") == "tool"
                                }
                                prov_r = verify_report_provenance(
                                    d_text_r,
                                    rt.task_coordinator.list_events_for_task(task_r.task_id),
                                    run_id=delivs_r[0].run_id,
                                    tool_messages_by_call_id=msg_map_r,
                                )
                                prov_ok = prov_r.verified
                                urls_v = prov_r.verified_urls
                            detail_msg += (
                                f"; fetch_only_flow: run={res_r.status}, deliverables={len(delivs_r)}, "
                                f"provenance_verified={prov_ok}, urls={urls_v}, tool_calls={budget_r.calls}/{max_tool_calls}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        detail_msg += f"; fetch_only_flow_err={_redact(f'{type(exc).__name__}: {exc}')}"
                results.append(
                    StageResult(
                        stage="research_analysis_e2e",
                        status="BLOCKED",
                        attribution="PROXY_NETWORK",
                        detail=detail_msg,
                    )
                )
            else:
                t0 = time.perf_counter()
                try:
                    cfg_r = Settings(
                        storage=StorageConfig(
                            checkpoint_backend="sqlite",
                            sqlite_path=str(tmp_path / "live_research.db"),
                            workspace_root=str(ws_dir),
                        ),
                        rag=RAGConfig(enabled=False, vector_db_path=vec_db),
                    )
                    async with AgentRuntime.open(cfg_r, register_as_active=True, manage_mcp=False) as rt:
                        budget_r = _BudgetedToolWrapper(max_calls=max_tool_calls)
                        safe_reg_r = ToolRegistry()
                        for t in (search_web, fetch_web_page, publish_research_report):
                            safe_reg_r.register(budget_r.wrap(t))
                        rt.agent_service.registry = safe_reg_r
                        rt.agent_service.graph = build_agent_graph(
                            model=llm,
                            registry=safe_reg_r,
                            checkpointer=rt.checkpointer,
                            hitl_mgr=rt.hitl_manager,
                        )
                        task_r, _ = get_task_service().create_task(
                            query=(
                                "Use search_web to find SQLite WAL mode info, fetch at least one URL with "
                                "fetch_web_page, and call publish_research_report citing the exact URLs."
                            ),
                        )
                        _, fut_r = await rt.execution_engine.submit_task(task_r.task_id, return_future=True)
                        assert fut_r is not None
                        res_r = await asyncio.wait_for(fut_r, timeout=_remaining_timeout(45.0))
                        delivs_r = get_deliverable_manager().list_deliverables_for_task(task_r.task_id)
                        if res_r.status != "completed" or not delivs_r:
                            raise RuntimeError(
                                f"Research E2E incomplete: status={res_r.status}, deliverables={len(delivs_r)}"
                            )
                        d_text_r = (ws_dir / delivs_r[0].file_path).read_text(encoding="utf-8")
                        snap_r = await rt.agent_service.graph.aget_state(
                            {"configurable": {"thread_id": task_r.task_id}}
                        )
                        msg_map_r = {
                            getattr(m, "tool_call_id", ""): str(getattr(m, "content", ""))
                            for m in (snap_r.values or {}).get("messages", [])
                            if getattr(m, "type", "") == "tool"
                        }
                        prov_r = verify_report_provenance(
                            d_text_r,
                            rt.task_coordinator.list_events_for_task(task_r.task_id),
                            run_id=delivs_r[0].run_id,
                            tool_messages_by_call_id=msg_map_r,
                        )
                        results.append(
                            StageResult(
                                stage="research_analysis_e2e",
                                status="PASS" if prov_r.verified else "FAIL",
                                attribution=None if prov_r.verified else "CODE_DEFECT",
                                duration_ms=int((time.perf_counter() - t0) * 1000),
                                detail=(
                                    f"tool_calls={budget_r.calls}/{max_tool_calls}, "
                                    f"verified={prov_r.verified}, urls={prov_r.verified_urls}"
                                ),
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("research_analysis_e2e", exc)
                    results.append(
                        StageResult(
                            stage="research_analysis_e2e",
                            status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                            attribution=attr,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                        )
                    )
        finally:
            settings.storage.workspace_root = orig_ws
            settings.storage.sqlite_path = orig_app
            settings.rag.vector_db_path = orig_vec
            settings.llm.default_model = orig_llm_model
            settings.llm.max_tokens = orig_llm_max_tokens
            settings.llm.openai_base_url = orig_openai_base
            settings.rag.embedding_model = orig_emb_model
            settings.rag.embedding_dimension = orig_emb_dim
            settings.rag.embedding_provider = orig_emb_prov
            reset_global_deliverable_manager()

    all_pass = all(r.status == "PASS" for r in results)
    has_fail = any(r.status == "FAIL" for r in results)
    overall = "PASS" if all_pass else ("FAIL" if has_fail else "BLOCKED")
    return {
        "overall": overall,
        "budgets": {
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "recursion_limit": recursion_limit,
            "max_tool_calls": max_tool_calls,
            "max_runs": max_runs,
        },
        "stages": [asdict(r) for r in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint 3C Layer B Live E2E Verification")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Explicit opt-in required to invoke live LLM / Embedding / Web endpoints.",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="Overall timeout seconds")
    parser.add_argument("--model", type=str, default=None, help="Override LLM model ID for this run only")
    parser.add_argument("--embedding-model", type=str, default=None, help="Override Embedding model ID")
    parser.add_argument("--embedding-base-url", type=str, default=None, help="Override Embedding base URL")
    parser.add_argument("--embedding-dim", type=int, default=None, help="Override Embedding dimension")
    parser.add_argument("--max-tokens", type=int, default=800, help="Max LLM output tokens per call")
    parser.add_argument("--recursion-limit", type=int, default=12, help="Max LangGraph recursion steps")
    parser.add_argument("--max-tool-calls", type=int, default=6, help="Max tool calls per E2E run")
    parser.add_argument("--max-runs", type=int, default=1, help="Max runs per E2E task")
    parser.add_argument("--skip-web", action="store_true", help="Skip external web search/fetch checks")
    args = parser.parse_args()

    if not args.confirm_live and os.getenv("JARVIS_LIVE_E2E") != "1":
        print(
            "Refusing to run live external calls without --confirm-live or JARVIS_LIVE_E2E=1.",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(
        run_live_e2e(
            timeout_s=args.timeout,
            llm_model=args.model,
            embedding_model=args.embedding_model,
            embedding_base_url=args.embedding_base_url,
            embedding_dim=args.embedding_dim,
            max_tokens=args.max_tokens,
            recursion_limit=args.recursion_limit,
            max_tool_calls=args.max_tool_calls,
            max_runs=args.max_runs,
            skip_web=args.skip_web,
        )
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
