"""The keyless API sources added 2026-11-09, routing by topic, failure memory,
and the browser helper's idle shutdown. Network faked throughout."""
from __future__ import annotations

import base64
import sys
import time

import pytest
from research_fakes import FIXTURES, FakeFetcher, fail, ok

from services.research.engine import ResearchEngine
from services.research.net import Response
from services.research.routing import topics
from services.research.sources import Backoff, Source, SourceError, default_sources
from services.research.sources.arxiv import ArxivSource, search_query
from services.research.sources.browser_search import BrowserSearchSource, HelperClient, unwrap
from services.research.sources.ddg_instant import DdgInstantSource, lookups
from services.research.sources.endoflife import EndOfLifeSource, match_products
from services.research.sources.github_search import GitHubSearchSource
from services.research.sources.hackernews import HackerNewsSource
from services.research.sources.mwmbl import MwmblSource
from services.research.sources.openalex import OpenAlexSource, abstract
from services.research.sources.stackexchange import StackExchangeSource, site_for
from services.research.types import Hit, Plan


def plan(prompt, queries=None, fresh="any", entities=None):
    return Plan(True, queries=queries or [prompt], freshness=fresh, entities=entities or [], prompt=prompt)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# ---------------------------------------------------------------- routing
@pytest.mark.parametrize("prompt, expected, absent", [
    ("how to fix ModuleNotFoundError: No module named pip", {"code"}, {"science"}),
    ("Rust borrow checker lifetime elision rules", {"code"}, set()),
    ("recent papers on transformer attention efficiency", {"science"}, set()),
    ("latest Python release", {"release", "code"}, set()),
    ("is Ubuntu 20.04 still supported", {"release"}, set()),
    ("what is the zip code of Palo Alto", set(), {"code"}),
    ("first class flight prices to Tokyo", set(), {"code", "science"}),
])
def test_routing_topics(prompt, expected, absent):
    found = topics(plan(prompt))
    assert "general" in found and expected <= found and not (absent & found)


def test_news_topic_follows_freshness():
    assert "news" in topics(plan("COP31 summit", fresh="live"))
    assert "news" not in topics(plan("COP31 summit", fresh="any"))


def test_sources_run_only_for_their_topics():
    code, science, general = plan("No module named pip error"), plan("arxiv paper on protein folding"), plan(
        "who is the CEO of Nvidia")
    assert StackExchangeSource().wanted(code) and not StackExchangeSource().wanted(general)
    assert ArxivSource().wanted(science) and not ArxivSource().wanted(code)
    assert OpenAlexSource().wanted(science) and not OpenAlexSource().wanted(general)
    assert MwmblSource().wanted(general) and DdgInstantSource().wanted(general)
    assert EndOfLifeSource().wanted(plan("latest node.js LTS version")) and not EndOfLifeSource().wanted(general)


def test_default_sources_are_keyless_and_complete():
    names = [s.name for s in default_sources()]
    for name in ("browser", "mwmbl", "searxng_public", "wikipedia", "ddg_instant", "stackexchange",
                 "github_search", "hackernews", "arxiv", "openalex", "endoflife", "news_rss", "direct"):
        assert name in names
    assert names.index("browser") < names.index("mwmbl") < names.index("searxng_public")


# ---------------------------------------------------------------- failure memory
def test_backoff_refusal_soft_errors_and_success():
    clock = Clock()
    memory = Backoff(clock=clock)
    memory.refuse("HTTP 429")
    assert memory.parked() == (True, "HTTP 429")
    clock.now += 601
    assert memory.parked() == (False, "")
    memory.refuse("Retry-After", 30)
    clock.now += 31
    assert not memory.parked()[0]
    for _ in range(2):
        memory.error("timed out")
    assert not memory.parked()[0]
    memory.error("timed out")
    assert memory.parked()[0] and "3 failures" in memory.parked()[1]
    memory.success()
    assert memory.parked() == (False, "")


