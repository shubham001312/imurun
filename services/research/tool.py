"""``web_search`` backed by the research engine.

The model calls it with the query *it* wrote (``query``, optionally up to
three ``queries`` for different parts of the question); the pre-fetch calls
``run_plan`` with the planner's plan. Either way the result is the cleaned,
ranked Context block with numbered sources, and ``data["sources"]`` in the
shape ``response/loop.py::collect_sources`` turns into source chips.

Import layout: ``services.tools`` imports this package (``builtin/web_search.py``),
so this module must not import ``services.tools`` at load time. The logic is
``ResearchToolMixin``; the concrete class is
``services.tools.builtin.web_search.WebSearchTool`` (``Tool`` + the mixin),
re-exported here as ``ResearchTool``.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from .engine import ResearchEngine, default_engine
from .events import emit
from .planner import guess_entities
from .types import FRESHNESS, Plan, ResearchResult

if TYPE_CHECKING:
    from ..tools.base import ToolResult

BLOCKED_HINT = (
    "Every search source refused or failed, so the web could not be searched right now. "
    "Try again in a few minutes."
)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def plan_from_args(args: dict[str, Any]) -> Plan:
    """The plan for a model-issued call: its own queries, no second guessing."""
    queries: list[str] = []
    for value in [args.get("query"), *(args.get("queries") or [])]:
        text = _clean(value)[:160]
        if text and text.lower() not in {q.lower() for q in queries}:
            queries.append(text)
    fresh = str(args.get("freshness") or "").lower()
    if fresh not in FRESHNESS:
        from ..tools.policy import is_realtime

        fresh = "live" if any(is_realtime(q) for q in queries) else "recent"
    entities = []
    for query in queries:
        entities.extend(e for e in guess_entities(query) if e not in entities)
    return Plan(needs_web=bool(queries), reason="the model asked for a search", queries=queries[:3],
                freshness=fresh, entities=entities[:6], method="given",
                prompt=" ".join(queries))


def to_tool_result(result: ResearchResult) -> ToolResult:
    from ..tools.base import ToolResult

    plan = result.plan
    query = plan.queries[0] if plan.queries else ""
    legacy = [{"title": h.title, "url": h.url, "snippet": h.snippet[:300], "provider": h.source}
              for h in result.hits[:10]]
    data = {
        "query": query, "queries": plan.queries, "provider": "research", "plan": plan.view(),
        "results": legacy, "sources": result.sources, "searches": result.searches,
        "fetches": result.fetches, "elapsed": round(result.elapsed, 3),
    }
    source = "web (research)"
    if result.offline:
        return ToolResult(ok=False, error="No network access right now.",
                          content="No network access right now, so the web cannot be searched.",
                          data=data, source=source, offline=True)
    if result.ok:
        return ToolResult(ok=True, content=result.context, data=data, source=source)
    if result.hits:
        # Results exist but none had usable text: still honest, still useful.
        lines = [f"Search results for {query} (no page text could be read):"]
        for index, hit in enumerate(result.hits[:6], start=1):
            lines.append(f"{index}. {hit.title} - {hit.snippet[:200]}".rstrip(" -"))
            lines.append(f"   {hit.url}")
        data["sources"] = [{"title": h.title[:120], "url": h.url, "domain": h.domain, "provider": h.source,
                            "snippet": h.snippet[:300]} for h in result.hits[:6]]
        return ToolResult(ok=True, content="\n".join(lines), data=data, source=source)
    answered = any(s.get("ok") and not s.get("skipped") for s in result.searches)
    if answered:
        return ToolResult(ok=True, content=f"The search for {query} returned nothing.", data=data, source=source)
    failures = [s for s in result.searches if s.get("error")]
    if result.blocked:
        message = "The search sources refused the automated search (bot check). " + BLOCKED_HINT
    else:
        reasons = ("; ".join(f"{s['source']}: {s['error']}" for s in failures[:4])
                   or "; ".join(result.errors[:3]) or "no source answered")
        message = f"The search could not be run ({reasons})."
    return ToolResult(ok=False, error=message, content=message, data=data, source=source)


class ResearchToolMixin:
    """Everything ``web_search`` does; combined with ``Tool`` in
    ``services/tools/builtin/web_search.py``."""

    name = "web_search"
    category = "network"
    requires_network = True
    description = (
        "Search the web and read the best pages for current or specific facts: news, who holds an "
        "office now, prices, releases and versions, documentation, events. Returns cleaned passages "
        "with numbered sources [n] to cite."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A focused search query in keywords. Replace pronouns with names; "
                               "add the year for recent events.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Optional: up to 3 extra queries for other parts of the question.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, engine: ResearchEngine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> ResearchEngine:
        return self._engine or default_engine()

    def available(self) -> bool:
        from ..tools.network import network_available

        return network_available()

    def unavailable_reason(self) -> str:
        return "" if self.available() else "No network access right now."

    def run_plan(self, plan: Plan, *, publish: Callable | None = None, turn_id: str = "",
                 stop: Callable[[], bool] | None = None, legacy_events: bool = False) -> ToolResult:
        started = time.monotonic()
        query = plan.queries[0] if plan.queries else ""
        if legacy_events:
            emit("web_search.start", {"turn_id": turn_id, "query": query, "queries": plan.queries}, publish)
        result = self.engine.run(plan, publish=publish, turn_id=turn_id, stop=stop)
        tool_result = to_tool_result(result)
        tool_result.latency_ms = int((time.monotonic() - started) * 1000)
        if legacy_events:
            emit("web_search.done", {"turn_id": turn_id, "query": query,
                                     "results": tool_result.data.get("results", [])}, publish)
        return tool_result

    def run(self, args: dict[str, Any]) -> ToolResult:
        return self.run_with_context(args, None)

    def run_with_context(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        from ..tools.base import ToolResult

        plan = plan_from_args(args or {})
        if not plan.queries:
            return ToolResult(ok=False, error="No query was given.",
                              content="No query was given, so nothing could be searched.", source="web")
        emit("research.plan", {"turn_id": getattr(ctx, "turn_id", ""), **plan.view()})
        stop = getattr(ctx, "stop", None)
        return self.run_plan(plan, turn_id=getattr(ctx, "turn_id", ""), stop=stop, legacy_events=True)


def __getattr__(name: str) -> Any:
    if name == "ResearchTool":
        from ..tools.builtin.web_search import WebSearchTool

        return WebSearchTool
    raise AttributeError(name)
