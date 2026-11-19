"""Search tools and pluggable search providers for research and analysis tasks."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from langchain_core.tools import tool

from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata

_logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A verified search hit with real provenance."""

    title: str
    url: str
    snippet: str
    published_date: str | None = None
    source: str = "web"


@runtime_checkable
class SearchProvider(Protocol):
    """Protocol for pluggable search engines."""

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Perform search query and return structured results."""
        ...


class DuckDuckGoSearchProvider:
    """Lightweight web search provider using DuckDuckGo HTML endpoint without heavy dependencies."""

    ENDPOINT = "https://html.duckduckgo.com/html/"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        encoded = urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(
            f"{self.ENDPOINT}?{encoded}",
            headers={"User-Agent": self.USER_AGENT, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                html = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            _logger.warning("DuckDuckGo web request failed: %s", exc)
            return []

        results: list[SearchResult] = []
        # Parse result blocks: <a class="result__snippet" ...> or <a class="result__url" ...>
        # Match <a class="result__url" href="(?P<url>[^"]+)">(?P<title>[^<]+)</a>
        link_matches = re.finditer(
            r'<a[^>]+class="result__snippet[^"]*"[^>]*href="(?P<link>[^"]+)"[^>]*>(?P<snippet>.*?)</a>',
            html,
            re.DOTALL,
        )
        titles = re.findall(
            r'<a[^>]+class="result__url"[^>]*>(?P<title>.*?)</a>',
            html,
        )

        # Fallback simpler regex for result links
        if not titles:
            result_blocks = re.findall(
                r'<h2 class="result__title">\s*<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
                html,
                re.DOTALL,
            )
            snippets = re.findall(
                r'<a class="result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
                html,
                re.DOTALL,
            )
            for idx, (href, raw_title) in enumerate(result_blocks[:top_k]):
                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                # DuckDuckGo wraps target in uddg= param
                parsed_url = href
                if "uddg=" in href:
                    m = re.search(r"uddg=([^&]+)", href)
                    if m:
                        parsed_url = urllib.parse.unquote(m.group(1))

                snip = ""
                if idx < len(snippets):
                    snip = re.sub(r"<[^>]+>", "", snippets[idx]).strip()

                if clean_title and parsed_url:
                    results.append(
                        SearchResult(
                            title=clean_title,
                            url=parsed_url,
                            snippet=snip,
                            source="duckduckgo",
                        )
                    )
        return results[:top_k]


class LocalKnowledgeFallbackProvider:
    """Fallback search provider querying local SQLite-vec store when web is unavailable."""

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        try:
            from jarvis_studio.rag.embeddings import EmbeddingsFactory
            from jarvis_studio.rag.store import SqliteVecStore

            store = SqliteVecStore()
            embeddings = EmbeddingsFactory.create_embeddings()
            query_vec = embeddings.embed_query(query)
            matches = store.search(query_embedding=query_vec, top_k=top_k)

            results: list[SearchResult] = []
            for m in matches:
                doc_path = m["doc_path"]
                results.append(
                    SearchResult(
                        title=f"Local KB: {doc_path}",
                        url=f"workspace://{doc_path}",
                        snippet=m["content"][:300],
                        source="local_kb",
                    )
                )
            return results
        except Exception as exc:
            _logger.debug("Local KB fallback failed: %s", exc)
            return []


class CompositeSearchEngine:
    """Orchestrates primary web search and gracefully degrades to local knowledge base."""

    def __init__(self, primary: SearchProvider | None = None, fallback: SearchProvider | None = None) -> None:
        self.primary = primary or DuckDuckGoSearchProvider()
        self.fallback = fallback or LocalKnowledgeFallbackProvider()

    def search(self, query: str, top_k: int = 5) -> tuple[list[SearchResult], str]:
        """Search with automatic fallback. Returns (results, provider_used)."""
        clean_query = query.strip()
        if not clean_query:
            return [], "none"

        # Try primary web search
        try:
            web_results = self.primary.search(clean_query, top_k=top_k)
            if web_results:
                return web_results, "web"
        except Exception as exc:
            _logger.warning("Primary web search error: %s", exc)

        # Fallback to local KB
        fallback_results = self.fallback.search(clean_query, top_k=top_k)
        if fallback_results:
            return fallback_results, "local_kb"

        return [], "exhausted"


_search_engine = CompositeSearchEngine()


@tool
def search_web(query: str, top_k: int = 5) -> str:
    """Search the web for real-time information, research documents, and technical references.

    Always returns real URLs and titles. If external web search is offline, gracefully
    falls back to local knowledge base. Never generates hallucinated URLs.

    Args:
        query: Search keywords or research question.
        top_k: Number of search results to return (default 5, max 10).
    """
    k = max(1, min(10, top_k))
    results, provider = _search_engine.search(query, top_k=k)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if not results:
        return (
            f"No verified search results found for query: '{query}'.\n"
            f"(Search attempted at {now_iso}; external network unavailable and local KB had no matches. "
            f"No synthetic or unverified citations were generated.)"
        )

    out = [f"### Search Results for '{query}' (Retrieved: {now_iso}, Provider: {provider})\n"]
    for i, r in enumerate(results, 1):
        out.append(f"[{i}] **{r.title}**")
        out.append(f"    - URL: {r.url}")
        if r.snippet:
            out.append(f"    - Snippet: {r.snippet}")
        out.append("")

    out.append("Use the verified URLs above as citation sources when writing deliverables.")
    return "\n".join(out)


mark_tool_metadata(
    search_web,
    risk_level=RiskLevel.SAFE,
    category="research",
    description="Search the web for real-world factual information with genuine source citations.",
)
