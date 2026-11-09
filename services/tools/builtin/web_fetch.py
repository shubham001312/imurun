"""Read one web page - through the research extractor.

The page is fetched with the research engine's capped client (2 MB, honest
User-Agent, public addresses only) and cleaned by
``services/research/extract.py``: the main content is kept (navigation,
cookie banners, share bars, sidebars and other boilerplate removed) and
rendered as Markdown-ish text with headings, lists and tables; the
publication date and language are reported when the page declares them.
Nothing from the page is ever executed. The text handed to the model is
capped (``max_chars``, default 8000) with an explicit note when it was cut.

``extract_text`` is the old whole-page text flattener, kept for callers that
want every visible line rather than the main content.
"""
from __future__ import annotations

import html
import re
from typing import Any

from ...research.engine import fetch_page
from ...research.net import default_fetcher, is_public_url
from ..base import Tool, ToolResult

SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|template|svg|head|nav|footer|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCKS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|pre|blockquote)\b[^>]*>", re.IGNORECASE)
TAGS = re.compile(r"<[^>]+>")
SPACES = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES = re.compile(r"\n\s*\n+")
MAX_TEXT = 8000
MAX_TEXT_LIMIT = 40_000
FETCH_TIMEOUT = 15.0


def extract_text(markup: str, limit: int | None = MAX_TEXT) -> str:
    """Turn HTML into readable plain text (capped at ``limit`` characters)."""
    cleaned = COMMENTS.sub(" ", markup)
    cleaned = SCRIPT_STYLE.sub(" ", cleaned)
    cleaned = BLOCKS.sub("\n", cleaned)
    cleaned = TAGS.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = SPACES.sub(" ", cleaned)
    cleaned = BLANK_LINES.sub("\n", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
    return cleaned if limit is None else cleaned[:limit]


class WebFetchTool(Tool):
    name = "web_fetch"
    category = "network"
    description = (
        "Read the main text of one web page (cleaned, with headings, lists and tables). Use it after "
        "a search when a page looks like the authoritative answer, or when the user gives a URL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http or https address to read.",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Characters of text to return (default {MAX_TEXT}).",
                "minimum": 500,
                "maximum": MAX_TEXT_LIMIT,
                "default": MAX_TEXT,
            },
        },
        "required": ["url"],
    }
    requires_network = True

    def __init__(self, fetcher: Any = None) -> None:
        self._fetcher = fetcher

    def available(self) -> bool:
        from ..network import network_available

        return network_available()

    def run(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")) and "." in url and " " not in url:
            url = "https://" + url  # "example.com/page" is what models write
        if not url:
            return ToolResult(ok=False, error="No address was given.",
                              content="No address was given, so nothing could be read.", source="web")
        if not is_public_url(url):
            message = "Only public http and https addresses can be read (not local or private networks)."
            return ToolResult(ok=False, error=message, content=message, source="web")
        try:
            limit = max(500, min(int(args.get("max_chars") or MAX_TEXT), MAX_TEXT_LIMIT))
        except (TypeError, ValueError):
            limit = MAX_TEXT
        if self._fetcher is None:
            from ..network import network_available

            if not network_available():
                return ToolResult(ok=False, error="No network access right now.",
                                  content="No network access right now, so the page cannot be read.",
                                  source="web", offline=True)
        page = fetch_page(self._fetcher or default_fetcher(), url, FETCH_TIMEOUT, allow_text=True)
        if page.error:
            return ToolResult(ok=False, error=page.error, content=f"The page could not be read: {page.error}",
                              source="web")
        full = page.text
        text = full[:limit]
        truncated = len(full) > limit
        data = {"url": page.url or url, "title": page.title, "text": text, "truncated": truncated,
                "chars": len(full), "published": page.published, "lang": page.lang,
                "fetched_at": page.fetched_at}
        if not text:
            return ToolResult(ok=True, content=f"The page at {url} had no readable text.", data=data, source="web")
        header = [page.title] if page.title else []
        header.append(page.url or url)
        if page.published:
            header.append(f"Published: {page.published[:10]}")
        note = f"\n... [page cut at {limit} of {len(full)} characters]" if truncated else ""
        return ToolResult(ok=True, content="\n".join(header) + "\n" + text + note, data=data, source="web")
