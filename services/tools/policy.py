"""The deterministic pre-fetch rules: weather, clock, location - and the
fallback for web search.

The model can call tools itself (``calls.py`` / ``response/loop.py``), but
that costs a full generation before the first tool even runs. For the
obvious tool cases - "weather in Berlin", "what time is it", "where am I" -
the question is read for intent here, the matching tools run before the
first generation, and their output is injected as a Context block. The model
can still call more tools afterwards.

Web search is different: *whether* to search and *what* to search for is
decided by the model, through the research planner
(``services/research/planner.py``). The ``web_search`` patterns below and
``is_realtime`` are only its fallback, used when no model is available for
planning or the model does not produce a readable plan.

Two things matter as much as the tools themselves:

* the model must be told how to behave: lead with the answer, keep it
  short, use Markdown, never invent a fact;
* the model must be told what to do when a tool fails or the machine is
  offline: say so in one sentence and answer from what it knows.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .base import ToolResult

EM_DASH = "—"

# Patterns that point at a tool. They are deliberately narrow: a search on
# every question would be slow, noisy and wrong.
INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "weather": (
        r"\bweather\b",
        r"\bforecast\b",
        r"\btemperature\b",
        r"\bhumidity\b",
        r"\bwind speed\b",
        r"\bhow (?:hot|cold|warm) is it\b",
        r"\bis it (?:raining|snowing|sunny)\b",
        r"\bwill it (?:rain|snow|be sunny|be cold|be hot)\b",
        r"\bgoing to (?:rain|snow)\b",
        r"\braining\b",
        r"\bsnowing\b",
    ),
    "time_now": (
        r"\bwhat time\b",
        r"\btime is it\b",
        r"\bcurrent time\b",
        # "what is the time (now)", "whats teh time", "tell me the time",
        # "time now?", "the time in London" - the clock was never read for these.
        r"\bwhat(?:'?s|\s+is)?\s+(?:the|teh|da)\s+time\b(?!\s*(?:complexity|limit|frame|series|"
        r"travel|management|cost|signature|zone\s+of\s+a|step|budget|window|scale))",
        r"\b(?:tell|give|show)\s+me\s+(?:the|teh)\s+time\b",
        r"^\W*(?:the\s+)?time\s+(?:now|right\s+now|please)\W*$",
        r"\b(?:the|teh)\s+time\s+(?:now|right\s+now|in\s+[a-z])",
        r"\bwhat(?:'?s|\s+is)\s+(?:the|teh) (?:date|day|week)\b",
        r"\btoday(?:'s)? date\b",
        r"\bday of the week\b",
        r"\bwhat day\b",
        r"\bwhat(?:'s| is) today\b",
    ),
    # FALLBACK ONLY (see the module docstring): the research planner asks the
    # model. These narrow patterns decide only when the model cannot. A web
    # search costs seconds of wall clock before the model can even start, so
    # the fallback fires only for questions about the live world.
    "web_search": (
        r"\bsearch (?:for|the web|online)\b",
        r"\blook up\b",
        r"\blook (?:it|this|that) up\b",
        r"\bgoogle\b",
        r"\bfind (?:out|information|news|details) (?:about|on)\b",
        r"\blatest\b",
        r"\bmost recent\b",
        r"\bnewest\b",
        r"\bnews\b",
        r"\bheadlines\b",
        r"\bcurrent (?:price|version|release|score|status|rate|model|events?)\b",
        r"\btoday'?s (?:price|news|score|headlines|weather)\b",
        r"\b(?:stock|share) price\b",
        r"\bprice of\b",
        r"\bhow much (?:does|do|is|are) .{0,40}\b(?:cost|worth|price)\b",
        r"\bwhen (?:did|does|was|is|will) .{0,60}\b(?:release|released|born|die|died|announced|launch|launched)\b",
        r"\bwho (?:won|is winning|leads)\b",
        r"\bexchange rate\b",
        r"\bwhat happened (?:to|with|in)\b",
        r"\bis .{0,40}\b(?:down|up|out|available) (?:right )?now\b",
        r"\brelease date\b",
        # Offices and roles change: "who is the current prime minister of India",
        # "present CEO of X", "who's the PM now".
        r"\b(?:current|present|incumbent|sitting|new|now|today)\b.{0,40}\b(?:prime minister|pm|president|"
        r"chief minister|minister|governor|chancellor|mayor|ceo|chairman|chairperson|director|"
        r"captain|coach|manager|champion|winner|leader|head of state|king|queen|pope)\b",
        r"\bwho (?:is|'s) the (?:current |present |new )?(?:prime minister|pm|president|chief minister|"
        r"ceo|chairman|chairperson|governor|chancellor|mayor|captain|coach|champion|leader|pope|"
        r"king|queen)\b",
        r"\b(?:right now|these days|at the moment|as of (?:today|now)|this (?:year|month|week))\b",
        r"\b(?:current|present|new|today'?s)\b.{0,20}\b(?:government|cabinet|administration|ruling party|"
        r"parliament|lineup|roster|standings|ranking|rankings|population|census)\b",
    ),
    "location": (
        r"\bwhere am i\b",
        r"\bmy location\b",
        r"\bmy city\b",
        r"\bmy area\b",
        r"\bmy region\b",
        r"\bmy timezone\b",
        r"\bnearby\b",
    ),
}

PLACE_PATTERNS = (
    r"(?:weather|forecast|temperature|humidity|wind|rain|snow)\s+(?:in|at|for|around)\s+([^,?.!]+)",
    r"how (?:hot|cold|warm) is it\s+(?:in|at)\s+([^,?.!]+)",
    r"is it (?:raining|snowing)\s+in\s+([^,?.!]+)",
    r"weather\s+([A-Z][\w' -]+)",
)

STOP_WORDS = {
    "today",
    "tomorrow",
    "now",
    "right now",
    "this week",
    "this morning",
    "this evening",
    "please",
    "at the moment",
    "currently",
    "weather",
    "forecast",
    "temperature",
    "humidity",
    "wind",
    "rain",
    "snow",
    "the",
    "a",
    "an",
    "is",
    "it",
    "in",
    "at",
    "for",
    "and",
    "or",
    "what",
    "how",
}


def detect_intents(text: str) -> list[str]:
    """Tool names worth running for this message, in a stable order."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return []
    found = [
        name
        for name, patterns in INTENT_PATTERNS.items()
        if any(re.search(pattern, lowered) for pattern in patterns)
    ]
    return found


