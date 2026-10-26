"""G-6 KillSwitch: one command stops everything, and every stop is marked."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StopResult:
    name: str
    ok: bool
    error: str | None = None


class KillSwitch:
    def __init__(self, mark: Callable[[str, str, str], object] | None = None) -> None:
        self._stops: dict[str, Callable[[], object]] = {}
        self._mark = mark
        self._lock = threading.Lock()

    def register(self, name: str, stop: Callable[[], object]) -> None:
        with self._lock:
            self._stops[name] = stop

    def unregister(self, name: str) -> None:
        with self._lock:
            self._stops.pop(name, None)

    def fire(self) -> list[StopResult]:
        """Stop everything registered. A failing stop is recorded, never
        silently skipped (V9), and the remaining stops still run."""
        with self._lock:
            stops = list(self._stops.items())
        results: list[StopResult] = []
        for name, stop in stops:
            try:
                stop()
                results.append(StopResult(name=name, ok=True))
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                message = f"{type(exc).__name__}: {exc}"
                results.append(StopResult(name=name, ok=False, error=message))
                if self._mark is not None:
                    try:
                        self._mark("G6-killswitch", name, message)
                    except Exception:  # noqa: BLE001 - marking must never break the kill
                        pass
        return results
