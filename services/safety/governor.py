"""G-5 Governor: total-load accounting across every caller.

Parents, children, and API clients all acquire from the same counter, so
calling twice cannot bypass the cap (rules/api.md #9).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class Ticket:
    weight: int
    released: bool = False


class Governor:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self, weight: int = 1) -> Ticket | None:
        """Return a ticket, or None when the total cap would be exceeded."""
        if weight <= 0:
            raise ValueError("weight must be positive")
        with self._lock:
            if self._used + weight > self._capacity:
                return None
            self._used += weight
            return Ticket(weight=weight)

    def release(self, ticket: Ticket | None) -> None:
        """Idempotent: releasing twice never corrupts the counter."""
        if ticket is None:
            return
        with self._lock:
            if ticket.released:
                return
            ticket.released = True
            self._used = max(0, self._used - ticket.weight)
