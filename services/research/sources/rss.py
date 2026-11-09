"""News through public RSS / Atom feeds (no key).

* **Google News RSS search** (``https://news.google.com/rss/search?q=``) - a
  query-driven news feed: headline, publisher, publication date. Its item
  links are Google redirect pages that need JavaScript, so these hits are not
  fetched; the headline + publisher + date are the passage, and the
  publisher's own domain is shown on the source chip.
* **Public world-news feeds** (BBC, NPR, The Guardian, Al Jazeera) - fetched
  at most once every 5 minutes and filtered to items that share the query's
  key terms. Their links are ordinary article pages and may be fetched.

Reuters and AP no longer publish public RSS feeds, so they are not listed
(no dead URLs). Feeds are parsed with ``xml.etree``; a document that
declares entities is refused (entity-expansion attacks).

Feed terms: Google News and the publishers offer these feeds for personal,
non-commercial reading. IMURUN runs on the user's own machine for that
user; see docs/research/README.md ("Terms").
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from .. import config
from ..net import with_params
from ..types import Hit, Plan, domain_of
from . import Source, SourceError

GOOGLE_NEWS = "https://news.google.com/rss/search"
FEEDS = (
    ("bbc", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("bbc", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("npr", "https://feeds.npr.org/1001/rss.xml"),
    ("guardian", "https://www.theguardian.com/world/rss"),
    ("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
)
FEED_TTL = 300.0
MAX_ITEMS = 8
_TAGS = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[\w']+", re.UNICODE)
_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was", "who", "what",
         "when", "where", "which", "how", "latest", "news", "current", "today", "now", "new", "about"}


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join(html.unescape(_TAGS.sub(" ", "".join(element.itertext()))).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(element: ET.Element, *names: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def iso_date(value: str) -> str:
    """RFC 822 (RSS) or ISO 8601 (Atom) -> ISO 8601; '' when unreadable."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    match = re.match(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?", value)
    return match.group(0).replace(" ", "T") if match else ""


def parse_feed(body: bytes | str, provider: str) -> list[Hit]:
    """RSS 2.0, RSS 1.0 (RDF) and Atom items as hits. Raises ``SourceError``."""
    raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if "<!ENTITY" in raw[:4096].upper():
        raise SourceError(f"{provider}: feed declares entities; refused")
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceError(f"{provider}: not a feed ({exc})") from exc
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    hits: list[Hit] = []
    for index, item in enumerate(items):
        title = _text(_child(item, "title"))
        link_el = _child(item, "link")
        url = ""
        if link_el is not None:
            url = (link_el.get("href") or link_el.text or "").strip()
        if not url:
            guid = _child(item, "guid", "id")
            candidate = (guid.text or "").strip() if guid is not None else ""
            url = candidate if candidate.startswith("http") else ""
        if not title or not url.startswith(("http://", "https://")):
            continue
        description = _text(_child(item, "description", "summary", "content"))
        published = iso_date(_text(_child(item, "pubdate", "published", "updated", "date")))
        source_el = _child(item, "source")
        site = ""
        publisher = ""
        if source_el is not None:
            publisher = _text(source_el)
            site = domain_of(source_el.get("url") or "")
        hits.append(Hit(title=title, url=url, snippet=description[:500], published=published,
                        source=provider, rank=index, site=site))
        if publisher and publisher not in hits[-1].snippet:
            hits[-1].snippet = (hits[-1].snippet + f" ({publisher})").strip()
    return hits


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if len(w) > 2 and w.lower() not in _STOP}


class NewsRssSource(Source):
    """Google News RSS search per query, plus filtered public feeds."""

    name = "news_rss"

    def __init__(self, feeds: tuple[tuple[str, str], ...] = FEEDS, use_feeds: bool = True) -> None:
        self._feeds = feeds
        self._use_feeds = use_feeds
        self._cache: dict[str, tuple[float, list[Hit]]] = {}
        self._lock = threading.Lock()

    def wanted(self, plan: Plan) -> bool:
        return plan.freshness in ("live", "recent")

    def _google(self, query: str, fetcher: Any, timeout: float) -> list[Hit]:
        hl, gl, ceid = config.news_locale()
        url = with_params(GOOGLE_NEWS, {"q": query, "hl": hl, "gl": gl, "ceid": ceid})
        response = fetcher.get(url, headers={"Accept": "application/rss+xml, application/xml"}, timeout=timeout)
        if not response.ok:
            raise SourceError(f"google news rss: {response.error or response.status}",
                              blocked=response.status in (403, 429, 503))
        hits = parse_feed(response.body, "google_news")[:MAX_ITEMS]
        for hit in hits:
            hit.fetchable = False  # news.google.com/rss/articles/... needs JavaScript to redirect
            # "Headline - Publisher": keep the headline as the title.
            if " - " in hit.title:
                head, _, tail = hit.title.rpartition(" - ")
                if head and len(tail) < 60:
                    hit.title = head
                    if tail not in hit.snippet:
                        hit.snippet = f"{head} ({tail})"
            hit.query = query
        return hits

    def _feed(self, provider: str, url: str, fetcher: Any, timeout: float) -> list[Hit]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(url)
            if cached and now - cached[0] < FEED_TTL:
                return cached[1]
        response = fetcher.get(url, headers={"Accept": "application/rss+xml, application/xml"}, timeout=timeout)
        hits: list[Hit] = []
        if response.ok:
            try:
                hits = parse_feed(response.body, provider)
            except SourceError:
                hits = []
        with self._lock:
            self._cache[url] = (now, hits)
        return hits

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        errors: list[str] = []
        blocked = False
        results: list[Hit] = []
        try:
            results.extend(self._google(query, fetcher, timeout))
        except SourceError as exc:
            errors.append(str(exc))
            blocked = exc.blocked
        if self._use_feeds and plan.freshness == "live":
            entity_terms = {t for e in plan.entities for t in _terms(e)}
            wanted = _terms(query) | entity_terms
            need = 2 if len(wanted) >= 3 else 1
            for provider, url in self._feeds:
                for hit in self._feed(provider, url, fetcher, timeout):
                    words = _terms(hit.title + " " + hit.snippet)
                    # A general feed item must name what the question is about
                    # ("Ethiopian PM ..." is not about India's PM).
                    if entity_terms and not entity_terms & words:
                        continue
                    if len(wanted & words) >= need:
                        results.append(Hit(**{**hit.__dict__, "query": query}))
        if not results and errors:
            raise SourceError("; ".join(errors), blocked=blocked)
        return results