def test_api_source_429_is_blocked_and_parks_the_source():
    source = MwmblSource()
    fetcher = FakeFetcher({"https://api.mwmbl.org/search": Response("", 429, {"retry-after": "120"}, b"",
                                                                    error="HTTP 429")})
    with pytest.raises(SourceError) as info:
        source.search("x", plan("x"), fetcher, 5)
    assert info.value.blocked
    available, why = source.available()
    assert not available and "HTTP 429" in why
    # Parked: the engine skips it (no request) and says why.
    engine = ResearchEngine(fetcher=fetcher, sources=[source], online=lambda: True)
    result = engine.run(plan("x"))
    assert result.searches[0]["skipped"] and fetcher.count("https://api.mwmbl.org") == 1


def test_engine_applies_the_per_source_time_cap():
    seen = {}

    class Capped(Source):
        name = "capped"
        timeout_cap = 2.5

        def search(self, query, plan_, fetcher, timeout):
            seen["timeout"] = timeout
            return [Hit(title="t", url="https://a.example/", text="Some text about the capped source here.")]

    ResearchEngine(fetcher=FakeFetcher(), sources=[Capped()], online=lambda: True).run(plan("capped source"),
                                                                                     max_pages=0)
    assert seen["timeout"] == 2.5


# ---------------------------------------------------------------- Mwmbl
MWMBL = [
    {"url": "https://users.rust-lang.org/t/lifetime-elision-rules/112754",
     "title": [{"value": "Lifetime", "is_bold": True}, {"value": " Elision rules", "is_bold": False}],
     "extract": [{"value": "The three ", "is_bold": False}, {"value": "elision", "is_bold": True},
                 {"value": " rules are ...", "is_bold": False}], "source": "mwmbl"},
    {"url": "javascript:alert(1)", "title": [{"value": "bad"}], "extract": []},
    {"url": "https://doc.rust-lang.org/reference/lifetime-elision.html", "title": "Lifetime elision…",
     "extract": []},
]


def test_mwmbl_joins_segments_and_keeps_pages_fetchable():
    hits = MwmblSource().search("rust lifetime elision", plan("rust lifetime elision"),
                                FakeFetcher({"https://api.mwmbl.org/search?s=rust": ok(MWMBL)}), 5)
    assert [h.url for h in hits] == [MWMBL[0]["url"], MWMBL[2]["url"]]
    assert hits[0].title == "Lifetime Elision rules" and hits[0].snippet == "The three elision rules are ..."
    assert hits[1].title == "Lifetime elision" and all(h.fetchable and h.source == "mwmbl" for h in hits)


# ---------------------------------------------------------------- DuckDuckGo Instant Answers
def test_ddg_instant_asks_names_and_reads_the_abstract():
    p = plan("who is the CEO of Nvidia", entities=["Jensen Huang"])
    assert lookups("who is the CEO of Nvidia", p) == ["Jensen Huang", "CEO of Nvidia"]
    answer = {"Heading": "Jensen Huang", "AbstractText": "Jensen Huang is the co-founder, president and CEO of Nvidia.",
              "AbstractURL": "https://en.wikipedia.org/wiki/Jensen_Huang", "AbstractSource": "Wikipedia",
              "Infobox": {"content": [{"label": "Born", "value": "February 17, 1963"},
                                      {"label": "Children", "value": {"nested": True}}]}}
    fetcher = FakeFetcher({"https://api.duckduckgo.com/?q=Jensen": ok(answer),
                           "https://api.duckduckgo.com/?q=CEO": ok({"AbstractText": "", "AbstractURL": ""})})
    hits = DdgInstantSource().search(p.queries[0], p, fetcher, 4)
    assert len(hits) == 1 and hits[0].url == answer["AbstractURL"] and not hits[0].fetchable
    assert "CEO of Nvidia" in hits[0].text and "Born: February 17, 1963" in hits[0].text
    assert "Children" not in hits[0].text and "t=imurun" in fetcher.calls[0]


