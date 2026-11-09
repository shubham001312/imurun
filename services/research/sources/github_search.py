"""GitHub search API (unauthenticated) for code questions.

* an **error message** ("ModuleNotFoundError: No module named pip",
  "E0502") -> ``/search/issues``: issues whose title or body carry the
  error, with the issue text (often the reproduction and the fix);
* a request for a **project** ("a library for X", "alternatives to Y") ->
  ``/search/repositories``: description, stars and last push;
* anything else -> not asked.

Unauthenticated search allows 10 requests a minute per IP address; a 403 /
429 parks the source until GitHub's reset time (``Retry-After`` or
``X-RateLimit-Reset``). Measured 2026-11-09: 200 JSON in 0.9 s.
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..extract import clean_lines
from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource, SourceError

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
BODY_CHARS = 1800
MAX_RESULTS = 3
ASKS_FOR_PROJECT = re.compile(r"\b(?:library|libraries|package for|crate for|module for|tool for|tools for|"
                              r"framework for|alternative(?:s)? to|open[- ]source|repo(?:sitory)?|github project|"
                              r"cli for|sdk for|plugin for|extension for)\b", re.IGNORECASE)
ERRORISH = re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception)\b|\berror\b|\bE\d{4}\b|\berrno\b|\bfailed\b|"
                      r"\bpanicked\b|\bsegfault\b|\btraceback\b", re.IGNORECASE)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


class GitHubSearchSource(ApiSource):
    name = "github_search"
    per_plan = True
    topics = frozenset({"code"})
    timeout_cap = 5.0

    @staticmethod
    def mode(plan: Plan) -> str:
        """``issues`` for an error message, ``repositories`` when a project is
        asked for, else ``""`` (repository search for "latest Python release"
        returns repositories *named* like that, which is noise)."""
        text = " ".join([plan.prompt or "", *plan.queries])
        if ERRORISH.search(text):
            return "issues"
        if ASKS_FOR_PROJECT.search(text):
            return "repositories"
        return ""

    def wanted(self, plan: Plan) -> bool:
        return super().wanted(plan) and bool(self.mode(plan))

    def _json(self, fetcher: Any, url: str, timeout: float) -> dict:
        response = fetcher.get(url, headers=HEADERS, timeout=timeout)
        if response.status in (403, 429):
            reset = response.headers.get("x-ratelimit-reset", "")
            wait = None
            try:
                wait = max(1.0, float(reset) - time.time()) if reset else None
            except ValueError:
                wait = None
            retry = response.headers.get("retry-after", "")
            if retry.strip().isdigit():
                wait = float(retry)
            self.backoff.refuse(f"HTTP {response.status} (rate limit)", wait)
            raise SourceError(f"github: refused (HTTP {response.status}, rate limit)", blocked=True)
        if not response.ok:
            self.backoff.error(response.error or f"HTTP {response.status}")
            raise SourceError(f"github: {response.error or response.status}")
        try:
            data = response.json()
        except ValueError as exc:
            self.backoff.error("not JSON")
            raise SourceError("github: not JSON") from exc
        self.backoff.success()
        return data if isinstance(data, dict) else {}

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        hits: list[Hit] = []
        mode = self.mode(plan)
        if not mode:
            return hits
        if mode == "issues":
            data = self._json(fetcher, with_params(f"{API}/search/issues",
                                                   {"q": f"{query} is:issue", "per_page": MAX_RESULTS}), timeout)
            for item in data.get("items") or []:
                if not isinstance(item, dict) or not str(item.get("html_url") or "").startswith("https://"):
                    continue
                repo = "/".join(str(item.get("repository_url") or "").split("/")[-2:])
                title = _clean(item.get("title"))
                body = clean_lines(str(item.get("body") or ""))[:BODY_CHARS]
                text = (f"GitHub issue in {repo}: {title} ({item.get('state', '')}, "
                        f"{item.get('comments', 0)} comments)\n{body}").strip()
                hits.append(Hit(title=f"{title} · {repo}", url=item["html_url"], snippet=body[:300],
                                published=str(item.get("updated_at") or "")[:10], source="github",
                                query=query, rank=len(hits), text=text, fetchable=False))
            return hits
        data = self._json(fetcher, with_params(f"{API}/search/repositories",
                                               {"q": query, "per_page": MAX_RESULTS}), timeout)
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not str(item.get("html_url") or "").startswith("https://"):
                continue
            name = _clean(item.get("full_name"))
            description = _clean(item.get("description"))
            text = (f"{name} on GitHub: {description} ({item.get('stargazers_count', 0)} stars, "
                    f"language {item.get('language') or 'n/a'}, last push {str(item.get('pushed_at') or '')[:10]})")
            hits.append(Hit(title=f"{name} - GitHub", url=item["html_url"], snippet=description[:300],
                            source="github", query=query, rank=len(hits), text=text, fetchable=False))
        return hits
