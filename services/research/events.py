"""Where research events go.

The pipeline publishes ``research.plan`` / ``research.search`` /
``research.fetch`` / ``research.done``. A caller that has the event bus passes
``publish``; otherwise the process-wide sink set with ``set_publisher`` is used
(the server sets it once at startup, see docs/research/REQUESTS.md). Without
either, events are dropped. Publishing never raises into the pipeline.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

Publisher = Callable[[str, dict], Any]

_lock = threading.Lock()
_publisher: Publisher | None = None


def set_publisher(publish: Publisher | None) -> None:
    global _publisher
    with _lock:
        _publisher = publish


def get_publisher() -> Publisher | None:
    with _lock:
        return _publisher


def emit(kind: str, data: dict, publish: Publisher | None = None) -> None:
    target = publish or get_publisher()
    if target is None:
        return
    try:
        target(kind, data)
    except Exception:  # noqa: BLE001 - the UI feed must not break research
        pass
