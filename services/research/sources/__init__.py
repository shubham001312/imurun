"""Keyless search sources.

Each source turns a query into ``Hit`` objects (``title, url, snippet,
published?, source``). A source never invents a result; when it cannot
answer it raises :class:`SourceError` (``blocked=True`` for a refusal such
as a bot check or a rate limit) and the engine records that honestly.

Whole web:
* ``browser_search`` - the app's embedded browser (helper protocol): DuckDuckGo, Bing as fallback
* ``mwmbl``          - Mwmbl, the open non-profit web index (JSON API)
* ``searxng_public`` - rotating public SearXNG instances (``format=json``)

Reference and entities:
* ``wikipedia``      - Wikipedia's public API (direct title + full text)
* ``ddg_instant``    - DuckDuckGo's Instant Answer API (entity abstracts)

By topic (``routing.topics``):
* ``stackexchange``  - code: Stack Overflow / Ask Ubuntu / Super User questions + best answer
* ``github_search``  - code: GitHub issues (error messages) and repositories
* ``hackernews``     - code / tech: Hacker News stories (Algolia API)
* ``arxiv``, ``openalex`` - science: papers with abstracts
* ``endoflife``      - releases: endoflife.date product cycles
* ``rss``            - news: Google News RSS search and public feeds
* ``direct``         - sites / package registries named in the conversation

* ``keyed``          - developer-only: Brave / Tavily / Serper / own SearXNG from environment variables
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..types import Hit, Plan

FAIL_TTL = 600.0       # a refusal (403 / 429) keeps a source out for 10 minutes
SOFT_FAILS = 3         # consecutive errors / timeouts before a short pause
SOFT_TTL = 120.0
MAX_RETRY_AFTER = 3600.0


class SourceError(RuntimeError):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


class Backoff:
    """Failure memory of one source (thread-safe).

    A refusal (HTTP 403 / 429, a quota message) parks the source for
    ``FAIL_TTL`` or the server's ``Retry-After`` (capped at an hour); three
    errors in a row park it for ``SOFT_TTL``. A success clears both. While
    parked, ``available()`` says so and the engine does not ask it.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._until = 0.0
        self._why = ""
        self._errors = 0
        self._lock = threading.Lock()

    def parked(self) -> tuple[bool, str]:
        with self._lock:
            if self._clock() < self._until:
                return True, self._why
            return False, ""

    def refuse(self, why: str, seconds: float | None = None) -> None:
        with self._lock:
            wait = FAIL_TTL if seconds is None else max(1.0, min(MAX_RETRY_AFTER, seconds))
            self._until = self._clock() + wait
            self._why = why
            self._errors = 0

    def error(self, why: str) -> None:
        with self._lock:
            self._errors += 1
            if self._errors >= SOFT_FAILS:
                self._until = self._clock() + SOFT_TTL
                self._why = f"{why} ({self._errors} failures in a row)"
                self._errors = 0

    def success(self) -> None:
        with self._lock:
            self._errors = 0
            self._until = 0.0
            self._why = ""


class Source:
    """Base class. ``per_plan`` sources run once per plan (with the first
    query), not per query. ``topics`` limits a source to plans of those kinds
    (``routing.topics``); ``None`` means every plan. ``timeout_cap`` caps the
    per-request time below the engine's per-source timeout."""

    name = "source"
    per_plan = False
    topics: frozenset[str] | None = None
    timeout_cap: float | None = None

    def available(self) -> tuple[bool, str]:
        return True, ""

    def wanted(self, plan: Plan) -> bool:
        """Whether this source is worth asking for this plan."""
        if self.topics is None:
            return True
        from ..routing import topics

        return bool(self.topics & topics(plan))

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:  # pragma: no cover
        raise NotImplementedError


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = (headers or {}).get("retry-after", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class ApiSource(Source):
    """A keyless JSON/XML API with failure memory (``Backoff``)."""

    label = ""  # name in error messages; defaults to ``name``

    def __init__(self, backoff: Backoff | None = None) -> None:
        self.backoff = backoff or Backoff()

    def available(self) -> tuple[bool, str]:
        parked, why = self.backoff.parked()
        if parked:
            return False, f"paused after a failure: {why}"
        return True, ""

    def get(self, fetcher: Any, url: str, timeout: float, *, headers: dict[str, str] | None = None,
            accept: str = "application/json") -> Any:
        """GET ``url``; returns the response (``ok``). Raises ``SourceError``."""
        label = self.label or self.name
        response = fetcher.get(url, headers={"Accept": accept, **(headers or {})}, timeout=timeout)
        if response.status in (403, 429):
            why = f"HTTP {response.status}"
            self.backoff.refuse(why, _retry_after(response.headers))
            raise SourceError(f"{label}: refused ({why})", blocked=True)
        if not response.ok:
            why = response.error or f"HTTP {response.status}"
            self.backoff.error(why)
            raise SourceError(f"{label}: {why}")
        return response

    def get_json(self, fetcher: Any, url: str, timeout: float, *, headers: dict[str, str] | None = None) -> Any:
        response = self.get(fetcher, url, timeout, headers=headers)
        try:
            data = response.json()
        except ValueError as exc:
            self.backoff.error("not JSON")
            raise SourceError(f"{self.label or self.name}: answered something that is not JSON") from exc
        self.backoff.success()
        return data


def default_sources() -> list[Source]:
    from .arxiv import ArxivSource
    from .browser_search import BrowserSearchSource
    from .ddg_instant import DdgInstantSource
    from .direct import DirectSource
    from .endoflife import EndOfLifeSource
    from .github_search import GitHubSearchSource
    from .hackernews import HackerNewsSource
    from .keyed import KeyedSource
    from .mwmbl import MwmblSource
    from .openalex import OpenAlexSource
    from .rss import NewsRssSource
    from .searxng_public import SearxngPublicSource
    from .stackexchange import StackExchangeSource
    from .wikipedia import WikipediaSource

    return [KeyedSource(), BrowserSearchSource(), MwmblSource(), SearxngPublicSource(), WikipediaSource(),
            DdgInstantSource(), StackExchangeSource(), GitHubSearchSource(), HackerNewsSource(),
            ArxivSource(), OpenAlexSource(), EndOfLifeSource(), NewsRssSource(), DirectSource()]


__all__ = ["ApiSource", "Backoff", "Source", "SourceError", "default_sources"]
