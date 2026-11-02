"""Keyed search providers - used when the user configured one, never required.

Brave, Serper and Tavily need an API key; SearXNG needs the address of an
instance you run or trust (``IMURUN_SEARXNG_URL``). The keys come from the
environment (the Settings screen exports them, ``services/server/settings.py``)
and are never logged or written anywhere by this module.

These functions moved here from ``tools/builtin/web_search.py`` (which
re-exports them) and keep their old return shape ``[{title, url, snippet}]``.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ...tools.http import HttpError, build_url, request_json
from ...tools.network import OfflineError
from ..types import Hit, Plan
from . import Source, SourceError

MAX_RESULTS = 6
SEARCH_TIMEOUT = float(os.getenv("IMURUN_SEARCH_TIMEOUT", "8"))
KEYED = ("brave", "serper", "tavily", "searxng")


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def configured_provider() -> str:
    """The keyed provider to use, or '' when none is configured."""
    chosen = _env("IMURUN_SEARCH_PROVIDER").lower()
    keys = {"brave": "IMURUN_BRAVE_API_KEY", "serper": "IMURUN_SERPER_API_KEY",
            "tavily": "IMURUN_TAVILY_API_KEY", "searxng": "IMURUN_SEARXNG_URL"}
    if chosen in keys and _env(keys[chosen]):
        return chosen
    for name in KEYED:
        if _env(keys[name]):
            return name
    return ""


def search_brave(query: str, timeout: float = SEARCH_TIMEOUT) -> list[dict[str, str]]:
    key = _env("IMURUN_BRAVE_API_KEY")
    if not key:
        raise HttpError("Brave search needs IMURUN_BRAVE_API_KEY.")
    url = build_url("https://api.search.brave.com/res/v1/web/search", {"q": query, "count": MAX_RESULTS})
    payload = request_json(url, headers={"X-Subscription-Token": key}, timeout=timeout)
    return [{"title": _clean(i.get("title")), "url": str(i.get("url") or ""),
             "snippet": _clean(i.get("description")), "published": str(i.get("page_age") or "")}
            for i in ((payload.get("web") or {}).get("results") or [])[:MAX_RESULTS]]


def search_serper(query: str, timeout: float = SEARCH_TIMEOUT) -> list[dict[str, str]]:
    key = _env("IMURUN_SERPER_API_KEY")
    if not key:
        raise HttpError("Serper search needs IMURUN_SERPER_API_KEY.")
    body = json.dumps({"q": query, "num": MAX_RESULTS}).encode("utf-8")
    payload = request_json("https://google.serper.dev/search", method="POST", data=body,
                           headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=timeout)
    return [{"title": _clean(i.get("title")), "url": str(i.get("link") or ""),
             "snippet": _clean(i.get("snippet")), "published": str(i.get("date") or "")}
            for i in (payload.get("organic") or [])[:MAX_RESULTS]]


def search_tavily(query: str, timeout: float = SEARCH_TIMEOUT) -> list[dict[str, str]]:
    key = _env("IMURUN_TAVILY_API_KEY")
    if not key:
        raise HttpError("Tavily search needs IMURUN_TAVILY_API_KEY.")
    body = json.dumps({"api_key": key, "query": query, "max_results": MAX_RESULTS}).encode("utf-8")
    payload = request_json("https://api.tavily.com/search", method="POST", data=body,
                           headers={"Content-Type": "application/json"}, timeout=timeout)
    return [{"title": _clean(i.get("title")), "url": str(i.get("url") or ""),
             "snippet": _clean(i.get("content")), "published": str(i.get("published_date") or "")}
            for i in (payload.get("results") or [])[:MAX_RESULTS]]


def search_searxng(query: str, timeout: float = SEARCH_TIMEOUT) -> list[dict[str, str]]:
    base = _env("IMURUN_SEARXNG_URL").rstrip("/")
    if not base:
        raise HttpError("SearXNG search needs IMURUN_SEARXNG_URL.")
    payload = request_json(build_url(f"{base}/search", {"q": query, "format": "json"}), timeout=timeout)
    return [{"title": _clean(i.get("title")), "url": str(i.get("url") or ""),
             "snippet": _clean(i.get("content")), "published": str(i.get("publishedDate") or "")}
            for i in (payload.get("results") or [])[:MAX_RESULTS]]


FUNCTIONS = {"brave": search_brave, "serper": search_serper, "tavily": search_tavily, "searxng": search_searxng}


class KeyedSource(Source):
    name = "keyed"

    def available(self) -> tuple[bool, str]:
        if configured_provider():
            return True, ""
        return False, "no search key configured (optional)"

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        provider = configured_provider()
        if not provider:
            return []
        try:
            rows = FUNCTIONS[provider](query, timeout)
        except OfflineError as exc:
            raise SourceError(f"{provider}: offline") from exc
        except HttpError as exc:
            raise SourceError(f"{provider}: {exc}", blocked=getattr(exc, "status", None) in (401, 403, 429)) from exc
        hits = []
        for index, row in enumerate(rows):
            if not str(row.get("url") or "").startswith(("http://", "https://")):
                continue
            hits.append(Hit(title=row.get("title") or row["url"], url=row["url"], snippet=row.get("snippet", ""),
                            published=row.get("published", ""), source=provider, query=query, rank=index))
        return hits
