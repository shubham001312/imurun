"""The agent loop: steps, queued prompts at boundaries, kill switch, persona.

Rules are enforced here in code (V25):
- queued prompts are delivered at a step boundary, never mid-token;
- before a final answer the loop drains again, so a prompt that arrived
  during the last generation still reaches the model;
- the kill switch stops the loop between steps;
- every delivery and every stop is marked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..prompts.queue import PromptQueue


class ModelClient(Protocol):
    def generate(self, messages: list[dict]) -> str: ...


@dataclass(frozen=True)
class Step:
    index: int
    answer: str


@dataclass(frozen=True)
class LoopResult:
    status: str  # done | stopped | max-steps
    steps: tuple[Step, ...]
    messages: tuple[dict, ...]

    @property
    def answer(self) -> str:
        return self.steps[-1].answer if self.steps else ""


class AgentLoop:
    def __init__(
        self,
        model: ModelClient,
        *,
        queue: PromptQueue | None = None,
        audit=None,
        kill=None,
        persona=None,
        max_steps: int = 20,
        session_id: str = "default",
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not session_id:
            raise ValueError("session_id is required")
        self._model = model
        self._queue = queue or PromptQueue()
        self._audit = audit
        self._persona = persona
        self._max_steps = max_steps
        self._session_id = session_id
        self._stopped = False
        self._kill = kill
        self._kill_id = (
            kill.register(f"agent-loop:{id(self)}", self.stop)
            if kill is not None
            else None
        )

    def stop(self) -> None:
        self._stopped = True

    def close(self) -> None:
        """Detach from the kill switch so long-lived servers do not leak stops."""
        if self._kill is None or self._kill_id is None:
            return
        kill = self._kill
        stop_id = self._kill_id
        self._kill_id = None
        kill.unregister(stop_id)

    def run(self, prompt: str) -> LoopResult:
        if not prompt or not prompt.strip():
            raise ValueError("a prompt is required")
        messages: list[dict] = []
        directive = self._persona.directive() if self._persona is not None else ""
        if directive:
            messages.append({"role": "system", "content": directive})
        messages.append({"role": "user", "content": prompt})
        steps: list[Step] = []

        for index in range(self._max_steps):
            if self._stopped:
                self._mark("G6-killswitch", f"loop:{self._session_id}", "stopped before step")
                return LoopResult("stopped", tuple(steps), tuple(messages))

            self._deliver(messages)
            answer = self._model.generate([dict(message) for message in messages])
            messages.append({"role": "assistant", "content": answer})
            steps.append(Step(index=index, answer=answer))

            if self._stopped:
                self._mark("G6-killswitch", f"loop:{self._session_id}", "stopped after step")
                return LoopResult("stopped", tuple(steps), tuple(messages))

            if self._queue.has_pending(self._session_id):
                self._mark("G1-prompt", self._session_id, "continuing: queued prompt pending")
                continue
            if self._deliver(messages):
                continue
            return LoopResult("done", tuple(steps), tuple(messages))

        self._mark("G1-loop", self._session_id, f"max steps {self._max_steps} reached")
        return LoopResult("max-steps", tuple(steps), tuple(messages))

    def _deliver(self, messages: list[dict]) -> bool:
        delivered = self._queue.drain(self._session_id)
        for item in delivered:
            messages.append({"role": "user", "content": item.text})
            self._mark("G1-prompt", str(item.id), "delivered at step boundary")
        return bool(delivered)

    def _mark(self, rule: str, subject: str, why: str) -> None:
        if self._audit is None:
            return
        try:
            self._audit.mark(rule, subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not stop the run
            pass
