"""Text and document chunking utilities for local RAG."""

import hashlib
from typing import Any

from pydantic import BaseModel, Field


class DocumentChunk(BaseModel):
    """A semantic chunk of a larger document or source code file."""

    chunk_id: str = Field(..., description="Unique deterministic or random chunk ID")
    doc_path: str = Field(
        ..., description="Relative or absolute path of the source file"
    )
    chunk_index: int = Field(..., description="0-indexed position within the document")
    content: str = Field(..., description="The chunk text payload")
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentSplitter:
    """Splits documents and source code files into overlapping semantic chunks."""

    def __init__(self, chunk_size: int = 600, chunk_overlap: int = 100):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(
        self,
        text: str,
        doc_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Split text into chunks using recursive paragraph and newline separators."""
        meta = metadata or {}
        clean_text = text.strip()
        if not clean_text:
            return []

        if len(clean_text) <= self.chunk_size:
            chunk_hash = hashlib.sha256(
                f"{doc_path}:0:{clean_text}".encode()
            ).hexdigest()[:12]
            return [
                DocumentChunk(
                    chunk_id=f"chk_{chunk_hash}",
                    doc_path=doc_path,
                    chunk_index=0,
                    content=clean_text,
                    metadata=meta,
                )
            ]

        # Break text into paragraphs / sections first
        paragraphs = clean_text.split("\n\n")
        raw_chunks = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current) + len(para) + 2 <= self.chunk_size:
                current = f"{current}\n\n{para}".strip()
            else:
                if current:
                    raw_chunks.append(current)
                # If paragraph itself exceeds chunk_size, split by lines
                if len(para) > self.chunk_size:
                    lines = para.split("\n")
                    sub_cur = ""
                    for line in lines:
                        if len(sub_cur) + len(line) + 1 <= self.chunk_size:
                            sub_cur = f"{sub_cur}\n{line}".strip()
                        else:
                            if sub_cur:
                                raw_chunks.append(sub_cur)
                            sub_cur = line
                    if sub_cur:
                        raw_chunks.append(sub_cur)
                    current = ""
                else:
                    current = para

        if current:
            raw_chunks.append(current)

        # Apply overlap between consecutive chunks if applicable
        final_chunks: list[DocumentChunk] = []
        for idx, text_block in enumerate(raw_chunks):
            chunk_hash = hashlib.sha256(
                f"{doc_path}:{idx}:{text_block}".encode()
            ).hexdigest()[:12]
            final_chunks.append(
                DocumentChunk(
                    chunk_id=f"chk_{chunk_hash}",
                    doc_path=doc_path,
                    chunk_index=idx,
                    content=text_block,
                    metadata=meta,
                )
            )

        return final_chunks
