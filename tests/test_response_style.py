"""Answer hygiene: disclaimers and invented tool results never reach the user."""
from __future__ import annotations

from services.response.style import DEFAULT_DIRECTIVE, DICTATION_RULE, looks_like_fake_tool_result, scrub_disclaimers


def test_scrub_removes_cutoff_sentences_but_keeps_facts():
    text = ("Narendra Modi is the Prime Minister of India, in office since May 2014. "
            "As of my knowledge cutoff in October 2023, that was the case. "
            "Please note that I'm a large language model and don't have real-time access to current information.")
    out = scrub_disclaimers(text)
    assert out == "Narendra Modi is the Prime Minister of India, in office since May 2014."


def test_scrub_removes_bracket_notes_and_keeps_code():
    text = "The current PM is Modi [note: unable to verify].\n\n```python\nprint('training data')\n```"
    out = scrub_disclaimers(text)
    assert "[note" not in out
    assert "print('training data')" in out
    assert out.startswith("The current PM is Modi.")


def test_scrub_never_returns_empty():
    text = "I don't have real-time access to that."
    assert scrub_disclaimers(text) == text


def test_fake_tool_result_detection():
    assert looks_like_fake_tool_result("[tool result: web_search]  \nShubham is ...")
    assert looks_like_fake_tool_result("Tool result: nothing found")
    assert not looks_like_fake_tool_result("The result of 2+2 is 4.")
    assert not looks_like_fake_tool_result("```tool\n{\"name\": \"web_search\"}\n```")


def test_dictation_rule_is_part_of_the_house_style():
    assert DICTATION_RULE in DEFAULT_DIRECTIVE
    assert "speech-to-text" in DICTATION_RULE and "never comment on the spelling" in DICTATION_RULE
