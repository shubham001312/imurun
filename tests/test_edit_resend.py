"""Edit and resend: drop a user message (counted from the end) and what followed."""
from __future__ import annotations

import pytest

from services.sessions.transcript import TranscriptStore


@pytest.fixture
def store(tmp_path):
    s = TranscriptStore(tmp_path)
    for role, text in [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"),
                       ("user", "q3"), ("assistant", "a3")]:
        s.append("s1", role, text)
    return s


def contents(store):
    return [m.content for m in store.read("s1")]


def test_editing_the_last_message_drops_it_and_its_answer(store):
    assert store.truncate_at_user("s1", 1) == 2
    assert contents(store) == ["q1", "a1", "q2", "a2"]


def test_editing_an_earlier_message_drops_everything_after_it(store):
    assert store.truncate_at_user("s1", 3) == 6
    assert contents(store) == []


@pytest.mark.parametrize("bad", [0, -1, 4])
def test_out_of_range_is_refused_and_nothing_changes(store, bad):
    with pytest.raises(ValueError):
        store.truncate_at_user("s1", bad)
    assert len(contents(store)) == 6


def test_the_next_append_continues_normally(store):
    store.truncate_at_user("s1", 1)
    store.append("s1", "user", "q3 edited")
    assert contents(store)[-1] == "q3 edited"
