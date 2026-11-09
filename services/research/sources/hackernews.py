"""Hacker News through its Algolia search API (keyless).

``https://hn.algolia.com/api/v1/search?query=...&tags=story`` returns the
stories that match, with points, comment count and date. A story's URL is
the article it links to, so these hits are fetched and read like search
results (an "Ask HN" without a URL links the discussion). Good for
technology news, launches and write-ups. Measured 2026-11-09: 200 in 1.3 s.
"""
from __future__ import annotations

from typing import Any

from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource

API = "https://hn.algolia.com/api/v1/search"
MAX_RESULTS = 5
MIN_POINTS = 5


class HackerNewsSource(ApiSource):
    name = "hackernews"
    per_plan = True
    topics = frozenset({"code"})
    timeout_cap = 5.0

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        params = {"query": query, "tags": "story", "hitsPerPage": 10}
        payload = self.get_json(fetcher, with_params(API, params), timeout)
        hits: list[Hit] = []
        for item in (payload or {}).get("hits") or [] if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or int(item.get("points") or 0) < MIN_POINTS:
                continue
            title = " ".join(str(item.get("title") or "").split())
            discussion = f"https://news.ycombinator.com/item?id={item.get('objectID')}"
            url = str(item.get("url") or "") or discussion
            if not title or not url.startswith(("http://", "https://")):
                continue
            snippet = (f"{title} ({item.get('points', 0)} points, {item.get('num_comments', 0)} comments "
                       f"on Hacker News)")
            hits.append(Hit(title=title, url=url, snippet=snippet, published=str(item.get("created_at") or "")[:25],
                            source="hackernews", query=query, rank=len(hits)))
            if len(hits) >= MAX_RESULTS:
                break
        return hits