# ---------------------------------------------------------------- Stack Exchange
SE_SEARCH = {"items": [
    {"question_id": 11, "title": "How to fix &quot;No module named pip&quot;?", "answer_count": 2,
     "link": "https://stackoverflow.com/questions/11/how-to-fix", "body": "<p>I get <code>No module named pip</code>.</p>",
     "last_activity_date": 1700000000},
    {"question_id": 12, "title": "Unanswered", "answer_count": 0, "link": "https://stackoverflow.com/questions/12/x"},
], "quota_remaining": 250}
SE_ANSWERS = {"items": [
    {"question_id": 11, "is_accepted": False, "score": 50, "body": "<p>Reinstall Python.</p>"},
    {"question_id": 11, "is_accepted": True, "score": 40,
     "body": "<p>Run:</p><pre><code>python -m ensurepip --upgrade</code></pre>", "last_activity_date": 1710000000},
]}


def test_stackexchange_question_plus_accepted_answer():
    fetcher = FakeFetcher({"https://api.stackexchange.com/2.3/search/advanced": ok(SE_SEARCH),
                           "https://api.stackexchange.com/2.3/questions/11/answers": ok(SE_ANSWERS)})
    p = plan("ModuleNotFoundError No module named pip")
    hits = StackExchangeSource().search(p.queries[0], p, fetcher, 5)
    assert len(hits) == 1 and hits[0].title == 'How to fix "No module named pip"?'
    assert "Accepted answer (score 40)" in hits[0].text and "python -m ensurepip --upgrade" in hits[0].text
    assert "Reinstall" not in hits[0].text and not hits[0].fetchable
    assert hits[0].published == "2024-03-09"


def test_stackexchange_throttle_is_a_refusal_and_site_choice():
    source = StackExchangeSource()
    fetcher = FakeFetcher({"https://api.stackexchange.com": ok({"error_id": 502, "error_name": "throttle_violation",
                                                                 "error_message": "too many requests"})})
    with pytest.raises(SourceError) as info:
        source.search("x", plan("python x"), fetcher, 5)
    assert info.value.blocked and not source.available()[0]
    backoff = StackExchangeSource()
    backoff.search("x", plan("python x"), FakeFetcher({"https://api.stackexchange.com": ok(
        {"items": [], "backoff": 10})}), 5)
    assert backoff.available()[0] is False
    assert site_for(plan("apt-get broken on ubuntu"), "x") == "askubuntu"
    assert site_for(plan("bluetooth stops on windows 11"), "x") == "superuser"
    assert site_for(plan("python error on windows 11"), "x") == "stackoverflow"


# ---------------------------------------------------------------- GitHub
def test_github_issues_for_errors_repositories_for_projects_nothing_else():
    source = GitHubSearchSource()
    assert source.mode(plan("ModuleNotFoundError: No module named pip")) == "issues"
    assert source.mode(plan("python library for parsing PDF tables")) == "repositories"
    assert source.mode(plan("latest Python release")) == "" and not source.wanted(plan("latest Python release"))
    issues = {"items": [{"html_url": "https://github.com/pypa/pip/issues/1", "title": "No module named pip",
                         "repository_url": "https://api.github.com/repos/pypa/pip", "state": "closed",
                         "comments": 4, "body": "Fixed by `python -m ensurepip`.", "updated_at": "2026-01-02T00:00:00Z"}]}
    hits = source.search("No module named pip", plan("ModuleNotFoundError: No module named pip"),
                         FakeFetcher({"https://api.github.com/search/issues": ok(issues)}), 5)
    assert hits[0].title == "No module named pip · pypa/pip" and "ensurepip" in hits[0].text
    repos = {"items": [{"html_url": "https://github.com/camelot-dev/camelot", "full_name": "camelot-dev/camelot",
                        "description": "PDF table extraction", "stargazers_count": 3000, "language": "Python"}]}
    hits = source.search("pdf tables", plan("python library for parsing PDF tables"),
                         FakeFetcher({"https://api.github.com/search/repositories": ok(repos)}), 5)
    assert "3000 stars" in hits[0].text


