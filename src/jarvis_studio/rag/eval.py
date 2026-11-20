"""Lightweight SQLite-backed RAG Evaluation Core (Recall@K, Precision@K, Citation Accuracy)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from langchain_core.embeddings import Embeddings
from pydantic import BaseModel, Field

from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.store import SqliteVecStore


class EvalCaseCreate(BaseModel):
    """Input payload to create an evaluation test case."""

    dataset_name: str = Field(default="default", min_length=1)
    query: str = Field(..., min_length=1)
    expected_doc_ids: list[str] = Field(default_factory=list)
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_page_numbers: list[int] = Field(default_factory=list)
    ground_truth_answer: str | None = None
    collection: str | None = None


class EvalCaseRecord(BaseModel):
    """Persisted RAG evaluation test case."""

    case_id: str
    dataset_name: str
    query: str
    expected_doc_ids: list[str] = Field(default_factory=list)
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_page_numbers: list[int] = Field(default_factory=list)
    ground_truth_answer: str | None = None
    collection: str | None = None
    created_at: float


class RAGEvaluator:
    """Manages evaluation datasets and executes deterministic retrieval evaluations."""

    def __init__(
        self,
        store: SqliteVecStore | None = None,
        embeddings: Embeddings | None = None,
    ) -> None:
        self.store = store or SqliteVecStore()
        self.embeddings = embeddings or EmbeddingsFactory.create_embeddings()
        self._init_tables()

    def _init_tables(self) -> None:
        with self.store._db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_eval_cases (
                    case_id TEXT PRIMARY KEY,
                    dataset_name TEXT NOT NULL,
                    query TEXT NOT NULL,
                    expected_doc_ids_json TEXT NOT NULL DEFAULT '[]',
                    expected_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
                    expected_page_numbers_json TEXT NOT NULL DEFAULT '[]',
                    ground_truth_answer TEXT,
                    collection TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_dataset ON rag_eval_cases(dataset_name)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_eval_runs (
                    run_id TEXT PRIMARY KEY,
                    dataset_name TEXT NOT NULL,
                    top_k INTEGER NOT NULL,
                    search_mode TEXT NOT NULL,
                    total_cases INTEGER NOT NULL,
                    avg_recall_at_k REAL,
                    avg_precision_at_k REAL,
                    avg_citation_accuracy REAL,
                    groundedness_method TEXT NOT NULL DEFAULT 'not_run',
                    avg_groundedness REAL,
                    results_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def add_case(self, payload: EvalCaseCreate) -> EvalCaseRecord:
        if not payload.expected_doc_ids and not payload.expected_chunk_ids:
            raise ValueError(
                "Evaluation case must specify at least one expected_doc_ids or expected_chunk_ids"
            )
        case_id = f"evalc_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.store._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO rag_eval_cases (
                    case_id, dataset_name, query,
                    expected_doc_ids_json, expected_chunk_ids_json, expected_page_numbers_json,
                    ground_truth_answer, collection, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    payload.dataset_name.strip(),
                    payload.query.strip(),
                    json.dumps(payload.expected_doc_ids),
                    json.dumps(payload.expected_chunk_ids),
                    json.dumps(payload.expected_page_numbers),
                    payload.ground_truth_answer,
                    payload.collection,
                    now,
                ),
            )
        return EvalCaseRecord(
            case_id=case_id,
            dataset_name=payload.dataset_name.strip(),
            query=payload.query.strip(),
            expected_doc_ids=payload.expected_doc_ids,
            expected_chunk_ids=payload.expected_chunk_ids,
            expected_page_numbers=payload.expected_page_numbers,
            ground_truth_answer=payload.ground_truth_answer,
            collection=payload.collection,
            created_at=now,
        )

    def list_cases(self, dataset_name: str | None = None) -> list[EvalCaseRecord]:
        query = "SELECT * FROM rag_eval_cases"
        params: tuple[Any, ...] = ()
        if dataset_name:
            query += " WHERE dataset_name = ?"
            params = (dataset_name,)
        query += " ORDER BY created_at DESC"

        with self.store._db.read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                EvalCaseRecord(
                    case_id=r["case_id"],
                    dataset_name=r["dataset_name"],
                    query=r["query"],
                    expected_doc_ids=json.loads(r["expected_doc_ids_json"] or "[]"),
                    expected_chunk_ids=json.loads(r["expected_chunk_ids_json"] or "[]"),
                    expected_page_numbers=json.loads(r["expected_page_numbers_json"] or "[]"),
                    ground_truth_answer=r["ground_truth_answer"],
                    collection=r["collection"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def delete_case(self, case_id: str) -> bool:
        with self.store._db.transaction() as conn:
            cur = conn.execute("DELETE FROM rag_eval_cases WHERE case_id = ?", (case_id,))
            return cur.rowcount > 0

    @staticmethod
    def _compute_case_metrics(
        case: EvalCaseRecord,
        hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compute deterministic Recall@K, Precision@K, and Citation Accuracy for a single case."""
        if case.expected_chunk_ids:
            expected_set = set(case.expected_chunk_ids)
            retrieved_ids = [h["chunk_id"] for h in hits]
            relevant_hits = sum(1 for cid in retrieved_ids if cid in expected_set)
            recall = len(set(retrieved_ids) & expected_set) / len(expected_set)
            precision = relevant_hits / len(retrieved_ids) if retrieved_ids else 0.0
        else:
            expected_docs = set(case.expected_doc_ids)
            retrieved_docs = [h["doc_id"] for h in hits]
            relevant_hits = sum(1 for did in retrieved_docs if did in expected_docs)
            recall = (len(set(retrieved_docs) & expected_docs) / len(expected_docs)) if expected_docs else 0.0
            precision = relevant_hits / len(retrieved_docs) if retrieved_docs else 0.0

        # Deterministic Citation Accuracy:
        # Fraction of expected sources verified with non-empty provenance (and expected page if specified)
        verified_citations = 0
        expected_targets = (
            set(case.expected_doc_ids)
            if case.expected_doc_ids
            else {h["doc_id"] for h in hits if h["chunk_id"] in set(case.expected_chunk_ids)}
        )
        if not expected_targets:
            citation_accuracy = 0.0
        else:
            expected_pages = set(case.expected_page_numbers)
            for target_doc in expected_targets:
                matching_hits = [h for h in hits if h["doc_id"] == target_doc]
                if not matching_hits:
                    continue
                has_provenance = any(
                    bool(h.get("doc_path") and h.get("title") and h.get("content"))
                    for h in matching_hits
                )
                if not has_provenance:
                    continue
                if expected_pages:
                    hit_pages = {
                        (h.get("metadata") or {}).get("page_number") for h in matching_hits
                    }
                    if not (hit_pages & expected_pages):
                        continue
                verified_citations += 1
            citation_accuracy = verified_citations / len(expected_targets)

        return {
            "case_id": case.case_id,
            "query": case.query,
            "recall_at_k": round(recall, 4),
            "precision_at_k": round(precision, 4),
            "citation_accuracy": round(citation_accuracy, 4),
            "retrieved_doc_ids": [h["doc_id"] for h in hits],
            "retrieved_chunk_ids": [h["chunk_id"] for h in hits],
            "retrieved_pages": [(h.get("metadata") or {}).get("page_number") for h in hits],
        }

    def run_evaluation(
        self,
        *,
        dataset_name: str = "default",
        top_k: int = 4,
        search_mode: str = "hybrid",
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Run deterministic retrieval evaluation over all cases in dataset_name."""
        cases = self.list_cases(dataset_name=dataset_name)
        if not cases:
            raise ValueError(f"No evaluation cases found for dataset '{dataset_name}'")

        case_results: list[dict[str, Any]] = []
        for c in cases:
            q_vec = self.embeddings.embed_query(c.query)
            hits = self.store.search(
                query_embedding=q_vec,
                top_k=top_k,
                query_text=c.query,
                collection=c.collection,
                min_score=min_score,
                search_mode=search_mode,
            )
            case_results.append(self._compute_case_metrics(c, hits))

        n = len(case_results)
        avg_recall = round(sum(r["recall_at_k"] for r in case_results) / n, 4)
        avg_precision = round(sum(r["precision_at_k"] for r in case_results) / n, 4)
        avg_citation = round(sum(r["citation_accuracy"] for r in case_results) / n, 4)

        run_id = f"evalr_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.store._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO rag_eval_runs (
                    run_id, dataset_name, top_k, search_mode, total_cases,
                    avg_recall_at_k, avg_precision_at_k, avg_citation_accuracy,
                    groundedness_method, avg_groundedness, results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_run', NULL, ?, ?)
                """,
                (
                    run_id,
                    dataset_name,
                    top_k,
                    search_mode,
                    n,
                    avg_recall,
                    avg_precision,
                    avg_citation,
                    json.dumps(case_results, ensure_ascii=False),
                    now,
                ),
            )

        return {
            "run_id": run_id,
            "dataset_name": dataset_name,
            "top_k": top_k,
            "search_mode": search_mode,
            "total_cases": n,
            "avg_recall_at_k": avg_recall,
            "avg_precision_at_k": avg_precision,
            "avg_citation_accuracy": avg_citation,
            "groundedness_method": "not_run",
            "avg_groundedness": None,
            "results": case_results,
            "created_at": now,
        }

    def list_runs(self, dataset_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM rag_eval_runs"
        params: list[Any] = []
        if dataset_name:
            query += " WHERE dataset_name = ?"
            params.append(dataset_name)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self.store._db.read_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                {
                    "run_id": r["run_id"],
                    "dataset_name": r["dataset_name"],
                    "top_k": r["top_k"],
                    "search_mode": r["search_mode"],
                    "total_cases": r["total_cases"],
                    "avg_recall_at_k": r["avg_recall_at_k"],
                    "avg_precision_at_k": r["avg_precision_at_k"],
                    "avg_citation_accuracy": r["avg_citation_accuracy"],
                    "groundedness_method": r["groundedness_method"],
                    "avg_groundedness": r["avg_groundedness"],
                    "results": json.loads(r["results_json"] or "[]"),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
