"""The whole research pipeline with the network faked, plus one live smoke test."""
from __future__ import annotations

import time

import pytest
from research_fakes import Events, FakeFetcher, FakeModel, fail, fixture, ok

from services.research.engine import ResearchEngine, research_for_prompt
from services.research.planner import Planner
from services.research.sources import Source, SourceError
from services.research.sources.searxng_public import InstancePool, SearxngPublicSource
from services.research.sources.wikipedia import WikipediaSource
from services.research.types import Hit, Plan
from services.response.loop import collect_sources
from services.tools.builtin.web_fetch import WebFetchTool
from services.tools.builtin.web_search import WebSearchTool
from services.tools.registry import ToolRegistry
from services.tools.runner import ToolRunner

ARTICLE = "https://news.example.com/business/port-strike-ends"
FORUM = "https://forum.example.net/t/starter"
SEARX = {"results": [
    {"title": "Port strike ends after 12 days", "url": ARTICLE + "?utm_source=searx", "content": "Dock workers accept"},
    {"title": "Port strike ends (mirror)", "url": "https://www.news.example.com/business/port-strike-ends/",
     "content": "duplicate of the first"},
    {"title": "Starter thread", "url": FORUM, "content": "unrelated forum"},
    {"title": "A PDF", "url": "https://files.example.com/report.pdf", "content": "pdf"},
]}


def fake_network():
    return FakeFetcher({
        "https://searx.test/search": ok(SEARX),
        "https://en.wikipedia.org": fail(status=404, error="HTTP 404"),
        ARTICLE: ok(fixture("news_article.html")),
        FORUM: ok(fixture("forum_thread.html")),
        "https://files.example.com/report.pdf": ok(b"%PDF-1.7", "application/pdf"),
    })


def engine_for(fetcher, extra=None, deadline=None):
    sources = [SearxngPublicSource(pool=InstancePool(["https://searx.test"])), WikipediaSource()]
    return ResearchEngine(fetcher=fetcher, sources=sources + list(extra or []), online=lambda: True,
                          deadline=deadline)


def test_full_pipeline_with_fake_network():
    fetcher = fake_network()
    events = Events()
    plan = Plan(True, queries=["Northhaven port strike pay deal"], freshness="recent", entities=["Northhaven"])
    result = engine_for(fetcher).run(plan, publish=events.publish, turn_id="t1")
    assert result.ok and not result.errors
    # the duplicate URL was fetched once; the PDF was refused as not HTML
    assert fetcher.count(ARTICLE) == 1
    assert sum(1 for f in result.fetches if "not an HTML page" in (f.get("error") or "")) <= 1
    context = result.context
    assert "[1] Port strike ends after 12 days as workers accept pay deal — news.example.com — 2026-10-30" in context
    assert "9% pay rise" in context and "cookie" not in context.lower()
    assert "sourdough" not in context  # the forum page is irrelevant to the query
    assert result.sources[0]["url"].startswith(ARTICLE)
    # events: per source, per page, and a final summary
    kinds = events.kinds()
    assert "research.search" in kinds and "research.fetch" in kinds and kinds[-1] == "research.done"
    searx = [e for e in events.of("research.search") if e["source"] == "searxng_public"][0]
    assert searx["ok"] and searx["results"] == 4 and searx["turn_id"] == "t1"
    done = events.of("research.done")[0]
    assert done["ok"] and done["sources"] == result.sources and done["elapsed"] >= 0


def test_tool_result_feeds_source_chips():
    tool = WebSearchTool(engine=engine_for(fake_network()))
    result = tool.run({"query": "Northhaven port strike", "queries": ["Northhaven dock pay rise"]})
    assert result.ok and result.source == "web (research)"
    assert result.data["queries"] == ["Northhaven port strike", "Northhaven dock pay rise"]
    chips: list[dict] = []
    collect_sources(result.data, chips)
    assert chips and chips[0]["domain"] == "news.example.com"
    assert set(chips[0]) == {"title", "url", "domain", "provider", "snippet"}
    assert result.data["results"][0]["title"]  # web_search.done compatibility
    assert "query" in tool.parameters["required"] and "queries" in tool.parameters["properties"]


class Slow(Source):
    name = "slow"

    def search(self, query, plan, fetcher, timeout):
        time.sleep(5)
        return [Hit("late", "https://late.example/")]


def test_deadline_is_honoured_and_slow_sources_are_reported():
    events = Events()
    engine = engine_for(fake_network(), extra=[Slow()], deadline=3.0)
    started = time.monotonic()
    result = engine.run(Plan(True, queries=["Northhaven port strike"], freshness="any"), publish=events.publish)
    assert time.monotonic() - started < 3.6
    assert result.ok  # the fast source still answered
    slow = [s for s in result.searches if s["source"] == "slow"]
    assert slow and slow[0]["error"] == "timed out"


