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

The source is **on by default** (``IMURUN_BROWSER_SEARCH=0`` switches it
off). It reports itself unavailable, with the reason, when pywebview is not
installed, when ``window.py`` lacks the ``--search-helper`` mode, or for 10
minutes after the helper failed to start (no GUI backend, WebView2
missing ...); the other sources run either way.

One call per plan: the plan's queries go to DuckDuckGo one after the other
inside the source's time budget; Bing is asked only when DuckDuckGo refused,
failed or found nothing. The helper exits after ``browser_idle_seconds()``
(10 minutes) without a search, and with the server.
"""
from __future__ import annotations

import base64
import binascii
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
from . import Backoff, Source, SourceError

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
    if (r.classList.contains('result--ad')) return;
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
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        # /ck/a?...&u=a1<base64url of the target>
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            payload = encoded[2:]
            try:
                target = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return url
            if target.startswith(("http://", "https://")):
                return target
    return url


def helper_supported(script: Path = WINDOW_SCRIPT) -> bool:
    try:
        return HELPER_FLAG in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


class HelperClient:
    """One helper process, one request at a time. Thread-safe."""

    def __init__(self, command: list[str] | None = None, *, ready_timeout: float = READY_TIMEOUT,
                 idle_seconds: float | None = None) -> None:
        self._command = command or [sys.executable, str(WINDOW_SCRIPT), HELPER_FLAG, str(os.getpid())]
        self._ready_timeout = ready_timeout
        self._idle = idle_seconds
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._failed_at = 0.0
        self._last_used = 0.0
        self._reaper: threading.Thread | None = None
        self.last_error = ""

    def _idle_limit(self) -> float:
        return self._idle if self._idle is not None else config.browser_idle_seconds()

    def _reap(self) -> None:
        """Stop the helper (a WebView process holds ~100 MB) when unused."""
        while True:
            limit = self._idle_limit()
            time.sleep(max(0.05, min(30.0, limit / 4)))
            if not self._lock.acquire(blocking=False):
                continue  # a request is running
            try:
                proc = self._proc
                if proc is None or proc.poll() is not None:
                    self._proc = None
                    self._reaper = None
                    return
                if time.monotonic() - self._last_used < limit:
                    continue
                self._proc = None
                self._reaper = None
            finally:
                self._lock.release()
            self._shutdown(proc)
            return

    @staticmethod
    def _shutdown(proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

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
                self._last_used = time.monotonic()
                if self._reaper is None:
                    self._reaper = threading.Thread(target=self._reap, name="imurun-search-helper-idle",
                                                    daemon=True)
                    self._reaper.start()
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
            self._last_used = time.monotonic()
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
        self._shutdown(proc)



class BrowserSearchSource(Source):
    name = "browser"
    per_plan = True  # the queries run one after the other in one helper session

    def __init__(self, client: HelperClient | None = None, engines: tuple[str, ...] = ("duckduckgo", "bing"),
                 *, require_flag: bool = True, script: Path = WINDOW_SCRIPT) -> None:
        self._client = client
        self._engines = engines
        self._require_flag = require_flag
        self._script = script
        self._lock = threading.Lock()
        # A results page that showed a bot check keeps that engine out for 10 minutes.
        self.refused = {engine: Backoff() for engine in ENGINES}

    def available(self) -> tuple[bool, str]:
        if self._require_flag and not config.browser_search_enabled():
            return False, "browser search is switched off (IMURUN_BROWSER_SEARCH=0)"
        if self._client is None:
            if not helper_supported(self._script):
                return False, "window.py has no --search-helper mode"
            if importlib.util.find_spec("webview") is None:
                return False, "pywebview is not installed (no embedded browser)"
        client = self._client
        if client is not None and client._failed_at and time.monotonic() - client._failed_at < RETRY_AFTER:
            return False, f"browser helper failed recently: {client.last_error}"
        if all(self.refused.get(e, Backoff()).parked()[0] for e in self._engines):
            return False, "every search engine showed a bot check in the last 10 minutes"
        return True, ""

    @property
    def client(self) -> HelperClient:
        with self._lock:
            if self._client is None:
                self._client = HelperClient()
            return self._client

    def _engine(self, engine: str, query: str, deadline: float) -> tuple[str, list[Hit], str]:
        """(status, hits, error) for one results page. status: ok | empty | blocked | failed | broken."""
        template, script = ENGINES[engine]
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            return "failed", [], f"{engine}: no time left"
        url = template.format(q=quote_plus(query))
        try:
            reply = self.client.request({"op": "search", "url": url, "script": script,
                                         "wait_ms": int(remaining * 1000)}, remaining + 1.5)
        except SourceError as exc:
            return "broken", [], f"{engine}: {exc}"
        if not reply.get("ok"):
            return "failed", [], f"{engine}: {reply.get('error') or 'failed'}"
        result = reply.get("result") or {}
        status = str(result.get("status") or "")
        if status == "blocked":
            self.refused[engine].refuse("bot check")
            return "blocked", [], f"{engine}: refused the search (bot check)"
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
        return ("ok" if hits else "empty"), hits, ""

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        deadline = time.monotonic() + timeout
        queries = [q for q in (plan.queries or [query]) if q.strip()][:3] or [query]
        errors: list[str] = []
        blocked = True
        answered = False
        hits: list[Hit] = []
        for number, text in enumerate(queries):
            if number and time.monotonic() > deadline - 1.0:
                break
            for engine in self._engines:
                if self.refused[engine].parked()[0]:
                    continue
                status, found, error = self._engine(engine, text, deadline)
                if error:
                    errors.append(error)
                if status == "broken":
                    # The helper itself failed; another engine or query will not do better.
                    raise SourceError("; ".join(errors), blocked=False)
                if status != "blocked":
                    blocked = False
                if status in ("ok", "empty"):
                    answered = True
                if found:
                    hits.extend(found)
                    break
        if hits or answered:
            return hits
        raise SourceError("; ".join(errors) or "browser search failed", blocked=blocked and bool(errors))
