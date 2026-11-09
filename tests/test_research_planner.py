"""The research planner: the model decides, JSON is read forgivingly, the
heuristic is only a fallback, and nothing is generated twice."""
from __future__ import annotations

from research_fakes import Events, FakeModel

from services.research.planner import (
    conceptual_question,
    Planner,
    build_messages,
    generate_capped,
    heuristic_plan,
    parse_plan,
    prefilter,
)
from services.tools.base import Tool, ToolResult
from services.tools.registry import ToolRegistry
from services.tools.runner import ToolRunner


def test_parse_plan_strict_json():
    plan = parse_plan('{"web": true, "fresh": "live", "q": ["prime minister of India"], '
                      '"ent": ["India"], "why": "office holder"}')
    assert plan.needs_web and plan.method == "model"
    assert plan.queries == ["prime minister of India"]
    assert plan.freshness == "live" and plan.entities == ["India"] and plan.reason == "office holder"


def test_parse_plan_forgiving_shapes():
    # single quotes, trailing comma, Python booleans, a <think> block and prose around it
    raw = "<think>needs data</think>Sure: {'web': True, 'q': ['rust 1.90 release notes', 'rust 1.90 release notes'],}"
    plan = parse_plan(raw)
    assert plan.needs_web and plan.queries == ["rust 1.90 release notes"]  # de-duplicated
    # truncated by the token cap: the parser closes it
    plan = parse_plan('{"web": true, "fresh": "recent", "q": ["python 3.14 release date"')
    assert plan.needs_web and plan.queries == ["python 3.14 release date"] and plan.freshness == "recent"
    # alternative key names
    plan = parse_plan('{"needs_web": "yes", "queries": "nvidia stock price", "freshness": "today"}')
    assert plan.needs_web and plan.queries == ["nvidia stock price"] and plan.freshness == "live"
    # at most three queries
    plan = parse_plan('{"web": true, "q": ["a b", "c d", "e f", "g h"]}')
    assert len(plan.queries) == 3


def test_parse_plan_no_search_and_unusable():
    plan = parse_plan('{"web": false, "q": ["ignored"], "why": "chit-chat"}')
    assert not plan.needs_web and plan.queries == []
    assert parse_plan("I think you should search for it.") is None
    assert parse_plan('{"q": ["no decision key"]}') is None
    assert parse_plan("") is None
    # web true without queries: the message itself becomes the query
    plan = parse_plan('{"web": true}', prompt="Can you tell me who won the 2026 Ballon d\'Or?")
    assert plan.needs_web and plan.queries == ["who won the 2026 Ballon d'Or"]


def test_prefilter_skips_trivial_messages():
    for text in ("hi", "Hello there!", "thanks so much", "ok", "Thank you :)", "good morning"):
        assert prefilter(text) is not None and not prefilter(text).needs_web, text
    code = "def f(x):\n    return x + 1\n\nclass A:\n    pass\n"
    assert prefilter(code).reason == "code paste"
    assert prefilter("```py\nprint(1)\n```").reason == "code paste"
    assert prefilter("who is the current prime minister of India") is None
    assert prefilter("hi, who won the match yesterday?") is None


def test_model_decides_not_keywords():
    planner = Planner()
    # The words say "latest news", but the model reads the conversation and
    # decides this is a writing task: no search.
    model = FakeModel(['{"web": false, "why": "creative writing"}'])
    plan = planner.plan("Write a poem titled 'The latest news'", [], model)
    assert not plan.needs_web and plan.method == "model"
    # No live-data keyword at all, but the model knows it needs fresh facts.
    model = FakeModel(['{"web": true, "fresh": "recent", "q": ["Tarique Rahman visit Delhi 2026"], '
                       '"ent": ["Tarique Rahman"]}'])
    plan = planner.plan("Did he end up going to Delhi?", [
        {"role": "user", "content": "Tell me about Tarique Rahman"},
        {"role": "assistant", "content": "Tarique Rahman is a Bangladeshi politician."},
    ], model)
    assert plan.needs_web and plan.queries == ["Tarique Rahman visit Delhi 2026"]
    # The history reached the planner so "he" can be resolved.
    assert "Tarique Rahman" in model.calls[0][1]["content"]


def test_fallback_to_heuristic_when_model_fails():
    planner = Planner()
    plan = planner.plan("who is the current prime minister of India", [], FakeModel(["no json here"]))
    assert plan.needs_web and plan.method == "heuristic"
    assert "no usable JSON" in plan.reason
    assert plan.queries == ["who is the current prime minister of India"]

    class Broken:
        def generate(self, messages):
            raise RuntimeError("engine gone")

    plan = Planner().plan("tell me about the Mumbai port strike", [], Broken())  # not a prefilter case
    assert not plan.needs_web and plan.method == "heuristic"
    assert heuristic_plan("latest news about the port strike").freshness == "live"


