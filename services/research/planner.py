"""Query planning: the model decides whether to search, and what for.

Before the answer, one short *routing generation* with the same local model
reads the last user message (plus a little history, to resolve "he" / "it")
and writes one JSON object::

    {"web": true, "fresh": "live", "q": ["prime minister of India"],
     "ent": ["India"], "why": "office holder can change"}

which becomes a :class:`~services.research.types.Plan` (``needs_web``,
``freshness``, ``queries`` 1-3, ``entities``, ``reason``). There is no
keyword list deciding *whether* to search; the regex intents in
``tools/policy.py`` are only the fallback when the model is missing or does
not produce JSON.

Cost control:

* a cheap pre-filter skips the generation for messages that are trivially
  not a question about the world (greetings, thanks, a code paste);
* output is capped at ``planner_max_tokens()`` (60) - by a ``max_tokens``
  argument when the model client accepts one, else by streaming and stopping
  as soon as the JSON object closes (the engine is cancelled cleanly when the
  stream is closed early, ``fish_native._stream``);
* the plan is cached per (message, history) hash, so ``intents()`` and
  ``observe_full()`` in the same turn never generate twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from . import config
from .events import emit
from .types import FRESHNESS, Plan

CACHE_SIZE = 64
CACHE_TTL = 300.0
HISTORY_TURNS = 4
HISTORY_CHARS = 280

PLANNER_SYSTEM = """You plan web research for an assistant. Today is {today}.
Decide whether answering the user's LAST message accurately needs facts from the web.
Reply with ONE JSON object and nothing else, in this exact shape:
{{"web": true, "fresh": "live", "q": ["search query"], "ent": ["name"], "why": "few words"}}
Rules:
- web: true when the answer depends on facts that change or that you may not know for sure: news, who holds an office now, prices, scores, releases and versions, schedules, recent events, specific people, companies or products, or when the user asks you to search or look something up. false for greetings, chit-chat, opinions, writing, coding, maths, translation and stable textbook knowledge.
- Definitions and explanations are stable knowledge: "what is photosynthesis", "what is a Python decorator", "how does a transistor work", "explain recursion" -> web: false. "who is the prime minister of India", "price of bitcoin", "latest Node version", "who is <a specific person>" -> web: true.
- fresh: "live" (changes daily or weekly: news, prices, scores, who holds an office now), "recent" (this year), "any" (stable).
- q: 1 to 3 short search-engine queries (keywords, no question words). Replace "he", "it", "that" with the real names from the conversation. Add the year for recent events. Empty list when web is false.
- ent: the people, places, organisations or products the question is about.
- why: at most six words."""

# ------------------------------------------------------------- pre-filter
_GREETING = re.compile(
    r"^\s*(?:hi+|hello+|hey+|hiya|yo|howdy|greetings|good (?:morning|afternoon|evening|night)|"
    r"thanks?(?: you)?(?: so much| a lot| again)?|thx|ty|cheers|ok(?:ay)?|k|cool|great|nice|awesome|"
    r"perfect|got it|understood|sure|yes|yep|yeah|no|nope|bye|goodbye|see you|lol|haha+|wow|"
    r"sounds good|that'?s (?:great|fine|all|it)|never ?mind|nvm)"
    r"(?:[\s,]+(?:there|imurun|buddy|mate|friend|man|again|everyone|all))?\s*[!.?\s:)(]*$",
    re.IGNORECASE,
)
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \S+ import|#include|function |const |let |var |public |private |"
    r"return\b|if\s*\(|for\s*\(|while\s*\(|\}|\{|</?\w+[^>]*>|//|/\*|\*|@\w+|SELECT |INSERT |UPDATE )"
    r"|[;{}]\s*$",
    re.IGNORECASE,
)


# "What is X?" / "how does X work" / "explain X" about a common noun is a
# textbook question. Small (and not so small) local models answered
# `web: true` to "What is photosynthesis?", so every such question paid a
# search round-trip and got pasted passages (symptom reported by the owner:
# "for every 'what is' it is making a web search"). A proper noun, quotes,
# live or recent wording, or an explicit "search"/"look up" keep the planner.
_CONCEPTUAL = re.compile(
    r"^\s*(?:(?:hey|hi|ok(?:ay)?|so|please|pls|imurun)[,\s]+)*"
    r"(?:(?:can|could|would) you (?:please )?)?(?:tell me |explain (?:to me )?)?"
    r"(?:what(?:'s| is| are| was| were| does| do)|explain|define|describe|"
    r"how (?:do|does|did|to|can|is|are|would)|why (?:do|does|did|is|are))\b",
    re.IGNORECASE,
)
_ASKS_TO_SEARCH = re.compile(r"\b(?:search|look (?:it )?up|google|browse|online|on the web|internet)\b", re.IGNORECASE)


def conceptual_question(text: str) -> bool:
    """True for a definitional / how-it-works question with no sign of
    needing the live web (no live or recent wording, no proper noun other
    than the first word, no quotes, no digits, no request to search)."""
    message = " ".join((text or "").split())
    if not message or len(message) > 240 or not _CONCEPTUAL.match(message):
        return False
    if _LIVE_WORDS.search(message) or _RECENT_WORDS.search(message) or _ASKS_TO_SEARCH.search(message):
        return False
    if re.search(r"[\"“‘']|\d", message):
        return False
    body = message[0].lower() + message[1:]
    return not guess_entities(body)


def prefilter(text: str) -> Plan | None:
    """A no-search plan for messages that are trivially not about the world.

    Returns ``None`` when the message needs the planner.
    """
    message = (text or "").strip()
    if not message:
        return Plan(False, "empty message", method="prefilter")
    if _GREETING.match(message):
        return Plan(False, "greeting or acknowledgement", method="prefilter")
    if conceptual_question(message):
        return Plan(False, "definition or explanation: stable knowledge", method="prefilter")
    if "```" in message:
        return Plan(False, "code paste", method="prefilter")
    lines = [line for line in message.splitlines() if line.strip()]
    if len(lines) >= 4:
        code = sum(1 for line in lines if _CODE_LINE.search(line))
        if code / len(lines) >= 0.4:
            return Plan(False, "code paste", method="prefilter")
    if len(message) > 4000:
        return Plan(False, "long pasted text", method="prefilter")
    return None


# --------------------------------------------------------------- parsing
def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("true", "yes", "y", "1", "search", "web"):
        return True
    if text in ("false", "no", "n", "0", "none", "null", ""):
        return False
    return None


def _str_list(value: Any, limit: int, max_chars: int = 120) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split()).strip(" \"'")[:max_chars]
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def parse_plan(raw: str, prompt: str = "") -> Plan | None:
    """Read the planner's JSON. ``None`` when there is no usable decision."""
    from ..tools.calls import loads_forgiving, strip_think

    text = strip_think(raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    value, _error = loads_forgiving(text[start:])
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in value.items()}
    needs = None
    for key in ("web", "needs_web", "search", "need_web", "use_web"):
        if key in lowered:
            needs = _as_bool(lowered[key])
            break
    if needs is None:
        return None
    queries: list[str] = []
    for key in ("q", "queries", "query", "searches"):
        if key in lowered:
            queries = _str_list(lowered[key], 3)
            break
    entities: list[str] = []
    for key in ("ent", "entities", "entity", "names"):
        if key in lowered:
            entities = _str_list(lowered[key], 6, 80)
            break
    fresh = str(lowered.get("fresh", lowered.get("freshness", "any")) or "any").strip().lower()
    if fresh not in FRESHNESS:
        fresh = {"now": "live", "today": "live", "current": "live", "daily": "live",
                 "year": "recent", "new": "recent", "latest": "recent"}.get(fresh, "any")
    reason = " ".join(str(lowered.get("why", lowered.get("reason", "")) or "").split())[:120]
    if needs and not queries:
        fallback = simplify_query(prompt)
        queries = [fallback] if fallback else []
    if not needs:
        queries = []
    return Plan(bool(needs) and bool(queries), reason, queries, fresh, entities, method="model", raw=raw)


