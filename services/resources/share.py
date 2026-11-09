"""One CPU-thread budget shared by chat (the engine), voice and tuning.

Rule (owner's ask): chat comes first. While a chat generation runs, voice
and tuning get the leftover - never more. Voice always gets at least one
thread (it runs below normal priority, so the OS still lets the user's apps
win); tuning gets nothing while chat runs (the trainer is paused anyway,
services/tuning/scheduler.py) and at least one otherwise.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

CHAT = "chat"
VOICE = "voice"
TUNING = "tuning"


class ThreadLedger:
    def __init__(self, total: int) -> None:
        self._total = max(1, int(total))
        self._held: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return self._total

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = max(1, int(total))

    def chat_active(self) -> bool:
        with self._lock:
            return self._held.get(CHAT, 0) > 0

    def leftover(self, name: str) -> int:
        """Threads not held by anyone else (chat counts in full)."""
        with self._lock:
            return self._leftover_locked(name)

    def _leftover_locked(self, name: str) -> int:
        others = sum(n for k, n in self._held.items() if k != name)
        return max(0, self._total - others)

    def grant(self, name: str, want: int) -> int:
        """Reserve up to ``want`` threads for ``name`` and return how many it
        may use. Re-granting replaces the previous reservation."""
        want = max(0, int(want))
        with self._lock:
            if name == CHAT:
                n = min(want, self._total)
            else:
                left = self._leftover_locked(name)
                chat_on = self._held.get(CHAT, 0) > 0
                floor = 1 if (name == VOICE or not chat_on) else 0
                n = max(min(floor, want), min(want, left))
            if n > 0:
                self._held[name] = n
            else:
                self._held.pop(name, None)
            return n

    def release(self, name: str) -> None:
        with self._lock:
            self._held.pop(name, None)

    @contextmanager
    def hold(self, name: str, want: int):
        n = self.grant(name, want)
        try:
            yield n
        finally:
            self.release(name)

    def view(self) -> dict:
        with self._lock:
            return {"total": self._total, "held": dict(self._held),
                    "free": self._leftover_locked("")}
