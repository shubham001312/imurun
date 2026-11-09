"""Time to first token: the planner only spends a generation on fast-prefilling
models, and the turn reports how long the user waited for the first token."""
from __future__ import annotations

from services.response.loop import PLANNER_MIN_PREFILL_TPS, AgentLoop


class _Raw:
    def __init__(self, rate):
        self._rate = rate

    def stats(self):
        return {} if self._rate is None else {"prefill_tokens_per_second": self._rate}


def _loop(raw):
    return AgentLoop(object(), raw_model=raw, max_steps=1, compact=False, prefetch=False)


def test_planner_model_only_when_prefill_is_fast():
    assert _loop(_Raw(PLANNER_MIN_PREFILL_TPS + 1))._prefill_fast()
    assert not _loop(_Raw(PLANNER_MIN_PREFILL_TPS - 1))._prefill_fast()
    assert not _loop(_Raw(None))._prefill_fast()          # first turn: speed unknown
    assert not _loop(object())._prefill_fast()            # no stats at all
    assert PLANNER_MIN_PREFILL_TPS >= 300                 # the 8B here prefills at ~23 tok/s


def test_turn_view_has_first_token_seconds():
    from services.server.turns import Turn

    turn = Turn(id="t", session_id="s", prompt="p")
    assert turn.view()["first_token_seconds"] == 0.0
    turn.first_token_seconds = 1.25
    assert turn.view()["first_token_seconds"] == 1.25
