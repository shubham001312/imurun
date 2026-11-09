"""The house style.

Every answer follows the same rules, whether a persona is configured or
not. The rules are short because a small model follows short rules:

* the first sentence is the answer;
* no filler, no restating the question;
* Markdown when it carries meaning - bullets, tables, fenced code;
* an unknown value is an em dash, never a guess;
* nothing is invented, and the model says when it does not know;
* live data (the present) only comes from a tool result in this
  conversation - without one, the answer says so instead of guessing. This
  is the rule behind "the assistant gives inappropriate answers about
  real-time data": a model trained months ago will otherwise state an old
  price, score or "latest version" as if it were current.

``LIVE_DATA_RULE`` uses the same wording as the pre-fetch offline sentence
(``tools/policy.py`` ``OFFLINE_SENTENCE``) so the model sees one rule, not
two that disagree.
"""

EM_DASH = "—"

LIVE_DATA_RULE = (
    "- Facts about the present: when a [tool result] or Context block in this conversation "
    "has them, use those values and nothing else. When it does not: for things that change "
    "slowly (who holds an office, a company's CEO, a product's current version) give your "
    "best-known answer with a short 'as of <year>' qualifier; for things that change fast "
    "(prices, scores, weather, exchange rates, the time) do not state a number - say in one "
    "short sentence that it needs a live check."
)

# The app shows the verification state of every answer itself (a badge and
# source chips), so the model must never write it as prose. Small models
# otherwise produce paragraphs about their training data or notes like
# "[note: unable to verify]" in the middle of the answer.
NO_DISCLAIMER_RULE = (
    "- Never write disclaimers about your training data, knowledge cutoff, being an AI, or "
    "not having internet access, and never add bracketed notes such as [note: ...] or "
    "[unverified]. The app displays whether an answer was verified against a live source."
)

# The owner dictates prompts through speech-to-text, so prompts arrive with
# homophones, dropped words and misspelt names ("five 3" for Phi-3, "immurin"
# for IMURUN, "RTTS" for TTS). The model reads them by meaning and never
# lectures about spelling.
DICTATION_RULE = (
    "- Prompts may be dictated (speech-to-text): expect misspellings, homophones and dropped "
    "words. Read them by meaning, use the intended term in your answer, and never comment on "
    "the spelling. Ask one short question only when two readings lead to different answers."
)

import re as _re

# Sentences a small model still writes despite NO_DISCLAIMER_RULE. They are
# removed from the final visible answer only (never from reasoning), and only
# whole sentences, so the factual content around them is untouched.
_DISCLAIMER_SENTENCE = _re.compile(
    r"(?<![\w\]])[^.!?\n]*?(?:"
    r"knowledge[ -]cutoff|training data|my training|last update(?:d)?\b|"
    r"as an ai(?: language)? model|i am an ai\b|i'm an ai\b|large language model|"
    r"(?:do not|don't|cannot|can't) have (?:real[- ]time|live|current|internet|browsing) (?:access|information|data|capabilit)|"
    r"(?:no|without) (?:real[- ]time|live|internet) access"
    r")[^.!?\n]*[.!?]?",
    _re.IGNORECASE,
)
_BRACKET_NOTE = _re.compile(r"\s*\[(?:note|unverified|verification)[^\]]*\]", _re.IGNORECASE)
_FAKE_TOOL_RESULT = _re.compile(r"^\s*(?:\[tool result[^\]]*\]|tool result:|result:)\s*", _re.IGNORECASE)


def scrub_disclaimers(text: str) -> str:
    """Remove knowledge-cutoff / "I'm an AI" sentences and bracket notes from
    an answer. The app shows verification itself; these sentences only hide
    the answer the user asked for. Code blocks are left alone."""
    if not text:
        return text
    parts = _re.split(r"(```.*?```)", text, flags=_re.DOTALL)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # fenced code
            out.append(part)
            continue
        part = _BRACKET_NOTE.sub("", part)
        part = _DISCLAIMER_SENTENCE.sub("", part)
        part = _re.sub(r"[ \t]{2,}", " ", part)
        part = _re.sub(r"\n{3,}", "\n\n", part)
        out.append(part)
    cleaned = "".join(out).strip()
    # Never return nothing for something: if scrubbing ate the whole answer,
    # keep the original (the badge still says it is unverified).
    return cleaned if cleaned else text


def looks_like_fake_tool_result(text: str) -> bool:
    """True when the model *wrote* a tool result instead of calling a tool
    (e.g. a reply that starts with "[tool result: web_search]")."""
    return bool(text) and bool(_FAKE_TOOL_RESULT.match(text))


FAKE_RESULT_NOTE = (
    "[note] You wrote a tool result yourself instead of calling the tool. Do not invent "
    "tool output. Either call the tool now (one ```tool block, nothing else) or answer "
    "from what you know, without disclaimers."
)

DEFAULT_DIRECTIVE = "\n".join(
    [
        "You are Imurun, a local-first assistant that runs on this machine.",
        "Answer style:",
        "- Lead with the answer. The first sentence is the point.",
        "- Be concise. No filler, no restating the question, no preamble.",
        "- Use Markdown when it helps: bullets for lists, a table when comparing, fenced code for code.",
        "- Write an unknown value as an em dash, never as a guess.",
        "- Never invent facts. If you do not know something, say so plainly.",
        LIVE_DATA_RULE,
        NO_DISCLAIMER_RULE,
        DICTATION_RULE,
    ]
)
