"""Launcher tests: the server must come up, and a failing native window must
never take it down (first-run safety).

These start real processes with `sys.executable`, so they run with whatever
interpreter runs pytest (use the project .venv).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
START = ROOT / "start.py"

# The window tests start the real app with a native window on the developer's
# screen. They are opt-in so a routine `pytest` run (or an agent running the
# suite repeatedly) never pops windows: set IMURUN_WINDOW_TESTS=1 to run them.
window_tests = pytest.mark.skipif(
    not os.environ.get("IMURUN_WINDOW_TESTS"),
    reason="opens a native window on this screen; set IMURUN_WINDOW_TESTS=1 to run",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.3)
    return False


def _spawn(args: list[str], env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_extra)
    # Never pop a real browser during tests: an unknown BROWSER command makes
    # webbrowser.open fail quietly.
    env.setdefault("BROWSER", "imurun-no-browser-for-tests")
    return subprocess.Popen(
        [sys.executable, str(START), *args],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )


def _stop(proc: subprocess.Popen) -> tuple[str, str]:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    out, err = proc.communicate(timeout=10)
    return out or "", err or ""


def test_server_starts_without_opening_anything():
    port = _free_port()
    proc = _spawn(["--no-open", "--port", str(port)], {"IMURUN_DATA_ROOT": str(ROOT / ".pytest-imurun-home")})
    try:
        if not _wait_health(port):
            out, err = _stop(proc)
            pytest.fail(f"server never answered /health (exit {proc.returncode})" + chr(10)
                        + "--- stdout ---" + chr(10) + out + chr(10) + "--- stderr ---" + chr(10) + err)
        assert proc.poll() is None, "server exited early"
    finally:
        out, err = _stop(proc)
    assert "Traceback" not in err, err


@window_tests
def test_failing_window_falls_back_and_server_survives():
    """A window process that dies must not kill the server (first-run safety)."""
    port = _free_port()
    proc = _spawn(
        ["--window", "--port", str(port)],
        {"IMURUN_WINDOW_SELFTEST_FAIL": "1", "IMURUN_DATA_ROOT": str(ROOT / ".pytest-imurun-home")},
    )
    try:
        assert _wait_health(port), "server never answered /health"
        # The window has failed by now; the launcher must still be serving.
        time.sleep(2.0)
        assert proc.poll() is None, "launcher exited after the window failed"
        assert _wait_health(port, timeout=5)
    finally:
        out, err = _stop(proc)
    assert "could not be shown" in err, err
    assert "Traceback" not in err, err
    assert "Unhandled Exception" not in err, err


@window_tests
@pytest.mark.skipif(sys.platform != "win32", reason="native window check is run on Windows here")
def test_native_window_stays_up_when_available():
    try:
        import webview  # noqa: F401
    except Exception:
        pytest.skip("pywebview not installed")
    if os.environ.get("CI"):
        pytest.skip("no display on CI")
    port = _free_port()
    proc = _spawn(["--window", "--port", str(port)], {"IMURUN_DATA_ROOT": str(ROOT / ".pytest-imurun-home")})
    try:
        assert _wait_health(port)
        time.sleep(8.0)  # longer than the launcher's startup grace
        if proc.poll() is not None:
            # Exit code 0 here means the window process closed *itself* after
            # the grace period; the launcher then stops as designed. Keep the
            # process output in the failure so an intermittent close (seen once
            # in a full-suite run, never alone) can be diagnosed.
            out, err = proc.communicate(timeout=10)
            log = Path.home() / ".imurun" / "logs" / "window.log"
            window_log = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "(no window.log)"
            pytest.fail(
                f"launcher exited with code {proc.returncode} while the window should be up\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n--- window.log ---\n{window_log}"
            )
    finally:
        out, err = _stop(proc)
    assert "Unhandled Exception" not in err, err
    assert "could not be shown" not in err, err
