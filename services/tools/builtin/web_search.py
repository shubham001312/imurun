"""Web search - now the research engine (``services/research``).

``WebSearchTool`` is :class:`services.research.tool.ResearchTool`: the model
(or the planner, for the pre-fetch) writes the queries; keyless sources
(public SearXNG, Wikipedia, news RSS, direct sites and registries, the
embedded-browser helper) are searched in parallel, the best pages are fetched
and cleaned, passages are ranked and returned as a Context block with
numbered sources. No API key is required.

Keyed providers still work and are used first when configured:
``IMURUN_BRAVE_API_KEY``, ``IMURUN_SERPER_API_KEY``, ``IMURUN_TAVILY_API_KEY``,
``IMURUN_SEARXNG_URL`` (optionally ``IMURUN_SEARCH_PROVIDER`` to choose).

This module keeps the old helper names (``current_provider``, ``as_sources``,
``_search_*``) for callers and tests. See docs/research/README.md.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ...research.net import default_fetcher
from ...research.sources.keyed import (
    configured_provider,
    search_brave as _search_brave,
    search_searxng as _search_searxng,
    search_serper as _search_serper,
    search_tavily as _search_tavily,
)
from ...research.sources.wikipedia import WikipediaSource
from ...research.tool import ResearchToolMixin
from ...research.types import Plan
from ..base import Tool

DEFAULT_PROVIDER = "research"
MAX_RESULTS = 6


def current_provider() -> str:
    """The keyed provider in use, else ``research`` (keyless)."""
    return configured_provider() or DEFAULT_PROVIDER


def _search_wikipedia(query: str) -> list[dict[str, str]]:
    """Wikipedia alone, in the old ``[{title, url, snippet, provider}]`` shape."""
    hits = WikipediaSource().search(query, Plan(True, queries=[query]), default_fetcher(), 8.0)
    return [{"title": h.title, "url": h.url, "snippet": h.snippet, "provider": "wikipedia"} for h in hits]


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    return host[4:] if host.startswith("www.") else host


def as_sources(results: list[dict[str, Any]], provider: str) -> list[dict[str, str]]:
    """The shape the UI renders as source chips."""
    out = []
    for item in results:
        url = str(item.get("url") or "")
        if not url:
            continue
        out.append({
            "title": str(item.get("title") or _domain(url) or url)[:120],
            "url": url,
            "domain": _domain(url),
            "provider": str(item.get("provider") or provider),
            "snippet": str(item.get("snippet") or "")[:300],
        })
    return out


class WebSearchTool(ResearchToolMixin, Tool):
    """The model's ``web_search`` (research engine). Also exported as
    ``services.research.ResearchTool``."""


__all__ = ["WebSearchTool", "as_sources", "current_provider", "_search_brave", "_search_serper",
           "_search_tavily", "_search_searxng", "_search_wikipedia"]
