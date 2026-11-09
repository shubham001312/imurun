"""G6 KillSwitch: one command stops everything, and every stop is marked.

Stops are identified by id, so two parts sharing a name (two streams, two
engines) both stop; a duplicate can never silently replace another (C-05).
Every stop - success or failure - is written through mark() (H-26), and a
non-callable is rejected at register time, not during the emergency (M-30).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StopResult:
    id: int
    name: str
    ok: bool
    error: str | None = None


class KillSwitch:
    def __init__(self, mark: Callable[[str, str, str], object] | None = None) -> None:
        self._stops: list[tuple[int, str, Callable[[], object]]] = []
        self._next_id = 1
        self._mark = mark
        self._lock = threading.Lock()

    def register(self, name: str, stop: Callable[[], object]) -> int:
        """Register a stop and return its id. Raises on a non-callable."""
        if not callable(stop):
            raise TypeError(f"stop for {name!r} is not callable")
        with self._lock:
            stop_id = self._next_id
            self._next_id += 1
            self._stops.append((stop_id, name, stop))
            return stop_id

    def unregister(self, stop_id: int) -> bool:
        with self._lock:
            before = len(self._stops)
            self._stops = [entry for entry in self._stops if entry[0] != stop_id]
            removed = len(self._stops) != before
        if not removed and self._mark is not None:
            try:
                self._mark("G6-killswitch", str(stop_id), "unregister: unknown id")
            except Exception:  # noqa: BLE001 - marking must never break the kill
                pass
        return removed

    def fire(self) -> list[StopResult]:
        """Stop everything registered. A failing stop is recorded, never
        silently skipped (V9), and the remaining stops still run."""
        with self._lock:
            stops = list(self._stops)
        results: list[StopResult] = []
        for stop_id, name, stop in stops:
            try:
                stop()
                result = StopResult(id=stop_id, name=name, ok=True)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                result = StopResult(
                    id=stop_id,
                    name=name,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if self._mark is not None:
                try:
                    self._mark(
                        "G6-killswitch",
                        name,
                        "stopped" if result.ok else (result.error or "failed"),
                    )
                except Exception:  # noqa: BLE001 - marking must never break the kill
                    pass
        return results
