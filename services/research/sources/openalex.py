"""OpenAlex (keyless JSON): the open catalogue of scholarly works.

``https://api.openalex.org/works?search=...`` covers journals, conferences
and preprints across every field (it includes what Crossref has, plus
abstracts). Abstracts come as an inverted index (word -> positions) and are
rebuilt here. The source chip links the DOI when there is one.

Without a key, requests from one IP address share a small free daily budget
(the API says so in ``meta.notice``); the source runs once per plan and a
refusal parks it. Measured 2026-11-09: 200 JSON in 2.7 s.
"""
from __future__ import annotations

from typing import Any

from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource

API = "https://api.openalex.org/works"
MAX_RESULTS = 4
ABSTRACT_CHARS = 2000
FIELDS = "id,doi,display_name,publication_date,abstract_inverted_index,cited_by_count,authorships,primary_location"


def abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, places in index.items():
        for place in places if isinstance(places, list) else []:
            if isinstance(place, int):
                positions.append((place, str(word)))
    return " ".join(word for _, word in sorted(positions))[:ABSTRACT_CHARS]


def parse(payload: Any, query: str) -> list[Hit]:
    hits: list[Hit] = []
    for item in (payload.get("results") or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("display_name") or "").split())
        summary = abstract(item.get("abstract_inverted_index"))
        location = item.get("primary_location") or {}
        url = str(item.get("doi") or (location.get("landing_page_url") if isinstance(location, dict) else "")
                  or item.get("id") or "")
        if not title or not summary or not url.startswith(("http://", "https://")):
            continue
        venue = ""
        if isinstance(location, dict) and isinstance(location.get("source"), dict):
            venue = str(location["source"].get("display_name") or "")
        names = [str(((a or {}).get("author") or {}).get("display_name") or "")
                 for a in (item.get("authorships") or [])[:4] if isinstance(a, dict)]
        published = str(item.get("publication_date") or "")[:10]
        byline = ", ".join(n for n in names if n)
        text = (f"{title} ({byline}{'; ' + venue if venue else ''}; {published}; "
                f"cited by {item.get('cited_by_count', 0)})\n{summary}")
        hits.append(Hit(title=title, url=url, snippet=summary[:400], published=published, source="openalex",
                        query=query, rank=len(hits), text=text, fetchable=False))
        if len(hits) >= MAX_RESULTS:
            break
    return hits


class OpenAlexSource(ApiSource):
    name = "openalex"
    per_plan = True
    topics = frozenset({"science"})
    timeout_cap = 6.0

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        url = with_params(API, {"search": query, "per-page": 8, "select": FIELDS})
        return parse(self.get_json(fetcher, url, timeout), query)
