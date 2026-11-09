"""Warm start: load the model the user last chose right after the server starts.

Before this, the weights were loaded by the *first message* of the session, so
the first reply of every launch paid the whole load (8B on this laptop: read
4.9 GB, re-quantise, upload to VRAM) before its first token - the owner
measured 225 s. Now the load starts in a background thread as soon as the
server is up, at below-normal process priority and with the governor's
thread/RAM/VRAM budget, so the UI opens at once and the first message finds
the weights resident (or waits only for the remainder of the load).

Which model: ``cfg.fish.model_path`` (``--model`` / ``FISH_MODEL``) if set,
else the last model picked in the Models tab (``<home>/last_model.json``,
written by ``POST /api/models/select``). No model known = nothing loads;
picking a model stays the user's decision. ``IMURUN_WARM_START=0`` turns the
warm start off. Progress: ``/api/status`` ``engine.warm_start`` and the
events ``model.load.start`` / ``model.load.done`` / ``model.load.error``.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

OFF_VALUES = ("0", "false", "no", "off")

STATE: dict = {"state": "idle", "model": "", "seconds": 0.0, "error": "", "started_at": 0.0}


def enabled() -> bool:
    return os.environ.get("IMURUN_WARM_START", "1").strip().lower() not in OFF_VALUES


def pick_model(cfg, home) -> Path | None:
    """The file to warm, or None when the user never chose one."""
    configured = str(getattr(getattr(cfg, "fish", None), "model_path", "") or "")
    if configured and Path(configured).is_file():
        return Path(configured)
    try:
        from ..config import load_last_model, resolve_model_path

        resolved = resolve_model_path(Path(home), load_last_model(Path(home)))
    except Exception:  # noqa: BLE001 - a broken registry must not stop the server
        return None
    if resolved and Path(resolved).is_file():
        return Path(resolved)
    return None


def view() -> dict:
    out = dict(STATE)
    if out["state"] == "loading" and out["started_at"]:
        out["seconds"] = round(time.time() - out["started_at"], 1)
    out.pop("started_at", None)
    return out


def start(server, cfg, home) -> threading.Thread | None:
    """Kick off the background load. Returns the thread, or None when nothing
    is to be loaded (no model known, hosted model, warm start disabled)."""
    if not enabled():
        return None
    turns = getattr(server, "_turns", None)
    model = getattr(server, "_model", None)
    if turns is None or not hasattr(turns, "request_model_switch") or model is None:
        return None
    if getattr(model, "remote", False) or getattr(model, "loaded", False):
        return None
    target = pick_model(cfg, home)
    if target is None:
        return None

    events = getattr(server, "_events", None)
    publish = getattr(events, "publish", None)

    def emit(name: str, data: dict) -> None:
        if callable(publish):
            try:
                publish(name, data)
            except Exception:  # noqa: BLE001
                pass

    def run() -> None:
        STATE.update(state="loading", model=target.name, started_at=time.time(), seconds=0.0, error="")
        emit("model.load.start", {"model_name": target.name, "reason": "warm_start"})
        guard = None
        try:
            from ..resources import get_governor

            guard = get_governor().priority
            guard.acquire("load")
        except Exception:  # noqa: BLE001 - priority is a courtesy, never a requirement
            guard = None
        started = time.time()
        try:
            result = turns.request_model_switch(target)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if guard is not None:
                try:
                    guard.release("load")
                except Exception:  # noqa: BLE001
                    pass
        STATE["seconds"] = round(time.time() - started, 2)
        if result.get("ok"):
            STATE["state"] = "ready"
            emit("model.load.done", {"model_name": target.name, "seconds": STATE["seconds"],
                                     "reason": "warm_start"})
        else:
            STATE.update(state="error", error=str(result.get("error") or "the model did not load"))
            emit("model.load.error", {"model_name": target.name, "error": STATE["error"],
                                      "reason": "warm_start"})
            print(f"warm start: {target.name} did not load: {STATE['error']}", file=sys.stderr)

    thread = threading.Thread(target=run, name="imurun-warm-start", daemon=True)
    thread.start()
    return thread
