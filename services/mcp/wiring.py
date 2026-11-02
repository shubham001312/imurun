"""Attach an ``McpManager`` to a live ApiServer (called from connectors.wiring)."""
from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any

from .registry import McpManager


def wire_mcp(server: Any, home: Path | str, *, start: bool = True, transport_factory=None) -> dict:
    executor = getattr(server, "_executor", None)
    registry = getattr(executor, "registry", None) or getattr(getattr(server, "_tools", None), "registry", None)
    events = getattr(server, "_events", None)
    record = getattr(server, "_record", None)
    manager = McpManager(
        getattr(server, "_settings", None),
        registry,
        publish=getattr(events, "publish", None),
        mark=record if callable(record) else None,
        home=home,
        transport_factory=transport_factory,
    )
    previous = getattr(server, "_mcp", None)
    if previous is not None:
        previous.close_all()
    server._mcp = manager
    # Stdio children see EOF on stdin when this process ends; closing them
    # explicitly also kills a server that ignores EOF.
    atexit.register(manager.close_all)
    manager.sync(background=True, connect=start)
    return {"servers": len(manager.configs()),
            "enabled": sum(1 for c in manager.configs() if c.enabled),
            "connecting": bool(start)}
