"""The data that flows through the research pipeline.

plan (planner) -> hits (sources) -> pages (extract) -> passages (rank) ->
ResearchResult (the Context block, the source chips, the event log).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

FRESHNESS = ("live", "recent", "any")


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


@dataclass
class Plan:
    """What the planner decided for one user message."""

    needs_web: bool
    reason: str = ""
    queries: list[str] = field(default_factory=list)
    freshness: str = "any"
    entities: list[str] = field(default_factory=list)
    # model | heuristic | prefilter | given (a query the model passed to the tool)
    method: str = "heuristic"
    elapsed: float = 0.0
    raw: str = ""
    prompt: str = ""  # the user's message (site names in it); never published

    def view(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("raw", None)
        out.pop("prompt", None)
        out["elapsed"] = round(self.elapsed, 3)
        return out


@dataclass
class Hit:
    """One search result, from any source."""

    title: str
    url: str
    snippet: str = ""
    published: str = ""
    source: str = ""          # provider name: wikipedia, searxng, google_news, ...
    query: str = ""
    rank: int = 0             # position in that source's list (0 = first)
    text: str = ""            # already-clean full text (APIs), so no fetch is needed
    fetchable: bool = True    # False for redirect links (Google News) and API hits
    site: str = ""            # display domain when the URL is a redirect

    @property
    def domain(self) -> str:
        return self.site or domain_of(self.url)

    def view(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet[:300],
                "published": self.published, "provider": self.source, "domain": self.domain}


@dataclass
class Page:
    """A fetched page after cleaning (``extract.extract``)."""

    url: str
    title: str = ""
    text: str = ""
    published: str = ""
    lang: str = ""
    description: str = ""
    site_name: str = ""
    canonical: str = ""
    fetched_at: str = ""
    status: int = 0
    error: str = ""


@dataclass
class Passage:
    url: str
    title: str
    domain: str
    text: str
    provider: str = ""
    published: str = ""
    score: float = 0.0
    query: str = ""
    snippet_only: bool = False


@dataclass
class ResearchResult:
    plan: Plan
    context: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    searches: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    offline: bool = False
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.passages)
