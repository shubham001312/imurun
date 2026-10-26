"""Queued user prompts: nothing the user says is lost while a task runs.

Delivery is decided by code, not by prompt text (V4):
- mid-task: drained at the next step boundary, in order, marked as queued;
- before the end: the agent loop must check has_pending() and continue;
- never mid-token: the boundary is after the current generation segment.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedPrompt:
    id: int
    session_id: str
    text: str
    at: float
    delivered: bool = False


class PromptQueue:
    def __init__(self, history_limit: int = 1000, max_text: int = 1_000_000) -> None:
        if history_limit <= 0:
            raise ValueError("history_limit must be positive")
        if max_text <= 0:
            raise ValueError("max_text must be positive")
        self._history_limit = history_limit
        self._max_text = max_text
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending: list[QueuedPrompt] = []
        self._history: list[QueuedPrompt] = []
        self._next_id = 1

    def push(self, session_id: str, text: str) -> QueuedPrompt:
        if not session_id:
            raise ValueError("session_id is required")
        if not text:
            raise ValueError("text is required")
        if len(text) > self._max_text:
            raise ValueError(f"a queued prompt cannot exceed {self._max_text} characters")
        with self._condition:
            prompt = QueuedPrompt(
                id=self._next_id,
                session_id=session_id,
                text=text,
                at=time.time(),
            )
            self._next_id += 1
            self._pending.append(prompt)
            self._condition.notify_all()
            return prompt

    def pending(self, session_id: str) -> list[QueuedPrompt]:
        with self._lock:
            return [item for item in self._pending if item.session_id == session_id]

    def has_pending(self, session_id: str) -> bool:
        with self._lock:
            return any(item.session_id == session_id for item in self._pending)

    def drain(self, session_id: str) -> list[QueuedPrompt]:
        """Deliver every waiting message for this session, in arrival order."""
        with self._condition:
            delivered = [item for item in self._pending if item.session_id == session_id]
            self._pending = [item for item in self._pending if item.session_id != session_id]
            marked = [
                QueuedPrompt(
                    id=item.id,
                    session_id=item.session_id,
                    text=item.text,
                    at=item.at,
                    delivered=True,
                )
                for item in delivered
            ]
            self._history.extend(marked)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit :]
            return marked

    def cancel(self, prompt_id: int) -> bool:
        """Cancel a message that has not been delivered yet."""
        with self._lock:
            for index, item in enumerate(self._pending):
                if item.id == prompt_id:
                    self._pending.pop(index)
                    return True
            return False

    def wait(self, session_id: str, timeout: float | None = None) -> QueuedPrompt | None:
        """Block until a message is waiting for this session, or timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                for item in self._pending:
                    if item.session_id == session_id:
                        return item
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def history(self, session_id: str) -> list[QueuedPrompt]:
        """Everything ever queued for the session, delivered or not."""
        with self._lock:
            items = [item for item in self._pending + self._history if item.session_id == session_id]
            return sorted(items, key=lambda item: item.id)
