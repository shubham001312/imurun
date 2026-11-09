"""Wikipedia through its public API (no key).

Two lookups per query, as in the old ``_search_wikipedia``:

1. *direct title*: "current prime minister of India" -> the article
   "Prime Minister of India", whose summary names the incumbent. Full-text
   search alone ranks the PM's Office first;
2. *full-text search* (``list=search``) for the rest, with REST summaries.

The best article also gets its plain-text body (``prop=extracts``) so the
ranker can pick a passage deeper than the intro. Everything comes back
already clean, so these hits are never fetched as HTML.

Wikimedia asks automated clients to identify themselves: the explicit
User-Agent is required (a generic one gets HTTP 403).
"""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

from ..net import with_params
from ..types import Hit, Plan
from . import Source, SourceError

WIKI_UA = "IMURUN/0.3 (local-first assistant; https://github.com/imutechz/imurun)"
API = "https://en.wikipedia.org/w/api.php"
REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"
BODY_CHARS = 20_000
_TAGS = re.compile(r"<[^>]+>")
_LEAD = re.compile(
    r"^(?:who|what|which)\s+(?:is|are|was|were)\s+(?:the\s+)?|"
    r"^(?:the\s+)?(?:current|present|incumbent|new|latest|sitting|today'?s)\s+|"
    r"\s+(?:now|today|currently|right now|at the moment|as of (?:today|now)|in 20\d\d)\s*\??$|\?$",
    re.IGNORECASE,
)


def topic_of(query: str) -> str:
    """The article title a question most likely names."""
    topic = (query or "").strip()
    for _ in range(3):
        stripped = _LEAD.sub("", topic).strip()
        if stripped == topic:
            break
        topic = stripped
    topic = re.sub(r"\s+20\d\d$", "", topic).strip()
    return topic[:1].upper() + topic[1:] if topic else ""


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


class WikipediaSource(Source):
    name = "wikipedia"

    def _get(self, fetcher: Any, url: str, timeout: float) -> Any:
        response = fetcher.get(url, headers={"User-Agent": WIKI_UA, "Accept": "application/json"},
                               timeout=timeout)
        if not response.ok:
            raise SourceError(f"wikipedia: {response.error or response.status}",
                              blocked=response.status in (403, 429))
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("wikipedia: not JSON") from exc

    def _summary(self, fetcher: Any, title: str, timeout: float) -> Hit | None:
        try:
            data = self._get(fetcher, REST + quote(title.replace(" ", "_"), safe=""), timeout)
        except SourceError:
            return None
        if not isinstance(data, dict) or str(data.get("type") or "") != "standard":
            return None
        extract = _clean(data.get("extract"))
        if not extract:
            return None
        page = str(((data.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
        name = _clean(data.get("title")) or title
        return Hit(
            title=name,
            url=page or "https://en.wikipedia.org/wiki/" + quote(name.replace(" ", "_")),
            snippet=extract[:700],
            # No `published`: the REST `timestamp` is the last edit, not when the
            # facts were true; using it made encyclopaedic text outrank today's news.
            source=self.name,
            text=extract,
            fetchable=False,
        )

    def _body(self, fetcher: Any, title: str, timeout: float) -> str:
        url = with_params(API, {"action": "query", "prop": "extracts", "explaintext": 1,
                                "redirects": 1, "titles": title, "format": "json"})
        try:
            data = self._get(fetcher, url, timeout)
        except SourceError:
            return ""
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        for page in pages.values():
            text = str(page.get("extract") or "")
            if text:
                # Drop the reference-only tail; keep section headings as Markdown.
                text = re.split(r"\n==\s*(?:See also|References|External links|Notes|Further reading)\s*==",
                                text)[0]
                text = re.sub(r"\n(=+)\s*(.*?)\s*\1\n", lambda m: "\n" + "#" * min(6, len(m.group(1))) +
                              " " + m.group(2) + "\n", text)
                return text[:BODY_CHARS]
        return ""

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        results: list[Hit] = []
        seen: set[str] = set()
        topic = topic_of(query)
        if topic and len(topic.split()) <= 8:
            direct = self._summary(fetcher, topic, timeout)
            if direct:
                results.append(direct)
                seen.add(direct.url)
        url = with_params(API, {"action": "query", "list": "search", "srsearch": query,
                                "format": "json", "srlimit": 3})
        try:
            payload = self._get(fetcher, url, timeout)
        except SourceError:
            if not results:
                raise
            payload = {}
        for hit in ((payload or {}).get("query") or {}).get("search") or []:
            title = _clean(hit.get("title"))
            if not title:
                continue
            item = self._summary(fetcher, title, timeout) or Hit(
                title=title,
                url="https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                snippet=_clean(html.unescape(_TAGS.sub(" ", str(hit.get("snippet") or "")))),
                source=self.name, fetchable=False,
            )
            if item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
            if len(results) >= 3:
                break
        if results:
            body = self._body(fetcher, results[0].title, timeout)
            if len(body) > len(results[0].text):
                results[0].text = body
        for index, item in enumerate(results):
            item.rank = index
            item.query = query
        return results
