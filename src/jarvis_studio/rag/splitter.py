"""Text and document chunking utilities for local RAG."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from jarvis_studio.rag.parser import ParsedSection


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
        self.chunk_size = max(50, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size // 2))

    def _hard_slice_line(self, line: str) -> list[str]:
        """Slice an oversized single line into bounded chunks."""
        step = max(1, self.chunk_size - self.chunk_overlap)
        return [line[i : i + self.chunk_size] for i in range(0, len(line), step)]

    def _split_raw_blocks(self, clean_text: str) -> list[str]:
        if len(clean_text) <= self.chunk_size:
            return [clean_text]

        paragraphs = clean_text.split("\n\n")
        raw_chunks: list[str] = []
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
                if len(para) > self.chunk_size:
                    lines = para.split("\n")
                    sub_cur = ""
                    for line in lines:
                        if len(line) > self.chunk_size:
                            if sub_cur:
                                raw_chunks.append(sub_cur)
                                sub_cur = ""
                            raw_chunks.extend(self._hard_slice_line(line))
                        elif len(sub_cur) + len(line) + 1 <= self.chunk_size:
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

        if self.chunk_overlap <= 0 or len(raw_chunks) <= 1:
            return raw_chunks

        overlapped: list[str] = [raw_chunks[0]]
        for idx in range(1, len(raw_chunks)):
            prev_tail = raw_chunks[idx - 1][-self.chunk_overlap :].lstrip()
            cur_block = raw_chunks[idx]
            if prev_tail and not cur_block.startswith(prev_tail):
                combined = f"{prev_tail}\n{cur_block}".strip()
                # Bound combined chunk size slightly above chunk_size to preserve overlap context
                overlapped.append(combined)
            else:
                overlapped.append(cur_block)
        return overlapped

    def split(
        self,
        text: str,
        doc_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Split text into overlapping chunks using paragraph and newline boundaries."""
        meta = dict(metadata or {})
        clean_text = text.strip()
        if not clean_text:
            return []

        raw_chunks = self._split_raw_blocks(clean_text)
        final_chunks: list[DocumentChunk] = []
        for idx, text_block in enumerate(raw_chunks):
            chunk_hash = hashlib.sha256(
                f"{doc_path}:{idx}:{text_block}".encode("utf-8")
            ).hexdigest()[:12]
            final_chunks.append(
                DocumentChunk(
                    chunk_id=f"chk_{chunk_hash}",
                    doc_path=doc_path,
                    chunk_index=idx,
                    content=text_block,
                    metadata=dict(meta),
                )
            )
        return final_chunks

    def split_sections(
        self,
        sections: list[ParsedSection],
        doc_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Split structured sections while preserving page_number and section_title provenance."""
        base_meta = dict(metadata or {})
        final_chunks: list[DocumentChunk] = []
        global_idx = 0

        for sec in sections:
            clean_text = sec.content.strip()
            if not clean_text:
                continue
            sec_meta = dict(base_meta)
            if sec.page_number is not None:
                sec_meta["page_number"] = sec.page_number
            if sec.section_title:
                sec_meta["section_title"] = sec.section_title

            blocks = self._split_raw_blocks(clean_text)
            for block in blocks:
                chunk_hash = hashlib.sha256(
                    f"{doc_path}:{global_idx}:{block}".encode("utf-8")
                ).hexdigest()[:12]
                final_chunks.append(
                    DocumentChunk(
                        chunk_id=f"chk_{chunk_hash}",
                        doc_path=doc_path,
                        chunk_index=global_idx,
                        content=block,
                        metadata=dict(sec_meta),
                    )
                )
                global_idx += 1

        return final_chunks
