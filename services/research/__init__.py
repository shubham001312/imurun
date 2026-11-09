"""IMURUN research engine: keyless, model-planned web research.

    planner.py   the model decides whether to search and writes the queries
    sources/     keyless sources (public SearXNG, Wikipedia, news RSS, direct
                 sites and registries, embedded-browser helper) + keyed ones
    extract.py   main-content extraction and cleaning
    rank.py      dedupe, BM25 + trust + freshness ranking, the Context block
    engine.py    the time-boxed pipeline; ``research_for_prompt``
    tool.py      the logic of the model's ``web_search`` (``ResearchTool``)

Names are imported lazily: ``services.tools`` imports this package and this
package's tool imports ``services.tools``, so nothing here may run an
import of either at package load time. See docs/research/README.md.
"""
from __future__ import annotations

from typing import Any

_EXPORTS = {
    "ResearchEngine": "engine", "default_engine": "engine", "fetch_page": "engine",
    "research_for_prompt": "engine", "set_publisher": "events",
    "Planner": "planner", "default_planner": "planner", "heuristic_plan": "planner",
    "parse_plan": "planner", "prefilter": "planner", "ResearchTool": "tool",
    "Hit": "types", "Page": "types", "Passage": "types", "Plan": "types", "ResearchResult": "types",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    import importlib

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
