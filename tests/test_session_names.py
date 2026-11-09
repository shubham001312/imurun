"""Session names read like titles: derived from the first message, cut at a
word, without markdown or line breaks (services/sessions/session.derive_name)."""
from __future__ import annotations

from services.sessions.session import derive_name


def test_short_question_is_kept_without_trailing_punctuation():
    assert derive_name("What is FISH?") == "What is FISH"


def test_long_message_is_cut_at_a_word_with_an_ellipsis():
    name = derive_name("Explain what FISH streaming does in one paragraph and compare it with llama.cpp in detail")
    assert name.endswith("…")
    assert len(name) <= 49
    assert name == "Explain what FISH streaming does in one…"


def test_only_the_first_line_counts():
    assert derive_name("Fix the login bug\n\nHere is the traceback:\nTraceback ...") == "Fix the login bug"


def test_markdown_and_code_fences_are_stripped():
    assert derive_name("## **Refactor** the `parser` module") == "Refactor the parser module"
    assert derive_name("```python\ndef f(x):\n    return x\n```\nwhy is this slow?") == "Why is this slow"


def test_filler_openings_are_dropped_and_first_letter_capitalised():
    assert derive_name("please summarise this workspace") == "Summarise this workspace"
    assert derive_name("can you draft a release note") == "Draft a release note"
    assert derive_name("hey, how do I add a connector?") == "How do I add a connector"


def test_urls_become_their_host():
    assert derive_name("summarise https://example.com/blog/post-1?x=2 for me") == "Summarise example.com for me"


def test_blank_or_symbol_only_input_falls_back():
    assert derive_name("   ") == "Untitled session"
    assert derive_name("```\n```") == "Untitled session"


def test_control_characters_never_survive():
    assert "\x00" not in derive_name("hello\x00world") and derive_name("hello\x00world") == "Hello world"


def test_shell_paths_keep_their_tilde():
    assert derive_name("!ls ~/Desktop") == "!ls ~/Desktop"
    assert derive_name("~~old~~ new plan") == "Old new plan"
