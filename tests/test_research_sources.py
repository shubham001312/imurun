"""Keyless sources against a fake network (and a fake browser helper)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from research_fakes import FIXTURES, FakeFetcher, fail, ok

from services.research.sources import SourceError
from services.research.sources.browser_search import BrowserSearchSource, HelperClient, helper_supported, unwrap
from services.research.sources.direct import DirectSource
from services.research.sources.rss import NewsRssSource, parse_feed
from services.research.sources.searxng_public import InstancePool, SearxngPublicSource
from services.research.sources.wikipedia import WikipediaSource, topic_of
from services.research.types import Plan

PLAN = Plan(True, queries=["northhaven port strike"], freshness="live", entities=["Northhaven"])
SEARX_JSON = {"results": [
    {"title": "Port strike ends", "url": "https://news.example.com/strike", "content": "Workers accept deal.",
     "publishedDate": "2026-10-30T08:00:00"},
    {"title": "No URL", "url": ""},
    {"title": "Union statement", "url": "https://union.example.org/statement", "content": "We won."},
]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---------------------------------------------------------------- SearXNG
def test_searxng_rotation_and_failure_memory():
    clock = Clock()
    instances = ["https://a.example", "https://b.example", "https://c.example", "https://d.example"]
    pool = InstancePool(instances, clock=clock)
    fetcher = FakeFetcher({
        "https://a.example/search": fail(status=429, error="HTTP 429"),
        "https://b.example/search": ok("<html><title>Making sure you're not a bot!</title></html>"),
        "https://c.example/search": ok(SEARX_JSON),
        "https://d.example/search": ok(SEARX_JSON),
    })
    source = SearxngPublicSource(pool=pool)
    hits = source.search("northhaven port strike", PLAN, fetcher, 6.0)
    # a and b failed (429, challenge page) -> second round asked c and d
    assert [h.url for h in hits] == ["https://news.example.com/strike", "https://union.example.org/statement"]
    assert hits[0].published.startswith("2026-10-30") and hits[0].source == "searxng"
    assert set(pool.failures()) == {"https://a.example", "https://b.example"}
    assert "bot challenge" in pool.failures()["https://b.example"]
    # Failed instances are skipped for 10 minutes; good ones are preferred.
    fetcher.calls.clear()
    source.search("northhaven port strike", PLAN, fetcher, 6.0)
    assert fetcher.count("https://a.example") == 0 and fetcher.count("https://b.example") == 0
    clock.now += 601
    assert set(pool.healthy()) == set(instances)


def test_searxng_all_failing_is_an_honest_error():
    pool = InstancePool(["https://a.example", "https://b.example"], clock=Clock())
    fetcher = FakeFetcher({"https://a.example": fail(status=429, error="HTTP 429"),
                           "https://b.example": ok("<html>challenge</html>")})
    source = SearxngPublicSource(pool=pool)
    with pytest.raises(SourceError) as info:
        source.search("x", PLAN, fetcher, 6.0)
    assert info.value.blocked
    assert source.available() == (False, "every public SearXNG instance failed in the last 10 minutes")


def test_searxng_answered_but_empty_is_not_an_error():
    pool = InstancePool(["https://a.example"], clock=Clock())
    hits = SearxngPublicSource(pool=pool).search("x", PLAN, FakeFetcher({"https://a.example": ok({"results": []})}), 6)
    assert hits == []


# ---------------------------------------------------------------- RSS
GOOGLE_NEWS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>
<item><title>Port strike ends after 12 days - Example News</title>
<link>https://news.google.com/rss/articles/CBMiabc?oc=5</link>
<pubDate>Fri, 30 Oct 2026 08:15:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiabc"&gt;Port strike ends&lt;/a&gt;</description>
<source url="https://news.example.com">Example News</source></item>
<item><title>Unrelated - Other</title><link>https://news.google.com/rss/articles/2</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Northhaven dock deal approved</title><link href="https://feed.example/a1"/>
<updated>2026-10-30T09:00:00Z</updated><summary>Workers at Northhaven port voted yes.</summary></entry>
<entry><title>Weather in Lisbon</title><link href="https://feed.example/a2"/></entry></feed>"""


