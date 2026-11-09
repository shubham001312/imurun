"""Time questions read the clock. From the field: "what is teh time now" got
"The current time is 09:00:00 AM." - the detector missed the phrasing, so the
time_now tool never ran and a small model made a time up."""
from __future__ import annotations

import pytest

from services.tools.policy import detect_intents


@pytest.mark.parametrize("text", [
    "what is teh time now", "what is the time now", "what time is it", "time now?",
    "whats the time", "current time please", "what time is it in london", "tell me the time",
    "what is the date today", "whats the date", "the time in london?", "what is the time",
])
def test_time_questions_read_the_clock(text):
    assert "time_now" in detect_intents(text)


@pytest.mark.parametrize("text", [
    "what is the time complexity of quicksort", "how much time does it take to build",
    "it is time now to refactor this module", "save time with this trick",
    "explain the timeline", "what is the time limit for this api",
])
def test_other_uses_of_time_do_not(text):
    assert "time_now" not in detect_intents(text)