# ------------------------------------------------------------- heuristic
_FILLER = re.compile(
    r"^(?:(?:hey|hi|ok(?:ay)?|so|please|pls|imurun)[,\s]+)*"
    r"(?:(?:can|could|would|will) you (?:please )?|please |i (?:want|need) (?:to know|you to) |"
    r"(?:do you know|tell me|let me know|find out|look up|search(?: the web| online)?(?: for)?|google|"
    r"check(?: online)?)\s+)*",
    re.IGNORECASE,
)
_ENTITY = re.compile(r"\b([A-Z][\w&.'-]*(?:\s+(?:of|the|and|de|la|von|van|[A-Z][\w&.'-]*))*\s*[A-Z]?[\w&.'-]*)")
_LIVE_WORDS = re.compile(r"\b(?:now|today|tonight|current(?:ly)?|latest|live|breaking|right now|price|score|"
                         r"news|headlines|this week|at the moment|incumbent|sitting)\b", re.IGNORECASE)
_RECENT_WORDS = re.compile(r"\b(?:recent(?:ly)?|new|newest|this (?:year|month)|20[2-3]\d)\b", re.IGNORECASE)


def simplify_query(prompt: str) -> str:
    """A search-engine query from a chat message (filler and '?' removed)."""
    text = " ".join((prompt or "").split())
    text = _FILLER.sub("", text).strip()
    text = re.sub(r"[?!.]+$", "", text).strip()
    return text[:160]


