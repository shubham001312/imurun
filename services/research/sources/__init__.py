"""Keyless search sources.

Each source turns a query into ``Hit`` objects (``title, url, snippet,
published?, source``). A source never invents a result; when it cannot
answer it raises :class:`SourceError` (``blocked=True`` for a refusal such
as a bot check) and the engine records that honestly.

* ``keyed``          - Brave / Tavily / Serper / your own SearXNG, only when configured
* ``searxng_public`` - rotating public SearXNG instances (``format=json``)
* ``wikipedia``      - Wikipedia's public API (direct title + full text)
* ``rss``            - news through Google News RSS search and public feeds
* ``direct``         - authoritative sites / package registries named by the plan
* ``browser_search`` - the app's embedded browser (helper protocol; off by default)
"""
from __future__ import annotations

from typing import Any

from ..types import Hit, Plan


class SourceError(RuntimeError):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


class Source:
    """Base class. ``per_plan`` sources run once per plan, not per query."""

    name = "source"
    per_plan = False

    def available(self) -> tuple[bool, str]:
        return True, ""

    def wanted(self, plan: Plan) -> bool:
        """Whether this source is worth asking for this plan."""
        return True

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:  # pragma: no cover
        raise NotImplementedError


def default_sources() -> list[Source]:
    from .browser_search import BrowserSearchSource
    from .direct import DirectSource
    from .keyed import KeyedSource
    from .rss import NewsRssSource
    from .searxng_public import SearxngPublicSource
    from .wikipedia import WikipediaSource

    return [KeyedSource(), SearxngPublicSource(), WikipediaSource(), NewsRssSource(),
            DirectSource(), BrowserSearchSource()]


__all__ = ["Source", "SourceError", "default_sources"]
