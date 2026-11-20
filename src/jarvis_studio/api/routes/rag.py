"""Local RAG and Knowledge Base API endpoints."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from jarvis_studio.config.settings import get_settings
from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.indexer import DEFAULT_SUPPORTED_EXTENSIONS, WorkspaceIndexer
from jarvis_studio.rag.store import SqliteVecStore

router = APIRouter(prefix="/rag", tags=["Local RAG"])

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


class RAGSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Query text to search for")
    top_k: int = Field(default=4, ge=1, le=20)
    path_filter: str | None = Field(
        default=None, description="Optional path prefix filter"
    )
    collection: str | None = Field(
        default=None, description="Optional knowledge collection filter"
    )
    doc_id: str | None = Field(
        default=None, description="Optional document ID filter"
    )
    min_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Minimum relevance score threshold"
    )


class RAGIndexRequest(BaseModel):
    directory_path: str | None = Field(
        default=None, description="Target folder to scan and index"
    )
    force: bool = Field(default=False, description="Force re-indexing unchanged files")
    collection: str = Field(default="default", description="Collection name")


class RAGIngestFileRequest(BaseModel):
    file_path: str = Field(..., min_length=1, description="Local file path to ingest")
    collection: str = Field(default="default", description="Collection name")
    title: str | None = Field(default=None, description="Optional display title")
    force: bool = Field(default=False, description="Force re-indexing if unchanged")


def _sanitize_upload_filename(raw_name: str | None) -> str:
    base = Path(raw_name or "document.txt").name
    clean = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base).strip("._")
    return clean or f"doc_{uuid.uuid4().hex[:8]}.txt"


def _sanitize_collection_name(collection: str) -> str:
    clean = re.sub(r"[^\w\-]+", "_", collection.strip())
    return clean or "default"


@router.post("/search", response_model=list[dict[str, Any]])
async def search_rag(payload: RAGSearchRequest) -> list[dict[str, Any]]:
    """Query the local knowledge base using hybrid vector + FTS5 retrieval."""
    store = SqliteVecStore()
    embeddings = EmbeddingsFactory.create_embeddings()
    query_vec = embeddings.embed_query(payload.query)

    return store.search(
        query_embedding=query_vec,
        top_k=payload.top_k,
        doc_path_prefix=payload.path_filter,
        query_text=payload.query,
        collection=payload.collection,
        doc_id=payload.doc_id,
        min_score=payload.min_score,
    )


@router.post("/index", response_model=dict[str, Any])
async def index_workspace(payload: RAGIndexRequest) -> dict[str, Any]:
    """Scan and index local documents and code files."""
    indexer = WorkspaceIndexer()
    return indexer.index_directory(
        directory_path=payload.directory_path,
        force=payload.force,
        collection=payload.collection,
    )


@router.post("/documents/upload", response_model=dict[str, Any])
async def upload_document(
    file: UploadFile = File(...),
    collection: str = Form(default="default"),
    title: str | None = Form(default=None),
    force: bool = Form(default=True),
) -> dict[str, Any]:
    """Upload a document (PDF, DOCX, Markdown, TXT, HTML, etc.) and index it into the knowledge base."""
    safe_name = _sanitize_upload_filename(file.filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in DEFAULT_SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported document extension '{suffix}'. Supported: {sorted(DEFAULT_SUPPORTED_EXTENSIONS)}",
        )

    raw_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Uploaded file exceeds maximum size limit ({_MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    safe_collection = _sanitize_collection_name(collection)
    ws_root = Path(get_settings().storage.workspace_root).resolve()
    target_dir = (ws_root / "knowledge_uploads" / safe_collection).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = (target_dir / safe_name).resolve()

    # Atomic-style write
    tmp_path = target_path.with_suffix(f"{suffix}.tmp")
    tmp_path.write_bytes(raw_bytes)
    tmp_path.replace(target_path)

    indexer = WorkspaceIndexer()
    result = indexer.index_file(
        target_path,
        force=force,
        collection=safe_collection,
        title=title.strip() if title and title.strip() else None,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("error", "Failed to index document"))
    return result


@router.post("/documents/ingest", response_model=dict[str, Any])
async def ingest_local_document(payload: RAGIngestFileRequest) -> dict[str, Any]:
    """Ingest or re-index an existing local document file by path."""
    target = Path(payload.file_path).resolve()
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: '{payload.file_path}'")
    if target.suffix.lower() not in DEFAULT_SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '{target.suffix.lower()}'",
        )

    indexer = WorkspaceIndexer()
    result = indexer.index_file(
        target,
        force=payload.force,
        collection=_sanitize_collection_name(payload.collection),
        title=payload.title,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("error", "Failed to ingest document"))
    return result


@router.get("/documents", response_model=list[dict[str, Any]])
async def list_documents(
    collection: str | None = Query(default=None, description="Filter by collection"),
) -> list[dict[str, Any]]:
    """List all indexed documents in the knowledge base."""
    store = SqliteVecStore()
    return store.list_indexed_files(collection=collection)


@router.get("/documents/{doc_id}", response_model=dict[str, Any])
async def get_document_status(doc_id: str) -> dict[str, Any]:
    """Retrieve ingestion status and metadata for a specific document."""
    store = SqliteVecStore()
    doc = store.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return doc


@router.delete("/documents/{doc_id}", response_model=dict[str, Any])
async def delete_document(doc_id: str) -> dict[str, Any]:
    """Delete a document and purge all associated chunks, FTS entries, and vectors."""
    store = SqliteVecStore()
    deleted = store.delete_document_by_id(doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return {"status": "deleted", "doc_id": doc_id}
