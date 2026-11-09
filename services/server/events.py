"""In-process event broker for the SSE stream.

Each subscriber has a bounded queue: a slow UI must never grow server memory
without limit. A dropped event is counted and reported, never silent.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

CLOSE = "__close__"


@dataclass(frozen=True)
class Event:
    type: str
    data: dict


class Subscription:
    def __init__(self, max_queue: int = 512, on_drop=None) -> None:
        if max_queue <= 0:
            raise ValueError("max_queue must be positive")
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=max_queue)
        self._on_drop = on_drop
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        return self._dropped

    def put(self, event: Event) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        self._dropped += 1
        if self._on_drop is not None:
            try:
                self._on_drop(self._dropped)
            except Exception:  # noqa: BLE001 - a broken sink must not stop delivery
                pass
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass

    def get(self, timeout: float | None = None) -> Event | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(Event(CLOSE, {}))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(Event(CLOSE, {}))
            except queue.Full:
                pass


class EventBroker:
    def __init__(self, *, max_queue: int = 512, mark=None) -> None:
        self._max_queue = max_queue
        self._mark = mark
        self._subscribers: list[Subscription] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Subscription:
        sub = Subscription(
            max_queue=self._max_queue,
            on_drop=lambda dropped: self._drop_note(dropped),
        )
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)
        sub.close()

    def publish(self, event_type: str, data: dict) -> int:
        if not event_type:
            raise ValueError("event_type is required")
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.put(Event(event_type, dict(data)))
        return len(subscribers)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers = []
        for sub in subscribers:
            sub.close()

    def _drop_note(self, dropped: int) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-events", "subscriber", f"dropped {dropped} event(s): slow reader")
        except Exception:  # noqa: BLE001 - marking must never stop the stream
            pass
