"""G1 Governor: one accounting point for the whole machine.

A slot is measured in megabytes of RAM. Callers queue in arrival order, a
per-client cap stops one session from starving the others, and every slot is
a lease so a crashed caller cannot hold capacity forever (H-12, H-13, M-32).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Ticket:
    id: int
    weight: int
    client: str
    expires_at: float
    released: bool = False


@dataclass
class _Waiter:
    client: str
    weight: int
    event: threading.Event = field(default_factory=threading.Event)


class Governor:
    def __init__(
        self,
        capacity: int,
        per_client: int | None = None,
        lease_seconds: float = 300.0,
        mark: Callable[[str, str, str], object] | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if per_client is not None and per_client <= 0:
            raise ValueError("per_client must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._capacity = capacity
        self._per_client = per_client
        self._lease = lease_seconds
        self._mark = mark
        self._used = 0
        self._tickets: dict[int, Ticket] = {}
        self._client_used: dict[str, int] = {}
        self._waiters: deque[_Waiter] = deque()
        self._next_id = 1
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)

    @property
    def capacity(self) -> int:
        return self._capacity

    def status(self) -> dict:
        with self._lock:
            return {
                "capacity": self._capacity,
                "used": self._used,
                "per_client": self._per_client,
                "queued": [waiter.client for waiter in self._waiters],
            }

    def acquire(
        self,
        weight: int = 1,
        client: str = "default",
        timeout: float | None = None,
    ) -> Ticket | None:
        """Wait in line for a slot. Returns a ticket, or None on timeout."""
        if weight <= 0:
            raise ValueError("weight must be positive")
        if weight > self._capacity:
            raise ValueError(f"weight {weight} exceeds the governor capacity {self._capacity}")
        if self._per_client is not None and weight > self._per_client:
            raise ValueError(f"weight {weight} exceeds the per-client cap {self._per_client}")
        deadline = None if timeout is None else time.monotonic() + timeout
        waiter = _Waiter(client=client, weight=weight)
        with self._condition:
            self._expire_locked()
            self._waiters.append(waiter)
            try:
                while True:
                    self._expire_locked()
                    if self._waiters[0] is waiter and self._can_grant_locked(weight, client):
                        self._waiters.popleft()
                        return self._grant_locked(weight, client)
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return None
                    self._condition.wait(remaining)
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def release(self, ticket: Ticket | None) -> None:
        if ticket is None:
            return
        with self._condition:
            self._release_locked(ticket)
            self._condition.notify_all()

    def _can_grant_locked(self, weight: int, client: str) -> bool:
        if self._used + weight > self._capacity:
            return False
        if self._per_client is not None and self._client_used.get(client, 0) + weight > self._per_client:
            return False
        return True

    def _grant_locked(self, weight: int, client: str) -> Ticket:
        ticket = Ticket(
            id=self._next_id,
            weight=weight,
            client=client,
            expires_at=time.monotonic() + self._lease,
        )
        self._next_id += 1
        self._tickets[ticket.id] = ticket
        self._used += weight
        self._client_used[client] = self._client_used.get(client, 0) + weight
        return ticket

    def _release_locked(self, ticket: Ticket) -> None:
        if ticket.released:
            return
        ticket.released = True
        self._tickets.pop(ticket.id, None)
        self._used = max(0, self._used - ticket.weight)
        left = self._client_used.get(ticket.client, 0) - ticket.weight
        if left > 0:
            self._client_used[ticket.client] = left
        else:
            self._client_used.pop(ticket.client, None)

    def _expire_locked(self) -> None:
        now = time.monotonic()
        expired = [ticket for ticket in self._tickets.values() if ticket.expires_at <= now]
        for ticket in expired:
            self._release_locked(ticket)
            if self._mark is not None:
                try:
                    self._mark(
                        "G1-lease",
                        ticket.client,
                        f"slot {ticket.id} expired after {self._lease:.0f}s without release",
                    )
                except Exception:  # noqa: BLE001 - marking must never break accounting
                    pass
        if expired:
            self._condition.notify_all()
