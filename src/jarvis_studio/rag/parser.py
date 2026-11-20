"""Multi-format document parser preserving page numbers, headings, and provenance metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedSection:
    """A logical section or page extracted from a document with source provenance."""

    content: str
    page_number: int | None = None
    section_title: str | None = None


@dataclass
class ParsedDocument:
    """Structured parse output for an ingested document."""

    title: str
    file_type: str
    sections: list[ParsedSection] = field(default_factory=list)


def _parse_pdf(path: Path) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta_title = ""
    if reader.metadata and getattr(reader.metadata, "title", None):
        meta_title = str(reader.metadata.title).strip()

    sections: list[ParsedSection] = []
    for page_idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(
                ParsedSection(
                    content=text,
                    page_number=page_idx,
                    section_title=f"Page {page_idx}",
                )
            )
    return ParsedDocument(
        title=meta_title or path.stem,
        file_type="pdf",
        sections=sections,
    )


def _parse_docx(path: Path) -> ParsedDocument:
    from docx import Document

    doc = Document(str(path))
    doc_title = ""
    if doc.core_properties and doc.core_properties.title:
        doc_title = str(doc.core_properties.title).strip()

    sections: list[ParsedSection] = []
    current_heading: str | None = None
    buffer: list[str] = []

    def _flush() -> None:
        text = "\n\n".join(buffer).strip()
        if text:
            sections.append(
                ParsedSection(
                    content=text,
                    page_number=None,
                    section_title=current_heading,
                )
            )
        buffer.clear()

    for para in doc.paragraphs:
        txt = para.text.strip()
        if not txt:
            continue
        style_name = (para.style.name if para.style else "") or ""
        if style_name.lower().startswith("heading"):
            _flush()
            current_heading = txt
            if not doc_title and style_name.lower() in ("heading 1", "title"):
                doc_title = txt
            buffer.append(txt)
        else:
            buffer.append(txt)

    # Also extract table cell text if present
    for table in doc.tables:
        rows_text: list[str] = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                rows_text.append(" | ".join(cells))
        if rows_text:
            buffer.append("\n".join(rows_text))

    _flush()
    return ParsedDocument(
        title=doc_title or path.stem,
        file_type="docx",
        sections=sections,
    )


def _parse_markdown(path: Path) -> ParsedDocument:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    sections: list[ParsedSection] = []
    current_heading: str | None = None
    doc_title: str | None = None
    buffer: list[str] = []

    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for line in raw.splitlines():
        m = heading_re.match(line)
        if m:
            if buffer:
                text = "\n".join(buffer).strip()
                if text:
                    sections.append(
                        ParsedSection(content=text, section_title=current_heading)
                    )
                buffer.clear()
            heading_text = m.group(2).strip()
            current_heading = heading_text
            if doc_title is None and len(m.group(1)) == 1:
                doc_title = heading_text
            buffer.append(line)
        else:
            buffer.append(line)

    if buffer:
        text = "\n".join(buffer).strip()
        if text:
            sections.append(ParsedSection(content=text, section_title=current_heading))

    return ParsedDocument(
        title=doc_title or path.stem,
        file_type="markdown",
        sections=sections,
    )


def _parse_html_file(path: Path) -> ParsedDocument:
    from jarvis_studio.tools.builtin.web_fetch_tools import extract_readable_text_from_html

    raw = path.read_text(encoding="utf-8", errors="ignore")
    title, extracted = extract_readable_text_from_html(raw)
    sections = [ParsedSection(content=extracted, section_title=title or None)] if extracted else []
    return ParsedDocument(
        title=title or path.stem,
        file_type="html",
        sections=sections,
    )


def parse_document(file_path: str | Path) -> ParsedDocument:
    """Parse a document file into structured sections preserving page/heading provenance."""
    path = Path(file_path).resolve()
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix in (".md", ".markdown"):
        return _parse_markdown(path)
    if suffix in (".html", ".htm"):
        return _parse_html_file(path)

    raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    sections = [ParsedSection(content=raw)] if raw else []
    return ParsedDocument(
        title=path.name,
        file_type=suffix.lstrip(".") or "text",
        sections=sections,
    )