def test_rss_parsing_google_news_and_atom():
    hits = parse_feed(GOOGLE_NEWS, "google_news")
    assert hits[0].title == "Port strike ends after 12 days - Example News"
    assert hits[0].published.startswith("2026-10-30T08:15") and hits[0].site == "news.example.com"
    atom = parse_feed(ATOM, "feed")
    assert atom[0].url == "https://feed.example/a1" and atom[0].published.startswith("2026-10-30")
    with pytest.raises(SourceError):
        parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss/>', "evil")
    with pytest.raises(SourceError):
        parse_feed("<html>not a feed", "bad")


def test_news_source_filters_feeds_and_marks_redirects_unfetchable():
    fetcher = FakeFetcher({"https://news.google.com/rss/search": ok(GOOGLE_NEWS, "application/rss+xml"),
                           "https://feed.example/rss": ok(ATOM, "application/atom+xml")})
    source = NewsRssSource(feeds=(("feed", "https://feed.example/rss"),))
    hits = source.search("northhaven port strike", PLAN, fetcher, 6)
    google = [h for h in hits if h.source == "google_news"]
    assert google[0].title == "Port strike ends after 12 days" and not google[0].fetchable
    assert google[0].domain == "news.example.com"  # the publisher, not news.google.com
    feed = [h for h in hits if h.source == "feed"]
    assert [h.title for h in feed] == ["Northhaven dock deal approved"]  # Lisbon weather filtered out
    assert source.wanted(PLAN) and not source.wanted(Plan(True, queries=["x"], freshness="any"))


# ---------------------------------------------------------------- Wikipedia
def test_wikipedia_direct_title_then_full_text():
    assert topic_of("who is the current prime minister of India") == "Prime minister of India"
    assert topic_of("latest Python release 2026") == "Python release"
    summary = {"type": "standard", "title": "Prime Minister of India",
               "extract": "The prime minister of India is the head of government.",
               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Prime_Minister_of_India"}},
               "timestamp": "2026-10-16T10:00:00Z"}
    fetcher = FakeFetcher({
        "https://en.wikipedia.org/api/rest_v1/page/summary/Prime_minister_of_India": ok(summary),
        "https://en.wikipedia.org/api/rest_v1/page/summary/Prime_Minister%27s_Office": ok({"type": "disambiguation"}),
        "https://en.wikipedia.org/w/api.php?action=query&list=search": ok({"query": {"search": [
            {"title": "Prime Minister's Office (India)", "snippet": "the <span>PMO</span> staff"}]}}),
        "https://en.wikipedia.org/api/rest_v1/page/summary/Prime_Minister%27s_Office_%28India%29": ok({}),
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts": ok({"query": {"pages": {"1": {
            "extract": "The prime minister of India is the head of government.\nNarendra Modi is the current "
                       "prime minister.\n\n== History ==\nLong history.\n\n== References ==\nrefs"}}}}),
    })
    hits = WikipediaSource().search("who is the current prime minister of India", PLAN, fetcher, 6)
    assert hits[0].url == "https://en.wikipedia.org/wiki/Prime_Minister_of_India" and not hits[0].fetchable
    assert "Narendra Modi is the current prime minister" in hits[0].text
    assert "# History" in hits[0].text and "refs" not in hits[0].text
    assert hits[1].title == "Prime Minister's Office (India)" and hits[1].snippet == "the PMO staff"
    ua = [c for c in fetcher.calls]
    assert ua  # every call went through the fake


def test_wikipedia_refusal_is_reported():
    fetcher = FakeFetcher({"https://en.wikipedia.org": fail(status=403, error="HTTP 403")})
    with pytest.raises(SourceError) as info:
        WikipediaSource().search("x y", PLAN, fetcher, 6)
    assert info.value.blocked


