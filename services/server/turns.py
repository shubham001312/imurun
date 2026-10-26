"""Turn runner: one AgentLoop per turn, in a worker thread, events to SSE.

The runner owns the lifecycle the frontend sees: start, step, end, cancel.
The model is injected, so the server never chooses or names a model (V10).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from ..prompts.queue import PromptQueue
from ..response.loop import AgentLoop, LoopResult, ModelClient
from ..sessions.transcript import TranscriptStore
from .events import EventBroker


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

    def view(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "steps": self.steps,
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
        max_steps: int = 20,
        mark=None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        self._model = model
        self._queue = queue or PromptQueue()
        self._transcripts = transcripts or TranscriptStore()
        self._events = events or EventBroker()
        self._audit = _MarkAudit(mark) if audit is None and mark is not None else audit
        self._kill = kill
        self._persona = persona
        self._max_steps = max_steps
        self._mark = mark
        self._turns: dict[str, Turn] = {}
        self._loops: dict[str, AgentLoop] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._model is not None

    def start(self, session_id: str, prompt: str) -> Turn:
        if self._model is None:
            raise RuntimeError("model unavailable")
        if not session_id:
            raise ValueError("session_id is required")
        if not prompt or not prompt.strip():
            raise ValueError("a prompt is required")
        turn = Turn(id=uuid.uuid4().hex[:12], session_id=session_id, prompt=prompt)
        loop = AgentLoop(
            self._publishing_model(turn),
            queue=self._queue,
            audit=self._audit,
            kill=self._kill,
            persona=self._persona,
            max_steps=self._max_steps,
            session_id=session_id,
        )
        with self._lock:
            self._turns[turn.id] = turn
            self._loops[turn.id] = loop
        threading.Thread(
            target=self._run,
            args=(turn, loop),
            name=f"turn-{turn.id}",
            daemon=True,
        ).start()
        return turn

    def cancel(self, turn_id: str) -> bool:
        with self._lock:
            loop = self._loops.get(turn_id)
        if loop is None:
            return False
        loop.stop()
        return True

    def get(self, turn_id: str) -> Turn | None:
        with self._lock:
            return self._turns.get(turn_id)

    def list(self) -> list[Turn]:
        with self._lock:
            return list(self._turns.values())

    def _publishing_model(self, turn: Turn) -> ModelClient:
        inner = self._model
        publish = self._events.publish
        counter = {"n": 0}

        class _Publishing:
            def generate(self, messages: list[dict]) -> str:
                counter["n"] += 1
                index = counter["n"]
                publish("step.start", {"turn_id": turn.id, "step": index})
                answer = inner.generate([dict(message) for message in messages])
                publish("step", {"turn_id": turn.id, "step": index, "text": answer})
                return answer

        return _Publishing()

    def _run(self, turn: Turn, loop: AgentLoop) -> None:
        try:
            self._transcripts.append(turn.session_id, "user", turn.prompt, turn.id)
            self._events.publish(
                "turn.start",
                {"turn_id": turn.id, "session_id": turn.session_id},
            )
            self._publish_queue(turn)
            result: LoopResult = loop.run(turn.prompt)
            turn.status = result.status
            turn.answer = result.answer
            turn.steps = len(result.steps)
            turn.state = "stopped" if result.status == "stopped" else "done"
            if result.answer:
                self._transcripts.append(
                    turn.session_id, "assistant", result.answer, turn.id
                )
            self._publish_queue(turn)
            self._events.publish(
                "turn.end",
                {
                    "turn_id": turn.id,
                    "status": result.status,
                    "steps": turn.steps,
                    "answer": turn.answer,
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

    def _mark_turn(self, subject: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-turn", subject, why)
        except Exception:  # noqa: BLE001 - marking must never change the turn
            pass

    def _publish_queue(self, turn: Turn) -> None:
        self._events.publish(
            "queue",
            {
                "session_id": turn.session_id,
                "pending": self._queue.pending_count(),
            },
        )
