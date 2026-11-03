"""Unit and integration tests for local RAG knowledge base and sqlite-vec store."""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from jarvis_studio.api.app import create_app
from jarvis_studio.cli.main import cli
from jarvis_studio.rag.embeddings import DeterministicLocalEmbeddings
from jarvis_studio.rag.indexer import WorkspaceIndexer
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore


def test_document_splitter():
    """Verify document chunking logic."""
    splitter = DocumentSplitter(chunk_size=100, chunk_overlap=20)
    text = "Short text"
    chunks = splitter.split(text, doc_path="test.txt")
    assert len(chunks) == 1
    assert chunks[0].doc_path == "test.txt"
    assert chunks[0].content == "Short text"

    # Multi-paragraph text
    long_text = ("First paragraph with some words.\n\n" * 5) + (
        "Second section with more text.\n\n" * 5
    )
    long_chunks = splitter.split(long_text, doc_path="doc.md")
    assert len(long_chunks) > 1
    for idx, c in enumerate(long_chunks):
        assert c.chunk_index == idx
        assert c.doc_path == "doc.md"


def test_deterministic_local_embeddings():
    """Verify offline local embedding generation and L2 normalization."""
    dim = 64
    embedder = DeterministicLocalEmbeddings(dimension=dim)
    vec1 = embedder.embed_query("Hello world")
    assert len(vec1) == dim

    # Determinism: same input produces exact same vector
    vec2 = embedder.embed_query("Hello world")
    assert vec1 == vec2

    # L2 norm should be approximately 1.0
    norm = sum(x * x for x in vec1) ** 0.5
    assert pytest.approx(norm, rel=1e-3) == 1.0


def test_sqlite_vec_store(tmp_path):
    """Verify sqlite-vec virtual table indexing and KNN similarity search."""
    db_file = str(tmp_path / "test_vec.db")
    dim = 32
    store = SqliteVecStore(db_path=db_file, dimension=dim)
    embedder = DeterministicLocalEmbeddings(dimension=dim)

    splitter = DocumentSplitter(chunk_size=200)
    chunks1 = splitter.split(
        "LangGraph manages agent workflows and cyclic graphs.",
        doc_path="docs/langgraph.md",
    )
    vecs1 = embedder.embed_documents([c.content for c in chunks1])

    chunks2 = splitter.split(
        "FastAPI provides high-speed asynchronous REST endpoints.",
        doc_path="docs/fastapi.md",
    )
    vecs2 = embedder.embed_documents([c.content for c in chunks2])

    store.add_chunks(chunks1, vecs1, file_hash="hash1")
    store.add_chunks(chunks2, vecs2, file_hash="hash2")

    # Search for LangGraph
    q_vec = embedder.embed_query("How does LangGraph work?")
    results = store.search(query_embedding=q_vec, top_k=2)

    assert len(results) > 0
    top_result = results[0]
    assert (
        "langgraph" in top_result["doc_path"].lower()
        or "langgraph" in top_result["content"].lower()
    )
    assert top_result["score"] > 0.0

    # Verify indexed files listing
    files = store.list_indexed_files()
    assert len(files) == 2

    # Delete
    store.delete_document("docs/langgraph.md")
    assert len(store.list_indexed_files()) == 1


def test_workspace_indexer(tmp_path):
    """Verify recursive scanning, chunking, and hashing incremental updates."""
    # Create sample files
    sub = tmp_path / "src"
    sub.mkdir()
    f1 = sub / "agent.py"
    f1.write_text(
        "class ProductionAgent:\n    def run(self):\n        pass\n", encoding="utf-8"
    )

    f2 = tmp_path / "README.md"
    f2.write_text(
        "# Project Documentation\nDetails about agent architecture.\n", encoding="utf-8"
    )

    db_file = str(tmp_path / "idx_test.db")
    dim = 32
    store = SqliteVecStore(db_path=db_file, dimension=dim)
    embedder = DeterministicLocalEmbeddings(dimension=dim)

    indexer = WorkspaceIndexer(store=store, embeddings=embedder)
    summary = indexer.index_directory(str(tmp_path))

    assert summary["status"] == "completed"
    assert summary["indexed"] == 2
    assert summary["scanned"] == 2

    # Second pass: files unchanged -> should be skipped
    summary2 = indexer.index_directory(str(tmp_path))
    assert summary2["indexed"] == 0
    assert summary2["skipped"] == 2


def test_rag_api_endpoints(tmp_path):
    """Verify FastAPI /rag/search and /rag/documents routes."""
    app = create_app()
    with TestClient(app) as client:
        # Search endpoint
        search_res = client.post(
            "/api/v1/rag/search", json={"query": "test query", "top_k": 2}
        )
        assert search_res.status_code == 200
        assert isinstance(search_res.json(), list)

        # Documents endpoint
        docs_res = client.get("/api/v1/rag/documents")
        assert docs_res.status_code == 200
        assert isinstance(docs_res.json(), list)


def test_cli_rag_commands():
    """Verify CLI commands: rag list, rag search."""
    runner = CliRunner()

    res_list = runner.invoke(cli, ["rag", "list"])
    assert res_list.exit_code == 0

    res_search = runner.invoke(cli, ["rag", "search", "LangGraph"])
    assert res_search.exit_code == 0