def test_prefilter_never_calls_the_model_and_plans_are_cached():
    model = FakeModel(['{"web": true, "q": ["x"]}'] * 5)
    planner = Planner()
    events = Events()
    assert not planner.plan("thanks!", [], model, publish=events.publish).needs_web
    assert model.calls == []
    first = planner.plan("price of gold today", [], model, publish=events.publish)
    second = planner.plan("price of gold today", [], model, publish=events.publish)
    assert first is second and len(model.calls) == 1
    assert [e["method"] for e in events.of("research.plan")] == ["prefilter", "model"]
    assert "prompt" not in events.of("research.plan")[1]  # the message itself is not published


def test_generation_is_capped():
    class KwModel:
        def __init__(self):
            self.kwargs = None

        def generate(self, messages, max_tokens=None):
            self.kwargs = max_tokens
            return '{"web": false}'

    kw = KwModel()
    assert generate_capped(kw, [], 60, 5) == '{"web": false}' and kw.kwargs == 60

    class Streamer:
        def __init__(self):
            self.closed = False
            self.yielded = 0

        def generate(self, messages):  # pragma: no cover - must not be used
            raise AssertionError("streaming must be preferred")

        async def generate_stream(self, messages):
            try:
                for token in ['{"web"', ': true', ', "q": ["a"]', "}", " and then", " a", " lot", " more"]:
                    self.yielded += 1
                    yield token
            finally:
                self.closed = True

    streamer = Streamer()
    out = generate_capped(streamer, [], 60, 5)
    assert out == '{"web": true, "q": ["a"]}' and streamer.yielded == 4 and streamer.closed

    class Endless(Streamer):
        async def generate_stream(self, messages):
            while True:
                self.yielded += 1
                yield "x"

    endless = Endless()
    assert len(generate_capped(endless, [], 60, 5)) == 60


def test_planner_prompt_shape():
    messages = build_messages("and his party?", [{"role": "user", "content": "who is the PM of India"},
                                                 {"role": "assistant", "content": "<think>x</think>Narendra Modi."}],
                              today="Sunday 01 November 2026")
    assert messages[0]["role"] == "system" and "01 November 2026" in messages[0]["content"]
    assert '"web"' in messages[0]["content"]
    assert "Narendra Modi." in messages[1]["content"] and "<think>" not in messages[1]["content"]
    assert messages[1]["content"].rstrip().endswith("JSON:")


class _Search(Tool):
    name = "web_search"
    category = "network"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self):
        self.args = []

    def run(self, args):
        self.args.append(args)
        return ToolResult(ok=True, content="searched " + args["query"])


class _Clock(Tool):
    name = "time_now"

    def run(self, args):
        return ToolResult(ok=True, content="12:00")


def test_runner_uses_the_plan_not_the_regex():
    search = _Search()
    registry = ToolRegistry()
    registry.extend([search, _Clock()])
    runner = ToolRunner(registry, planner=Planner())
    model = FakeModel(['{"web": false, "why": "joke request"}'])
    # "latest" would have triggered the old regex search.
    assert runner.intents("tell me the latest joke you know", model=model) == []
    model = FakeModel(['{"web": true, "fresh": "live", "q": ["Northhaven port strike"]}'])
    assert runner.intents("is the strike over yet?", model=model) == ["web_search"]
    text, results = runner.observe_full("is the strike over yet?", model=model)
    assert len(model.calls) == 1  # intents + observe_full planned once
    assert search.args == [{"query": "Northhaven port strike"}]
    assert "searched Northhaven port strike" in text and results[0]["args"] == {"query": "Northhaven port strike"}
    # weather/time/location keep their own intents
    assert runner.intents("what time is it", model=FakeModel(['{"web": false}'])) == ["time_now"]
    # without any model the heuristic decides (the old regex, as a fallback)
    assert runner.intents("who is the current prime minister of India") == ["web_search"]


def test_conceptual_questions_never_search():
    """Owner report: every "what is" question paid a web search. Definitions and
    how-it-works questions are stable knowledge and skip the planner entirely."""
    for text in ("What is photosynthesis?", "what is a python decorator", "explain recursion",
                 "How does a transistor work", "why is the sky blue", "can you tell me what is a mutex",
                 "what does gdp mean", "What is love"):
        assert conceptual_question(text), text
        plan = prefilter(text)
        assert plan is not None and plan.needs_web is False and plan.method == "prefilter", text
    # Proper nouns, live/recent words, quotes, digits and explicit search requests keep the planner.
    for text in ("What is the current price of bitcoin", "who is the pm of india", "What is Kubernetes used for",
                 "What is the latest version of Node", "what is new in python 3.13", "search what is a monad",
                 "Explain Shubham Mallick", "what is 'imurun'", "What is the population of India"):
        assert not conceptual_question(text), text
