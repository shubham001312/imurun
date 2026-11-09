"""Public SearXNG instances (meta-search, JSON API, no key).

SearXNG is open-source meta-search; many volunteers run public instances.
Only instances that allow ``format=json`` are useful, and public instances
come and go, rate-limit (HTTP 429) or put a bot challenge in front (Anubis
"Making sure you're not a bot!"). So:

* a curated list (``INSTANCES``; override with ``IMURUN_SEARXNG_INSTANCES``,
  comma separated) is tried in rotation;
* each search asks two instances in parallel, ``search_timeout()`` (6 s) each;
* an instance that fails (error, 429, non-JSON / challenge page) is skipped
  for ``FAIL_TTL`` (10 minutes); a healthy one is remembered.

A challenge page is a refusal: it is reported, never worked around.
Measured from the development machine: 2026-11-01, 0 of 40 public
instances answered JSON to an honest client (all 429 or challenge);
2026-11-09, 1 of 77 searx.space instances (51 answered 429, the rest a
challenge page, HTML or 403). So this source is best-effort.
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from typing import Any

from ..net import with_params
from ..types import Hit, Plan
from . import Source, SourceError

INSTANCES = (
    # 2026-11-09: the only one of 77 searx.space instances that served JSON to an honest client.
    "https://search.lumy.live",
    "https://searx.be",
    "https://search.sapti.me",
    "https://searx.tiekoetter.com",
    "https://priv.au",
    "https://paulgo.io",
    "https://search.inetol.net",
    "https://opnxng.com",
    "https://etsi.me",
    "https://baresearch.org",
    "https://search.hbubli.cc",
    "https://search.disroot.org",
    "https://searx.perennialte.ch",
)
FAIL_TTL = 600.0
PARALLEL = 2
MAX_RESULTS = 8


class InstancePool:
    """Rotation plus a failure memory, shared by every search. Thread-safe."""

    def __init__(self, instances: list[str] | tuple[str, ...], *, fail_ttl: float = FAIL_TTL,
                 clock=time.monotonic) -> None:
        self.instances = [i.rstrip("/") for i in instances if i.strip()]
        self._fail_ttl = fail_ttl
        self._clock = clock
        self._failed: dict[str, tuple[float, str]] = {}
        self._good: set[str] = set()
        self._next = 0
        self._lock = threading.Lock()

    def healthy(self) -> list[str]:
        now = self._clock()
        with self._lock:
            return [i for i in self.instances
                    if i not in self._failed or now - self._failed[i][0] >= self._fail_ttl]

    def pick(self, count: int = PARALLEL, exclude: set[str] | None = None) -> list[str]:
        """The next ``count`` healthy instances; known-good ones first."""
        exclude = exclude or set()
        now = self._clock()
        with self._lock:
            n = len(self.instances)
            order = [self.instances[(self._next + k) % n] for k in range(n)] if n else []
            ready = [i for i in order if i not in exclude and
                     (i not in self._failed or now - self._failed[i][0] >= self._fail_ttl)]
            ready.sort(key=lambda i: 0 if i in self._good else 1)
            chosen = ready[:count]
            if n:
                self._next = (self._next + max(1, len(chosen))) % n
            return chosen

    def fail(self, instance: str, reason: str) -> None:
        with self._lock:
            self._failed[instance] = (self._clock(), reason)
            self._good.discard(instance)

    def succeed(self, instance: str) -> None:
        with self._lock:
            self._failed.pop(instance, None)
            self._good.add(instance)

    def failures(self) -> dict[str, str]:
        now = self._clock()
        with self._lock:
            return {i: r for i, (t, r) in self._failed.items() if now - t < self._fail_ttl}


def _configured_instances() -> list[str]:
    raw = os.getenv("IMURUN_SEARXNG_INSTANCES", "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(INSTANCES)


_pool: InstancePool | None = None
_pool_lock = threading.Lock()


def shared_pool() -> InstancePool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = InstancePool(_configured_instances())
        return _pool


def parse_results(payload: Any, query: str, instance: str) -> list[Hit]:
    hits: list[Hit] = []
    for index, item in enumerate((payload or {}).get("results") or []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = " ".join(str(item.get("title") or "").split())
        if not url.startswith(("http://", "https://")) or not title:
            continue
        hits.append(Hit(
            title=title, url=url,
            snippet=" ".join(str(item.get("content") or "").split())[:500],
            published=str(item.get("publishedDate") or "")[:25],
            source="searxng", query=query, rank=index,
        ))
        if len(hits) >= MAX_RESULTS:
            break
    return hits


class SearxngPublicSource(Source):
    name = "searxng_public"
    # Usually refused (see above): never let it hold the search phase open long.
    timeout_cap = 4.0

    def __init__(self, pool: InstancePool | None = None, parallel: int = PARALLEL) -> None:
        self._pool = pool
        self._parallel = max(1, parallel)

    @property
    def pool(self) -> InstancePool:
        return self._pool or shared_pool()

    def available(self) -> tuple[bool, str]:
        from .. import config

        if not config.searxng_public_enabled():
            return False, "public SearXNG is switched off (IMURUN_RESEARCH_SEARXNG_PUBLIC=0)"
        if not self.pool.healthy():
            return False, "every public SearXNG instance failed in the last 10 minutes"
        return True, ""

    def _one(self, instance: str, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        params = {"q": query, "format": "json", "language": "en", "safesearch": 1}
        if plan.freshness == "live":
            params["time_range"] = "month"
        response = fetcher.get(with_params(instance + "/search", params),
                               headers={"Accept": "application/json"}, timeout=timeout)
        if not response.ok:
            raise SourceError(f"{instance}: {response.error or response.status}",
                              blocked=response.status in (403, 429))
        if "json" not in response.content_type:
            raise SourceError(f"{instance}: answered a {response.content_type or 'non-JSON'} page "
                              "(JSON disabled or a bot challenge)", blocked=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"{instance}: invalid JSON") from exc
        return parse_results(payload, query, instance)

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        pool = self.pool
        tried: set[str] = set()
        errors: list[str] = []
        blocked = True
        answered = False
        started = time.monotonic()
        for _round in range(2):
            remaining = timeout - (time.monotonic() - started)
            if remaining < 1.0:
                break
            chosen = pool.pick(self._parallel, exclude=tried)
            if not chosen:
                break
            tried.update(chosen)
            workers = concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen))
            merged: list[Hit] = []
            try:
                futures = {workers.submit(self._one, inst, query, plan, fetcher, remaining): inst
                           for inst in chosen}
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=remaining + 0.5):
                        instance = futures[future]
                        try:
                            hits = future.result()
                        except SourceError as exc:
                            pool.fail(instance, str(exc))
                            errors.append(str(exc))
                            blocked = blocked and exc.blocked
                            continue
                        except Exception as exc:  # noqa: BLE001
                            pool.fail(instance, f"{type(exc).__name__}")
                            errors.append(f"{instance}: {type(exc).__name__}")
                            blocked = False
                            continue
                        pool.succeed(instance)
                        answered = True
                        merged.extend(hits)
                except concurrent.futures.TimeoutError:
                    for future, instance in futures.items():
                        if not future.done():
                            pool.fail(instance, "timed out")
                            errors.append(f"{instance}: timed out")
                            blocked = False
            finally:
                workers.shutdown(wait=False, cancel_futures=True)
            if merged:
                seen: set[str] = set()
                unique = []
                for hit in sorted(merged, key=lambda h: h.rank):
                    if hit.url not in seen:
                        seen.add(hit.url)
                        unique.append(hit)
                return unique[:MAX_RESULTS]
        if answered:
            return []  # instances answered, the web had nothing: not an error
        if not tried:
            raise SourceError("no healthy public SearXNG instance")
        message = "public SearXNG: " + ("; ".join(errors[:4]) if errors else "no results")
        raise SourceError(message, blocked=blocked and bool(errors))
