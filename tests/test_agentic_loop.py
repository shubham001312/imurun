"""The multi-step agent loop with a scripted fake model."""
from __future__ import annotations

import json

import pytest

from services.prompts.queue import PromptQueue
from services.response.context import estimate_tokens
from services.response.loop import AgentLoop
from services.server.settings import SettingsStore
from services.tools.base import Tool, ToolResult
from services.tools.builtin.agent import RunAgentTool
from services.tools.executor import ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


def call(name, **args):
    return "```tool\n" + json.dumps({"name": name, "args": args}) + "\n```"


class FakeModel:
    """Returns scripted replies; a callable entry is called with the messages."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            return "(out of script)"
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class Echo(Tool):
    name = "echo"
    category = "info"
    description = "Echo the text back."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self):
        self.calls = 0

    def run(self, args):
        self.calls += 1
        return ToolResult(ok=True, content=f"echo: {args['text']}")


class Big(Tool):
    name = "big"
    description = "Returns a lot of text."
    parameters = {"type": "object", "properties": {}}

    def run(self, args):
        return ToolResult(ok=True, content="x" * 50_000)


class Stopper(Tool):
    name = "stopper"
    description = "Stops the loop."
    parameters = {"type": "object", "properties": {}}
    loop = None

    def run(self, args):
        Stopper.loop.stop()
        return ToolResult(ok=True, content="stopping")


class WebSearchFake(Tool):
    name = "web_search"
    category = "network"
    description = "Search the web."
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def run(self, args):
        return ToolResult(ok=True, content="Python 3.14.0 was released on 7 October 2025.", data={
            "sources": [{"title": "Python.org", "url": "https://www.python.org/downloads/",
                         "provider": "brave", "snippet": "3.14.0"}]})


class Events:
    def __init__(self):
        self.items: list[tuple[str, dict]] = []

    def publish(self, kind, data):
        self.items.append((kind, data))
        return 1

    def kinds(self):
        return [k for k, _ in self.items]


@pytest.fixture()
def setup(tmp_path):
    registry = ToolRegistry()
    echo = Echo()
    registry.extend([echo, Big(), Stopper(), WebSearchFake(), RunAgentTool()])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    events = Events()
    executor = ToolExecutor(registry, policy=PermissionPolicy(settings), publish=events.publish)
    return executor, echo, events


def make_loop(model, executor, events=None, **kw):
    return AgentLoop(model, executor=executor, events=events, session_id="s", turn_id="t", **kw)


def test_tool_then_answer(setup):
    executor, echo, events = setup
    model = FakeModel([call("echo", text="hi"), "The tool said hi."])
    result = make_loop(model, executor, events).run("say hi with the tool")
    assert result.status == "done" and result.answer == "The tool said hi."
    assert [s.kind for s in result.steps] == ["tool", "answer"]
    assert echo.calls == 1 and result.tool_calls == 1
    fed_back = model.seen[1][-1]
    assert fed_back["role"] == "user" and fed_back["content"].startswith("[tool result: echo]")
    assert "echo: hi" in fed_back["content"]
    # The canonical call is what the model sees in history, not its raw text.
    assert model.seen[1][-2] == {"role": "assistant", "content": call("echo", text="hi")}
    assert "tool.call" in events.kinds() and "tool.result" in events.kinds()
    system = model.seen[0][0]["content"]
    assert "# Tools" in system and "- echo" in system and "Facts about the present" in system
    # The app shows verification; the model must not write disclaimers.
    assert "Never write disclaimers" in system
    assert result.new_messages == ({"role": "assistant", "content": "The tool said hi."},)


def test_step_budget_is_a_hard_cap(setup):
    executor, echo, _ = setup
    model = FakeModel([call("echo", text=str(i)) for i in range(10)])
    result = make_loop(model, executor, max_steps=4).run("loop forever")
    assert len(model.seen) == 4
    assert result.status == "max-steps"
    assert "[note] No more tool calls" in model.seen[-1][-1]["content"]
    assert echo.calls == 3  # the last step never runs tools
    assert "tool step limit" in result.answer


def test_unreadable_call_retries_once(setup):
    executor, echo, _ = setup
    model = FakeModel(["```tool\nplease echo hi\n```", call("echo", text="hi"), "done"])
    result = make_loop(model, executor).run("echo hi")
    assert [s.kind for s in result.steps] == ["retry", "tool", "answer"]
    assert "[tool error]" in model.seen[1][-1]["content"]
    assert echo.calls == 1 and result.answer == "done"


def test_second_unreadable_call_is_returned_as_text(setup):
    executor, _, _ = setup
    model = FakeModel(["```tool\nnope\n```", "```tool\nstill nope\n```"])
    result = make_loop(model, executor).run("echo")
    assert result.status == "done" and len(model.seen) == 2


def test_duplicate_call_uses_cache(setup):
    executor, echo, _ = setup
    model = FakeModel([call("echo", text="a"), call("echo", text="a"), "ok"])
    make_loop(model, executor).run("x")
    assert echo.calls == 1
    assert "already made" in model.seen[2][-1]["content"]


def test_stop_between_steps(setup):
    executor, _, _ = setup
    model = FakeModel([call("stopper"), "never reached"])
    loop = make_loop(model, executor)
    Stopper.loop = loop
    result = loop.run("stop")
    assert result.status == "stopped" and len(model.seen) == 1


def test_r1_think_is_stripped_from_parsing_and_history(setup):
    executor, echo, _ = setup
    model = FakeModel([
        "<think>I should call echo</think>" + call("echo", text="r1"),
        "<think>got it</think>Echoed r1.",
    ])
    loop = make_loop(model, executor)
    loop.set_prior_messages([
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "<think>old thought</think>Earlier answer."},
    ])
    result = loop.run("echo r1")
    assert echo.calls == 1
    assert result.answer == "<think>got it</think>Echoed r1."  # the UI keeps the thought
    history = [m["content"] for m in model.seen[0] if m["role"] == "assistant"]
    assert history == ["Earlier answer."]


def test_think_only_reply_gets_one_answer_now(setup):
    executor, _, _ = setup
    model = FakeModel(["<think>hmm, endless thinking", "The answer is 4."])
    result = make_loop(model, executor).run("what is 2+2")
    assert result.answer == "The answer is 4." and "final answer now" in model.seen[1][-1]["content"]


def test_realtime_question_without_tool_is_marked_unverified(setup):
    """The nudge was retired: a live question answered without any tool result
    is reported as `verification == "unverified"` so the UI shows a badge,
    instead of the model being asked to write an admission into the answer."""
    executor, _, _ = setup
    model = FakeModel(["The latest Python is 3.9."])
    result = make_loop(model, executor).run("What is the latest version of Python?")
    assert [s.kind for s in result.steps] == ["answer"]
    assert result.verification == "unverified" and result.sources == ()


def test_tool_result_marks_turn_verified_and_collects_sources(setup):
    executor, _, _ = setup
    model = FakeModel([call("web_search", query="latest python"), "Python 3.14.0."])
    result = make_loop(model, executor).run("What is the latest version of Python?")
    assert result.verification == "verified"
    assert [s["domain"] for s in result.sources] == ["python.org"]
    assert result.sources[0]["url"] == "https://www.python.org/downloads/"


def test_invented_tool_result_is_retried_then_scrubbed(setup):
    """A reply that *is* a fake "[tool result: …]" gets one retry; a real call
    then works, and the final answer carries no disclaimer sentences."""
    executor, _, _ = setup
    model = FakeModel([
        "[tool result: web_search]\nShubham is a researcher. As of my knowledge cutoff in 2023, that is all.",
        call("web_search", query="Shubham Mallick"),
        "Python 3.14.0 is the latest release. I'm a large language model, so verify this.",
    ])
    result = make_loop(model, executor).run("who is Shubham Mallick?")
    assert [s.kind for s in result.steps] == ["retry", "tool", "answer"]
    assert "[note] You wrote a tool result yourself" in model.seen[1][-1]["content"]
    assert result.answer == "Python 3.14.0 is the latest release."
    assert result.verification == "verified"


def test_non_live_question_is_not_a_verification_matter(setup):
    executor, _, _ = setup
    result = make_loop(FakeModel(["A monad is a monoid in the category of endofunctors."]), executor).run(
        "what is a monad")
    assert result.verification == "n/a" and result.sources == ()


def test_no_nudge_when_answer_admits_or_question_is_not_live(setup):
    executor, _, _ = setup
    model = FakeModel(["I can't check live data right now."])
    assert len(make_loop(model, executor).run("What is the latest Python version?").steps) == 1
    model = FakeModel(["Paris."])
    assert len(make_loop(model, executor).run("What is the capital of France?").steps) == 1


def test_context_budget_shrinks_big_results(setup):
    executor, _, _ = setup
    model = FakeModel([call("big"), "summary"])
    make_loop(model, executor, context_limit=2048).run("get big")
    assert estimate_tokens(model.seen[1]) <= 2048


def test_queued_prompt_delivered_at_boundary(setup):
    executor, _, _ = setup
    queue = PromptQueue()
    model = FakeModel([lambda m: (queue.push("s", "and another?"), "first")[1], "second"])
    result = make_loop(model, executor, queue=queue).run("one")
    assert result.answer == "second"
    assert [m["content"] for m in result.new_messages] == ["first", "and another?", "second"]


def test_sub_agent_runs_nested_and_respects_depth(setup):
    executor, echo, events = setup

    def sub_agent_prompt_has_no_run_agent_at_depth_2(messages):
        return "unused"

    model = FakeModel([
        call("run_agent", task="echo sub", tools=["echo", "run_agent"]),  # depth 0
        call("echo", text="from sub"),                                    # depth 1
        "Sub summary: echoed 'from sub'.",                                # depth 1 answer
        "Main: the helper echoed it.",                                    # depth 0 answer
    ])
    result = make_loop(model, executor, events).run("delegate")
    assert result.answer == "Main: the helper echoed it."
    assert echo.calls == 1
    sub_system = model.seen[1][0]["content"]
    assert "helper agent" in sub_system and "- echo" in sub_system
    assert "- run_agent" in sub_system  # depth 1 may still delegate once more
    assert "Sub summary" in model.seen[3][-1]["content"]
    assert "agent.start" in events.kinds() and "agent.end" in events.kinds()
    depths = {d.get("depth") for k, d in events.items if k == "tool.call"}
    assert depths == {0, 1}


def test_depth_two_has_no_run_agent(setup):
    executor, _, _ = setup
    assert "run_agent" not in [t.name for t in executor.offered(depth=2)]
    assert "run_agent" in [t.name for t in executor.offered(depth=1)]


def test_single_step_loop_offers_no_tools(setup):
    executor, _, _ = setup
    model = FakeModel(["plain"])
    result = make_loop(model, executor, max_steps=1).run("hi")
    assert result.answer == "plain" and "# Tools" not in model.seen[0][0]["content"]


def test_skills_are_injected(setup, tmp_path):
    from services.skills.invoke import find_invocations
    from services.skills.store import SkillsStore

    executor, _, _ = setup
    store = SkillsStore(tmp_path / "skills")
    store.create("Haiku", "Answer as a haiku", "Always answer in a 5-7-5 haiku.")
    found = find_invocations("/haiku tell me about rain", store.list())
    assert found.ids == ["haiku"] and found.prompt == "tell me about rain"
    assert find_invocations("see /usr/bin and /nothing", store.list()).ids == []
    model = FakeModel(["rain falls"])
    make_loop(model, executor, skills=found.skills).run(found.prompt)
    assert "5-7-5 haiku" in model.seen[0][0]["content"]
    assert store.delete("haiku") and store.get("haiku") is None
