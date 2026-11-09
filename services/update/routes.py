"""``GET/POST /api/update`` (wired in services/server/api.py) and the launch hook.

Contract: services/server/doc/README.md "Update contract".
"""
from __future__ import annotations

import sys

from .manager import UpdateManager, enabled, make_manager

ACTIONS = ("check", "apply", "dismiss", "later")


def manager_for(server) -> UpdateManager:
    """The server's manager, created on first use (no network)."""
    mgr = getattr(server, "_update", None)
    if mgr is None:
        events = getattr(server, "_events", None)
        mgr = make_manager(getattr(server, "_home", None),
                           publish=events.publish if events is not None else None)
        server._update = mgr
    return mgr


def start_checker(server) -> UpdateManager | None:
    """Count this launch and check in the background (app.py, after the warm start)."""
    try:
        mgr = manager_for(server)
        mgr.record_launch()
        if enabled():
            mgr.check_async()
        return mgr
    except Exception as exc:  # noqa: BLE001 - the shell must come up regardless
        print(f"update check: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def handle_get(server) -> tuple[int, dict]:
    return 200, manager_for(server).view()


def handle_post(server, payload: dict) -> tuple[int, dict]:
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in ACTIONS:
        return 400, {"error": "action must be one of: " + ", ".join(ACTIONS)}
    mgr = manager_for(server)
    if action == "check":
        mgr.check_async(force=True)
        return 202, {"ok": True, "update": mgr.view()}
    if action == "dismiss":
        return 200, {"ok": True, "update": mgr.dismiss()}
    if action == "later":
        return 200, {"ok": True, "update": mgr.later()}
    view = mgr.view()
    if view["state"] in ("checking", "updating", "restart_pending"):
        return 409, {"error": f"busy ({view['state']})", "update": view}
    if not view["available"]:
        return 409, {"error": "no update available", "update": view}
    if view["applyBlocked"]:
        return 409, {"error": view["applyBlocked"], "update": view}
    mgr.apply_async()
    return 202, {"ok": True, "update": mgr.view()}
