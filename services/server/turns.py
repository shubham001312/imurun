"""Turn runner: one AgentLoop per turn, in a worker thread, events to SSE.

The runner owns the lifecycle the frontend sees: start, step, end, cancel.
The model is injected, so the server never chooses or names a model (V10).

Agentic additions:

* the loop runs up to ``max_steps`` generations (default 6; per turn from
  the ``agent.maxSteps`` setting or ``IMURUN_MAX_STEPS``) and can call tools
  through the injected ``ToolExecutor``;
* ``/skill-name`` in a prompt injects that stored skill for the turn
  (``skill.used`` event);
* the live token stream hides tool-call blocks (``StreamGuard``), and the
  ``step`` event for a tool step carries a readable "Using `tool` {...}"
  line instead of raw JSON (``kind: "tool"``);
* only the final answers (and delivered queued prompts) go to the
  transcript - tool chatter does not.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..prompts.queue import PromptQueue
from ..response.loop import (
    DEFAULT_MAX_STEPS,
    SUBAGENT_DIRECTIVE,
    AgentLoop,
    LoopResult,
    ModelClient,
)
from ..sessions.transcript import TranscriptStore
from ..tools.calls import StreamGuard, display_step, parse_tool_calls, split_think
from .events import EventBroker

MAX_STEPS_LIMIT = 20


class _MarkAudit:
    def __init__(self, mark) -> None:
        self._mark = mark

    def mark(self, rule: str, subject: str, why: str) -> None:
        try:
            self._mark(rule, subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not stop the run
            pass


@dataclass
class Turn:
    id: str
    session_id: str
    prompt: str
    state: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str | None = None
    answer: str = ""
    error: str | None = None
    steps: int = 0
    mode: str = "build"
    language: str = ""   # ISO 639-1 the user spoke (voice), "" = unknown/English
    queue_wait: float = 0.0
    first_token_seconds: float = 0.0   # turn start -> first visible token (what the user waits for)
    prompt_tokens: int = 0
    generated_tokens: int = 0
    # Engine counters for the last generation of this turn (empty when the
    # engine does not report them). Published on `step` and `turn.end`.
    stats: dict = field(default_factory=dict)
    tool_calls: int = 0
    # Verification badge data (see response/loop.py LoopResult).
    verification: str = "n/a"
    sources: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    model_prompt: str = ""  # the prompt with /skill tokens removed

    def view(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "first_token_seconds": self.first_token_seconds,
            "language": self.language,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "steps": self.steps,
            "stats": self.stats,
            "tool_calls": self.tool_calls,
            "verification": self.verification,
            "sources": self.sources,
            "tools_used": self.tools_used,
            "skills": list(self.skills),
        }


class TurnRunner:
    def __init__(
        self,
        model: ModelClient | None,
        *,
        queue: PromptQueue | None = None,
        transcripts: TranscriptStore | None = None,
        events: EventBroker | None = None,
        audit=None,
        kill=None,
        persona=None,
        tools=None,
        max_steps: int | None = None,
        mark=None,
        usage=None,
        executor=None,
        skills=None,
        settings=None,
    ) -> None:
        if max_steps is None:
            try:
                max_steps = int(os.getenv("IMURUN_MAX_STEPS", "") or DEFAULT_MAX_STEPS)
            except ValueError:
                max_steps = DEFAULT_MAX_STEPS
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        self._model = model
        self._queue = queue or PromptQueue()
        self._transcripts = transcripts or TranscriptStore()
        self._events = events or EventBroker()
        self._audit = _MarkAudit(mark) if audit is None and mark is not None else audit
        self._kill = kill
        self._persona = persona
        self._tools = tools
        self._max_steps = min(max_steps, MAX_STEPS_LIMIT)
        self._mark = mark
        self._usage = usage
        self._executor = executor
        self._skills = skills
        self._settings = settings
        self._turns: dict[str, Turn] = {}
        self._loops: dict[str, AgentLoop] = {}
        self._lock = threading.Lock()
        # Model switch queue: when a turn is running, switch requests wait here
        self._model_switch_queue: list[tuple[Path, threading.Event]] = []
        self._switch_lock = threading.Lock()
        self._current_model_path: str | None = None

    @property
    def available(self) -> bool:
        if self._model is None:
            return False
        probe = getattr(self._model, "available", None)
        if probe is None:
            return True
        try:
            return bool(probe() if callable(probe) else probe)
        except Exception:  # noqa: BLE001 - an unreachable model is unavailable
            return False

    def start(self, session_id: str, prompt: str, mode: str = "build", language: str = "") -> Turn:
        # `available` is the engine's "configured" flag (library + a chosen
        # model file), not "weights resident", so lazy loading still works.
        # Without it the turn used to start and die with turn.error; the API
        # contract says 503 model_unavailable.
        if self._model is None or not self.available:
            raise RuntimeError("model unavailable")
        if not session_id:
            raise ValueError("session_id is required")
        if not prompt or not prompt.strip():
            raise ValueError("a prompt is required")
        turn = Turn(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            prompt=prompt,
            mode="plan" if str(mode).lower() == "plan" else "build",
            language=str(language or "")[:16],
        )
        invoked, model_prompt = self._invoke_skills(prompt)
        turn.skills = [skill.id for skill in invoked]
        turn.model_prompt = model_prompt
        context_limit = self._context_limit()
        self._prune_turns()

        holder: dict = {"loop": None}
        loop = AgentLoop(
            self._publishing_model(turn, holder),
            raw_model=self._model,
            queue=self._queue,
            audit=self._audit,
            kill=self._kill,
            persona=self._persona,
            tools=self._tools,
            executor=self._executor,
            skills=invoked,
            turn_id=turn.id,
            max_steps=self._steps_for_turn(),
            session_id=session_id,
            transcripts=self._transcripts,
            events=self._events,
            context_limit=context_limit,
            server_ref=self,  # Pass TurnRunner as server reference for platform context
            language=turn.language,
            settings=getattr(self, "_settings", None),  # profile -> "About the user" (user_context.py)
        )
        holder["loop"] = loop
        with self._lock:
            self._turns[turn.id] = turn
            self._loops[turn.id] = loop
        threading.Thread(
            target=self._run,
            args=(turn, loop, holder),
            name=f"turn-{turn.id}",
            daemon=True,
        ).start()
        return turn

    # ------------------------------------------------------- turn settings
    def _context_limit(self) -> int:
        """Ask the engine (it knows the loaded model's real window), else the
        configured size, else 4096."""
        context_limit = 0
        limiter = getattr(self._model, "context_limit", None)
        if callable(limiter):
            try:
                context_limit = int(limiter() or 0)
            except Exception:  # noqa: BLE001 - fall through to the config
                context_limit = 0
        if context_limit <= 0:
            cfg = getattr(self._model, "config", None)
            context_limit = int(getattr(cfg, "context_size", 0) or 0) or 4096
        return context_limit

    def _steps_for_turn(self) -> int:
        """``agent.maxSteps`` from Settings when set, else the runner's default."""
        if self._settings is not None:
            try:
                value = self._settings.read("agent").get("maxSteps")
                if value not in (None, ""):
                    return max(1, min(int(value), MAX_STEPS_LIMIT))
            except Exception:  # noqa: BLE001 - a broken setting keeps the default
                pass
        return self._max_steps

    def _invoke_skills(self, prompt: str) -> tuple[list, str]:
        """``/name`` tokens that match stored skills, and the prompt without them."""
        if self._skills is None or "/" not in prompt:
            return [], prompt
        try:
            from ..skills.invoke import find_invocations

            found = find_invocations(prompt, self._skills.list())
        except Exception:  # noqa: BLE001 - a broken skills folder must not block chat
            self._mark_turn("skills", "could not read skills")
            return [], prompt
        return found.skills, found.prompt

    def spawn_agent(self, task: str, tools: list[str] | None = None, steps: int = 4,
                    session_id: str = "manual") -> "LoopResult":
        """Run a sub-agent outside a turn (``POST /api/tools/run`` with run_agent)."""
        if self._model is None:
            raise RuntimeError("model unavailable")
        loop = AgentLoop(
            self._model, raw_model=self._model, executor=self._executor,
            max_steps=max(2, min(int(steps or 4), 8)), session_id=session_id or "manual",
            depth=1, allowed_tools=frozenset(tools) if tools else None,
            use_history=False, prefetch=False, context_limit=self._context_limit(),
            events=self._events, audit=self._audit, kill=self._kill,
            directive=SUBAGENT_DIRECTIVE,
        )
        try:
            return loop.run(task)
        finally:
            loop.close()

    def cancel(self, turn_id: str) -> bool:
        with self._lock:
            loop = self._loops.get(turn_id)
        if loop is None:
            return False
        loop.stop()
        # Stopping the loop only prevents the *next* step; the engine must be
        # told to stop decoding the current one or Stop waits for the answer.
        canceller = getattr(self._model, "cancel", None)
        if callable(canceller):
            try:
                canceller()
            except Exception:  # noqa: BLE001 - best effort
                pass
        return True

    def _prune_turns(self, keep: int = 200) -> None:
        """Forget old finished turns so a long-running server does not grow
        without bound. Running turns are always kept."""
        with self._lock:
            if len(self._turns) <= keep:
                return
            finished = sorted(
                (t for t in self._turns.values() if t.state != "running"),
                key=lambda t: t.finished_at or t.started_at,
            )
            for turn in finished[: max(0, len(self._turns) - keep)]:
                self._turns.pop(turn.id, None)

    def _select_model(self, model_path: Path, result: dict) -> None:
        """Run the engine's model switch and record the outcome into `result`."""
        selector = getattr(self._model, "select", None)
        if selector is None:
            result["ok"] = False
            result["error"] = "this engine cannot switch models"
            return
        try:
            ok = selector(model_path)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            return
        if ok:
            result["ok"] = True
            self._current_model_path = str(model_path)
            self._mark_turn(f"model:{model_path.name}", "selected")
        else:
            result["ok"] = False
            err = getattr(getattr(self._model, "_fish", None), "_native", None)
            detail = getattr(err, "error", None) if err is not None else None
            result["error"] = detail or "the model could not be selected"
            # The engine refused the file (not a GGUF, truncated, unsupported):
            # a property of the request, answered 422 rather than 500.
            result["reason"] = "load_failed"

    def request_model_switch(self, model_path: Path) -> dict:
        """Request a model switch. Returns immediately with status.
        
        If a turn is running, the switch is queued and executed after completion.
        If no turn is running, the switch happens immediately.
        """
        event = threading.Event()
        result = {"ok": False, "error": None, "queued": False}

        with self._switch_lock:
            with self._lock:
                running = any(t.state == "running" for t in self._turns.values())
            if running:
                # Queue it; the turn that is running will switch when it ends.
                result["queued"] = True
                self._model_switch_queue.append((model_path, event, result))
                if self._events:
                    self._events.publish("model.switch.queued", {"model_name": model_path.name})
            else:
                # The engine's own select() waits for any decode in flight, so
                # the weights are never freed under a running generation.
                self._select_model(model_path, result)
                if result["ok"] and self._events:
                    self._events.publish("model.switch.done", {"model_name": model_path.name})
                return result

        # Wait *outside* the lock: the finishing turn needs it to drain the
        # queue (holding it here was a 5-minute deadlock).
        if not event.wait(timeout=300):
            result["ok"] = False
            result["error"] = "timed out waiting for the running turn to finish"
        return result

    def _process_model_switch_queue(self) -> None:
        """Process any queued model switches after a turn completes."""
        with self._switch_lock:
            queue = self._model_switch_queue
            self._model_switch_queue = []

        for model_path, event, result in queue:
            self._select_model(model_path, result)
            if result["ok"] and self._events:
                self._events.publish("model.switch.done", {"model_name": model_path.name})
            event.set()

    def get(self, turn_id: str) -> Turn | None:
        with self._lock:
            return self._turns.get(turn_id)

    def list(self) -> list[Turn]:
        with self._lock:
            return list(self._turns.values())

    def _publishing_model(self, turn: Turn, holder: dict | None = None) -> ModelClient:
        inner = self._model
        publish = self._events.publish
        counter = {"n": 0}
        first_call = {"at": None}
        if holder is not None:
            holder["counter"] = counter

        def known_tools() -> dict:
            loop = (holder or {}).get("loop")
            schemas = getattr(loop, "_schemas", None)
            try:
                return schemas() if callable(schemas) else {}
            except Exception:  # noqa: BLE001
                return {}

        class _Publishing:
            def generate(self, messages: list[dict]) -> str:
                counter["n"] += 1
                index = counter["n"]
                if first_call["at"] is None:
                    first_call["at"] = time.time()
                    turn.queue_wait = max(0.0, first_call["at"] - turn.started_at)
                publish("step.start", {"turn_id": turn.id, "step": index})

                generated = 0
                tools_known = known_tools()
                # Check if the model supports async streaming
                if hasattr(inner, 'generate_stream'):
                    # Use streaming generation. With tools on offer, a guard
                    # keeps tool-call JSON out of the live answer bubble.
                    guard = StreamGuard() if tools_known else None
                    answer_parts = []
                    loop = asyncio.new_event_loop()
                    try:
                        async def collect_tokens():
                            async for token in inner.generate_stream(messages):
                                answer_parts.append(token)
                                shown = guard.feed(token) if guard is not None else token
                                if shown:
                                    if not turn.first_token_seconds:
                                        turn.first_token_seconds = round(max(0.0, time.time() - turn.started_at), 3)
                                    publish("token", {"turn_id": turn.id, "step": index, "token": shown})
                            if guard is not None:
                                tail = guard.flush()
                                if tail:
                                    publish("token", {"turn_id": turn.id, "step": index, "token": tail})
                            return "".join(answer_parts)
                        answer = loop.run_until_complete(collect_tokens())
                    finally:
                        loop.close()
                    generated = len(answer_parts)
                else:
                    # Fallback to synchronous generation
                    answer = inner.generate([dict(message) for message in messages])

                # The native engine reports exact prefill/decode token counts
                # for the generation that just finished. Prefer them; the
                # streamed chunk count is a correct fallback for other paths.
                stats = {}
                getter = getattr(inner, "stats", None)
                if callable(getter):
                    try:
                        stats = getter() or {}
                    except Exception:  # noqa: BLE001 - counters must not break a turn
                        stats = {}
                if isinstance(stats, dict) and "error" not in stats:
                    turn.prompt_tokens += int(stats.get("prompt_tokens") or 0)
                    turn.generated_tokens += int(stats.get("generated_tokens") or 0) or generated
                    turn.stats = {
                        key: stats[key]
                        for key in (
                            "prompt_tokens", "cached_tokens", "generated_tokens",
                            "prefill_seconds", "decode_seconds", "tokens_per_second",
                            "prefill_tokens_per_second", "context_used", "context_size",
                            "truncated", "threads", "streaming",
                        )
                        if key in stats
                    }
                    if turn.first_token_seconds:
                        turn.stats["first_token_seconds"] = turn.first_token_seconds
                else:
                    turn.generated_tokens += generated

                text, kind = answer, "answer"
                if tools_known:
                    parsed = parse_tool_calls(split_think(answer)[0], tools_known)
                    if parsed.calls:
                        text, kind = display_step(answer, parsed), "tool"
                publish("step", {"turn_id": turn.id, "step": index, "text": text,
                                 "kind": kind, "stats": turn.stats})
                return answer

        return _Publishing()

    def _run(self, turn: Turn, loop: AgentLoop, holder: dict | None = None) -> None:
        # Load only recent history (last 3 turns = 6 messages) for fast context
        prior_messages = []
        if self._transcripts is not None:
            try:
                msgs = self._transcripts.read(turn.session_id, limit=6)
                prior_messages = [{"role": m.role, "content": m.content} for m in msgs]
            except Exception:
                pass
        # Inject prior messages into the loop for this run
        loop.set_prior_messages(prior_messages)
        
        self._transcripts.append(turn.session_id, "user", turn.prompt, turn.id)
        self._events.publish(
            "turn.start",
            {"turn_id": turn.id, "session_id": turn.session_id},
        )
        self._publish_queue(turn)
        try:
            if turn.skills:
                self._events.publish("skill.used", {"turn_id": turn.id, "session_id": turn.session_id,
                                                    "skills": list(turn.skills)})
            result: LoopResult = loop.run(turn.model_prompt or turn.prompt)
            turn.status = result.status
            turn.answer = result.answer
            turn.steps = len(result.steps)
            turn.tool_calls = result.tool_calls
            turn.verification = getattr(result, "verification", "n/a")
            turn.sources = [dict(s) for s in getattr(result, "sources", ())]
            turn.tools_used = list(getattr(result, "tools_used", ()))
            turn.state = "stopped" if result.status == "stopped" else "done"
            counter = (holder or {}).get("counter") or {}
            generated = int(counter.get("n", 0))
            if result.status == "max-steps" and turn.answer and generated:
                # The answer was written by the loop (the budget ran out on a
                # tool step), not generated: show it as the last step.
                self._events.publish("step", {"turn_id": turn.id, "step": generated + 1,
                                              "text": turn.answer, "kind": "answer",
                                              "stats": turn.stats,
                                              "verification": turn.verification,
                                              "sources": turn.sources})
            self._append_result(turn, result)
            self._record_usage(turn)
            self._publish_queue(turn)
            self._events.publish(
                "turn.end",
                {
                    "turn_id": turn.id,
                    "status": result.status,
                    "steps": turn.steps,
                    "answer": turn.answer,
                    "stats": turn.stats,
                    "prompt_tokens": turn.prompt_tokens,
                    "generated_tokens": turn.generated_tokens,
                    "elapsed": round(time.time() - turn.started_at, 3),
                    "tool_calls": turn.tool_calls,
                    "skills": list(turn.skills),
                    "verification": turn.verification,
                    "sources": turn.sources,
                    "tools_used": turn.tools_used,
                },
            )
        except Exception as exc:  # noqa: BLE001 - one turn must not kill the server
            turn.state = "error"
            turn.error = f"{type(exc).__name__}: {exc}"
            self._mark_turn(turn.id, f"failed: {type(exc).__name__}")
            self._publish_queue(turn)
            self._events.publish(
                "turn.error",
                {"turn_id": turn.id, "error": turn.error},
            )
        finally:
            turn.finished_at = time.time()
            try:
                loop.close()
            except Exception:  # noqa: BLE001 - closing must not hide the result
                self._mark_turn(turn.id, "failed to close the loop")
            with self._lock:
                self._loops.pop(turn.id, None)
            # Process any queued model switches now that this turn is done
            self._process_model_switch_queue()

    def _mark_turn(self, subject: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-turn", subject, why)
        except Exception:  # noqa: BLE001 - marking must never change the turn
            pass

    def _record_usage(self, turn: Turn) -> None:
        """Add the finished turn to the local usage counters.

        Counters are best-effort: a broken usage file must never fail a turn.
        The view is republished so the Usage panel updates without a refresh.
        """
        if self._usage is None:
            return
        try:
            self._usage.record_turn(turn.mode, wait_seconds=turn.queue_wait)
            self._usage.record_tokens(turn.prompt_tokens + turn.generated_tokens)
            self._events.publish("usage", self._usage.view())
        except Exception:  # noqa: BLE001 - usage is observability, not control
            pass

    def _publish_queue(self, turn: Turn) -> None:
        self._events.publish(
            "queue",
            {
                "session_id": turn.session_id,
                "pending": self._queue.pending_count(),
            },
        )

    def _append_result(self, turn: Turn, result: LoopResult) -> None:
        """Keep the transcript complete: every delivered queued prompt and the
        answer to each, in order. The initial prompt was appended at start.

        This used to walk ``result.messages``, which also holds the prior
        history the loop was given; skipping only "the first user message"
        re-appended that history to the transcript on every turn. The loop
        now reports exactly the new messages.
        """
        for message in result.new_messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"}:
                continue
            if not content:
                self._mark_turn(turn.id, f"skipped an empty {role} message")
                continue
            self._transcripts.append(
                turn.session_id, role, str(content), turn.id
            )
