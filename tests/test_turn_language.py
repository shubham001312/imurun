"""The spoken language rides on the turn and becomes one system-prompt line (REQUESTS #13)."""
from __future__ import annotations

import threading

from services.response.loop import AgentLoop
from services.voice.languages import answer_directive


class _Model:
    def __init__(self):
        self.seen = []

    def generate(self, messages, **kwargs):
        self.seen.append(messages)
        return "ok"

    def generate_stream(self, messages, **kwargs):
        self.seen.append(messages)
        yield "ok"


def _system(messages):
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


def test_language_line_is_added_for_hindi_and_not_for_english():
    for lang, expected in (("hi", True), ("en", False), ("", False), ("xx", False)):
        model = _Model()
        loop = AgentLoop(model, max_steps=1, language=lang, compact=False, prefetch=False)
        loop.run("hello there")
        prompt = _system(model.seen[0])
        assert ("Answer in Hindi" in prompt) is expected, (lang, prompt)
    assert answer_directive("de").startswith("- The user is speaking German")


def test_turn_runner_passes_language(monkeypatch):
    import services.server.turns as turns_mod

    captured = {}

    class FakeLoop:
        def __init__(self, *a, **kw):
            captured.update(kw)

        def set_prior_messages(self, m):
            pass

    monkeypatch.setattr(turns_mod, "AgentLoop", FakeLoop)
    runner = turns_mod.TurnRunner.__new__(turns_mod.TurnRunner)
    # Only what start() touches.
    runner._model = object()
    monkeypatch.setattr(turns_mod.TurnRunner, "available", property(lambda self: True))
    runner._invoke_skills = lambda prompt: ([], prompt)
    runner._context_limit = lambda: 4096
    runner._prune_turns = lambda: None
    runner._publishing_model = lambda turn, holder: object()
    runner._steps_for_turn = lambda: 1
    runner._run = lambda *a: None
    for name in ("_queue", "_audit", "_kill", "_persona", "_tools", "_executor", "_transcripts", "_events"):
        setattr(runner, name, None)
    runner._lock = threading.Lock()
    runner._turns, runner._loops = {}, {}
    turn = runner.start("s1", "hallo", language="de")
    assert turn.language == "de" and turn.view()["language"] == "de"
    assert captured["language"] == "de"
