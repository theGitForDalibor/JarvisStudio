"""Web page content fetching and extraction tool for Research & Analysis."""

from __future__ import annotations

import html as html_lib
import ipaddress
import logging
import re
import socket
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
from langchain_core.tools import tool

from jarvis_studio.config.settings import get_settings
from jarvis_studio.tools.base import RiskLevel, mark_tool_metadata

_logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024  # 2 MB cap to prevent memory exhaustion
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def validate_public_http_url(url: str) -> str:
    """Validate URL is public http/https and does not target private/loopback/link-local hosts."""
    clean = url.strip()
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}'; only http/https are allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL missing valid hostname.")

    lower_host = hostname.lower()
    if lower_host in ("localhost", "localhost.localdomain") or lower_host.endswith(".local"):
        raise PermissionError(f"Blocked internal/local hostname: '{hostname}'")

    # Check literal IP address first
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise PermissionError(f"Blocked non-public IP address: '{hostname}'")
        return clean
    except ValueError:
        pass  # Hostname is a domain name; resolve DNS

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        for info in addr_infos:
            resolved_ip = info[4][0]
            ip_obj = ipaddress.ip_address(resolved_ip)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                raise PermissionError(
                    f"Blocked URL '{hostname}' resolving to non-public IP '{resolved_ip}'"
                )
    except PermissionError:
        raise
    except OSError:
        # DNS resolution failures will be handled by HTTP client error path
        pass

    return clean


def extract_readable_text_from_html(raw_html: str) -> tuple[str, str]:
    """Extract (title, clean_text) from raw HTML document."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.DOTALL | re.IGNORECASE)
    title = ""
    if title_match:
        title = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(1)))).strip()

    # Strip non-content blocks
    cleaned = re.sub(
        r"<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>",
        " ",
        raw_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace block tags with newlines to preserve paragraph structure
    cleaned = re.sub(r"</?(p|div|br|li|h[1-6]|tr|section|article)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    # Collapse spaces per line and excess blank lines
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    non_empty = [ln for ln in lines if ln]
    return title, "\n".join(non_empty)


@tool
def fetch_web_page(url: str, max_chars: int = 12000) -> str:
    """Fetch and extract readable text content from a verified public web URL.

    Use this after `search_web` to inspect primary documentation, technical blogs,
    and pricing/architecture pages in detail before synthesizing a research report.

    Args:
        url: Public http:// or https:// URL to fetch.
        max_chars: Maximum characters of extracted content to return (default 12000).
    """
    cfg = get_settings().search
    effective_max = max(500, min(cfg.fetch_max_chars, max_chars))
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        safe_url = validate_public_http_url(url)
    except (ValueError, PermissionError) as sec_err:
        return f"Error fetching URL '{url}': {sec_err}"

    attempts = max(1, cfg.max_retries + 1)
    last_error: str = "Unknown error"
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=cfg.timeout_seconds, follow_redirects=True) as client:
                resp = client.get(
                    safe_url,
                    headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain"},
                )
                resp.raise_for_status()
                raw_bytes = resp.content[:_MAX_DOWNLOAD_BYTES]
                content_type = resp.headers.get("content-type", "").lower()
                text_body = raw_bytes.decode(resp.encoding or "utf-8", errors="replace")

                if "html" in content_type or "<html" in text_body[:500].lower():
                    title, extracted = extract_readable_text_from_html(text_body)
                else:
                    title = ""
                    extracted = text_body.strip()

                if not extracted:
                    return (
                        f"Fetched URL '{safe_url}' ({now_iso}), but no readable text content could be extracted."
                    )

                truncated = False
                if len(extracted) > effective_max:
                    extracted = extracted[:effective_max] + "\n\n... [Content truncated]"
                    truncated = True

                header_lines = [
                    f"### Web Page Content (Retrieved: {now_iso})",
                    f"- URL: {str(resp.url)}",
                ]
                if title:
                    header_lines.append(f"- Title: {title}")
                if truncated:
                    header_lines.append(f"- Note: Truncated to {effective_max} characters.")
                header_lines.append("")
                header_lines.append(extracted)
                return "\n".join(header_lines)
        except Exception as exc:
            last_error = str(exc)
            _logger.warning(
                "fetch_web_page failed for '%s' (attempt %d/%d): %s",
                safe_url,
                attempt + 1,
                attempts,
                exc,
            )
            if attempt + 1 < attempts:
                time.sleep(0.3 * (attempt + 1))

    return f"Error fetching URL '{safe_url}' after {attempts} attempt(s): {last_error}"


mark_tool_metadata(
    fetch_web_page,
    risk_level=RiskLevel.SAFE,
    category="research",
    description="Fetch and extract readable content from a public web page for detailed analysis.",
)
