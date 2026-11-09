"""DuckDuckGo Instant Answer API (``api.duckduckgo.com``, keyless JSON).

This is DuckDuckGo's documented public API, not its result pages: it
answers *names* ("Jensen Huang", "Nvidia", "Python programming language")
with an abstract (usually Wikipedia's), its source URL and sometimes an
infobox. It answers whole questions ("who is the CEO of Nvidia") with an
empty object, so this source asks for the plan's entities, else the
question's topic (``wikipedia.topic_of``). ``t=imurun`` identifies the app
as DuckDuckGo asks API users to.

Measured 2026-11-09: 200 in 0.1-0.4 s; "Jensen Huang" -> "... co-founder,
president, and CEO of Nvidia"; "who is the ceo of nvidia" -> empty.
"""
from __future__ import annotations

from typing import Any

from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource
from .wikipedia import topic_of

API = "https://api.duckduckgo.com/"
MAX_LOOKUPS = 2
INFOBOX_ROWS = 12


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def lookups(query: str, plan: Plan) -> list[str]:
    """What to ask: the entities first (names are what the API knows), then the topic."""
    out: list[str] = []
    for value in [*plan.entities, topic_of(query)]:
        value = _clean(value)
        if value and len(value.split()) <= 6 and value.lower() not in {o.lower() for o in out}:
            out.append(value)
    return out[:MAX_LOOKUPS]


def parse(payload: Any, query: str) -> Hit | None:
    if not isinstance(payload, dict):
        return None
    abstract = _clean(payload.get("AbstractText") or payload.get("Abstract"))
    url = str(payload.get("AbstractURL") or "")
    if not abstract or not url.startswith(("http://", "https://")):
        return None
    lines = [abstract]
    infobox = payload.get("Infobox")
    if isinstance(infobox, dict):
        for row in (infobox.get("content") or [])[:INFOBOX_ROWS]:
            if not isinstance(row, dict):
                continue
            label, value = _clean(row.get("label")), row.get("value")
            if label and isinstance(value, (str, int, float)) and _clean(value):
                lines.append(f"{label}: {_clean(value)}")
    heading = _clean(payload.get("Heading")) or query
    source = _clean(payload.get("AbstractSource"))
    text = "\n".join(lines)
    return Hit(title=heading + (f" ({source})" if source else ""), url=url, snippet=abstract[:500],
               source="ddg_instant", query=query, text=text, fetchable=False)


class DdgInstantSource(ApiSource):
    name = "ddg_instant"
    per_plan = True  # the lookups are the plan's entities, the same for every query
    timeout_cap = 4.0

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        hits: list[Hit] = []
        for name in lookups(query, plan):
            url = with_params(API, {"q": name, "format": "json", "no_html": 1, "skip_disambig": 1,
                                    "no_redirect": 1, "t": "imurun"})
            hit = parse(self.get_json(fetcher, url, timeout), query)
            if hit is not None and hit.url not in {h.url for h in hits}:
                hit.rank = len(hits)
                hits.append(hit)
        return hits