def guess_entities(prompt: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"[\"“']([^\"”']{2,60})[\"”']", prompt or ""):
        found.append(match.group(1).strip())
    words = (prompt or "").split()
    for match in _ENTITY.finditer(prompt or ""):
        value = match.group(1).strip(" .'")
        first = words[0].strip("\"'“") if words else ""
        if not value or value in ("I",) or (value == first and len(value.split()) == 1):
            continue
        if value.lower() not in {f.lower() for f in found}:
            found.append(value)
    return found[:6]


def _ONLY_TIME_WORDS(text: str) -> bool:
    """is_realtime matched only on a time word ("now", "today", a year)."""
    from ..tools.policy import REALTIME_PATTERNS

    lowered = (text or "").lower()
    hits = [p for p in REALTIME_PATTERNS if re.search(p, lowered)]
    time_only = (REALTIME_PATTERNS[0], REALTIME_PATTERNS[-1], REALTIME_PATTERNS[-2])
    return bool(hits) and all(p in time_only for p in hits)


def heuristic_plan(prompt: str, history: list[dict] | None = None) -> Plan:
    """The fallback when the model cannot plan: the regex live-data rules."""
    from ..tools.policy import detect_intents, is_local, is_realtime

    text = prompt or ""
    # Never for a question about this computer, and a bare "now" / "today" is
    # not a reason to search (it only says the user wants it current).
    needs = "web_search" in detect_intents(text) or (is_realtime(text) and not is_local(text)
                                                      and not _ONLY_TIME_WORDS(text))
    if _LIVE_WORDS.search(text):
        fresh = "live"
    elif _RECENT_WORDS.search(text):
        fresh = "recent"
    else:
        fresh = "any"
    query = simplify_query(text)
    return Plan(
        needs_web=bool(needs and query),
        reason="live-data wording" if needs else "no live-data wording",
        queries=[query] if needs and query else [],
        freshness=fresh,
        entities=guess_entities(text),
        method="heuristic",
    )


