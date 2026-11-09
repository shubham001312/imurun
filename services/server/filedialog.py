"""A native "choose a file" dialog for the page (API-REQUESTS 32).

The native window runs in its own process and exposes no ``js_api`` to the
page (the strict CSP and the process split make the pywebview bridge
unattractive), so the page asks the server instead: ``POST
/api/system/pick-file {title?, patterns?}``. The server opens the OS dialog
through Tk in a short-lived subprocess, so a stuck or missing Tk can never
block or crash the server, and returns ``{"path": str | None}``.

Tk ships with python.org builds on Windows and macOS; on Linux it needs the
distribution's ``python3-tk``. Without it the route answers 501 with a reason
and the page falls back to a typed path. The server only ever runs on the
user's own machine (loopback, bearer token), so a dialog on the server side
is the user's own screen.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

TIMEOUT_S = 300.0  # the user may take a while to find the file

_SCRIPT = r"""
import json, sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    print(json.dumps({"available": False, "reason": "tkinter is not installed (%s)" % type(exc).__name__}))
    sys.exit(0)
spec = json.loads(sys.stdin.read() or "{}")
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
types = [(str(label), str(pattern)) for label, pattern in spec.get("filetypes") or []] or [("All files", "*.*")]
path = filedialog.askopenfilename(title=spec.get("title") or "Choose a file", filetypes=types, parent=root)
root.destroy()
print(json.dumps({"available": True, "path": path or None}))
"""


def normalise_patterns(patterns: Any) -> list[tuple[str, str]]:
    """``["*.json", ["JSON", "*.json"]]`` -> ``[("*.json", "*.json"), ("JSON", "*.json")]``."""
    out: list[tuple[str, str]] = []
    for item in patterns or []:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), item.strip()))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return out


def pick_file(title: str = "", patterns: Any = None, *, timeout: float = TIMEOUT_S,
              runner=None) -> dict[str, Any]:
    """Open the dialog; never raises. ``runner`` is for tests."""
    spec = {"title": str(title or "Choose a file")[:200], "filetypes": normalise_patterns(patterns)}
    run = runner or _run
    try:
        out = run(spec, timeout)
    except subprocess.TimeoutExpired:
        return {"available": True, "path": None, "reason": "the dialog was not answered in time"}
    except Exception as exc:  # noqa: BLE001 - a missing interpreter or Tk is "unavailable"
        return {"available": False, "path": None, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        data = json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"available": False, "path": None, "reason": "the dialog helper gave no answer"}
    return {"available": bool(data.get("available", True)), "path": data.get("path") or None,
            "reason": str(data.get("reason") or "")}


def _run(spec: dict, timeout: float) -> str:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT], input=json.dumps(spec), text=True,
        capture_output=True, timeout=timeout, encoding="utf-8", errors="replace",
    )
    return proc.stdout


def handle(payload: dict | None) -> tuple[int, dict]:
    """The route body: ``(status, json)``."""
    payload = payload if isinstance(payload, dict) else {}
    result = pick_file(str(payload.get("title") or ""), payload.get("patterns"))
    if not result["available"]:
        return 501, {"error": f"no file dialog on this machine: {result['reason']}", "path": None}
    return 200, {"path": result["path"], "cancelled": result["path"] is None}