def test_github_rate_limit_parks_until_reset():
    source = GitHubSearchSource()
    reset = str(int(time.time()) + 45)
    fetcher = FakeFetcher({"https://api.github.com": Response("", 403, {"x-ratelimit-reset": reset}, b"",
                                                              error="HTTP 403")})
    with pytest.raises(SourceError) as info:
        source.search("x", plan("TypeError in react"), fetcher, 5)
    assert info.value.blocked and not source.available()[0]


# ---------------------------------------------------------------- Hacker News
def test_hackernews_stories_link_the_article():
    payload = {"hits": [
        {"title": "Lifetime elision explained", "url": "https://blog.example.com/elision", "points": 120,
         "num_comments": 30, "objectID": "1", "created_at": "2026-05-01T10:00:00Z"},
        {"title": "Ask HN: elision?", "url": None, "points": 12, "num_comments": 3, "objectID": "2"},
        {"title": "Spam", "url": "https://spam.example/", "points": 1, "objectID": "3"},
    ]}
    hits = HackerNewsSource().search("rust elision", plan("rust elision"),
                                     FakeFetcher({"https://hn.algolia.com/api/v1/search": ok(payload)}), 5)
    assert [h.url for h in hits] == ["https://blog.example.com/elision", "https://news.ycombinator.com/item?id=2"]
    assert hits[0].fetchable and "120 points" in hits[0].snippet


# ---------------------------------------------------------------- arXiv / OpenAlex
ARXIV = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2601.00001v1</id><published>2026-01-05T00:00:00Z</published>
<title>Efficient Attention
 for Long Sequences</title><summary>We cut attention cost to linear.</summary>
