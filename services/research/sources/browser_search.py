"""Search through the app's embedded browser (helper protocol v1).

The server cannot drive the UI window directly (it is another process), so
this source starts a *search helper*: ``python window.py --search-helper``,
a pywebview process with one hidden window, and talks to it with one JSON
object per line over the helper's stdin/stdout. The full contract, including
what the window side must and must not do, is in
``docs/research/BROWSER-SEARCH.md``. In short::

    helper -> {"type": "ready", "protocol": 1}
    server -> {"id": "...", "op": "search", "url": "...", "script": "...", "wait_ms": 8000}
    helper -> {"id": "...", "ok": true, "result": {...}, "final_url": "...", "elapsed_ms": 1234}

The engine-specific parts (results URL + extraction script) live here, so
adding an engine never needs a window-side change. The script returns
``{"status": "ok"|"pending"|"blocked"|"empty", "results": [...]}``; the helper
keeps evaluating it until it is not ``pending``. A bot check / captcha is
``blocked``: reported, never solved or clicked through.

The source is **off by default** (``IMURUN_BROWSER_SEARCH=1`` enables it) and
reports itself unavailable, with the reason, until ``window.py`` implements
the helper (it must contain the ``--search-helper`` marker).
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .. import config
from ..types import Hit, Plan
from . import Source, SourceError

PROTOCOL = 1
HELPER_FLAG = "--search-helper"
READY_TIMEOUT = 20.0
RETRY_AFTER = 600.0
MAX_RESULTS = 8
ROOT = Path(__file__).resolve().parents[3]
WINDOW_SCRIPT = ROOT / "window.py"

# Each script is an expression evaluated in the results page. It only reads
# the DOM; it never clicks, types or submits anything.
_DDG_SCRIPT = r"""
(function () {
  var text = (document.body && document.body.innerText || '').toLowerCase();
  if (document.querySelector('.anomaly-modal, #challenge-form') || text.indexOf('unusual traffic') >= 0)
    return {status: 'blocked'};
  var rows = document.querySelectorAll('.result');
  if (!rows.length) return {status: document.readyState === 'complete' ? 'empty' : 'pending'};
  var out = [];
  rows.forEach(function (r) {
    var a = r.querySelector('a.result__a'); if (!a) return;
    var s = r.querySelector('.result__snippet');
    out.push({title: a.innerText, url: a.href, snippet: s ? s.innerText : ''});
  });
  return {status: 'ok', results: out.slice(0, 10)};
})()
"""
_BING_SCRIPT = r"""
(function () {
  if (document.querySelector('#b_captcha, .b_captcha, iframe[src*="challenge"]')) return {status: 'blocked'};
  var rows = document.querySelectorAll('#b_results > li.b_algo');
  if (!rows.length) return {status: document.readyState === 'complete' ? 'empty' : 'pending'};
  var out = [];
  rows.forEach(function (r) {
    var a = r.querySelector('h2 a'); if (!a) return;
    var s = r.querySelector('.b_caption p, .b_lineclamp2, .b_lineclamp3, .b_algoSlug');
    out.push({title: a.innerText, url: a.href, snippet: s ? s.innerText : ''});
  });
  return {status: 'ok', results: out.slice(0, 10)};
})()
"""
ENGINES: dict[str, tuple[str, str]] = {
    "duckduckgo": ("https://html.duckduckgo.com/html/?q={q}", _DDG_SCRIPT),
    "bing": ("https://www.bing.com/search?q={q}&setlang=en", _BING_SCRIPT),
}


def unwrap(url: str) -> str:
    """Search engines wrap result links in redirects; return the target."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else url
    return url


def helper_supported(script: Path = WINDOW_SCRIPT) -> bool:
    try:
        return HELPER_FLAG in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