def extract_place(text: str) -> str | None:
    """Pull a place name out of a question about the weather."""
    if not text:
        return None
    for pattern in PLACE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip(" ,.?!'\u201c\u201d\"")
        candidate = re.split(
            r"\b(?:today|tomorrow|now|right now|this week|please|currently)\b",
            candidate,
            flags=re.IGNORECASE,
        )[0].strip(" ,.?!")
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in STOP_WORDS:
            continue
        return candidate
    return None


# Broader than the pre-fetch patterns: anything that asks about the present.
# Used only to decide whether an answer that cites no tool result needs a
# reminder (see ``response/loop.py``), never to run a tool by itself.
REALTIME_PATTERNS = (
    r"\b(today|tonight|tomorrow|yesterday|right now|currently|at the moment|this (?:week|month|year))\b",
    r"\b(latest|newest|most recent|recent|current|live|breaking|up[- ]to[- ]date)\b",
    r"\b(news|headlines|price|prices|stock|shares?|exchange rate|score|scores|weather|forecast)\b",
    r"\bwhat (?:time|day|date) is it\b",
    r"\bwho is (?:the )?(?:current|now)\b",
    r"\bwho (?:is|'s) the (?:current |present |new )?(?:prime minister|pm|president|chief minister|ceo|"
    r"chairman|governor|chancellor|mayor|captain|coach|champion|leader|pope|king|queen)\b",
    r"\b(?:incumbent|sitting|right now|these days|at the moment|as of (?:today|now)|this (?:year|month|week))\b",
    r"\b20[2-3][0-9]\b",
)


def is_realtime(text: str) -> bool:
    """True when the question is about the present (live data)."""
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in REALTIME_PATTERNS)


# The one sentence the model must use when live data is unavailable. It is
# the same wording as the house rule in ``response/style.py``.
OFFLINE_SENTENCE = "I couldn't reach a live source for this just now."


def tool_instructions(
    available: Iterable[Any],
    offline: bool = False,
    *,
    names: Iterable[str] | None = None,
    failed: bool = False,
) -> str:
    """The instruction block that goes into the model context."""
    lines = ["Live facts were looked up with tools for this question."]
    described = []
    for tool in available:
        if names is not None and getattr(tool, "name", None) not in names:
            continue
        description = " ".join(str(getattr(tool, "description", "")).split())
        described.append(f"- {getattr(tool, 'name', 'tool')}: {description}")
    if described:
        lines.append("Tools:")
        lines.extend(described)
    lines.extend(
        [
            "",
            "How to answer:",
            f"- Lead with the answer in one sentence, then only the detail that matters.",
            "- Keep it short. Use Markdown: bullets for lists, a table when comparing.",
            "- Use the Context block as the facts. Do not repeat the whole block.",
            "- Never invent a fact. If a value is unknown, write an em dash (" + EM_DASH + ").",
            "- Do not claim you searched, fetched or checked anything unless the Context says you did.",
        ]
    )
    if offline:
        lines.extend(
            [
                "",
                "This machine has no network access right now, so the web and weather "
                f'tools could not run. Start with one short sentence: "{OFFLINE_SENTENCE}" '
                "Then answer from what you know: slowly-changing facts with an 'as of <year>' "
                "qualifier, no numbers for prices, scores or weather. No other disclaimers - "
                "the app shows that the answer is not live.",
            ]
        )
    elif failed:
        lines.extend(
            [
                "",
                "At least one lookup failed (marked 'failed' in the Context). For that part "
                "answer from what you know with an 'as of <year>' qualifier for slowly-changing "
                "facts, and no numbers for prices, scores or weather. No other disclaimers.",
            ]
        )
    return "\n".join(lines)


def format_observation(name: str, result: ToolResult) -> str:
    """One tool result as a compact block the model can quote."""
    if not result.ok:
        reason = result.error or "the tool failed"
        if result.offline:
            reason = "no network access right now"
        return f"[{name} failed: {reason}]"
    content = (result.content or "").strip()
    if not content:
        return f"[{name} returned nothing]"
    return f"[{name}] {content}"