def test_offline_blocked_and_empty_are_distinct():
    offline = ResearchEngine(fetcher=FakeFetcher(), sources=[], online=lambda: False)
    result = WebSearchTool(engine=offline).run({"query": "x"})
    assert not result.ok and result.offline

    class Refuses(Source):
        name = "refuses"

        def search(self, query, plan, fetcher, timeout):
            raise SourceError("challenge", blocked=True)

    class Empty(Source):
        name = "empty"

        def search(self, query, plan, fetcher, timeout):
            return []

    blocked = ResearchEngine(fetcher=FakeFetcher(), sources=[Refuses()], online=lambda: True)
    result = WebSearchTool(engine=blocked).run({"query": "x"})
    assert not result.ok and "bot check" in result.content
    empty = ResearchEngine(fetcher=FakeFetcher(), sources=[Refuses(), Empty()], online=lambda: True)
    result = WebSearchTool(engine=empty).run({"query": "x"})
    assert result.ok and "returned nothing" in result.content

    class Explodes(Source):
        name = "explodes"

        def search(self, query, plan, fetcher, timeout):
            raise ZeroDivisionError

    result = ResearchEngine(fetcher=FakeFetcher(), sources=[Explodes()], online=lambda: True).run(
        Plan(True, queries=["x"]))
    assert not result.ok and result.searches[0]["error"].startswith("ZeroDivisionError")


def test_research_for_prompt_plans_with_the_model():
    planner = Planner()
    model = FakeModel(['{"web": true, "fresh": "recent", "q": ["Northhaven port strike"], "ent": ["Northhaven"]}'])
    events = Events()
    result = research_for_prompt(
        [{"role": "user", "content": "any news about the docks?"},
         {"role": "assistant", "content": "Which docks?"},
         {"role": "user", "content": "Northhaven, is the strike over?"}],
        model, publish=events.publish, engine=engine_for(fake_network()), planner=planner)
    assert result is not None and result.ok and result.plan.method == "model"
    assert "Which docks?" in model.calls[0][1]["content"]
    assert events.kinds()[0] == "research.plan"
    none = research_for_prompt([{"role": "user", "content": "write a haiku about rain"}],
                               FakeModel(['{"web": false}']), planner=planner, engine=engine_for(fake_network()))
    assert none is None


def test_runner_prefetch_runs_the_plan_through_the_research_tool():
    registry = ToolRegistry()
    registry.extend([WebSearchTool(engine=engine_for(fake_network()))])
    runner = ToolRunner(registry, planner=Planner())
    events = Events()
    model = FakeModel(['{"web": true, "fresh": "recent", "q": ["Northhaven port strike"]}'])
    text, results = runner.observe_full("is it over at the port?", model=model, publish=events.publish, turn_id="t9")
    assert "Context:" in text and "[web_search] Web research for: Northhaven port strike" in text
    assert results[0]["tool"] == "web_search" and results[0]["ok"]
    assert results[0]["args"] == {"query": "Northhaven port strike"}
    assert results[0]["data"]["sources"]
    assert {e["turn_id"] for e in events.of("research.search")} == {"t9"}


def test_web_fetch_uses_the_extractor():
    fetcher = FakeFetcher({ARTICLE: ok(fixture("news_article.html"))})
    result = WebFetchTool(fetcher=fetcher).run({"url": ARTICLE})
    assert result.ok and result.content.startswith("Port strike ends after 12 days as workers accept pay deal\n")
    assert "Published: 2026-10-30" in result.content and "cookie" not in result.content.lower()
    assert result.data["lang"] == "en" and result.data["published"].startswith("2026-10-30")
    refused = WebFetchTool(fetcher=fetcher).run({"url": "http://127.0.0.1:8731/api/status"})
    assert not refused.ok and "public" in refused.content
    pdf = WebFetchTool(fetcher=FakeFetcher({"https://x.example/a.pdf": ok(b"%PDF", "application/pdf")}))
    assert not pdf.run({"url": "https://x.example/a.pdf"}).ok


@pytest.mark.network
def test_live_smoke_prime_minister_of_india():
    """Real network, real sources. Run with: IMURUN_NETWORK_TESTS=1 pytest -m network -s"""
    events = Events()
    started = time.monotonic()
    result = research_for_prompt([{"role": "user", "content": "who is the current prime minister of India"}],
                                 None, publish=events.publish, planner=Planner())
    elapsed = time.monotonic() - started
    print(f"\n--- plan: {result.plan.view() if result else None}")
    for item in events.of("research.search"):
        print(f"--- search {item['source']}: ok={item['ok']} results={item['results']} {item.get('error', '')[:100]}")
    for item in events.of("research.fetch"):
        print(f"--- fetch {item['url'][:80]}: {item['status']} ok={item['ok']} {item.get('error', '')}")
    print(f"--- elapsed {elapsed:.2f}s\n{result.context if result else '(no research)'}")
    assert result is not None and elapsed < 16
    assert result.ok, result.errors
