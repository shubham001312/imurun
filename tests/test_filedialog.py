"""The server-side file dialog never blocks or crashes the server."""
from __future__ import annotations

import json
import subprocess

from services.server import filedialog


def test_patterns_normalise():
    assert filedialog.normalise_patterns(["*.json", ["JSON", "*.json"], 5, ""]) == [
        ("*.json", "*.json"), ("JSON", "*.json")]


def test_pick_file_parses_helper_output():
    seen = {}

    def runner(spec, timeout):
        seen.update(spec)
        return "noise\n" + json.dumps({"available": True, "path": "C:/x/client_secret.json"})

    out = filedialog.pick_file("Pick", ["client_secret*.json"], runner=runner)
    assert out == {"available": True, "path": "C:/x/client_secret.json", "reason": ""}
    assert seen["title"] == "Pick" and seen["filetypes"] == [("client_secret*.json", "client_secret*.json")]


def test_cancel_timeout_and_missing_tk_are_results_not_errors():
    assert filedialog.pick_file(runner=lambda s, t: json.dumps({"available": True, "path": ""}))["path"] is None
    assert filedialog.pick_file(runner=lambda s, t: json.dumps({"available": False, "reason": "no tk"})) == {
        "available": False, "path": None, "reason": "no tk"}

    def timeout(spec, t):
        raise subprocess.TimeoutExpired("x", t)

    assert filedialog.pick_file(runner=timeout)["path"] is None

    def boom(spec, t):
        raise OSError("no interpreter")

    assert filedialog.pick_file(runner=boom)["available"] is False
    assert filedialog.pick_file(runner=lambda s, t: "")["available"] is False


def test_handle_maps_to_status_codes(monkeypatch):
    monkeypatch.setattr(filedialog, "pick_file", lambda *a, **k: {"available": False, "path": None, "reason": "no tk"})
    status, body = filedialog.handle({})
    assert status == 501 and "no tk" in body["error"]
    monkeypatch.setattr(filedialog, "pick_file", lambda *a, **k: {"available": True, "path": None, "reason": ""})
    assert filedialog.handle({"title": "x"}) == (200, {"path": None, "cancelled": True})
    monkeypatch.setattr(filedialog, "pick_file", lambda *a, **k: {"available": True, "path": "/a", "reason": ""})
    assert filedialog.handle("not a dict") == (200, {"path": "/a", "cancelled": False})


def test_helper_script_reports_missing_tk_cleanly():
    # Run the real helper with tkinter hidden: it must print a JSON answer, not a traceback.
    import sys

    script = "import sys; sys.modules['tkinter'] = None\n" + filedialog._SCRIPT
    proc = subprocess.run([sys.executable, "-I", "-c", script], input="{}", text=True,
                          capture_output=True, timeout=60)
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["available"] is False and "tkinter" in data["reason"]
