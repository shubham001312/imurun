"""The tool runner (the pre-fetch).

This is what the agent loop talks to before the first generation. It reads
the question, decides which tools could help, runs them, and returns one
context block for the model.

Who decides what:

* **web research** is decided by the model: the research planner
  (``services/research/planner.py``) runs one short routing generation with
  the local model and returns ``needs_web`` + the queries the model wrote.
  The regex ``web_search`` intents in ``policy.py`` are only the fallback
  when no model is available or it does not produce JSON;
* **weather, clock, location** still come from the narrow intents in
  ``policy.py`` (they are tools with their own arguments, not searches).

The model passed to ``intents`` / ``observe_full`` (``model=``) is used for
planning; without one, ``planner_model`` (set by the server) is used; without
that, the heuristic. The plan is cached per message, so ``intents`` followed
by ``observe_full`` plans once.

Latency rules (the tools run *before* the model can start answering):

* tools run concurrently, not one after another;
* the whole lookup has a deadline (``TOOL_DEADLINE`` seconds; web research
  has its own, ``IMURUN_RESEARCH_DEADLINE``); a tool that is still running
  when it expires is reported as timed out and the turn goes on without it;
* the network probe is cached (see ``network.py``).
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from typing import Any, Callable

from .base import ToolResult
from .network import network_available
from .policy import detect_intents, extract_place, format_observation, tool_instructions
from .registry import ToolRegistry

TOOL_DEADLINE = float(os.getenv("IMURUN_TOOL_DEADLINE", "10"))
SEARCH = "web_search"


class ToolRunner:
    """Runs the tools a question needs and formats the result for the model."""

    # The loop checks this to know it may pass history / model / publish.
    supports_research = True

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_tools: int = 2,
        enabled: bool = True,
        deadline: float = TOOL_DEADLINE,
        planner: Any = None,
    ) -> None:
        self._registry = registry
        self._max_tools = max(1, int(max_tools))
        self._enabled = bool(enabled)
        self._deadline = max(0.5, float(deadline))
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="imurun-tool"
        )
        self._planner = planner
        self.last_results: list[dict[str, Any]] = []
        # Set by the server: the permission policy, so a category switched
        # off in Settings (for example ``network``) is not pre-fetched either.
        self.policy: Any = None
        # Optional, set by the server: the raw model for the routing
        # generation, and the event bus for research.* events.
        self.planner_model: Any = None
        self.publish: Callable[[str, dict], Any] | None = None

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def planner(self) -> Any:
        if self._planner is None:
            from ..research.planner import default_planner

            self._planner = default_planner()
        return self._planner

    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------- planning
    def _allowed(self, name: str) -> bool:
        tool = self._registry.get(name)
        if tool is None:
            return False
        if self.policy is not None:
            try:
                if self.policy.generic(tool) == "deny":
                    return False
            except Exception:  # noqa: BLE001 - a broken policy does not block facts
                pass
        return True

    def plan(self, prompt: str, history: list[dict] | None = None, model: Any = None,
             publish: Callable | None = None, turn_id: str = "") -> Any:
        """The research plan for this message (cached; never raises)."""
        return self.planner.plan(prompt, history, model if model is not None else self.planner_model,
                                 publish=publish or self.publish, turn_id=turn_id)

    def intents(self, prompt: str, history: list[dict] | None = None, model: Any = None,
                publish: Callable | None = None, turn_id: str = "", search: bool = True,
                **_: Any) -> list[str]:
        """Tools this prompt would run, in order. Empty when none apply.

        ``search=False``: the answering model can call ``web_search`` itself,
        so no search is planned or run ahead of it (the model decides).
        """
        if not self._enabled:
            return []
        names = []
        regex = [n for n in detect_intents(prompt) if n != SEARCH]
        wants_search = False
        if search and self._allowed(SEARCH):
            try:
                wants_search = bool(self.plan(prompt, history, model, publish, turn_id).needs_web)
            except Exception:  # noqa: BLE001 - planning never blocks the turn
                wants_search = False
        ordered = [n for n in ("weather", "time_now") if n in regex]
        if wants_search:
            ordered.append(SEARCH)
        ordered += [n for n in regex if n not in ordered]
        for name in ordered[: self._max_tools]:
            if self._allowed(name):
                names.append(name)
        return names

    # -------------------------------------------------------------- running
    def observe(self, prompt: str, **kwargs: Any) -> str:
        """Return the context block for a prompt. Empty when no tool applies.

        The block holds the tool instructions and the observations. It is
        safe to put straight into the model context.
        """
        return self.observe_full(prompt, **kwargs)[0]

    def observe_full(self, prompt: str, history: list[dict] | None = None, model: Any = None,
                     publish: Callable | None = None, turn_id: str = "",
                     stop: Callable[[], bool] | None = None, search: bool = True,
                     **_: Any) -> tuple[str, list[dict[str, Any]]]:
        """``observe`` plus this call's own results.

        ``last_results`` is shared by every turn; two concurrent turns would
        read each other's results through it. The loop uses this instead.
        """
        names = self.intents(prompt, history, model, publish, turn_id, search=search)
        if not names:
            return "", []

        needs_network = any(
            getattr(self._registry.get(name), "requires_network", False) for name in names
        )
        offline = needs_network and not network_available()
        started = time.monotonic()

        plan = self.plan(prompt, history, model, publish, turn_id) if SEARCH in names else None
        arguments = {name: self._arguments(name, prompt, plan) for name in names}
        futures = {}
        deadlines = {}
        for name in names:
            tool = self._registry.get(name)
            if name == SEARCH and plan is not None and callable(getattr(tool, "run_plan", None)):
                futures[name] = self._pool.submit(
                    self._run_plan, tool, plan, publish or self.publish, turn_id, stop)
                from ..research import config as research_config

                deadlines[name] = max(self._deadline, research_config.deadline() + 1.5)
            else:
                futures[name] = self._pool.submit(self._registry.execute, name, arguments[name])
                deadlines[name] = self._deadline
        lines: list[str] = []
        results: list[dict[str, Any]] = []
        any_failed = False
        for name in names:
            remaining = deadlines[name] - (time.monotonic() - started)
            try:
                result = futures[name].result(timeout=max(0.0, remaining))
            except concurrent.futures.TimeoutError:
                result = ToolResult(
                    ok=False,
                    error=f"took longer than {deadlines[name]:.0f}s",
                    content=f"The {name} tool did not answer in time.",
                    source=name,
                    latency_ms=int(deadlines[name] * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", source=name)
            results.append({"tool": name, "args": arguments[name], **result.view()})
            if result.offline:
                offline = True
            if not result.ok:
                any_failed = True
            lines.append(format_observation(name, result))

        with self._lock:
            self.last_results = results

        # Describe every tool that ran, even one that is unavailable now
        # (offline): the model must know what was tried.
        ran = [tool for tool in self._registry.list_tools() if tool.name in names]
        header = tool_instructions(ran, offline=offline, names=names, failed=any_failed)
        return f"{header}\n\nContext:\n" + "\n".join(lines), results

    def _run_plan(self, tool: Any, plan: Any, publish: Callable | None, turn_id: str,
                  stop: Callable[[], bool] | None) -> ToolResult:
        started = time.monotonic()
        try:
            result = tool.run_plan(plan, publish=publish, turn_id=turn_id, stop=stop)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", source=SEARCH)
        if not isinstance(result, ToolResult):
            result = ToolResult(ok=False, error="the tool returned no result", source=SEARCH)
        result.latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
        result.source = result.source or SEARCH
        return result

    def _arguments(self, name: str, prompt: str, plan: Any = None) -> dict[str, Any]:
        """Build the arguments for one tool from the prompt (and the plan)."""
        if name == "weather":
            place = extract_place(prompt)
            return {"place": place} if place else {}
        if name == SEARCH:
            queries = list(getattr(plan, "queries", None) or [])
            if not queries:
                return {"query": prompt.strip()}
            args: dict[str, Any] = {"query": queries[0]}
            if len(queries) > 1:
                args["queries"] = queries[1:]
            return args
        if name == "location":
            place = extract_place(prompt)
            return {"place": place} if place else {}
        return {}

    def status(self) -> dict[str, Any]:
        """Honest state for the API: which tools exist and can they run."""
        status = self._registry.status()
        status["network"] = network_available()
        with self._lock:
            status["last_results"] = list(self.last_results)
        return status
