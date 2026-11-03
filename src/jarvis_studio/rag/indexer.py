"""Workspace and documentation indexer for local RAG knowledge base."""

import hashlib
import os
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.rag.embeddings import EmbeddingsFactory
from jarvis_studio.rag.splitter import DocumentSplitter
from jarvis_studio.rag.store import SqliteVecStore

DEFAULT_SUPPORTED_EXTENSIONS: set[str] = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".rst",
    ".html",
    ".js",
    ".ts",
    ".sh",
    ".bat",
    ".ps1",
}

DEFAULT_IGNORE_DIRECTORIES: set[str] = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".idea",
    ".vscode",
    ".system_generated",
    "data",
}


class WorkspaceIndexer:
    """Recursively scans, chunks, and indexes files into the local vector database."""

    def __init__(
        self,
        store: SqliteVecStore | None = None,
        embeddings: Embeddings | None = None,
        splitter: DocumentSplitter | None = None,
        supported_extensions: set[str] | None = None,
        ignore_dirs: set[str] | None = None,
    ):
        settings = get_settings()
        self.store = store or SqliteVecStore()
        self.embeddings = embeddings or EmbeddingsFactory.create_embeddings()
        self.splitter = splitter or DocumentSplitter(
            chunk_size=settings.rag.chunk_size,
            chunk_overlap=settings.rag.chunk_overlap,
        )
        self.supported_extensions = supported_extensions or DEFAULT_SUPPORTED_EXTENSIONS
        self.ignore_dirs = ignore_dirs or DEFAULT_IGNORE_DIRECTORIES

    def _calculate_file_hash(self, file_path: Path) -> str:
        """Compute SHA-256 digest of file bytes."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def index_file(self, file_path: str | Path, force: bool = False) -> dict[str, Any]:
        """Index or update a single file."""
        path = Path(file_path).resolve()
        if not path.is_file():
            return {"status": "error", "error": f"File '{path}' does not exist"}

        norm_path = str(path).replace("\\", "/")
        current_hash = self._calculate_file_hash(path)

        if not force:
            existing_hash = self.store.get_file_hash(norm_path)
            if existing_hash == current_hash:
                return {"status": "skipped", "file": norm_path, "reason": "unchanged"}

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            return {"status": "error", "file": norm_path, "error": str(e)}

        chunks = self.splitter.split(
            text=content,
            doc_path=norm_path,
            metadata={"filename": path.name, "suffix": path.suffix},
        )

        if not chunks:
            return {"status": "skipped", "file": norm_path, "reason": "empty"}

        texts = [c.content for c in chunks]
        vecs = self.embeddings.embed_documents(texts)
        count = self.store.add_chunks(chunks, vecs, file_hash=current_hash)

        return {"status": "indexed", "file": norm_path, "chunks": count}

    def index_directory(
        self,
        directory_path: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Recursively scan directory and index eligible source and document files."""
        settings = get_settings()
        target_dir = Path(directory_path or settings.storage.workspace_root).resolve()

        if not target_dir.exists():
            return {
                "status": "error",
                "error": f"Target directory '{target_dir}' does not exist",
                "scanned": 0,
                "indexed": 0,
                "skipped": 0,
                "chunks_added": 0,
            }

        scanned = 0
        indexed = 0
        skipped = 0
        chunks_added = 0
        errors = []

        logger.info(f"Scanning directory for indexing: {target_dir}")

        for root, dirs, files in os.walk(target_dir):
            # In-place prune ignored directories
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]

            for file in files:
                file_path = Path(root) / file
                if file_path.suffix.lower() not in self.supported_extensions:
                    continue

                scanned += 1
                res = self.index_file(file_path, force=force)
                match res.get("status"):
                    case "indexed":
                        indexed += 1
                        chunks_added += res.get("chunks", 0)
                    case "skipped":
                        skipped += 1
                    case "error":
                        errors.append(res)

        return {
            "status": "completed",
            "directory": str(target_dir).replace("\\", "/"),
            "scanned": scanned,
            "indexed": indexed,
            "skipped": skipped,
            "chunks_added": chunks_added,
            "errors": errors,
        }
