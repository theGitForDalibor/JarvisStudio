"""Sprint 3B-3 tests: Knowledge Workspace, Retrieval Playground, Evaluation APIs & UI."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jarvis_studio.api.routes.rag import router as rag_router
from jarvis_studio.config.settings import get_settings


@pytest.mark.asyncio
async def test_rag_workspace_search_modes_and_eval_api(tmp_path: Path) -> None:
    """Verify upload, inspect, reindex, delete, search_mode explainability, and eval APIs."""
    settings = get_settings()
    orig_vec = settings.rag.vector_db_path
    orig_dim = settings.rag.embedding_dimension
    orig_prov = settings.rag.embedding_provider
    orig_ws = settings.storage.workspace_root

    ws_root = tmp_path / "workspace"
    ws_root.mkdir(parents=True, exist_ok=True)
    vec_db = str(tmp_path / "vec_ws_eval.db")

    settings.rag.vector_db_path = vec_db
    settings.rag.embedding_dimension = 64
    settings.rag.embedding_provider = "local"
    settings.storage.workspace_root = str(ws_root)

    app = FastAPI()
    app.include_router(rag_router, prefix="/api/v1")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Upload document
            content = (
                "# Architecture Overview\n\n"
                "AgentStudio uses SQLite with sqlite-vec and FTS5 for hybrid retrieval.\n"
                "Task knowledge scope is enforced server-side."
            ).encode("utf-8")
            up_res = await client.post(
                "/api/v1/rag/documents/upload",
                files={"file": ("arch.md", content, "text/markdown")},
                data={"collection": "engineering", "title": "Architecture Spec"},
            )
            assert up_res.status_code == 200
            up_data = up_res.json()
            assert up_data["status"] == "indexed"
            doc_id = up_data["doc_id"]
            assert doc_id.startswith("doc_")

            # 2. List documents with collection filter
            list_res = await client.get("/api/v1/rag/documents", params={"collection": "engineering"})
            assert list_res.status_code == 200
            docs = list_res.json()
            assert len(docs) == 1
            assert docs[0]["doc_id"] == doc_id
            assert docs[0]["status"] == "READY"
            assert docs[0]["title"] == "Architecture Spec"

            # 3. Inspect document detail
            get_res = await client.get(f"/api/v1/rag/documents/{doc_id}")
            assert get_res.status_code == 200
            assert get_res.json()["collection"] == "engineering"

            # 4. Reindex document
            reindex_res = await client.post(f"/api/v1/rag/documents/{doc_id}/reindex")
            assert reindex_res.status_code == 200
            assert reindex_res.json()["status"] == "indexed"

            # 5. Retrieval Playground across modes
            for mode in ("hybrid", "vector", "fts"):
                s_res = await client.post(
                    "/api/v1/rag/search",
                    json={
                        "query": "sqlite-vec FTS5 hybrid retrieval",
                        "top_k": 3,
                        "collection": "engineering",
                        "search_mode": mode,
                    },
                )
                assert s_res.status_code == 200
                hits = s_res.json()
                assert len(hits) >= 1
                explain = hits[0]["explain"]
                assert explain["search_mode"] == mode
                assert explain["final_rank"] == 1
                if mode == "vector":
                    assert explain["vector_similarity"] is not None
                    assert explain["fts_bm25"] is None
                elif mode == "fts":
                    assert explain["fts_bm25"] is not None
                    assert explain["vector_similarity"] is None

            # 6. Evaluation Cases + Run + History
            bad_case_res = await client.post(
                "/api/v1/rag/eval/cases",
                json={"dataset_name": "sprint3b_smoke", "query": "missing targets"},
            )
            assert bad_case_res.status_code == 422

            case_res = await client.post(
                "/api/v1/rag/eval/cases",
                json={
                    "dataset_name": "sprint3b_smoke",
                    "query": "sqlite-vec hybrid retrieval",
                    "expected_doc_ids": [doc_id],
                    "expected_page_numbers": [],
                    "collection": "engineering",
                },
            )
            assert case_res.status_code == 200
            case_id = case_res.json()["case_id"]

            cases_res = await client.get("/api/v1/rag/eval/cases", params={"dataset_name": "sprint3b_smoke"})
            assert cases_res.status_code == 200
            assert len(cases_res.json()) == 1

            run_res = await client.post(
                "/api/v1/rag/eval/run",
                json={"dataset_name": "sprint3b_smoke", "top_k": 3, "search_mode": "hybrid"},
            )
            assert run_res.status_code == 200
            run_data = run_res.json()
            assert run_data["total_cases"] == 1
            assert run_data["avg_recall_at_k"] == 1.0
            assert run_data["avg_citation_accuracy"] == 1.0
            assert run_data["groundedness_method"] == "not_run"
            assert run_data["avg_groundedness"] is None

            runs_res = await client.get("/api/v1/rag/eval/runs", params={"dataset_name": "sprint3b_smoke"})
            assert runs_res.status_code == 200
            assert len(runs_res.json()) == 1

            del_case_res = await client.delete(f"/api/v1/rag/eval/cases/{case_id}")
            assert del_case_res.status_code == 200

            # 7. Delete document
            del_doc_res = await client.delete(f"/api/v1/rag/documents/{doc_id}")
            assert del_doc_res.status_code == 200
            assert (await client.get(f"/api/v1/rag/documents/{doc_id}")).status_code == 404
    finally:
        settings.rag.vector_db_path = orig_vec
        settings.rag.embedding_dimension = orig_dim
        settings.rag.embedding_provider = orig_prov
        settings.storage.workspace_root = orig_ws


def test_rag_ui_contains_three_workspace_sections() -> None:
    """Verify Local RAG Tab HTML includes Document Workspace, Retrieval Playground, and Evaluation."""
    html_path = Path(__file__).resolve().parents[1] / "src" / "jarvis_studio" / "api" / "static" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    for marker in (
        "rag-workspace-section",
        "rag-playground-section",
        "rag-eval-section",
        "uploadRagDoc",
        "reindexRagDoc",
        "runRagEval",
    ):
        assert marker in html
