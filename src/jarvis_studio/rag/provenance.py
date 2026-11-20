"""Deterministic Provenance Verifier for Agent-generated Deliverables.

Verifies that citations (doc_ids, chunk_ids, page numbers, quoted snippets, and
external web URLs) in a generated report match the actual tool outputs recorded
during that specific Run — never defaulting to pass when citations are missing
or unverifiable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis_studio.core.tasks.models import EventRecord, EventType

_MATCH_HEADER_RE = re.compile(
    r"^--- \[Match \d+\] (?P<meta>.+?) ---$",
    re.MULTILINE,
)
_DOC_ID_IN_META_RE = re.compile(r"Doc:\s*(?P<title>.+?)\s*\((?P<doc_id>[^)]+)\)")
_PATH_IN_META_RE = re.compile(r"Path:\s*(?P<path>[^|]+)")
_PAGE_IN_META_RE = re.compile(r"Page:\s*(?P<page>\d+)")
_CHUNK_IN_META_RE = re.compile(r"Chunk:\s*#(?P<idx>\d+)(?:\s*\((?P<chunk_id>[^)]+)\))?")

_REPORT_DOC_ID_RE = re.compile(r"\b(doc_[a-zA-Z0-9_-]{4,})\b")
_REPORT_CHUNK_ID_RE = re.compile(r"\b(chk_[a-zA-Z0-9_-]{4,})\b")
_REPORT_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`]+")
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.+)$", re.MULTILINE)
_INLINE_QUOTE_RE = re.compile(r"\"([^\"]{15,300})\"")
_PAGE_NEAR_DOC_RE = re.compile(r"(?:Page|p\.|页)\s*[:#]?\s*(\d+)", re.IGNORECASE)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class RetrievedChunkEvidence:
    """A single chunk actually returned by search_knowledge_base in a Run."""

    doc_id: str
    chunk_id: str
    doc_path: str
    title: str
    page_number: int | None
    content: str


@dataclass
class ProvenanceVerificationResult:
    """Result of deterministic citation verification for a Run's Deliverable."""

    verified: bool
    run_id: str | None
    retrieved_chunk_count: int
    retrieved_url_count: int
    verified_doc_ids: list[str] = field(default_factory=list)
    verified_chunk_ids: list[str] = field(default_factory=list)
    verified_urls: list[str] = field(default_factory=list)
    verified_quotes: list[str] = field(default_factory=list)
    unverified_citations: list[dict[str, str]] = field(default_factory=list)