# ------------------------------------------------------------ generation
def build_messages(prompt: str, history: list[dict] | None = None, today: str | None = None) -> list[dict]:
    today = today or time.strftime("%A %d %B %Y")
    lines: list[str] = []
    recent = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-HISTORY_TURNS:]
    if recent:
        from ..tools.calls import strip_think

        lines.append("Conversation so far:")
        for message in recent:
            content = " ".join(strip_think(str(message.get("content") or "")).split())
            if len(content) > HISTORY_CHARS:
                content = content[:HISTORY_CHARS] + " ..."
            lines.append(f"{str(message.get('role')).capitalize()}: {content}")
        lines.append("")
    lines.append("Last message: " + " ".join((prompt or "").split())[:1200])
    lines.append("")
    lines.append("JSON:")
    return [
        {"role": "system", "content": PLANNER_SYSTEM.format(today=today)},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _json_closed(text: str) -> bool:
    """True once the first top-level JSON object in ``text`` is complete."""
    start = text.find("{")
    if start < 0:
        return False
    depth = 0
    quote = False
    escaped = False
    for char in text[start:]:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return True
    return False


def _accepts_max_tokens(fn: Callable) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "max_tokens" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def generate_capped(model: Any, messages: list[dict], max_tokens: int, timeout: float) -> str:
    """One short generation. Never more than ``max_tokens`` when the client
    lets us cap it, and never longer than ``timeout`` when it streams."""
    generate = getattr(model, "generate", None)
    if callable(generate) and _accepts_max_tokens(generate):
        return str(generate(messages, max_tokens=max_tokens) or "")
    stream = getattr(model, "generate_stream", None)
    if callable(stream):
        async def collect() -> str:
            parts: list[str] = []
            started = time.monotonic()
            agen = stream(messages)
            try:
                async for chunk in agen:
                    parts.append(str(chunk))
                    joined = "".join(parts)
                    if len(parts) >= max_tokens or _json_closed(joined.split("</think>")[-1]):
                        break
                    if time.monotonic() - started > timeout:
                        break
            finally:
                closer = getattr(agen, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001 - closing cancels the engine; best effort
                        pass
            return "".join(parts)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(collect())
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, collect()).result()
    if callable(generate):
        return str(generate(messages) or "")
    raise TypeError("the model has no generate method")


def plan_key(prompt: str, history: list[dict] | None = None) -> str:
    digest = hashlib.sha1()
    digest.update(" ".join((prompt or "").split()).encode("utf-8"))
    for message in (history or [])[-HISTORY_TURNS:]:
        digest.update(b"\x00" + str(message.get("role")).encode() + b":" +
                      str(message.get("content") or "")[:HISTORY_CHARS].encode("utf-8", "replace"))
    return digest.hexdigest()


class Planner:
    """Plans with the model when one is given, else heuristically. Cached."""

    def __init__(self, *, ttl: float = CACHE_TTL, size: int = CACHE_SIZE) -> None:
        self._cache: OrderedDict[str, tuple[float, Plan]] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._size = size

    def cached(self, prompt: str, history: list[dict] | None = None) -> Plan | None:
        key = plan_key(prompt, history)
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
        return None

    def plan(
        self,
        prompt: str,
        history: list[dict] | None = None,
        model: Any = None,
        *,
        publish: Callable | None = None,
        turn_id: str = "",
    ) -> Plan:
        """The plan for this message. Never raises; never generates twice."""
        key = plan_key(prompt, history)
        while True:
            with self._lock:
                entry = self._cache.get(key)
                if entry and time.monotonic() - entry[0] < self._ttl:
                    self._cache.move_to_end(key)
                    return entry[1]
                waiter = self._inflight.get(key)
                if waiter is None:
                    self._inflight[key] = threading.Event()
                    break
            # Another thread is planning this exact message: wait for it.
            waiter.wait(timeout=config.planner_timeout() + 2.0)
            with self._lock:
                entry = self._cache.get(key)
                if entry:
                    return entry[1]
                # The other planner died; plan ourselves.
                self._inflight.setdefault(key, threading.Event())
                break
        try:
            plan = self._make(prompt, history, model)
        except Exception:  # noqa: BLE001 - planning must never break a turn
            plan = heuristic_plan(prompt, history)
            plan.reason = (plan.reason + "; planner failed").strip("; ")
        plan.prompt = prompt or ""
        with self._lock:
            self._cache[key] = (time.monotonic(), plan)
            self._cache.move_to_end(key)
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
            event = self._inflight.pop(key, None)
        if event is not None:
            event.set()
        emit("research.plan", {"turn_id": turn_id, **plan.view()}, publish)
        return plan

    def _make(self, prompt: str, history: list[dict] | None, model: Any) -> Plan:
        started = time.monotonic()
        quick = prefilter(prompt)
        if quick is not None:
            quick.elapsed = time.monotonic() - started
            return quick
        if model is None or not config.planner_enabled() or _too_small_to_route(model):
            plan = heuristic_plan(prompt, history)
            plan.elapsed = time.monotonic() - started
            return plan
        raw = ""
        try:
            raw = generate_capped(model, build_messages(prompt, history),
                                  config.planner_max_tokens(), config.planner_timeout())
        except Exception:  # noqa: BLE001 - a broken model falls back to the heuristic
            raw = ""
        plan = parse_plan(raw, prompt)
        if plan is None:
            plan = heuristic_plan(prompt, history)
            plan.reason = (plan.reason + "; the model gave no usable JSON").strip("; ")
            plan.raw = raw
        plan.elapsed = time.monotonic() - started
        return plan


def _too_small_to_route(model: Any) -> bool:
    """A ~1B model is a bad router: it answered `web: true` to "What is the
    capital of France?" and "What is 17 * 23?", and the pasted web passages
    then broke the answers. The heuristic gets those right, so small models
    route by heuristic (services/response/profile.py)."""
    try:
        from ..response.profile import is_compact

        return is_compact(model)
    except Exception:  # noqa: BLE001 - when unsure, keep the model planner
        return False


_default: Planner | None = None
_default_lock = threading.Lock()


def default_planner() -> Planner:
    global _default
    with _default_lock:
        if _default is None:
            _default = Planner()
        return _default
