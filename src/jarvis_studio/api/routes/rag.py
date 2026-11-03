"""Local RAG and Knowledge Base API endpoints."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.store import SqliteVecStore

router = APIRouter(prefix="/rag", tags=["Local RAG"])


class RAGSearchRequest(BaseModel):
    query: str = Field(..., description="Query text to search for")
    top_k: int = Field(default=4, ge=1, le=20)
    path_filter: str | None = Field(
        default=None, description="Optional path prefix filter"
    )


class RAGIndexRequest(BaseModel):
    directory_path: str | None = Field(
        default=None, description="Target folder to scan and index"
    )
    force: bool = Field(default=False, description="Force re-indexing unchanged files")


@router.post("/search", response_model=list[dict[str, Any]])
async def search_rag(payload: RAGSearchRequest) -> list[dict[str, Any]]:
    """Query the local knowledge base using semantic vector retrieval."""
    store = SqliteVecStore()
    embeddings = EmbeddingsFactory.create_embeddings()
    query_vec = embeddings.embed_query(payload.query)

    return store.search(
        query_embedding=query_vec,
        top_k=payload.top_k,
        doc_path_prefix=payload.path_filter,
    )


@router.post("/index", response_model=dict[str, Any])
async def index_workspace(payload: RAGIndexRequest) -> dict[str, Any]:
    """Scan and index local documents and code files."""
    indexer = WorkspaceIndexer()
    return indexer.index_directory(
        directory_path=payload.directory_path, force=payload.force
    )


@router.get("/documents", response_model=list[dict[str, Any]])
async def list_documents() -> list[dict[str, Any]]:
    """List all indexed files in the vector knowledge base."""
    store = SqliteVecStore()
    return store.list_indexed_files()
