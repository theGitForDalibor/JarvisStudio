"""Built-in Tools for RAG local knowledge base and code/document retrieval."""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.tools import tool

from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata

_ACTIVE_KNOWLEDGE_SCOPE: contextvars.ContextVar[dict[str, list[str]] | None] = (
    contextvars.ContextVar("active_knowledge_scope", default=None)
)


@contextmanager
def scoped_knowledge_context(
    scope: dict[str, Any] | None,
) -> Iterator[None]:
    """Bind an immutable Task Knowledge Scope for the duration of tool execution."""
    if scope is None or not isinstance(scope, dict):
        yield
        return
    cols = [str(c) for c in (scope.get("collections") or []) if str(c).strip()]
    docs = [str(d) for d in (scope.get("doc_ids") or []) if str(d).strip()]
    token = _ACTIVE_KNOWLEDGE_SCOPE.set({"collections": cols, "doc_ids": docs})
    try:
        yield
    finally:
        _ACTIVE_KNOWLEDGE_SCOPE.reset(token)


@tool
def search_knowledge_base(
    query: str,
    top_k: int = 4,
    path_filter: str | None = None,
    collection: str | None = None,
    doc_id: str | None = None,
    min_score: float = 0.0,
) -> str:
    """Search the local knowledge base and indexed documents using hybrid vector + keyword retrieval.

    Returns grounded chunks with exact document title, path, page number, and section heading.

    Args:
        query: Natural language or technical query to search for.
        top_k: Number of most relevant chunk matches to return (default 4).
        path_filter: Optional path substring or prefix to constrain search scope.
        collection: Optional knowledge collection name to restrict search scope.
        doc_id: Optional specific document ID (e.g. 'doc_...') to restrict search scope.
        min_score: Minimum similarity score threshold in [0.0, 1.0] (default 0.0).
    """
    active_scope = _ACTIVE_KNOWLEDGE_SCOPE.get()
    effective_collections: list[str] | None = [collection] if collection else None
    effective_doc_ids: list[str] | None = [doc_id] if doc_id else None

    if active_scope is not None:
        allowed_cols = active_scope.get("collections") or []
        allowed_docs = active_scope.get("doc_ids") or []
        if not allowed_cols and not allowed_docs:
            return (
                "Access denied: task has an explicit empty knowledge scope "
                "(no collections or documents authorized)."
            )

        if allowed_cols:
            if collection and collection not in allowed_cols:
                return (
                    f"Access denied: collection '{collection}' is outside the task's "
                    f"authorized knowledge scope {allowed_cols}."
                )
            effective_collections = [collection] if collection else allowed_cols

        if allowed_docs:
            if doc_id and doc_id not in allowed_docs:
                return (
                    f"Access denied: doc_id '{doc_id}' is outside the task's "
                    f"authorized knowledge scope {allowed_docs}."
                )
            effective_doc_ids = [doc_id] if doc_id else allowed_docs

    store = SqliteVecStore()
    embeddings = EmbeddingsFactory.create_embeddings()
    query_vec = embeddings.embed_query(query)

    matches = store.search(
        query_embedding=query_vec,
        top_k=max(1, min(20, top_k)),
        doc_path_prefix=path_filter,
        query_text=query,
        collections=effective_collections,
        doc_ids=effective_doc_ids,
        min_score=min_score,
    )

    if not matches:
        return f"No relevant documents or code chunks found in local knowledge base for query: '{query}'."

    output = [f"Found {len(matches)} relevant match(es) in local knowledge base for '{query}':\n"]
    for i, m in enumerate(matches, 1):
        meta = m.get("metadata") or {}
        doc_title = m.get("title") or m["doc_path"]
        doc_path = m["doc_path"]
        m_doc_id = m.get("doc_id", "")
        m_chunk_id = m.get("chunk_id", "")
        score = m["score"]
        chunk_idx = m.get("chunk_index", 0)
        page_num = meta.get("page_number")
        section_title = meta.get("section_title")

        loc_parts = [f"Doc: {doc_title} ({m_doc_id})", f"Path: {doc_path}"]
        if page_num is not None:
            loc_parts.append(f"Page: {page_num}")
        if section_title:
            loc_parts.append(f"Section: {section_title}")
        loc_parts.append(f"Chunk: #{chunk_idx} ({m_chunk_id})" if m_chunk_id else f"Chunk: #{chunk_idx}")
        loc_parts.append(f"Score: {score}")

        output.append(f"--- [Match {i}] {' | '.join(loc_parts)} ---")
        output.append(m["content"])
        output.append("")

    output.append("Cite exact document titles, page numbers, and section names from above when answering.")
    return "\n".join(output)


mark_tool_metadata(
    search_knowledge_base,
    name="search_knowledge_base",
    description="Search the local knowledge base and indexed documents/code using hybrid vector + keyword search.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="RAG",
)


@tool
def index_workspace_directory(
    directory_path: str | None = None,
    force_reindex: bool = False,
    collection: str = "default",
) -> str:
    """Scan and index files in a directory or workspace into the local vector database.

    Args:
        directory_path: Absolute or relative directory path to index. If omitted, uses configured workspace root.
        force_reindex: Whether to re-embed and overwrite already indexed files even if unchanged.
        collection: Logical collection name to group indexed documents (default 'default').
    """
    indexer = WorkspaceIndexer()
    summary = indexer.index_directory(
        directory_path=directory_path,
        force=force_reindex,
        collection=collection,
    )
    return json.dumps(summary, indent=2, ensure_ascii=False)


mark_tool_metadata(
    index_workspace_directory,
    name="index_workspace_directory",
    description="Scan and index files in a directory or workspace into the local vector database for semantic retrieval.",
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    category="RAG",
)


@tool
def list_indexed_documents(collection: str | None = None) -> str:
    """List all documents currently indexed in the local RAG knowledge base with status and provenance."""
    active_scope = _ACTIVE_KNOWLEDGE_SCOPE.get()
    if active_scope is not None:
        allowed_cols = active_scope.get("collections") or []
        allowed_docs = active_scope.get("doc_ids") or []
        if not allowed_cols and not allowed_docs:
            return (
                "Access denied: task has an explicit empty knowledge scope "
                "(no collections or documents authorized)."
            )
        if collection and allowed_cols and collection not in allowed_cols:
            return (
                f"Access denied: collection '{collection}' is outside the task's "
                f"authorized knowledge scope {allowed_cols}."
            )
    store = SqliteVecStore()
    files = store.list_indexed_files(collection=collection)
    if active_scope is not None:
        allowed_cols = active_scope.get("collections") or []
        allowed_docs = active_scope.get("doc_ids") or []
        files = [
            f
            for f in files
            if (not allowed_cols or f.get("collection") in allowed_cols)
            and (not allowed_docs or f.get("doc_id") in allowed_docs)
        ]

    if not files:
        return "The local knowledge base (within authorized scope) is currently empty."

    return json.dumps(files, indent=2, ensure_ascii=False)


mark_tool_metadata(
    list_indexed_documents,
    name="list_indexed_documents",
    description="List all documents currently indexed in the local RAG knowledge base.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="RAG",
)