class HelperClient:
    """One helper process, one request at a time. Thread-safe."""

    def __init__(self, command: list[str] | None = None, *, ready_timeout: float = READY_TIMEOUT) -> None:
        self._command = command or [sys.executable, str(WINDOW_SCRIPT), HELPER_FLAG, str(os.getpid())]
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._failed_at = 0.0
        self.last_error = ""

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._lines.put(line)
        except Exception:  # noqa: BLE001
            pass
        self._lines.put(None)

    def _start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER:
            raise SourceError(f"browser helper unavailable: {self.last_error}")
        self._lines = queue.Queue()
        try:
            proc = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            self._fail(f"could not start: {exc}")
            raise SourceError(f"browser helper: {self.last_error}") from exc
        threading.Thread(target=self._reader, args=(proc,), name="imurun-search-helper", daemon=True).start()
        deadline = time.monotonic() + self._ready_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                self._fail("no ready message")
                raise SourceError("browser helper: did not report ready")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self._fail(f"exited with {proc.poll()}")
                raise SourceError(f"browser helper: {self.last_error}")
            message = self._decode(line)
            if message.get("type") == "ready":
                if int(message.get("protocol") or 0) != PROTOCOL:
                    proc.kill()
                    self._fail(f"protocol {message.get('protocol')} (want {PROTOCOL})")
                    raise SourceError(f"browser helper: {self.last_error}")
                self._proc = proc
                self._failed_at = 0.0
                return

    def _fail(self, why: str) -> None:
        self.last_error = why
        self._failed_at = time.monotonic()
        self._proc = None

    @staticmethod
    def _decode(line: str) -> dict:
        try:
            value = json.loads(line)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def request(self, payload: dict, timeout: float) -> dict:
        with self._lock:
            self._start()
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            request_id = uuid.uuid4().hex[:12]
            try:
                proc.stdin.write(json.dumps({"id": request_id, **payload}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail(f"write failed: {exc}")
                raise SourceError("browser helper: the pipe is closed") from exc
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("browser helper: timed out")
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    self._fail("exited")
                    raise SourceError("browser helper: exited during the request")
                message = self._decode(line)
                if message.get("id") == request_id:
                    return message

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()


class BrowserSearchSource(Source):
    name = "browser"

    def __init__(self, client: HelperClient | None = None, engines: tuple[str, ...] = ("duckduckgo", "bing"),
                 *, require_flag: bool = True, script: Path = WINDOW_SCRIPT) -> None:
        self._client = client
        self._engines = engines
        self._require_flag = require_flag
        self._script = script
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if self._require_flag and not config.browser_search_enabled():
            return False, "browser search is off (set IMURUN_BROWSER_SEARCH=1 to enable)"
        if self._client is None:
            if not helper_supported(self._script):
                return False, "the window helper (window.py --search-helper) is not implemented yet"
            if importlib.util.find_spec("webview") is None:
                return False, "pywebview is not installed"
        client = self._client
        if client is not None and client._failed_at and time.monotonic() - client._failed_at < RETRY_AFTER:
            return False, f"browser helper failed recently: {client.last_error}"
        return True, ""

    @property
    def client(self) -> HelperClient:
        with self._lock:
            if self._client is None:
                self._client = HelperClient()
            return self._client

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        errors: list[str] = []
        blocked = True
        for engine in self._engines:
            template, script = ENGINES[engine]
            url = template.format(q=quote_plus(query))
            try:
                reply = self.client.request({"op": "search", "url": url, "script": script,
                                             "wait_ms": int(timeout * 1000)}, timeout + 2.0)
            except SourceError as exc:
                errors.append(f"{engine}: {exc}")
                blocked = False
                break  # the helper itself is broken; the other engine will not do better
            if not reply.get("ok"):
                errors.append(f"{engine}: {reply.get('error') or 'failed'}")
                blocked = False
                continue
            result = reply.get("result") or {}
            status = str(result.get("status") or "")
            if status == "blocked":
                errors.append(f"{engine}: refused the search (bot check)")
                continue
            hits: list[Hit] = []
            for index, row in enumerate(result.get("results") or []):
                if not isinstance(row, dict):
                    continue
                target = unwrap(str(row.get("url") or ""))
                title = " ".join(str(row.get("title") or "").split())
                if not title or not target.startswith(("http://", "https://")):
                    continue
                hits.append(Hit(title=title, url=target, snippet=" ".join(str(row.get("snippet") or "").split()),
                                source=f"browser:{engine}", query=query, rank=index))
                if len(hits) >= MAX_RESULTS:
                    break
            if hits or status == "empty":
                return hits
        raise SourceError("; ".join(errors) or "browser search failed", blocked=blocked and bool(errors))
