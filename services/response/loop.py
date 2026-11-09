"""The agent loop: model-driven tool calls, steps, queued prompts, kill switch.

One turn is a sequence of generations ("steps"), at most ``max_steps``
(default 6). After each generation the visible reply (``<think>`` removed)
is read for a tool call (``tools/calls.py``):

* a call -> the executor runs it (permissions, approval card, audit,
  ``tool.call`` / ``tool.result`` events), the result goes back to the model
  as a ``[tool result: name]`` user message, and the loop continues;
* an unreadable call -> the model is asked to retry once;
* an answer -> the turn ends, unless a queued prompt is waiting (delivered
  at this step boundary, V25) or one of the two correctness checks below
  asks for one more step.

Correctness checks (each fires at most once per turn):

* an R1-style reply that only thought and never answered gets "answer now";
* a question about the present (``tools.policy.is_realtime``) answered with
  no tool result and no admission that live data could not be checked gets a
  reminder to call a tool or say it could not check
  (``protocol.realtime_nudge``). This is the guard against stale "current"
  values from training data.

The last step never runs tools: if tools were used, the model is told the
budget is spent and to answer from what it has. Every step boundary checks
the stop flag (Stop button, kill switch, parent agent stopped); so does the
executor before running a tool and while an approval card is open.

The deterministic pre-fetch (``ToolRunner.observe``) still runs first for
the obvious cases; its results are injected as a Context block and count as
tool results for the checks above.

``run_agent`` calls come back here through ``_spawn``: a nested loop with no
history, its own step budget and depth + 1.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..prompts.queue import PromptQueue
from ..sessions.transcript import TranscriptStore
from ..tools.base import ToolResult, failed
from ..tools.calls import ParsedReply, ToolCall, parse_tool_calls, split_think, strip_think
from ..tools.executor import MAX_DEPTH, CallContext
from ..tools.offer import select_tools
from ..tools.policy import is_realtime
from ..tools.protocol import (
    budget_message,
    realtime_nudge,
    result_message,
    retry_message,
    tool_prompt,
)
from . import bridge
from . import context as ctxmod
from . import issues
from . import profile as model_profile
from . import user_context
from .profile import compact_context
from .style import (DEFAULT_DIRECTIVE, FAKE_RESULT_NOTE, LIVE_DATA_RULE,
                    looks_like_fake_tool_result, scrub_disclaimers)

if TYPE_CHECKING:  # importing services.server at runtime is circular
    from ..server.events import EventBroker

DEFAULT_MAX_STEPS = 6
MAX_CALLS_PER_STEP = 3
HISTORY_MESSAGES = 6
# Below this prefill rate (tokens/s, last generation) the research planner
# routes by heuristic instead of spending a generation (time to first token).
PLANNER_MIN_PREFILL_TPS = 400.0
LIVE_NUDGE = False  # see _needs_live_nudge
# Attempts per kind of recovery in one turn: an unreadable tool call, a
# "cannot run commands" refusal while tools were offered, and a tool that
# failed. Each attempt is logged to logs/agent-issues.log.
MAX_RETRIES = 3
ACTION_TOOLS = frozenset({"shell", "file_read", "file_list", "file_write", "make_dir"})
_REFUSAL = re.compile(
    r"(?:\b(?:i|we)\s+(?:can(?:not|'t|’t| not)|am\s+(?:not\s+able|unable)\s+to|"
    r"(?:do\s+not|don't|don’t)\s+have(?:\s+\w+){0,3}?\s+(?:ability|access|permission)s?|"
    r"have\s+no(?:\s+\w+){0,2}?\s+(?:way|access))"
    r"|\bas\s+an?\s+(?:ai|virtual\s+assistant|language\s+model|text-based)\b)"
    r"[^.!?\n]{0,90}?\b(?:run|execute|access|reach|open|read|list|see|use|interact|create|write|make)\w*"
    r"[^.!?\n]{0,70}?\b(?:commands?|shell|terminal|computer|system|machine|files?|folders?|"
    r"director(?:y|ies)|desktop|tools?|device)\b",
    re.IGNORECASE,
)
# "Do it" requests: an action on files/folders/projects of this computer. When
# the model answers one of these with instructions instead of a tool call, it
# is sent back to act (shares the refusal budget).
_ACTION_REQUEST = re.compile(
    r"\b(?:create|make|build|write|save|generate|set\s*up|scaffold|copy|"
    r"list|show|open|run|install|edit|update|fix)\b[^.?!\n]{0,80}?\b(?:files?|folders?|"
    r"director(?:y|ies)|websites?|site|project|app|desktop|documents|downloads|repo|script|page|"
    r"html|css|readme)\b|\b(?:what'?s|what\s+is|what\s+are)\s+(?:on|in)\s+(?:my|the)\s+"
    r"(?:desktop|documents|downloads|folder|directory)\b",
    re.IGNORECASE,
)
# A how-to question is answered with words, not acted on (security review H2):
# "how do I delete...", "explain how to build...", "what is a README?".
_HOW_TO = re.compile(
    r"^\s*(?:how|why|what|when|where|which|who|should|is|are|does|do|explain|tell\s+me\s+(?:how|why|about)|"
    r"teach|describe|show\s+me\s+how)\b|\bhow\s+(?:do|can|would|should)\s+(?:i|you|we|one)\b|"
    r"\bhow\s+to\b|\bexplain\b|\binstructions?\b|\bsteps?\s+(?:to|for)\b",
    re.IGNORECASE,
)
_POLITE_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\b", re.IGNORECASE)
_LISTING_QUESTION = re.compile(
    r"\b(?:what'?s|what\s+is|what\s+are)\s+(?:on|in)\s+(?:my|the)\s+"
    r"(?:desktop|documents|downloads|folder|directory)\b", re.IGNORECASE)
# Never send back a reply that declined for a reason (safety, privacy, harm):
# only plain "I cannot access your computer" claims are corrected (review M1).
_REASONED_REFUSAL = re.compile(
    r"\b(?:harm\w*|unsafe|danger\w*|illegal|unethical|malicious|inappropriate|not\s+appropriate|"
    r"privacy|private|security|secure|risk\w*|policy|polic(?:y|ies)|guidelines?|"
    r"won'?t|will\s+not|should(?:\s+not|n'?t)|not\s+comfortable|refuse|destroy\w*|"
    r"irreversibl\w*|permanent\w*|credentials?|passwords?)\b",
    re.IGNORECASE,
)


def _asks_for_action(question: str) -> bool:
    """True for a request to do something here, not a question about how."""
    text = (question or "").strip()
    if not text:
        return False
    if _LISTING_QUESTION.search(text):
        return True
    if not _ACTION_REQUEST.search(text):
        return False
    polite = bool(_POLITE_REQUEST.match(text))
    if _HOW_TO.search(text) and not (polite and not re.search(r"\bhow\b|\bexplain\b", text, re.I)):
        return False
    if text.endswith("?") and not polite:
        return False
    return True


def _capability_refusal(answer: str) -> bool:
    """The reply claims it cannot act here, without giving a reason to decline."""
    text = answer or ""
    return bool(_REFUSAL.search(text)) and not _REASONED_REFUSAL.search(text)


# Plan mode: the agent may look but not touch. Only these tool categories are
# offered, and the executor refuses everything else (CallContext.allowed).
PLAN_CATEGORIES = frozenset({"info", "network", "read", "connector-read", "agent"})
PLAN_DIRECTIVE = "\n".join([
    "# Plan mode",
    "The user switched to Plan mode: do not change anything. You can read files, list folders",
    "and search; writing, deleting and running commands are switched off in this mode.",
    "Look at what you need, then answer with a plan:",
    "1. A one-line goal.",
    "2. Numbered steps, each naming the files to create or change and the commands to run.",
    "3. Risks or open questions, if any.",
    "End with: \"Switch to Build to carry out this plan.\"",
])
_NOT_RETRYABLE = ("Permission denied for", "The turn was stopped", "There is no tool called")
_FAKE_RESULT_HEADER = re.compile(r"^\s*\[tool result[^\]]*\]\s*", re.IGNORECASE)

SUBAGENT_DIRECTIVE = "\n".join([
    "You are a helper agent working for the main assistant, not talking to the user.",
    "Do the task, using tools when they help. Then reply with a short, factual summary of",
    "what you found or did: key facts, numbers, file paths, command results, and anything",
    "that failed. No greetings, no questions back.",
    LIVE_DATA_RULE,
])

# An answer that already says it could not check live data needs no nudge.
_ADMITS = re.compile(
    r"(can(?:no|')t|cannot|unable to|not able to|don't have|do not have|no) "
    r"(?:\w+ ){0,4}(?:check|access|browse|real[- ]time|live|current|internet|network|look)",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"\?|^\s*(what|who|when|where|which|how|is|are|was|does|do|did|will|can|tell me|give me)\b",
                       re.IGNORECASE)


class ModelClient(Protocol):
    def generate(self, messages: list[dict]) -> str: ...


@dataclass
class PlatformContext:
    """Live platform state (kept for importers; the loop renders its own line)."""
    available_tools: list[str]
    active_connectors: list[str]
    session_count: int
    turns_today: int
    tokens_today: int
    engine_status: str
    model_name: str
    context_limit: int
    context_used_estimate: int


@dataclass(frozen=True)
class Step:
    index: int
    answer: str
    kind: str = "answer"  # answer | tool | retry | nudge
    calls: tuple[dict, ...] = ()


@dataclass(frozen=True)
class LoopResult:
    status: str  # done | stopped | max-steps
    steps: tuple[Step, ...]
    messages: tuple[dict, ...]
    # Messages that belong in the transcript: delivered queued prompts and
    # the final answer to each. Tool chatter stays out of the transcript.
    new_messages: tuple[dict, ...] = ()
    tool_calls: int = 0
    # Deterministic verification state for the UI badge:
    #   verified   a live tool result (search, page, clock, weather) fed the answer
    #   unverified the question was about the present and no live result existed
    #   n/a        not a live-data question
    verification: str = "n/a"
    sources: tuple[dict, ...] = ()
    # Tool names in call order (a count alone cannot rebuild the UI footer
    # after a dropped stream).
    tools_used: tuple[str, ...] = ()

    @property
    def answer(self) -> str:
        for step in reversed(self.steps):
            if step.kind == "answer":
                return step.answer
        return self.steps[-1].answer if self.steps else ""


def collect_sources(data: object, into: list[dict]) -> None:
    """Pull source descriptors out of a tool result's data (web_search,
    wikipedia, web_fetch) into `into`, de-duplicated by URL. These become the
    source chips and the "verified" badge in the UI."""
    if not isinstance(data, dict):
        return
    seen = {s.get("url") for s in into}
    items = data.get("sources")
    if not isinstance(items, list) and data.get("url") and data.get("text") is not None:
        url = str(data.get("url"))
        items = [{"title": str(data.get("title") or url)[:120], "url": url, "provider": "web_fetch"}]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        host = ""
        try:
            from urllib.parse import urlparse

            host = urlparse(url).netloc.lower()
            host = host[4:] if host.startswith("www.") else host
        except Exception:  # noqa: BLE001
            pass
        into.append({
            "title": str(item.get("title") or host or url)[:120],
            "url": url,
            "domain": str(item.get("domain") or host),
            "provider": str(item.get("provider") or ""),
            "snippet": str(item.get("snippet") or "")[:300],
        })
        if len(into) >= 12:
            break


@dataclass
class _TurnState:
    tool_ok: bool = False
    prefetched: bool = False
    offline: bool = False
    retried: bool = False
    retries: int = 0          # unreadable tool calls answered with a retry note
    refusals: int = 0         # "I cannot run commands" answers sent back
    tool_failures: dict = field(default_factory=dict)  # tool name -> failed calls
    nudged: bool = False
    empty_retry: bool = False
    tool_steps: int = 0
    calls: int = 0
    sources: list = field(default_factory=list)
    question: str = ""
    needs_web: bool = False   # the research planner judged this a live question
    names: list[str] = field(default_factory=list)
    cache: dict[str, ToolResult] = field(default_factory=dict)
    fake_result_retry: bool = False


class AgentLoop:
    def __init__(
        self,
        model: ModelClient,
        *,
        queue: PromptQueue | None = None,
        audit=None,
        kill=None,
        persona=None,
        tools=None,
        max_steps: int = DEFAULT_MAX_STEPS,
        session_id: str = "default",
        transcripts: TranscriptStore | None = None,
        events: EventBroker | None = None,
        context_limit: int = 4096,
        server_ref=None,
        executor=None,
        raw_model: ModelClient | None = None,
        skills: list | None = None,
        turn_id: str = "",
        depth: int = 0,
        allowed_tools: frozenset[str] | None = None,
        directive: str | None = None,
        language: str = "",
        use_history: bool = True,
        prefetch: bool = True,
        parent_stop=None,
        compact: bool | None = None,
        settings=None,
        mode: str = "build",
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not session_id:
            raise ValueError("session_id is required")
        self._model = model
        # Side generations (history summaries, sub-agents) use the raw model
        # so they do not stream into the user's answer bubble.
        self._raw_model = raw_model or model
        self._queue = queue or PromptQueue()
        self._audit = audit
        self._persona = persona
        self._tools = tools
        self._executor = executor
        self._max_steps = max_steps
        self._session_id = session_id
        self._transcripts = transcripts
        self._events = events
        self._context_limit = max(512, int(context_limit or 4096))
        self._server = server_ref
        self._skills = list(skills or [])
        self._turn_id = turn_id
        self._depth = depth
        self._allowed = allowed_tools
        self._directive = directive
        self._language = str(language or "")
        self._use_history = use_history
        self._prefetch = prefetch
        # None = decide at run() from the model's size (profile.is_compact);
        # deciding there keeps weight loading off the request thread.
        self._compact = compact
        self._parent_stop = parent_stop
        # Settings store (profile) for the "About the user" block; None in
        # sub-agents and tests that do not care who the user is.
        self._settings = settings
        self._mode = "plan" if str(mode or "").lower() == "plan" else "build"
        self._stopped = False
        self._kill = kill
        self._kill_id = (
            kill.register(f"agent-loop:{id(self)}", self.stop) if kill is not None else None
        )
        self._prior_messages: list[dict] = []
        self._offered: list = []
        self._hidden_services: list[str] = []

    def set_prior_messages(self, messages: list[dict]) -> None:
        """Prior conversation history, loaded by the caller before ``run``."""
        self._prior_messages = list(messages or [])

    # ------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stopped = True

    def stopped(self) -> bool:
        if self._stopped:
            return True
        if self._parent_stop is not None:
            try:
                return bool(self._parent_stop())
            except Exception:  # noqa: BLE001
                return False
        return False

    def close(self) -> None:
        """Detach from the kill switch so long-lived servers do not leak stops."""
        if self._kill is None or self._kill_id is None:
            return
        kill, stop_id = self._kill, self._kill_id
        self._kill_id = None
        kill.unregister(stop_id)

    # ------------------------------------------------------------------ run
    def run(self, prompt: str) -> LoopResult:
        if not prompt or not prompt.strip():
            raise ValueError("a prompt is required")
        state = _TurnState(question=prompt)
        if self._mode == "plan":
            self._restrict_to_reading()
        if self._compact is None:
            self._compact = model_profile.is_compact(self._raw_model)
        # A small model cannot drive tools: it copies the protocol's example
        # call instead of answering (see profile.py). It gets no catalogue.
        self._offered = [] if self._compact else self._offer(prompt)
        prefetch_text = self._run_prefetch(prompt, state) if self._prefetch else ""
        bridged = self._run_bridge(prompt, state)
        if bridged:
            prefetch_text = f"{prefetch_text}\n\n{bridged}" if prefetch_text else bridged
        remembered = self._remember(prompt)
        if remembered:
            prefetch_text = f"{prefetch_text}\n\n{remembered}" if prefetch_text else remembered
        messages = self._initial_messages(prompt, prefetch_text)
        new_messages: list[dict] = []
        steps: list[Step] = []
        question = prompt
        index = 0
        status = "max-steps"

        while index < self._max_steps:
            if self.stopped():
                self._mark("G6-killswitch", f"loop:{self._session_id}", f"stopped before step {index + 1}")
                return self._result("stopped", steps, messages, new_messages, state)
            last = index == self._max_steps - 1
            if last and state.tool_steps:
                messages.append({**budget_message(state.names), "_kind": "note"})
            messages = ctxmod.fit(
                messages, self._context_limit,
                summarize=self._raw_model.generate if self._events is not None else None,
                publish=self._events.publish if self._events is not None else None,
                session_id=self._session_id,
            )
            raw = self._model.generate(ctxmod.public(messages)) or ""
            index += 1
            visible, _thought = split_think(raw)
            parsed = (
                parse_tool_calls(visible, self._schemas(), max_calls=MAX_CALLS_PER_STEP)
                if self._offered else ParsedReply(prefix=visible)
            )

            if self.stopped():
                steps.append(Step(index - 1, raw))
                self._mark("G6-killswitch", f"loop:{self._session_id}", f"stopped after step {index}")
                return self._result("stopped", steps, messages, new_messages, state)

            if parsed.calls and not last:
                self._tool_step(index, raw, parsed, messages, steps, state)
                continue

            if parsed.error and not parsed.calls and not last:
                if state.retries < MAX_RETRIES:
                    state.retries += 1
                    state.retried = True
                    steps.append(Step(index - 1, raw, kind="retry"))
                    messages.append({"role": "assistant", "content": visible or "(empty)", "_kind": "tool_call"})
                    messages.append({**retry_message(parsed.error, [t.name for t in self._offered]),
                                     "_kind": "note"})
                    self._mark("G3-loop", self._session_id,
                               f"unreadable tool call; retry {state.retries} of {MAX_RETRIES}")
                    self._issue("unreadable-call", error=parsed.error, attempt=state.retries,
                                detail=visible[:300])
                    continue
                self._issue("retries-exhausted", error=parsed.error, attempt=state.retries,
                            detail="unreadable tool call")

            # The model wrote "[tool result: web_search] ..." itself: that is a
            # hallucinated result, never an answer. One retry, then whatever it
            # says next is treated as a plain answer (and scrubbed below).
            if not parsed.calls and looks_like_fake_tool_result(visible) and not state.fake_result_retry and not last:
                state.fake_result_retry = True
                steps.append(Step(index - 1, raw, kind="retry"))
                messages.append({"role": "assistant", "content": visible, "_kind": "draft"})
                messages.append({"role": "user", "content": FAKE_RESULT_NOTE, "_kind": "note"})
                self._mark("G3-loop", self._session_id, "model invented a tool result; asked to retry")
                continue

            answer_text = parsed.prefix if parsed.calls else visible
            if not answer_text.strip() and not state.empty_retry and not last:
                # R1 distills sometimes think until the budget and never answer.
                state.empty_retry = True
                steps.append(Step(index - 1, raw, kind="retry"))
                messages.append({"role": "user", "content": "[note] Give your final answer now, without thinking out loud.",
                                 "_kind": "note"})
                continue

            refused = _capability_refusal(answer_text)
            explained = (self._mode != "plan" and not refused and state.tool_steps == 0
                         and not _REASONED_REFUSAL.search(answer_text or "")
                         and _asks_for_action(question))
            if (not last and state.refusals < MAX_RETRIES and not state.tool_ok
                    and self._offers_action_tools() and (refused or explained)):
                state.refusals += 1
                tools = ", ".join(sorted(t.name for t in self._offered if t.name in ACTION_TOOLS))
                steps.append(Step(index - 1, raw, kind="retry"))
                messages.append({"role": "assistant", "content": answer_text, "_kind": "draft"})
                if refused:
                    note = ("[note] You do have tools that act on this computer: " + tools + ". Do not say "
                            "you lack access. If the request is safe and the user wants it, call the right tool now "
                            "with one tool block; risky steps still show the user an approval card. If you "
                            "should not do it, say why.")
                else:
                    note = ("[note] The user asked you to do this on their computer, not to explain how. "
                            "Do it now with the tools (" + tools + "): make_dir and file_write create real "
                            "folders and files (paths like ~/Desktop/demo/index.html work), shell runs "
                            "commands, file_list shows what is in a folder. Call one tool now unless the request is unsafe or "
                            "destructive; after the work is done, answer briefly with what you did.")
                messages.append({"role": "user", "_kind": "note", "content": note})
                kind = "refusal" if refused else "explained-instead-of-acting"
                self._mark("G3-loop", self._session_id, f"{kind}; retry {state.refusals} of {MAX_RETRIES}")
                self._issue(kind, attempt=state.refusals, detail=answer_text[:300])
                continue

            if not last and self._needs_live_nudge(question, answer_text, state):
                state.nudged = True
                steps.append(Step(index - 1, raw, kind="nudge"))
                messages.append({"role": "assistant", "content": answer_text, "_kind": "draft"})
                can_search = "web_search" in {t.name for t in self._offered}
                messages.append({**realtime_nudge(state.offline or not can_search), "_kind": "note"})
                self._mark("G3-loop", self._session_id, "live-data answer without a tool result; nudged")
                continue

            final_raw = raw
            if parsed.calls and last:
                # The budget is spent but the model still wants a tool.
                final_raw = answer_text or (
                    "I reached the tool step limit before I could finish. "
                    "Ask me to continue, or narrow the question."
                )
                status = "max-steps"
            else:
                status = "done"
            # Final answer hygiene: drop invented "[tool result]" headers,
            # bracket notes and knowledge-cutoff sentences from the visible
            # text; reasoning is kept as the model wrote it.
            visible_final, thought_final = split_think(final_raw)
            cleaned = scrub_disclaimers(_FAKE_RESULT_HEADER.sub("", visible_final))
            if cleaned != visible_final:
                final_raw = (f"<think>\n{thought_final}\n</think>\n{cleaned}" if thought_final else cleaned)
                self._mark("G3-loop", self._session_id, "scrubbed disclaimer text from the answer")
            steps.append(Step(index - 1, final_raw))
            final = {"role": "assistant", "content": strip_think(final_raw) or final_raw, "_kind": "answer"}
            messages.append(final)
            new_messages.append({"role": "assistant", "content": final_raw})

            if self.stopped():
                return self._result("stopped", steps, messages, new_messages, state)
            if status == "done" and not last and self._deliver(messages, new_messages):
                question = messages[-1]["content"]
                state.nudged = state.retried = state.empty_retry = False
                state.retries = state.refusals = 0
                state.tool_failures = {}
                continue
            if self._queue.has_pending(self._session_id):
                self._mark("G1-prompt", self._session_id, "queued prompt pending for the next turn")
            return self._result(status, steps, messages, new_messages, state)

        self._mark("G1-loop", self._session_id, f"max steps ({self._max_steps}) reached")
        return self._result(status, steps, messages, new_messages, state)

    # ----------------------------------------------------------- tool steps
    def _tool_step(self, index: int, raw: str, parsed: ParsedReply, messages: list[dict],
                   steps: list[Step], state: _TurnState) -> None:
        state.tool_steps += 1
        steps.append(Step(index - 1, raw, kind="tool", calls=tuple(c.view() for c in parsed.calls)))
        # Feed back the canonical form of the call, not the raw text: the
        # model sees the format it should use, and any invented "result"
        # text it wrote after the call is gone.
        messages.append({"role": "assistant", "content": _canonical(parsed), "_kind": "tool_call"})
        limit = ctxmod.result_chars(self._context_limit)
        for call in parsed.calls:
            if self.stopped():
                break
            result = self._call(call, index, state)
            state.calls += 1
            state.names.append(call.name)
            if result.ok:
                state.tool_ok = True
                collect_sources(getattr(result, "data", None), state.sources)
            elif result.offline:
                state.offline = True
            messages.append({**result_message(call.name, result, limit=limit), "_kind": "tool_result"})
            if not result.ok and not result.offline and not _not_retryable(result):
                count = state.tool_failures.get(call.name, 0) + 1
                state.tool_failures[call.name] = count
                error = result.error or result.content or "failed"
                self._issue("tool-failed", tool=call.name, error=error, attempt=count,
                            detail=json.dumps(call.args, ensure_ascii=False)[:300])
                if count < MAX_RETRIES:
                    note = (f"[note] {call.name} failed (attempt {count} of {MAX_RETRIES}): {error}\n"
                            "Find the cause (a wrong path, command, flag or argument), then call the tool "
                            "again with the fix. Do not repeat the exact same call.")
                else:
                    note = (f"[note] {call.name} has failed {count} times. Do not call it again in this "
                            "turn. Tell the user exactly what failed and the last error, and suggest a fix.")
                    self._issue("retries-exhausted", tool=call.name, error=error, attempt=count)
                messages.append({"role": "user", "content": note, "_kind": "note"})

    # ------------------------------------------------- bridge and recovery
    def _restrict_to_reading(self) -> None:
        """Plan mode: only read-only tools, enforced by the executor (``allowed``)."""
        names: set[str] = set()
        if self._executor is not None:
            try:
                tools = self._executor.offered(depth=self._depth)
            except Exception:  # noqa: BLE001 - no catalogue means nothing is allowed
                tools = []
            names = {t.name for t in tools if getattr(t, "category", "info") in PLAN_CATEGORIES}
        allowed = frozenset(names)
        self._allowed = allowed if self._allowed is None else (self._allowed & allowed)

    def _remember(self, prompt: str) -> str:
        """Save a name the user states ("my name is ..."), by code (user_context.capture)."""
        if self._depth != 0 or self._directive is not None:
            return ""
        try:
            return user_context.capture(prompt, self._settings)
        except Exception as exc:  # noqa: BLE001 - memory never breaks a turn
            self._issue("memory-failed", error=f"{type(exc).__name__}: {exc}")
            return ""

    def _offers_action_tools(self) -> bool:
        return any(t.name in ACTION_TOOLS for t in self._offered)

    def _issue(self, kind: str, **fields) -> None:
        issues.record(kind, session=self._session_id, turn=self._turn_id, **fields)

    def _run_bridge(self, prompt: str, state: _TurnState) -> str:
        """Run the command the message explicitly asks for (``bridge.py``).

        Works for every model size: the loop runs the ``shell`` tool itself,
        through the executor (permissions, approval card, audit, events). A
        start-up failure is retried up to ``MAX_RETRIES`` times; a command
        that ran and failed (non-zero exit, timeout) is a result, not retried,
        because running it again could repeat side effects. The output goes
        to the model as context so it can explain it.
        """
        # Only the user's own message at the top level: a sub-agent's task text
        # is written by the model, not the user (security review L1).
        if self._depth > 0 or self._directive is not None:
            return ""
        command = bridge.extract_command(prompt)
        if not command or self._executor is None:
            return ""
        if self._allowed is not None and "shell" not in self._allowed:
            return ""
        ctx = CallContext(
            turn_id=self._turn_id, session_id=self._session_id, depth=self._depth, step=0,
            stop=self.stopped, interactive=True, allowed=self._allowed, spawn=self._spawn,
            source="user",
        )
        result: ToolResult | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = self._executor.execute("shell", {"command": command}, ctx).result
            except Exception as exc:  # noqa: BLE001 - the executor never raises, but be sure
                result = failed("shell", f"The shell tool failed: {type(exc).__name__}: {exc}")
            if result.ok or not _transient(result) or self.stopped() or attempt == MAX_RETRIES:
                break
            self._issue("bridge-retry", tool="shell", error=result.error, attempt=attempt, detail=command)
        assert result is not None
        state.names.append("shell")
        state.cache[ToolCall("shell", {"command": command}).key()] = result
        if result.ok:
            state.tool_ok = True
        else:
            self._issue("bridge-failed", tool="shell", error=result.error or result.content,
                        detail=command)
        body = result_message("shell", result, limit=ctxmod.result_chars(self._context_limit))["content"]
        return ("The user asked to run a command on this computer, and IMURUN ran it for them:\n"
                f"{body}\n"
                "Explain the result briefly. Do not run the same command again.")

    def _call(self, call: ToolCall, index: int, state: _TurnState) -> ToolResult:
        key = call.key()
        if key in state.cache:
            cached = state.cache[key]
            return ToolResult(ok=cached.ok, content=(
                "(This exact call was already made in this turn; the result is repeated here. "
                "Use it instead of calling again.)\n" + (cached.content or "")
            ), error=cached.error, source=cached.source, offline=cached.offline)
        if self._executor is None:
            return failed(call.name, "Tools are not available in this session.")
        ctx = CallContext(
            turn_id=self._turn_id, session_id=self._session_id, depth=self._depth, step=index,
            stop=self.stopped, interactive=True, allowed=self._allowed, spawn=self._spawn,
            source="model" if self._depth == 0 else f"agent:{self._depth}",
        )
        try:
            result = self._executor.execute(call.name, call.args, ctx).result
        except Exception as exc:  # noqa: BLE001 - the executor never raises, but be sure
            result = failed(call.name, f"{type(exc).__name__}: {exc}")
        state.cache[key] = result
        return result

    def _spawn(self, task: str, tools: list[str] | None, steps: int) -> ToolResult:
        """Run a sub-agent for ``run_agent``."""
        depth = self._depth + 1
        if depth > MAX_DEPTH:
            return failed("run_agent", "Sub-agents cannot go deeper here.")
        parent = {t.name for t in self._offered}
        wanted = set(tools) if tools else set(parent)
        allowed = wanted & parent  # never more than this agent was offered (review L5)
        if depth >= MAX_DEPTH:
            allowed.discard("run_agent")
        self._publish("agent.start", {"turn_id": self._turn_id, "depth": depth, "task": task[:500],
                                      "tools": sorted(allowed), "max_steps": steps})
        started = time.monotonic()
        sub = AgentLoop(
            self._raw_model, raw_model=self._raw_model, executor=self._executor,
            max_steps=max(2, steps), session_id=self._session_id, turn_id=self._turn_id,
            depth=depth, allowed_tools=frozenset(allowed), directive=SUBAGENT_DIRECTIVE,
            use_history=False, prefetch=False, context_limit=self._context_limit,
            events=self._events, audit=self._audit, parent_stop=self.stopped,
        )
        try:
            result = sub.run(task)
        except Exception as exc:  # noqa: BLE001 - a broken sub-agent is a failed tool
            self._publish("agent.end", {"turn_id": self._turn_id, "depth": depth, "status": "error",
                                        "error": f"{type(exc).__name__}: {exc}"})
            return failed("run_agent", f"The sub-agent failed: {type(exc).__name__}: {exc}")
        finally:
            sub.close()
        summary = strip_think(result.answer).strip() or "(the sub-agent gave no summary)"
        self._publish("agent.end", {
            "turn_id": self._turn_id, "depth": depth, "status": result.status,
            "steps": len(result.steps), "tool_calls": result.tool_calls,
            "elapsed": round(time.monotonic() - started, 3), "summary": summary[:2000],
        })
        ok = result.status in ("done", "max-steps")
        return ToolResult(
            ok=ok,
            content=f"Sub-agent result ({result.status}, {len(result.steps)} steps, "
                    f"{result.tool_calls} tool calls):\n{summary}",
            data={"status": result.status, "steps": len(result.steps), "tool_calls": result.tool_calls},
            error=None if ok else f"sub-agent {result.status}",
            source="run_agent",
        )

    # ------------------------------------------------------------- helpers
    def _prefill_fast(self) -> bool:
        """True when the loaded model's last measured prefill rate makes a
        routing generation cheap (>= PLANNER_MIN_PREFILL_TPS tokens/s)."""
        getter = getattr(self._raw_model, "stats", None)
        if not callable(getter):
            return False
        try:
            stats = getter() or {}
            if hasattr(stats, "__await__"):  # an async client: unknown, be cheap
                return False
            rate = float(stats.get("prefill_tokens_per_second") or 0.0)
        except Exception:  # noqa: BLE001 - unknown speed: route by heuristic
            return False
        return rate >= PLANNER_MIN_PREFILL_TPS

    def _offer(self, prompt: str = "") -> list:
        self._hidden_services = []
        if self._executor is None or self._max_steps < 2:
            return []
        try:
            offered = list(self._executor.offered(depth=self._depth, allowed=self._allowed))
        except Exception:  # noqa: BLE001 - no tools is better than no answer
            self._mark("G3-loop", self._session_id, "could not list tools")
            return []
        # Connector tools per service only when the conversation points at the
        # service (services/tools/offer.py); hidden tools still run when named.
        try:
            offered, self._hidden_services = select_tools(offered, prompt, self._prior_messages)
        except Exception:  # noqa: BLE001 - selection is an optimisation
            self._hidden_services = []
        return offered

    def _schemas(self) -> dict[str, dict]:
        return {t.name: dict(t.parameters or {}) for t in self._offered}

    def _needs_live_nudge(self, question: str, answer: str, state: _TurnState) -> bool:
        # Retired: asking the model to "admit it could not check" produced
        # prose like "[note: unable to verify]" inside answers. The server now
        # reports `verification` on the turn and the UI shows a badge instead.
        # Kept behind a flag for experiments.
        if not LIVE_NUDGE:
            return False
        if state.nudged or state.tool_ok or state.prefetched:
            return False
        if not is_realtime(question) or not _QUESTION.search(question):
            return False
        if _ADMITS.search(answer or ""):
            return False
        # Without any tool machinery (a bare loop in a test or a script) the
        # nudge still applies: "no tools" means live data cannot be checked.
        return self._executor is not None or self._tools is not None

    def _initial_messages(self, prompt: str, prefetch_text: str) -> list[dict]:
        parts: list[str] = []
        if self._directive is not None:
            parts.append(self._directive)
        else:
            persona = self._persona.directive() if self._persona is not None else ""
            if persona:
                parts.append(persona)
            parts.append(model_profile.COMPACT_DIRECTIVE if self._compact else DEFAULT_DIRECTIVE)
            if self._mode == "plan":
                parts.append(PLAN_DIRECTIVE)
            if self._depth == 0:
                try:
                    parts.append(user_context.block(
                        self._settings, can_save=any(t.name == "facts" for t in self._offered)))
                except Exception:  # noqa: BLE001 - never block a turn on the profile
                    pass
        if self._language:
            # The user spoke this language (voice, docs/agentic/REQUESTS.md #13).
            from ..voice.languages import answer_directive

            line = answer_directive(self._language)
            if line:
                parts.append(line)
        parts.append(self._platform_line())
        if self._offered:
            workspace, folders = "", []
            try:
                policy = getattr(self._executor, "policy", None)
                if policy is not None:
                    workspace = str(policy.workspace.root)
                    home = os.path.expanduser("~")
                    folders = [("~" + str(p)[len(home):]) if str(p).startswith(home + os.sep) else str(p)
                               for p in policy.workspace.extra_roots]
            except Exception:  # noqa: BLE001
                workspace, folders = "", []
            # The compact tool list for every model: the JSON-schema form tripled
            # the system prompt (about 1,800 tokens against 670) and thinking
            # models spent long passages deliberating over it.
            parts.append(tool_prompt(self._offered, max_steps=self._max_steps,
                                     full=False, workspace=workspace, folders=folders,
                                     hidden_services=getattr(self, "_hidden_services", [])))
        if self._skills:
            from ..skills.invoke import skills_prompt

            parts.append(skills_prompt(self._skills))
        # The pre-fetched Context block changes every turn, so it must not sit
        # in the system message: the engine caches the KV of the longest
        # shared prefix, and a changing prefix cost a full prefill per turn
        # (measured: 1,930 prompt tokens, 0 cached, 15 s before the first
        # token on the 1B). It rides with the user prompt instead, after the
        # stable system text and history.
        messages: list[dict] = [{"role": "system", "content": "\n\n".join(p for p in parts if p),
                                 "_kind": "system"}]
        if self._use_history and self._prior_messages:
            for message in self._prior_messages[-HISTORY_MESSAGES:]:
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = str(message.get("content") or "")
                if role == "assistant":
                    content = strip_think(content)  # R1: never feed old thoughts back
                if content.strip():
                    messages.append({"role": role, "content": content, "_kind": "history"})
        user_text = f"{prefetch_text}\n\n{prompt}" if prefetch_text else prompt
        messages.append({"role": "user", "content": user_text, "_kind": "prompt"})
        return messages

    def _platform_line(self) -> str:
        # Today's date from this machine's clock: the cheapest live fact, and
        # the one small models get wrong most. Date only (no minutes) so the
        # engine's prefix cache is not invalidated every minute.
        today = time.strftime("%A, %d %B %Y")
        name = ""
        model = getattr(self._server, "_model", None) if self._server is not None else None
        getter = getattr(model, "selected_name", None)
        if callable(getter):
            try:
                name = str(getter() or "")
            except Exception:  # noqa: BLE001
                name = ""
        if self._compact:
            # A small model repeats a file name like "Llama-3.2-1B-...gguf"
            # back to the user; it only needs the date, in the tested wording.
            return f"Today's date is {time.strftime('%d %b %Y')}."
        line = f"Today is {today} (local time on this machine)."
        return line + (f" Model: {name}." if name else "")

    def _run_prefetch(self, prompt: str, state: _TurnState) -> str:
        """The deterministic fast path. A failure here never stops the turn."""
        if self._tools is None:
            return ""
        try:
            # The research planner decides with the *raw* model (the publishing
            # wrapper would stream its routing JSON into the answer bubble) and
            # sees recent history so follow-ups ("and in Germany?") plan well.
            extra: dict = {}
            if getattr(self._tools, "supports_research", False):
                # IMURUN_LOOP_PLANNER_MODEL=0 withholds the model from the
                # planner on this path (the regex fallback then decides). The
                # test suite sets it, because its scripted fake models would
                # otherwise spend a reply on the routing generation. Distinct
                # from IMURUN_RESEARCH_PLANNER, the research module's own switch.
                use_model = os.getenv("IMURUN_LOOP_PLANNER_MODEL", "1").strip() not in ("0", "false", "no")
                # A small model is a bad router: it answered `web: true` to
                # "What is the capital of France?". The regex router decides.
                use_model = use_model and not self._compact
                # The routing generation is a second prefill of a few hundred
                # tokens plus ~60 generated ones, and it evicts the engine's KV
                # prefix cache for the real prompt. On a model that prefills
                # slowly (the 8B on this card: ~23 tok/s) that alone cost tens
                # of seconds before the first token. Only a fast-prefilling
                # model may route; unknown speed (first turn) routes by heuristic.
                use_model = use_model and self._prefill_fast()
                # A model that is offered web_search decides itself whether to
                # search: no keyword or routing search runs ahead of it ("what's
                # on my desktop right now" searched the web). Only compact models,
                # which get no tools, keep the pre-fetched search.
                model_searches = (not self._compact
                                  and any(t.name == "web_search" for t in self._offered))
                extra = {
                    "search": not model_searches,
                    "history": [m for m in self._prior_messages[-HISTORY_MESSAGES:]
                                if m.get("role") in ("user", "assistant")],
                    "model": self._raw_model if use_model else None,
                    "publish": self._publish,
                    "turn_id": self._turn_id,
                }
            names = list(self._tools.intents(prompt, **extra)) if hasattr(self._tools, "intents") else []
            if extra and extra.get("search") and hasattr(self._tools, "plan"):
                try:
                    plan = self._tools.plan(prompt, extra["history"], extra["model"])  # cache hit
                    state.needs_web = bool(getattr(plan, "needs_web", False))
                except Exception:  # noqa: BLE001 - planning never blocks the turn
                    state.needs_web = False
            if not names:
                return ""
            searching = "web_search" in names
            self._publish("tools.start", {"tools": names})
            if searching:
                self._publish("web_search.start", {"query": prompt})
            if hasattr(self._tools, "observe_full"):
                text, results = self._tools.observe_full(prompt, **extra, stop=self.stopped) if extra \
                    else self._tools.observe_full(prompt)
            else:
                text = self._tools.observe(prompt)
                results = list(getattr(self._tools, "last_results", []) or [])
            self._publish("tools.done", {"tools": names, "results": results})
            if searching:
                hits = next((r.get("data", {}).get("results", []) for r in results
                             if r.get("tool") == "web_search"), [])
                self._publish("web_search.done", {"query": prompt, "results": hits})
            for item in results:
                self._publish("tool.result", {
                    "turn_id": self._turn_id, "session_id": self._session_id, "call_id": "",
                    "tool": item.get("tool"), "ok": bool(item.get("ok")), "decision": "allow",
                    "error": item.get("error"), "offline": bool(item.get("offline")),
                    "latency_ms": item.get("latency_ms", 0), "approval_id": None,
                    "content": str(item.get("content") or "")[:2000], "depth": self._depth,
                    "step": 0, "source": "prefetch",
                })
                if item.get("ok"):
                    state.tool_ok = True
                    collect_sources(item.get("data"), state.sources)
                if item.get("offline"):
                    state.offline = True
                cached = ToolResult(ok=bool(item.get("ok")), content=str(item.get("content") or ""),
                                    error=item.get("error"), offline=bool(item.get("offline")))
                state.cache[ToolCall(str(item.get("tool")), dict(item.get("args") or {})).key()] = cached
            if self._compact:
                # The full block (tool instructions + every observation + citation
                # rules) was copied verbatim into the answer by a 1B model.
                text = compact_context(results)
            state.prefetched = bool(text)
            return str(text or "")
        except Exception:  # noqa: BLE001 - a broken tool must not stop the turn
            self._mark("G3-loop", self._session_id, "tool pre-fetch failed")
            return ""

    def _deliver(self, messages: list[dict], new_messages: list[dict]) -> bool:
        delivered = self._queue.drain(self._session_id)
        for item in delivered:
            messages.append({"role": "user", "content": item.text, "_kind": "prompt"})
            new_messages.append({"role": "user", "content": item.text})
            self._mark("G1-prompt", str(item.id), "delivered at step boundary")
        return bool(delivered)

    def _result(self, status: str, steps: list[Step], messages: list[dict],
                new_messages: list[dict], state: _TurnState) -> LoopResult:
        if state.tool_ok:
            verification = "verified"
        elif state.needs_web or is_realtime(state.question):
            verification = "unverified"
        else:
            verification = "n/a"
        return LoopResult(status, tuple(steps), tuple(ctxmod.public(messages)),
                          tuple(new_messages), state.calls,
                          verification=verification, sources=tuple(state.sources),
                          tools_used=tuple(str(n) for n in getattr(state, "names", [])))

    def _publish(self, kind: str, data: dict) -> None:
        if self._events is None:
            return
        try:
            self._events.publish(kind, data)
        except Exception:  # noqa: BLE001 - the UI feed must not break the turn
            pass

    def _mark(self, rule: str, subject: str, why: str) -> None:
        if self._audit is None:
            return
        try:
            self._audit.mark(rule, subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not stop the run
            pass


def _canonical(parsed: ParsedReply) -> str:
    blocks = []
    if parsed.prefix:
        blocks.append(parsed.prefix)
    for call in parsed.calls:
        blocks.append("```tool\n" + json.dumps({"name": call.name, "args": call.args},
                                               ensure_ascii=False) + "\n```")
    return "\n".join(blocks)


def _not_retryable(result: ToolResult) -> bool:
    """Denied, declined, stopped or unknown-tool results are never retried."""
    text = str(result.error or result.content or "")
    return any(text.startswith(prefix) for prefix in _NOT_RETRYABLE)


def _transient(result: ToolResult) -> bool:
    """The tool could not start (not: the command ran and failed)."""
    text = str(result.error or "")
    return text.startswith("The shell tool failed") or text == "the tool returned no result"
