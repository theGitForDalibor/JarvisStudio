"""Built-in Tools for RAG local knowledge base and code/document retrieval."""

import json

from langchain_core.tools import tool

from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.store import SqliteVecStore
from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata


@tool
def search_knowledge_base(
    query: str,
    top_k: int = 4,
    path_filter: str | None = None,
) -> str:
    """Search the local knowledge base and indexed workspace documents/code using semantic vector search.

    Args:
        query: Natural language or technical query to search for.
        top_k: Number of most relevant chunk matches to return (default 4).
        path_filter: Optional path substring or prefix to constrain search scope.
    """
    store = SqliteVecStore()
    embeddings = EmbeddingsFactory.create_embeddings()
    query_vec = embeddings.embed_query(query)

    matches = store.search(
        query_embedding=query_vec,
        top_k=top_k,
        doc_path_prefix=path_filter,
    )

    if not matches:
        return f"No relevant documents or code chunks found in local knowledge base for query: '{query}'."

    output = [f"Found {len(matches)} relevant match(es) for '{query}':\n"]
    for i, m in enumerate(matches, 1):
        doc_name = m["doc_path"]
        score = m["score"]
        content = m["content"]
        output.append(f"--- [Match {i}] Source: {doc_name} (Similarity: {score}) ---")
        output.append(content)
        output.append("")

    return "\n".join(output)


mark_tool_metadata(
    search_knowledge_base,
    name="search_knowledge_base",
    description="Search the local knowledge base and indexed workspace documents/code using semantic vector search.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="RAG",
)


@tool
def index_workspace_directory(
    directory_path: str | None = None,
    force_reindex: bool = False,
) -> str:
    """Scan and index files in a directory or workspace into the local vector database.

    Args:
        directory_path: Absolute or relative directory path to index. If omitted, uses configured workspace root.
        force_reindex: Whether to re-embed and overwrite already indexed files even if unchanged.
    """
    indexer = WorkspaceIndexer()
    summary = indexer.index_directory(
        directory_path=directory_path, force=force_reindex
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
def list_indexed_documents() -> str:
    """List all source files and documents currently indexed in the local RAG knowledge base."""
    store = SqliteVecStore()
    files = store.list_indexed_files()
    if not files:
        return "The local knowledge base is currently empty. Run 'index_workspace_directory' to index files."

    return json.dumps(files, indent=2, ensure_ascii=False)


mark_tool_metadata(
    list_indexed_documents,
    name="list_indexed_documents",
    description="List all source files and documents currently indexed in the local RAG knowledge base.",
    risk_level=RiskLevel.SAFE,
    requires_approval=False,
    category="RAG",
)
