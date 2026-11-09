"""G-7 mark() and G-8 audit(): every guardrail event is visible.

mark() never raises: a broken sink must not take down the run, but the
failure is recorded locally so it is still visible. sink_errors is a
monotonic counter, so trimming the ring can never erase the fact that
evidence was lost (H-15).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Event:
    at: float
    rule: str
    subject: str
    why: str


class Audit:
    def __init__(
        self,
        sink: Callable[[Event], object] | None = None,
        capacity: int = 500,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._sink = sink
        self._capacity = capacity
        self._events: list[Event] = []
        self._sink_errors = 0
        self._lock = threading.Lock()

    @property
    def sink_errors(self) -> int:
        with self._lock:
            return self._sink_errors

    def mark(self, rule: str, subject: str, why: str) -> Event:
        """Record a guardrail event. Never throws."""
        event = Event(at=time.time(), rule=rule, subject=subject, why=why)
        with self._lock:
            self._append(event)
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception as exc:  # noqa: BLE001 - the mark must survive a bad sink
                with self._lock:
                    self._sink_errors += 1
                    self._append(
                        Event(at=time.time(), rule="G7-sink", subject=rule, why=f"sink failed: {exc}")
                    )
        return event

    def _append(self, event: Event) -> None:
        self._events.append(event)
        if len(self._events) > self._capacity:
            self._events = self._events[-self._capacity :]

    def tail(self, limit: int = 20) -> list[Event]:
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])
