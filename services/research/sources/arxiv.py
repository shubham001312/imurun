"""arXiv's public API (Atom, keyless) for science and research questions.

``https://export.arxiv.org/api/query?search_query=...`` - every content
word of the query must appear (``all:w1 AND all:w2 ...``), sorted by
relevance. Each entry gives the title, authors, abstract and dates, which
become the passage; the source chip links the abstract page. arXiv asks for
no more than one request every three seconds: the source runs once per plan
and the failure memory pauses it after a refusal. Measured 2026-11-09: 200
Atom in 1.2 s.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..net import with_params
from ..rank import tokens
from ..types import Hit, Plan
from . import ApiSource, SourceError

API = "https://export.arxiv.org/api/query"
MAX_RESULTS = 4
MAX_TERMS = 6
ATOM = "{http://www.w3.org/2005/Atom}"


def search_query(query: str) -> str:
    words = [w for w in tokens(query) if len(w) > 2 and not w.isdigit()][:MAX_TERMS]
    return " AND ".join(f"all:{w}" for w in words)


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def parse(body: bytes, query: str) -> list[Hit]:
    raw = body.decode("utf-8", "replace")
    if "<!ENTITY" in raw[:4096].upper():
        raise SourceError("arxiv: feed declares entities; refused")
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceError(f"arxiv: not Atom ({exc})") from exc
    hits: list[Hit] = []
    for entry in root.iter(ATOM + "entry"):
        url = _text(entry.find(ATOM + "id"))
        title = _text(entry.find(ATOM + "title"))
        summary = _text(entry.find(ATOM + "summary"))
        if not url.startswith(("http://", "https://")) or not title or not summary:
            continue
        url = re.sub(r"^http://", "https://", url)
        authors = [_text(a.find(ATOM + "name")) for a in entry.findall(ATOM + "author")]
        published = _text(entry.find(ATOM + "published"))[:10]
        byline = ", ".join(a for a in authors[:4] if a) + (" et al." if len(authors) > 4 else "")
        text = f"{title} ({byline}; arXiv, {published})\n{summary}"
        hits.append(Hit(title=title, url=url, snippet=summary[:400], published=published, source="arxiv",
                        query=query, rank=len(hits), text=text, fetchable=False))
        if len(hits) >= MAX_RESULTS:
            break
    return hits


class ArxivSource(ApiSource):
    name = "arxiv"
    per_plan = True
    topics = frozenset({"science"})
    timeout_cap = 6.0

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        expression = search_query(query)
        if not expression:
            return []
        url = with_params(API, {"search_query": expression, "max_results": MAX_RESULTS, "sortBy": "relevance"})
        response = self.get(fetcher, url, timeout, accept="application/atom+xml")
        hits = parse(response.body, query)
        self.backoff.success()
        return hits
