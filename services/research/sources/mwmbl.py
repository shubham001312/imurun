"""Mwmbl - the open, non-profit, community-crawled web index (no key).

``https://api.mwmbl.org/search?s=<query>`` is the JSON API the mwmbl.org
site and its browser extension use. Each result is
``{"url", "title": [{"value", "is_bold"}...], "extract": [...], "source"}``;
the title and extract segments are joined. The index is smaller than a
commercial engine's and not always fresh, so its hits are *pages to read*:
the engine fetches and cleans the best of them like any other result.

Measured 2026-11-09 with the honest User-Agent: 200 JSON in 0.4-1.5 s for
"latest python release" (python.org first), "ModuleNotFoundError No module
named pip" (discuss.python.org), "rust lifetime elision rules"
(users.rust-lang.org, softwareengineering.stackexchange.com).
"""
from __future__ import annotations

from typing import Any

from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource, SourceError

API = "https://api.mwmbl.org/search"
MAX_RESULTS = 8


def _joined(segments: Any) -> str:
    if isinstance(segments, str):
        return " ".join(segments.split())
    if not isinstance(segments, list):
        return ""
    return " ".join("".join(str((s or {}).get("value") or "") for s in segments if isinstance(s, dict)).split())


def parse(payload: Any, query: str) -> list[Hit]:
    hits: list[Hit] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = _joined(item.get("title")).rstrip("… ").strip() or url
        if not url.startswith(("http://", "https://")):
            continue
        hits.append(Hit(title=title, url=url, snippet=_joined(item.get("extract"))[:500], source="mwmbl",
                        query=query, rank=len(hits)))
        if len(hits) >= MAX_RESULTS:
            break
    return hits


class MwmblSource(ApiSource):
    name = "mwmbl"
    timeout_cap = 5.0

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        payload = self.get_json(fetcher, with_params(API, {"s": query}), timeout)
        if not isinstance(payload, list):
            raise SourceError("mwmbl: unexpected answer")
        return parse(payload, query)
