"""The research pipeline: plan -> search -> fetch + clean -> rank -> Context.

``ResearchEngine.run(plan)`` never raises and always returns within the
deadline (``IMURUN_RESEARCH_DEADLINE``, default 12 s) plus a small margin:

* search phase: every (query x source) pair in parallel, each source with
  its own timeout (6 s), the phase capped at half the deadline. A source
  still running when the phase ends is reported as timed out and ignored;
* fetch phase: the best fetchable results (one per domain, ``max_pages()``
  of them) in parallel, 8 s each, capped by what is left of the deadline;
* rank phase: local CPU only.

Events (``events.emit``): ``research.search`` per (source, query),
``research.fetch`` per page, ``research.done`` at the end. ``research.plan``
is emitted by the planner.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config
from .events import emit
from .extract import extract
from .net import Fetcher, default_fetcher, is_public_url
from .rank import build_context, dedupe_hits, fetch_order
from .sources import Source, SourceError, default_sources
from .types import Hit, Page, Plan, ResearchResult

HTML_TYPES = ("text/html", "application/xhtml+xml")
_pool: concurrent.futures.ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _shared_pool() -> concurrent.futures.ThreadPoolExecutor:
    # Shared and generously sized: a source that overruns its phase keeps
    # its worker until its own timeout, and must not starve the next turn.
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=24, thread_name_prefix="imurun-research")
        return _pool


def fetch_page(fetcher: Any, url: str, timeout: float, *, allow_text: bool = False) -> Page:
    """Download and clean one page. Failures come back as ``Page.error``."""
    if not is_public_url(url):
        return Page(url=url, error="refused: not a public http(s) address")
    response = fetcher.get(url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5"},
                           timeout=timeout, max_bytes=config.max_page_bytes())
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not response.ok:
        return Page(url=url, status=response.status, error=response.error or f"HTTP {response.status}",
                    fetched_at=fetched_at)
    kind = response.content_type
    if kind in HTML_TYPES or (not kind and b"<html" in response.body[:2048].lower()):
        page = extract(response.text(), response.url or url)
    elif allow_text and (kind.startswith("text/") or kind in ("application/json", "application/xml")):
        page = Page(url=response.url or url, text=response.text().strip())
    else:
        return Page(url=url, status=response.status, error=f"not an HTML page ({kind or 'unknown type'})",
                    fetched_at=fetched_at)
    page.status = response.status
    page.fetched_at = fetched_at
    if response.truncated:
        page.error = ""  # a capped body is still parsed; html.parser is forgiving
    return page


class ResearchEngine:
    def __init__(
        self,
        *,
        fetcher: Any = None,
        sources: list[Source] | None = None,
        online: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._sources = sources
        self._online = online
        self._deadline = deadline

    @property
    def fetcher(self) -> Any:
        return self._fetcher or default_fetcher()

    @property
    def sources(self) -> list[Source]:
        if self._sources is None:
            self._sources = default_sources()
        return self._sources

    def source_status(self) -> list[dict[str, Any]]:
        out = []
        for source in self.sources:
            try:
                ok, why = source.available()
            except Exception as exc:  # noqa: BLE001
                ok, why = False, f"{type(exc).__name__}"
            out.append({"source": source.name, "available": ok, "reason": why})
        return out

    def _is_online(self) -> bool:
        if self._online is not None:
            try:
                return bool(self._online())
            except Exception:  # noqa: BLE001
                return True
        from ..tools.network import network_available

        return network_available()

    def run(
        self,
        plan: Plan,
        *,
        publish: Callable | None = None,
        turn_id: str = "",
        stop: Callable[[], bool] | None = None,
        budget_tokens: int | None = None,
        max_pages: int | None = None,
    ) -> ResearchResult:
        started = time.monotonic()
        total = self._deadline if self._deadline is not None else config.deadline()
        end = started + total
        result = ResearchResult(plan=plan)
        try:
            self._run(plan, result, started, end, publish, turn_id, stop,
                      budget_tokens or config.budget_tokens(),
                      config.max_pages() if max_pages is None else max_pages)
        except Exception as exc:  # noqa: BLE001 - research never breaks a turn
            result.errors.append(f"research failed: {type(exc).__name__}: {exc}")
        result.elapsed = time.monotonic() - started
        emit("research.done", {
            "turn_id": turn_id, "ok": result.ok, "queries": plan.queries,
            "sources": result.sources, "passages": len(result.passages),
            "hits": len(result.hits), "errors": result.errors[:6], "offline": result.offline,
            "blocked": result.blocked, "elapsed": round(result.elapsed, 3),
        }, publish)
        return result

    def _stopped(self, stop: Callable[[], bool] | None) -> bool:
        if stop is None:
            return False
        try:
            return bool(stop())
        except Exception:  # noqa: BLE001
            return False

    def _run(self, plan: Plan, result: ResearchResult, started: float, end: float, publish, turn_id: str,
             stop, budget: int, pages_wanted: int) -> None:
        queries = [q for q in plan.queries if q.strip()][:3]
        if not queries:
            result.errors.append("no query to search")
            return
        if not self._is_online():
            result.offline = True
            result.errors.append("no network access right now")
            return
        fetcher = self.fetcher
        pool = _shared_pool()

        # ---------------------------------------------------------- search
        active: list[Source] = []
        for source in self.sources:
            try:
                ok, why = source.available()
                wanted = ok and source.wanted(plan)
            except Exception as exc:  # noqa: BLE001
                ok, why, wanted = False, type(exc).__name__, False
            if not ok:
                result.searches.append({"source": source.name, "ok": False, "skipped": True, "reason": why})
                continue
            if wanted:
                active.append(source)
        search_end = min(end - 1.0, started + max(2.0, (end - started) * config.search_share()))
        per_source_timeout = max(1.0, min(config.search_timeout(), search_end - time.monotonic()))
        futures: dict[concurrent.futures.Future, tuple[Source, str, float]] = {}
        for source in active:
            for query in (queries[:1] if source.per_plan else queries):
                future = pool.submit(source.search, query, plan, fetcher, per_source_timeout)
                futures[future] = (source, query, time.monotonic())
        hits: list[Hit] = []
        blocked_all = bool(futures)
        answered = False
        pending = set(futures)
        while pending:
            remaining = search_end - time.monotonic()
            if remaining <= 0 or self._stopped(stop):
                break
            done, pending = concurrent.futures.wait(pending, timeout=remaining,
                                                    return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                source, query, submitted = futures[future]
                record = {"source": source.name, "query": query, "ok": True, "results": 0,
                          "elapsed": round(time.monotonic() - submitted, 3)}
                try:
                    found = list(future.result() or [])
                    for hit in found:
                        hit.query = hit.query or query
                    hits.extend(found)
                    record["results"] = len(found)
                    answered = True
                    blocked_all = False
                except SourceError as exc:
                    record.update(ok=False, error=str(exc), blocked=exc.blocked)
                    if not exc.blocked:
                        blocked_all = False
                except Exception as exc:  # noqa: BLE001
                    record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                    blocked_all = False
                result.searches.append(record)
                emit("research.search", {"turn_id": turn_id, **record}, publish)
        for future in pending:
            source, query, submitted = futures[future]
            record = {"source": source.name, "query": query, "ok": False, "results": 0,
                      "error": "timed out", "elapsed": round(time.monotonic() - submitted, 3)}
            blocked_all = False
            result.searches.append(record)
            emit("research.search", {"turn_id": turn_id, **record}, publish)
        hits = dedupe_hits(hits)
        result.hits = hits
        if not hits:
            failures = [s.get("error") for s in result.searches if s.get("error")]
            result.blocked = blocked_all and bool(failures)
            if failures:
                result.errors.extend(str(f) for f in failures[:6])
            elif not answered:
                result.errors.append("no search source is available")
            return
        if self._stopped(stop):
            result.errors.append("stopped")
            return

        # ---------------------------------------------------------- fetch
        pages: dict[str, Page] = {}
        targets = fetch_order(hits, plan)[:max(0, pages_wanted)]
        fetch_end = end - 0.5
        fetch_timeout = min(config.fetch_timeout(), fetch_end - time.monotonic())
        if targets and fetch_timeout >= 1.0:
            # Pages are cleaned in this process (CPU work): at most
            # config.fetch_concurrency() at once, from the resource budget.
            gate = threading.BoundedSemaphore(config.fetch_concurrency())

            def bounded_fetch(url: str) -> Page:
                with gate:
                    return fetch_page(fetcher, url, fetch_timeout)

            page_futures = {pool.submit(bounded_fetch, hit.url): hit for hit in targets}
            pending = set(page_futures)
            while pending:
                remaining = fetch_end - time.monotonic()
                if remaining <= 0 or self._stopped(stop):
                    break
                done, pending = concurrent.futures.wait(pending, timeout=remaining,
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    hit = page_futures[future]
                    try:
                        page = future.result()
                    except Exception as exc:  # noqa: BLE001
                        page = Page(url=hit.url, error=f"{type(exc).__name__}")
                    record = {"url": hit.url, "status": page.status, "ok": not page.error and bool(page.text),
                              "chars": len(page.text), "error": page.error, "lang": page.lang,
                              "published": page.published}
                    result.fetches.append(record)
                    emit("research.fetch", {"turn_id": turn_id, **record}, publish)
                    if not page.error and page.text:
                        pages[hit.url] = page
            for future in pending:
                hit = page_futures[future]
                record = {"url": hit.url, "status": 0, "ok": False, "chars": 0, "error": "timed out"}
                result.fetches.append(record)
                emit("research.fetch", {"turn_id": turn_id, **record}, publish)

        # ---------------------------------------------------------- rank
        context, sources, passages = build_context(plan, hits, pages, budget)
        result.context = context
        result.sources = sources
        result.passages = passages
        if not passages:
            result.errors.append("the results did not contain usable text")


_default: ResearchEngine | None = None
_default_lock = threading.Lock()


def default_engine() -> ResearchEngine:
    global _default
    with _default_lock:
        if _default is None:
            _default = ResearchEngine()
        return _default


def research_for_prompt(
    messages: list[dict],
    model: Any = None,
    *,
    publish: Callable | None = None,
    turn_id: str = "",
    stop: Callable[[], bool] | None = None,
    engine: ResearchEngine | None = None,
    planner: Any = None,
) -> ResearchResult | None:
    """Plan from the conversation, then research if the plan says so.

    ``messages`` is the conversation, last user message last. Returns
    ``None`` when the planner decided no web research is needed.
    """
    from .planner import default_planner

    prompt = ""
    history: list[dict] = []
    for index in range(len(messages or []) - 1, -1, -1):
        if (messages[index] or {}).get("role") == "user":
            prompt = str(messages[index].get("content") or "")
            history = [m for m in messages[:index] if m.get("role") in ("user", "assistant")]
            break
    plan = (planner or default_planner()).plan(prompt, history, model, publish=publish, turn_id=turn_id)
    if not plan.needs_web:
        return None
    return (engine or default_engine()).run(plan, publish=publish, turn_id=turn_id, stop=stop)


__all__ = ["Fetcher", "ResearchEngine", "default_engine", "fetch_page", "research_for_prompt"]