# ---------------------------------------------------------------- direct
def test_direct_targets():
    source = DirectSource()
    plan = Plan(True, queries=["latest version of requests on pypi", "github.com/psf/requests latest release"],
                prompt="check python.org too")
    kinds = {kind: url for kind, url, _ in source.targets(plan, plan.prompt)}
    assert kinds["pypi"] == "https://pypi.org/pypi/requests/json"
    assert kinds["github"] == "https://api.github.com/repos/psf/requests/releases/latest"
    pm = Plan(True, queries=["current prime minister of India"])
    assert ("page", "https://www.pmindia.gov.in/en/", "Prime Minister of India (official site)") in source.targets(pm)
    assert not source.wanted(Plan(True, queries=["sourdough starter holiday"]))


def test_direct_registry_hits_are_clean_text():
    fetcher = FakeFetcher({"https://pypi.org/pypi/requests/json": ok({
        "info": {"name": "requests", "version": "2.33.1", "summary": "Python HTTP for Humans."},
        "urls": [{"upload_time_iso_8601": "2026-09-01T10:00:00Z"}]})})
    plan = Plan(True, queries=["requests on pypi latest version"])
    hits = DirectSource().search(plan.queries[0], plan, fetcher, 6)
    assert hits[0].text.startswith("requests: latest version on PyPI is 2.33.1, uploaded 2026-09-01")
    assert hits[0].url == "https://pypi.org/project/requests/" and not hits[0].fetchable


# ---------------------------------------------------------------- browser helper
def test_browser_search_is_honestly_unavailable(monkeypatch, tmp_path):
    monkeypatch.delenv("IMURUN_BROWSER_SEARCH", raising=False)
    ok_flag, why = BrowserSearchSource().available()
    assert not ok_flag and "IMURUN_BROWSER_SEARCH" in why
    monkeypatch.setenv("IMURUN_BROWSER_SEARCH", "1")
    window = tmp_path / "window.py"
    window.write_text("print('no helper here')\n")
    ok_flag, why = BrowserSearchSource(script=window).available()
    assert not ok_flag and "not implemented yet" in why
    assert not helper_supported(window)


def test_browser_helper_protocol_end_to_end():
    helper = [sys.executable, str(FIXTURES / "fake_search_helper.py")]
    client = HelperClient(helper, ready_timeout=10)
    source = BrowserSearchSource(client=client, require_flag=False)
    try:
        assert source.available() == (True, "")
        hits = source.search("northhaven port strike", PLAN, None, 5)
        assert [h.url for h in hits] == ["https://news.example.com/strike"]  # DDG redirect unwrapped, junk dropped
        assert hits[0].source == "browser:duckduckgo"
        # a blocked engine falls through to the next one
        blocked = BrowserSearchSource(client=client, engines=("duckduckgo", "bing"), require_flag=False)
        from services.research.sources import browser_search

        original = dict(browser_search.ENGINES)
        browser_search.ENGINES["duckduckgo"] = ("https://blocked.example/?q={q}", "1")
        try:
            hits = blocked.search("x", PLAN, None, 5)
        finally:
            browser_search.ENGINES.update(original)
        assert [h.source for h in hits] == ["browser:bing"]
    finally:
        client.close()


def test_browser_helper_wrong_protocol_is_refused():
    client = HelperClient([sys.executable, str(FIXTURES / "fake_search_helper.py"), "2"], ready_timeout=10)
    with pytest.raises(SourceError):
        client.request({"op": "ping"}, 2)
    assert "protocol 2" in client.last_error
    assert unwrap("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2F") == "https://a.example/"


def test_real_window_script_marker_is_detected():
    window = Path(__file__).resolve().parent.parent / "window.py"
    # Today window.py has no helper; when the launcher owner adds it this flips.
    assert helper_supported(window) == ("--search-helper" in window.read_text(encoding="utf-8"))