def collect_run_tool_outputs(
    events: list[EventRecord],
    *,
    run_id: str | None = None,
    tool_messages_by_call_id: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Collect (tool_name, full_or_preview_output) for successful tool calls in a Run."""
    outputs: list[tuple[str, str]] = []
    msg_map = tool_messages_by_call_id or {}

    for ev in events:
        if ev.event_type != EventType.TOOL_COMPLETED:
            continue
        if run_id is not None and ev.run_id != run_id:
            continue
        try:
            payload = json.loads(ev.payload_json) if ev.payload_json else {}
        except Exception:
            continue
        if not payload.get("success"):
            continue
        tool_name = str(payload.get("tool_name") or "")
        call_id = str(payload.get("tool_call_id") or "")
        full_output = msg_map.get(call_id)
        if full_output is None:
            full_output = str(payload.get("output_preview") or "")
        outputs.append((tool_name, full_output))
    return outputs


def parse_rag_tool_output(output: str) -> list[RetrievedChunkEvidence]:
    """Parse structured chunk evidence from search_knowledge_base output."""
    headers = list(_MATCH_HEADER_RE.finditer(output))
    chunks: list[RetrievedChunkEvidence] = []
    for idx, m in enumerate(headers):
        meta_str = m.group("meta")
        body_start = m.end() + 1
        body_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(output)
        body = output[body_start:body_end]
        body = body.replace(
            "Cite exact document titles, page numbers, and section names from above when answering.",
            "",
        ).strip()

        doc_m = _DOC_ID_IN_META_RE.search(meta_str)
        path_m = _PATH_IN_META_RE.search(meta_str)
        page_m = _PAGE_IN_META_RE.search(meta_str)
        chunk_m = _CHUNK_IN_META_RE.search(meta_str)

        chunks.append(
            RetrievedChunkEvidence(
                doc_id=doc_m.group("doc_id").strip() if doc_m else "",
                title=doc_m.group("title").strip() if doc_m else "",
                doc_path=path_m.group("path").strip() if path_m else "",
                page_number=int(page_m.group("page")) if page_m else None,
                chunk_id=(chunk_m.group("chunk_id") or "").strip() if chunk_m else "",
                content=body,
            )
        )
    return chunks


def verify_report_provenance(
    report_markdown: str,
    events: list[EventRecord],
    *,
    run_id: str | None = None,
    tool_messages_by_call_id: dict[str, str] | None = None,
    require_at_least_one_citation: bool = True,
) -> ProvenanceVerificationResult:
    """Deterministically verify report citations against a Run's actual tool outputs."""
    tool_outputs = collect_run_tool_outputs(
        events,
        run_id=run_id,
        tool_messages_by_call_id=tool_messages_by_call_id,
    )

    retrieved_chunks: list[RetrievedChunkEvidence] = []
    retrieved_urls: set[str] = set()

    for tool_name, output in tool_outputs:
        if tool_name == "search_knowledge_base":
            retrieved_chunks.extend(parse_rag_tool_output(output))
        elif tool_name in ("search_web", "fetch_web_page"):
            for url in _REPORT_URL_RE.findall(output):
                retrieved_urls.add(url.rstrip(".,;:"))

    valid_doc_ids = {c.doc_id for c in retrieved_chunks if c.doc_id}
    valid_chunk_ids = {c.chunk_id for c in retrieved_chunks if c.chunk_id}
    norm_chunk_contents = [_normalize_ws(c.content) for c in retrieved_chunks if c.content]

    cited_doc_ids = list(dict.fromkeys(_REPORT_DOC_ID_RE.findall(report_markdown)))
    cited_chunk_ids = list(dict.fromkeys(_REPORT_CHUNK_ID_RE.findall(report_markdown)))
    cited_urls = list(
        dict.fromkeys(u.rstrip(".,;:") for u in _REPORT_URL_RE.findall(report_markdown))
    )

    verified_doc_ids: list[str] = []
    verified_chunk_ids: list[str] = []
    verified_urls: list[str] = []
    verified_quotes: list[str] = []
    unverified: list[dict[str, str]] = []

    for did in cited_doc_ids:
        if did in valid_doc_ids:
            verified_doc_ids.append(did)
        else:
            unverified.append(
                {
                    "type": "doc_id",
                    "value": did,
                    "reason": "doc_id was not returned by search_knowledge_base in this Run",
                }
            )

    for cid in cited_chunk_ids:
        if cid in valid_chunk_ids:
            verified_chunk_ids.append(cid)
        else:
            unverified.append(
                {
                    "type": "chunk_id",
                    "value": cid,
                    "reason": "chunk_id was not returned by search_knowledge_base in this Run",
                }
            )

    # Verify page numbers mentioned on lines containing a verified doc_id
    for line in report_markdown.splitlines():
        line_docs = [d for d in _REPORT_DOC_ID_RE.findall(line) if d in valid_doc_ids]
        if not line_docs:
            continue
        for page_str in _PAGE_NEAR_DOC_RE.findall(line):
            page_num = int(page_str)
            matching_pages = {
                c.page_number for c in retrieved_chunks if c.doc_id in line_docs and c.page_number is not None
            }
            if matching_pages and page_num not in matching_pages:
                unverified.append(
                    {
                        "type": "page_number",
                        "value": f"{line_docs[0]}:Page {page_num}",
                        "reason": f"Page {page_num} was not in retrieved chunks {sorted(matching_pages)}",
                    }
                )

    for url in cited_urls:
        if url in retrieved_urls:
            verified_urls.append(url)
        else:
            unverified.append(
                {
                    "type": "url",
                    "value": url,
                    "reason": "URL was not returned by search_web or fetch_web_page in this Run",
                }
            )

    # Verify blockquotes and explicit inline quotes against retrieved chunk content
    quotes = list(dict.fromkeys(_BLOCKQUOTE_RE.findall(report_markdown) + _INLINE_QUOTE_RE.findall(report_markdown)))
    for q in quotes:
        nq = _normalize_ws(q)
        if len(nq) < 12:
            continue
        if any(nq in body for body in norm_chunk_contents):
            verified_quotes.append(q)
        else:
            unverified.append(
                {
                    "type": "quote",
                    "value": q[:120],
                    "reason": "Quoted text does not match any retrieved chunk content in this Run",
                }
            )

    total_citations = len(cited_doc_ids) + len(cited_chunk_ids) + len(cited_urls)
    has_required = total_citations > 0 if require_at_least_one_citation else True
    is_verified = has_required and len(unverified) == 0

    return ProvenanceVerificationResult(
        verified=is_verified,
        run_id=run_id,
        retrieved_chunk_count=len(retrieved_chunks),
        retrieved_url_count=len(retrieved_urls),
        verified_doc_ids=verified_doc_ids,
        verified_chunk_ids=verified_chunk_ids,
        verified_urls=verified_urls,
        verified_quotes=verified_quotes,
        unverified_citations=unverified,
    )
