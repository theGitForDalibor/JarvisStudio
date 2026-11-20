"""Phase 3B-1 tests: Retriever Explainability (Vector vs FTS5 vs RRF) and RAG Evaluation Core."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_studio.config.settings import get_settings
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings
from jarvis_studio.rag.eval import EvalCaseCreate, RAGEvaluator
from jarvis_studio.rag.parser import ParsedSection
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore


@pytest.fixture
def eval_env(tmp_path: Path):
    settings = get_settings()
    orig_dim = settings.rag.embedding_dimension
    settings.rag.embedding_dimension = 64

    db_path = str(tmp_path / "vec_3b1.db")
    store = SqliteVecStore(db_path=db_path, dimension=64)
    embedder = DeterministicLocalEmbeddings(dimension=64)
    splitter = DocumentSplitter(chunk_size=300, chunk_overlap=50)

    # Doc A: specs collection, 2 pages
    c_a = splitter.split_sections(
        [
            ParsedSection(content="RuntimeInstanceLock prevents dual writers.", page_number=1, section_title="Locks"),
            ParsedSection(content="Target P99 Latency SLA is 42ms.", page_number=2, section_title="SLA"),
        ],
        doc_path="specs/runtime.pdf",
        metadata={"title": "Runtime Spec"},
    )
    doc_a_id = "doc_spec_a"
    store.add_chunks(
        c_a,
        embedder.embed_documents([c.content for c in c_a]),
        file_hash="ha",
        title="Runtime Spec",
        collection="specs",
        doc_id=doc_a_id,
    )

    # Doc B: finance collection
    c_b = splitter.split_sections(
        [ParsedSection(content="Enterprise Tier pricing is $199/month.", page_number=1, section_title="Pricing")],
        doc_path="finance/pricing.docx",
        metadata={"title": "Pricing Policy"},
    )
    doc_b_id = "doc_fin_b"
    store.add_chunks(
        c_b,
        embedder.embed_documents([c.content for c in c_b]),
        file_hash="hb",
        title="Pricing Policy",
        collection="finance",
        doc_id=doc_b_id,
    )

    yield {
        "store": store,
        "embedder": embedder,
        "doc_a_id": doc_a_id,
        "doc_b_id": doc_b_id,
        "chunks_a": c_a,
        "chunks_b": c_b,
    }

    store.close()
    settings.rag.embedding_dimension = orig_dim


def test_retriever_explainability_modes(eval_env) -> None:
    """Verify hybrid, vector, and fts modes return distinct, non-conflated explain metrics."""
    store: SqliteVecStore = eval_env["store"]
    embedder: DeterministicLocalEmbeddings = eval_env["embedder"]
    q = "Latency SLA 42ms"
    q_vec = embedder.embed_query(q)

    # 1. Hybrid mode: both vector & FTS5 contribute
    res_hybrid = store.search(q_vec, top_k=2, query_text=q, search_mode="hybrid")
    assert len(res_hybrid) >= 1
    top_h = res_hybrid[0]
    assert top_h["doc_id"] == eval_env["doc_a_id"]
    exp_h = top_h["explain"]
    assert exp_h["search_mode"] == "hybrid"
    assert exp_h["final_rank"] == 1
    assert exp_h["vector_similarity"] is not None
    assert exp_h["vector_rank"] is not None
    assert exp_h["fts_bm25"] is not None
    assert exp_h["fts_rank"] is not None
    assert exp_h["rrf_score"] > 0

    # 2. Vector-only mode: FTS fields must be None
    res_vec = store.search(q_vec, top_k=2, query_text=q, search_mode="vector")
    assert len(res_vec) >= 1
    exp_v = res_vec[0]["explain"]
    assert exp_v["search_mode"] == "vector"
    assert exp_v["vector_similarity"] is not None
    assert exp_v["fts_bm25"] is None
    assert exp_v["fts_rank"] is None

    # 3. FTS-only mode: Vector fields must be None
    res_fts = store.search(q_vec, top_k=2, query_text=q, search_mode="fts")
    assert len(res_fts) >= 1
    exp_f = res_fts[0]["explain"]
    assert exp_f["search_mode"] == "fts"
    assert exp_f["fts_bm25"] is not None
    assert exp_f["fts_rank"] == 1
    assert exp_f["vector_similarity"] is None
    assert exp_f["vector_rank"] is None

    # 4. Multi-ID scope filtering
    res_scoped = store.search(
        q_vec,
        top_k=4,
        query_text=q,
        doc_ids=[eval_env["doc_b_id"]],
    )
    assert all(r["doc_id"] == eval_env["doc_b_id"] for r in res_scoped)


def test_rag_evaluator_deterministic_metrics(eval_env) -> None:
    """Verify RAGEvaluator computes Recall@K, Precision@K, Citation Accuracy, and separates Groundedness."""
    evaluator = RAGEvaluator(store=eval_env["store"], embeddings=eval_env["embedder"])

    # Case missing expected IDs must fail validation
    with pytest.raises(ValueError, match="expected_doc_ids or expected_chunk_ids"):
        evaluator.add_case(EvalCaseCreate(dataset_name="ds1", query="No targets"))

    c1 = evaluator.add_case(
        EvalCaseCreate(
            dataset_name="ds1",
            query="What is the Latency SLA?",
            expected_doc_ids=[eval_env["doc_a_id"]],
            expected_page_numbers=[2],
            collection="specs",
        )
    )
    c2 = evaluator.add_case(
        EvalCaseCreate(
            dataset_name="ds1",
            query="What is the Enterprise Tier pricing?",
            expected_chunk_ids=[eval_env["chunks_b"][0].chunk_id],
            expected_doc_ids=[eval_env["doc_b_id"]],
            collection="finance",
        )
    )
    assert len(evaluator.list_cases("ds1")) == 2

    run = evaluator.run_evaluation(dataset_name="ds1", top_k=2, search_mode="hybrid")
    assert run["total_cases"] == 2
    assert run["avg_recall_at_k"] == 1.0
    assert run["avg_citation_accuracy"] == 1.0
    assert run["avg_precision_at_k"] > 0.0
    # Groundedness strictly separated from deterministic metrics
    assert run["groundedness_method"] == "not_run"
    assert run["avg_groundedness"] is None

    runs = evaluator.list_runs("ds1")
    assert len(runs) == 1
    assert runs[0]["run_id"] == run["run_id"]

    assert evaluator.delete_case(c1.case_id) is True
    assert len(evaluator.list_cases("ds1")) == 1
