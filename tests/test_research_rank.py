"""Ranking: dedupe, ordering, trust, freshness, budget, and the Context format."""
from __future__ import annotations

from datetime import datetime, timezone

from services.research.rank import (
    build_context,
    canonical_url,
    chunk,
    dedupe_hits,
    fetch_order,
    freshness_score,
    trust,
)
from services.research.types import Hit, Page, Plan

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PM_PLAN = Plan(True, queries=["current prime minister of India"], freshness="live", entities=["India"])


def test_canonical_url_and_hit_dedupe():
    same = [
        "https://www.example.com/news/story/?utm_source=x&utm_medium=y",
        "http://example.com/news/story",
        "https://example.com/news/story/amp",
        "https://m.example.com/news/story#comments",
    ]
    assert len({canonical_url(u) for u in same}) == 1
    assert canonical_url("https://example.com/a?id=1") != canonical_url("https://example.com/a?id=2")
    hits = dedupe_hits([Hit("A", same[0], snippet="short"), Hit("A again", same[1], snippet="a longer snippet"),
                        Hit("B", "https://other.org/x")])
    assert [h.title for h in hits] == ["A", "B"] and hits[0].snippet == "a longer snippet"


def test_trust_tiers_and_freshness():
    assert trust("pmindia.gov.in") == 1.0 and trust("en.wikipedia.org") == 1.0 and trust("cdc.gov") == 1.0
    assert trust("thehindu.com") == 0.75
    assert trust("random-blog.net") == 0.45
    assert trust("pinterest.com") == 0.15
    assert freshness_score("2026-10-31T10:00:00Z", "live", NOW) == 1.0
    assert freshness_score("2024-01-01", "live", NOW) == 0.0
    assert freshness_score("", "any", NOW) == 0.5


def test_chunking_keeps_headings():
    text = "Intro paragraph about things, quite short.\n\n## Section A\n\n" + ("Sentence about A. " * 80)
    pieces = chunk(text, 300)
    assert pieces[0].startswith("Intro")
    assert all(p.startswith("## Section A") for p in pieces[1:]) and len(pieces) > 2


def test_the_answer_ranks_first_and_noise_is_left_out():
    hits = [
        Hit("Prime Minister's Office (India)", "https://en.wikipedia.org/wiki/PMO", source="wikipedia",
            text="The Prime Minister's Office consists of the immediate staff of the Prime Minister of India. "
                 "It is located in South Block.", fetchable=False),
        Hit("Prime Minister of India", "https://en.wikipedia.org/wiki/Prime_Minister_of_India", source="wikipedia",
            text="The prime minister of India is the head of government of the Republic of India.\n\n"
                 "Narendra Modi is the current prime minister of India, serving since 26 May 2014.",
            fetchable=False, published="2026-10-16"),
        Hit("Best pizza in Rome", "https://food.example.com/pizza", source="searxng",
            snippet="Our guide to the best pizza places in Rome, updated weekly."),
    ]
    context, sources, passages = build_context(PM_PLAN, hits, {}, 900, now=NOW)
    assert "serving since 26 May 2014" in passages[0].text
    assert sources[0]["url"] == "https://en.wikipedia.org/wiki/Prime_Minister_of_India"
    assert all("pizza" not in p.text for p in passages)
    # format: header, numbered sources, tagged passages, citation instruction
    lines = context.splitlines()
    assert lines[0].startswith("Web research for: current prime minister of India")
    assert lines[1] == "Sources:" and lines[2].startswith("[1] Prime Minister of India — en.wikipedia.org — 2026-10-16")
    assert "Passages:" in lines
    passages_block = context.split("Passages:\n", 1)[1]
    assert passages_block.startswith("[1] ") and "Narendra Modi" in passages_block.split("\n[2]")[0]
    assert lines[-1].startswith("Cite the passages you use as [n].")
    assert set(sources[0]) == {"title", "url", "domain", "provider", "snippet"}


def test_near_duplicate_pages_and_passages_are_dropped():
    story = ("Dock workers at the port of Northhaven have voted to accept a 9% pay rise, ending a 12-day strike "
             "that left hundreds of containers stranded and delayed deliveries across the region. ") * 3
    plan = Plan(True, queries=["Northhaven port strike"], freshness="recent", entities=["Northhaven"])
    hits = [Hit("Strike ends", "https://news-a.example/strike"), Hit("Strike ends (syndicated)", "https://news-b.example/s")]
    pages = {"https://news-a.example/strike": Page(url="", text=story, published="2026-10-30"),
             "https://news-b.example/s": Page(url="", text=story + " Reporting by agencies.", published="2026-10-30")}
    _context, sources, passages = build_context(plan, hits, pages, 900, now=NOW)
    assert len(sources) == 1 and len(passages) == 1


def test_budget_and_per_source_cap():
    long_text = "\n\n".join(f"Paragraph {i} about the Northhaven port strike and the pay deal, with details "
                            f"number {i} that matter to readers. " * 4 for i in range(20))
    plan = Plan(True, queries=["Northhaven port strike pay deal"], freshness="any")
    hits = [Hit("One page", "https://a.example/x", text=long_text, fetchable=False)]
    context, _sources, passages = build_context(plan, hits, {}, 300, now=NOW)
    assert len(passages) <= 2  # at most two passages from one source
    assert len(context) < 300 * 3.5 + 600  # budget plus the fixed header/footer lines


def test_each_query_gets_its_best_passage():
    plan = Plan(True, queries=["python 3.14 release date", "node 24 lts date"], freshness="recent")
    hits = [
        Hit("Python", "https://www.python.org/downloads/", text="Python 3.14.0 was released on 7 October 2025.",
            fetchable=False),
        Hit("Node.js releases", "https://nodejs.org/en/about/previous-releases",
            text="Node 24 entered long-term support (LTS) in October 2025.", fetchable=False),
    ]
    _context, sources, _passages = build_context(plan, hits, {}, 900, now=NOW)
    assert {s["domain"] for s in sources} == {"python.org", "nodejs.org"}


def test_fetch_order_prefers_trusted_relevant_distinct_domains():
    plan = Plan(True, queries=["port strike Northhaven"])
    hits = [
        Hit("Port strike Northhaven ends", "https://www.bbc.co.uk/news/1", source="searxng", rank=2),
        Hit("Port strike Northhaven: live", "https://www.bbc.co.uk/news/2", source="searxng", rank=0),
        Hit("Strike pins", "https://pinterest.com/pin/1", source="searxng", rank=1),
        Hit("Wiki", "https://en.wikipedia.org/wiki/Northhaven", source="wikipedia", text="already clean",
            fetchable=False),
    ]
    order = fetch_order(hits, plan)
    assert [h.domain for h in order] == ["bbc.co.uk", "pinterest.com"]
    assert order[0].url.endswith("/2")
