"""IMURUN native window — runs in its own process.

The UI shell lives in a separate process from the server on purpose: a
failure anywhere in the window stack (a WebView2/GTK/Qt problem, a .NET
exception from an icon, a driver quirk) can only kill *this* process. The
launcher (`start.py`) notices, opens the browser instead, and keeps serving.

Usage:  python window.py <url> [title]
Exit codes: 0 window closed by the user, 3 the window could not be shown.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BRAND_DIR = Path(__file__).resolve().parent / "frontend" / "design-lab"
APP_USER_MODEL_ID = "ImuTechz.IMURUN"


def _brand_process() -> None:
    """Own taskbar group and icon instead of python.exe's (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        ctypes.windll.kernel32.SetConsoleTitleW("IMURUN")
    except Exception:  # noqa: BLE001 - cosmetic
        pass


def _apply_window_icon(window) -> None:
    """Set the window icon through Win32 (LoadImage + WM_SETICON).

    System.Drawing.Icon rejects .ico files with PNG-compressed entries; the
    Win32 loader accepts them. Nothing here may raise into the .NET thread.
    """
    if sys.platform != "win32":
        return
    try:
        ico = BRAND_DIR / "logo.ico"
        if not ico.is_file():
            return
        native = getattr(window, "native", None)
        hwnd = int(native.Handle.ToInt64()) if native is not None else 0
        if not hwnd:
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x0010, 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        big = user32.LoadImageW(None, str(ico), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        small = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small or big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small or big)
    except BaseException:  # noqa: BLE001
        pass


def _watch_parent(parent_pid: int, on_gone) -> None:
    """Close the window when the launcher that owns it is gone.

    If start.py is killed (console closed, script timeout, task manager) the
    window must not survive it pointing at a dead server. Windows: wait on the
    parent's process handle. POSIX: poll until we are re-parented.
    """
    if parent_pid <= 0:
        return
    import threading

    def _run() -> None:
        import time

        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.windll.kernel32
                k32.OpenProcess.restype = wintypes.HANDLE
                k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                k32.WaitForSingleObject.restype = wintypes.DWORD
                k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                k32.GetExitCodeProcess.restype = wintypes.BOOL
                k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                SYNCHRONIZE, QUERY = 0x00100000, 0x1000
                WAIT_OBJECT_0, STILL_ACTIVE = 0, 259
                handle = k32.OpenProcess(SYNCHRONIZE | QUERY, False, parent_pid)
                if not handle:
                    return  # cannot observe the parent: never guess that it died
                while True:
                    rc = k32.WaitForSingleObject(handle, 2000)
                    if rc == WAIT_OBJECT_0:
                        break
                    if rc != 0x102:  # not WAIT_TIMEOUT: fall back to polling the exit code
                        code = wintypes.DWORD()
                        if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                            break
                        time.sleep(2.0)
            else:
                while os.getppid() == parent_pid:
                    time.sleep(1.0)
        except Exception:  # noqa: BLE001
            return
        try:
            on_gone()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name="imurun-parent-watch", daemon=True).start()


