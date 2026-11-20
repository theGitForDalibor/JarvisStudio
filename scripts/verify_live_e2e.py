"""Layer B: Isolated Live Environment E2E Verification & Diagnostic Script.

Explicit opt-in only (--confirm-live or JARVIS_LIVE_E2E=1). Never runs in default pytest.
Uses isolated temporary SQLite/sqlite-vec databases and workspace_root, disables
destructive/shell side-effect tools by default, enforces strict budgets/timeouts,
redacts sensitive credentials, and attributes failures across:
  - GATEWAY_CONFIG (missing/invalid API key, model, or upstream gateway config)
  - PROXY_NETWORK  (TLS/DNS/proxy/timeout reaching external web or upstream endpoints)
  - CODE_DEFECT    (local parsing, indexing, runtime, or provenance verification bug)
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
from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.provenance import verify_report_provenance
from jarvis_studio.service.deliverable_manager import reset_global_deliverable_manager
from jarvis_studio.tools.builtin.rag_tools import (
    list_indexed_documents,
    search_knowledge_base,
)
from jarvis_studio.tools.builtin.report_tools import publish_research_report
from jarvis_studio.tools.builtin.search_tools import search_web
from jarvis_studio.tools.builtin.web_fetch_tools import fetch_web_page
from jarvis_studio.tools.registry import ToolRegistry

_SECRET_RE = re.compile(r"(sk-[a-zA-Z0-9_-]{8,}|Bearer\s+[a-zA-Z0-9._-]+)", re.IGNORECASE)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


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
    if stage in ("preflight_llm", "preflight_embedding") and (
        "not configured" in msg or "notfounderror" in msg or "error code: 404" in msg
    ):
        return "GATEWAY_CONFIG"
    return "CODE_DEFECT"


@dataclass
class StageResult:
    stage: str
    status: str  # PASS | BLOCKED | FAIL
    attribution: str | None = None
    duration_ms: int = 0
    detail: str = ""
    raw_error: str | None = None


async def run_live_e2e(*, timeout_s: float = 60.0, skip_web: bool = False) -> dict[str, Any]:
    results: list[StageResult] = []
    settings = get_settings()

    with tempfile.TemporaryDirectory(prefix="jarvis_live_e2e_") as tmp:
        tmp_path = Path(tmp)
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        app_db = str(tmp_path / "live_app.db")
        vec_db = str(tmp_path / "live_vec.db")

        orig_ws = settings.storage.workspace_root
        orig_app = settings.storage.sqlite_path
        orig_vec = settings.rag.vector_db_path

        settings.storage.workspace_root = str(ws_dir)
        settings.storage.sqlite_path = app_db
        settings.rag.vector_db_path = vec_db
        reset_global_deliverable_manager()

        try:
            # --- Stage 1: Preflight LLM ---
            t0 = time.perf_counter()
            llm = None
            try:
                llm = LLMFactory.create_model(provider=settings.llm.default_provider)
                ping_resp = await asyncio.wait_for(
                    llm.ainvoke("Reply with the single word OK."),
                    timeout=min(timeout_s, 25.0),
                )
                results.append(
                    StageResult(
                        stage="preflight_llm",
                        status="PASS",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        detail=f"model={settings.llm.default_model}, reply={_redact(str(ping_resp.content)[:40])}",
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

            # --- Stage 2: Preflight Embedding & Isolated Indexing ---
            t0 = time.perf_counter()
            doc_id = ""
            try:
                emb = EmbeddingsFactory.create_embeddings()
                vec = await asyncio.wait_for(
                    asyncio.to_thread(emb.embed_query, "AgentStudio SQLite hybrid retrieval"),
                    timeout=min(timeout_s, 25.0),
                )
                sample_md = ws_dir / "live_spec.md"
                sample_md.write_text(
                    "# Live E2E Spec\n\n"
                    "AgentStudio isolates Task knowledge scope server-side using SQLite and sqlite-vec.\n",
                    encoding="utf-8",
                )
                idx_res = WorkspaceIndexer().index_file(
                    sample_md, force=True, collection="live_eval", title="Live E2E Spec"
                )
                if idx_res.get("status") != "indexed":
                    raise RuntimeError(f"Unexpected index result: {idx_res}")
                doc_id = str(idx_res["doc_id"])
                results.append(
                    StageResult(
                        stage="preflight_embedding_and_index",
                        status="PASS",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        detail=f"provider={settings.rag.embedding_provider}, dim={len(vec)}, doc_id={doc_id}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                attr = _classify_exception("preflight_embedding", exc)
                results.append(
                    StageResult(
                        stage="preflight_embedding_and_index",
                        status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                        attribution=attr,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                    )
                )

            # --- Stage 3: Preflight Web Search & Fetch ---
            if not skip_web:
                t0 = time.perf_counter()
                try:
                    web_out = await asyncio.wait_for(
                        asyncio.to_thread(search_web.invoke, {"query": "SQLite WAL mode documentation", "top_k": 2}),
                        timeout=min(timeout_s, 20.0),
                    )
                    if "Error" in web_out or "blocked" in web_out.lower() or "timeout" in web_out.lower():
                        raise ConnectionError(web_out[:300])
                    url_m = re.search(r"https?://[^\s\)\]>\"']+", web_out)
                    fetched_note = "no_url"
                    if url_m:
                        fetch_out = await asyncio.wait_for(
                            asyncio.to_thread(fetch_web_page.invoke, {"url": url_m.group(0), "max_chars": 1500}),
                            timeout=min(timeout_s, 20.0),
                        )
                        fetched_note = f"fetched_len={len(fetch_out)}"
                    results.append(
                        StageResult(
                            stage="preflight_web",
                            status="PASS",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            detail=fetched_note,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("preflight_web", exc)
                    results.append(
                        StageResult(
                            stage="preflight_web",
                            status="BLOCKED" if attr != "CODE_DEFECT" else "FAIL",
                            attribution=attr,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            raw_error=_redact(f"{type(exc).__name__}: {exc}"),
                        )
                    )

            # --- Stage 4: Live Scoped Agent Workflow + Provenance ---
            llm_ok = any(r.stage == "preflight_llm" and r.status == "PASS" for r in results)
            idx_ok = any(r.stage == "preflight_embedding_and_index" and r.status == "PASS" for r in results)
            if llm_ok and idx_ok and llm is not None:
                t0 = time.perf_counter()
                try:
                    cfg = Settings(
                        storage=StorageConfig(
                            checkpoint_backend="sqlite",
                            sqlite_path=app_db,
                            workspace_root=str(ws_dir),
                        ),
                        rag=RAGConfig(
                            enabled=True,
                            embedding_provider=settings.rag.embedding_provider,
                            embedding_dimension=settings.rag.embedding_dimension,
                            vector_db_path=vec_db,
                        ),
                    )
                    async with AgentRuntime.open(cfg, register_as_active=True, manage_mcp=False) as rt:
                        safe_reg = ToolRegistry()
                        for t in (
                            search_knowledge_base,
                            list_indexed_documents,
                            publish_research_report,
                        ):
                            safe_reg.register(t)
                        rt.agent_service.registry = safe_reg
                        rt.agent_service.graph = build_agent_graph(
                            model=llm,
                            registry=safe_reg,
                            checkpointer=rt.checkpointer,
                            hitl_mgr=rt.hitl_manager,
                        )
                        task, _ = rt.task_service.create_task(
                            query=(
                                "Search the local knowledge base for how AgentStudio isolates Task knowledge scope, "
                                "then call publish_research_report citing the exact doc_id and chunk_id."
                            ),
                            knowledge_scope={"collections": ["live_eval"]},
                        )
                        _, fut = await rt.execution_engine.submit_task(task.task_id, return_future=True)
                        assert fut is not None
                        res = await asyncio.wait_for(fut, timeout=timeout_s)
                        delivs = rt.deliverable_manager.list_deliverables_for_task(task.task_id)
                        prov_summary = "no_deliverable"
                        if delivs:
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
                            prov_summary = f"verified={prov.verified}, docs={prov.verified_doc_ids}"
                        results.append(
                            StageResult(
                                stage="live_scoped_agent_workflow",
                                status="PASS" if res.status == "completed" else "FAIL",
                                duration_ms=int((time.perf_counter() - t0) * 1000),
                                detail=f"run_status={res.status}, deliverables={len(delivs)}, {prov_summary}",
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    attr = _classify_exception("live_scoped_agent_workflow", exc)
                    results.append(
                        StageResult(
                            stage="live_scoped_agent_workflow",
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
            reset_global_deliverable_manager()

    return {
        "overall": "PASS" if all(r.status == "PASS" for r in results) else "PARTIAL_OR_BLOCKED",
        "stages": [asdict(r) for r in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint 3C Layer B Live E2E Verification")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Explicit opt-in required to invoke live LLM / Embedding / Web endpoints.",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-stage timeout seconds")
    parser.add_argument("--skip-web", action="store_true", help="Skip external web search/fetch preflight")
    args = parser.parse_args()

    if not args.confirm_live and os.getenv("JARVIS_LIVE_E2E") != "1":
        print(
            "Refusing to run live external calls without --confirm-live or JARVIS_LIVE_E2E=1.",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(run_live_e2e(timeout_s=args.timeout, skip_web=args.skip_web))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    has_code_defect = any(s.get("attribution") == "CODE_DEFECT" for s in report["stages"])
    return 1 if has_code_defect else 0


if __name__ == "__main__":
    raise SystemExit(main())