<author><name>A. Author</name></author><author><name>B. Author</name></author></entry>
<entry><id>http://arxiv.org/abs/x</id><title>No summary</title></entry>
</feed>"""


def test_arxiv_atom_and_query():
    assert search_query("recent papers on transformer attention 2026") == \
        "all:recent AND all:paper AND all:transformer AND all:attention"
    hits = ArxivSource().search("transformer attention", plan("transformer attention paper"),
                                FakeFetcher({"https://export.arxiv.org/api/query": ok(ARXIV, "application/atom+xml")}), 5)
    assert len(hits) == 1 and hits[0].url == "https://arxiv.org/abs/2601.00001v1"
    assert hits[0].title == "Efficient Attention for Long Sequences" and hits[0].published == "2026-01-05"
    assert "A. Author, B. Author" in hits[0].text and "linear" in hits[0].text
    with pytest.raises(SourceError):
        ArxivSource().search("protein folding", plan("protein folding paper"), FakeFetcher({"https://export.arxiv.org": ok(
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>', "application/atom+xml")}), 5)


def test_openalex_rebuilds_abstracts():
    assert abstract({"attention": [1], "Linear": [0], "wins": [2]}) == "Linear attention wins"
    payload = {"results": [
        {"display_name": "Linear attention", "doi": "https://doi.org/10.1/abc", "publication_date": "2025-03-01",
         "abstract_inverted_index": {"Linear": [0], "attention": [1]}, "cited_by_count": 9,
         "authorships": [{"author": {"display_name": "C. Writer"}}],
         "primary_location": {"source": {"display_name": "NeurIPS"}}},
        {"display_name": "No abstract", "doi": "https://doi.org/10.1/x", "abstract_inverted_index": None},
    ]}
    hits = OpenAlexSource().search("linear attention", plan("linear attention study"),
                                   FakeFetcher({"https://api.openalex.org/works": ok(payload)}), 5)
    assert len(hits) == 1 and hits[0].url == "https://doi.org/10.1/abc"
    assert "C. Writer; NeurIPS; 2025-03-01; cited by 9" in hits[0].text


# ---------------------------------------------------------------- endoflife.date
def test_endoflife_matches_products_and_describes_cycles():
    products = ["python", "nodejs", "ubuntu", "go", "amazon-linux", "postgresql"]
    assert match_products("latest Python release", products) == ["python"]
    assert match_products("node.js LTS version", products) == ["nodejs"]
    assert match_products("go to the latest release", products) == []
    assert match_products("is amazon linux 2 still supported", products) == ["amazon-linux"]
    cycles = [{"cycle": "3.15", "releaseDate": "2026-10-09", "latest": "3.15.2", "latestReleaseDate": "2026-10-25",
               "eol": "2031-10-31", "support": "2028-10-09", "lts": False},
              {"cycle": "2.7", "releaseDate": "2010-07-03", "eol": True}]
    source = EndOfLifeSource()
    fetcher = FakeFetcher({"https://endoflife.date/api/all.json": ok(products),
                           "https://endoflife.date/api/python.json": ok(cycles)})
    hits = source.search("latest Python release", plan("latest Python release"), fetcher, 5)
    assert hits[0].url == "https://endoflife.date/python" and hits[0].published == "2026-10-25"
    assert "python 3.15, released 2026-10-09, latest version 3.15.2 (2026-10-25)" in hits[0].text
    assert "end of life: reached" in hits[0].text
    source.search("latest Python release", plan("latest Python release"), fetcher, 5)
    assert fetcher.count("https://endoflife.date/api/all.json") == 1  # product list cached


# ---------------------------------------------------------------- browser helper
def test_bing_redirects_are_unwrapped():
    target = "https://en.wikipedia.org/wiki/Jensen_Huang"
    encoded = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert unwrap(f"https://www.bing.com/ck/a?!&&p=abc&u={encoded}&ntb=1") == target
    assert unwrap("https://www.bing.com/ck/a?u=zzz") == "https://www.bing.com/ck/a?u=zzz"
    assert unwrap("https://example.com/x") == "https://example.com/x"


def test_browser_runs_all_plan_queries_and_remembers_refusals():
    helper = [sys.executable, str(FIXTURES / "fake_search_helper.py")]
    client = HelperClient(helper, ready_timeout=10)
    source = BrowserSearchSource(client=client, require_flag=False)
    try:
        p = Plan(True, queries=["northhaven port strike", "northhaven union deal"])
        hits = source.search(p.queries[0], p, None, 5)
        assert [h.query for h in hits] == ["northhaven port strike", "northhaven union deal"]
        source.refused["duckduckgo"].refuse("bot check")
        hits = source.search("x", Plan(True, queries=["x"]), None, 5)
        assert [h.source for h in hits] == ["browser:bing"]  # DuckDuckGo skipped while it refuses
        source.refused["bing"].refuse("bot check")
        assert source.available() == (False, "every search engine showed a bot check in the last 10 minutes")
    finally:
        client.close()


def test_browser_helper_exits_when_idle():
    helper = [sys.executable, str(FIXTURES / "fake_search_helper.py")]
    client = HelperClient(helper, ready_timeout=10, idle_seconds=0.3)
    try:
        assert client.request({"op": "ping"}, 5).get("type") == "pong"
        proc = client._proc
        assert client.running
        deadline = time.monotonic() + 5
        while client.running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not client.running and proc.wait(timeout=5) == 0
        # The next search starts it again.
        assert client.request({"op": "ping"}, 5).get("type") == "pong"
    finally:
        client.close()


def test_a_missing_backend_degrades_to_the_other_sources():
    """window.py exits before `ready` when pywebview has no GUI backend: the
    browser source fails once, is parked, and the rest of the plan still answers."""
    client = HelperClient([sys.executable, "-c", "import sys; sys.exit(3)"], ready_timeout=5)
    browser = BrowserSearchSource(client=client, require_flag=False)
    mwmbl = MwmblSource()
    fetcher = FakeFetcher({"https://api.mwmbl.org/search": ok(MWMBL),
                           "https://users.rust-lang.org": fail(status=404, error="HTTP 404"),
                           "https://doc.rust-lang.org": fail(status=404, error="HTTP 404")})
    result = ResearchEngine(fetcher=fetcher, sources=[browser, mwmbl], online=lambda: True).run(
        plan("rust lifetime elision"))
    by_source = {s["source"]: s for s in result.searches}
    assert not by_source["browser"]["ok"] and "exited" in by_source["browser"]["error"]
    assert by_source["mwmbl"]["ok"] and result.ok
    assert browser.available()[0] is False and "failed recently" in browser.available()[1]