def search_helper(argv: list[str]) -> int:
    """Hidden-browser search helper (protocol v1, docs/research/BROWSER-SEARCH.md).

    The server starts this as its own process: `window.py --search-helper
    <server_pid>`. A hidden, private-mode webview loads a search results page
    the way the user's own browser would and returns what a read-only script
    finds on it. It never clicks, types or solves anything: a challenge page
    is reported as `blocked`. stdout carries only protocol lines.
    """
    import json
    import threading
    import time

    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        print(f"pywebview unavailable: {exc}", file=sys.stderr)
        return 3

    parent = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
    out_lock = threading.Lock()

    def send(obj: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    _brand_process()
    window = webview.create_window("imurun-search", "about:blank", hidden=True)
    loaded = threading.Event()
    try:
        window.events.loaded += lambda *_: loaded.set()
    except Exception:  # noqa: BLE001
        pass

    def serve() -> None:
        send({"type": "ready", "protocol": 1, "engines": ["duckduckgo", "bing"]})
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if not isinstance(req, dict):
                continue
            op, rid = req.get("op"), req.get("id")
            if op == "shutdown":
                break
            if op == "ping":
                send({"id": rid, "type": "pong"})
                continue
            if op != "search":
                send({"id": rid, "ok": False, "error": "unknown op"})
                continue
            url = str(req.get("url") or "")
            script = str(req.get("script") or "")
            if not url.startswith(("http://", "https://")) or not script:
                send({"id": rid, "ok": False, "error": "only http(s)"})
                continue
            started = time.monotonic()
            try:
                wait_ms = int(req.get("wait_ms") or 6000)
            except (TypeError, ValueError):
                wait_ms = 6000
            deadline = started + max(500, min(wait_ms, 30000)) / 1000.0
            value = None
            try:
                loaded.clear()
                window.load_url(url)
                if loaded.wait(max(0.1, deadline - time.monotonic())):
                    while time.monotonic() < deadline:
                        try:
                            value = window.evaluate_js(script)
                        except Exception as exc:  # noqa: BLE001
                            value = {"status": "error", "error": str(exc)[:200]}
                        if isinstance(value, dict) and value.get("status") not in (None, "pending"):
                            break
                        time.sleep(0.25)
            except Exception as exc:  # noqa: BLE001
                send({"id": rid, "ok": False, "error": f"navigation failed: {str(exc)[:200]}"})
                continue
            if isinstance(value, dict) and value.get("status") not in (None, "pending", "error"):
                try:
                    final_url = window.get_current_url()
                except Exception:  # noqa: BLE001
                    final_url = url
                send({"id": rid, "ok": True, "result": value, "final_url": final_url,
                      "elapsed_ms": int((time.monotonic() - started) * 1000)})
            else:
                err = value.get("error") if isinstance(value, dict) else None
                send({"id": rid, "ok": False, "error": err or "timeout"})
            try:
                window.load_url("about:blank")
            except Exception:  # noqa: BLE001
                pass
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    _watch_parent(parent, lambda: window.destroy())
    try:
        webview.start(serve, private_mode=True)
    except TypeError:  # older pywebview without private_mode
        webview.start(serve)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--search-helper":
        return search_helper(argv)
    if len(argv) < 2:
        print("usage: window.py <url> [title] [parent_pid]  |  window.py --search-helper <server_pid>",
              file=sys.stderr)
        return 2
    url = argv[1]
    title = argv[2] if len(argv) > 2 else "IMURUN"
    try:
        parent_pid = int(argv[3]) if len(argv) > 3 else 0
    except ValueError:
        parent_pid = 0
    # Test hook: lets the launcher's fallback path be exercised without a
    # broken machine (tests/test_launcher.py).
    if os.environ.get("IMURUN_WINDOW_SELFTEST_FAIL") == "1":
        print("selftest: simulated window failure", file=sys.stderr)
        return 3
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        print(f"pywebview unavailable: {exc}", file=sys.stderr)
        return 3
    try:
        _brand_process()
        window = webview.create_window(title, url, width=1200, height=800, min_size=(720, 560))
        try:
            window.events.shown += lambda *_: _apply_window_icon(window)
        except Exception:  # noqa: BLE001 - older pywebview without events
            pass
        _watch_parent(parent_pid, lambda: window.destroy())
        # pywebview's WinForms backend passes `icon` into System.Drawing.Icon,
        # which cannot read our PNG-entry .ico; Windows gets the icon above.
        png = BRAND_DIR / "logo-512.png"
        icon = str(png) if (png.is_file() and sys.platform != "win32") else None
        try:
            webview.start(icon=icon)
        except TypeError:
            webview.start()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - no backend, no display, ...
        print(f"native window failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
