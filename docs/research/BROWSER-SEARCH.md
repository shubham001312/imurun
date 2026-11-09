# Browser search helper — protocol v1

Status (2026-11-09): **both sides implemented and on by default.** Server side:
`services/research/sources/browser_search.py` (tested against a fake helper,
`tests/fixtures/research/fake_search_helper.py`). Window side:
`window.py::search_helper` (`python window.py --search-helper <server_pid>`),
written to this spec; R2 in `REQUESTS.md` is closed. Measured live on Windows 11
(WebView2), driven over the protocol by a scratch script with a hidden window,
which was shut down afterwards: helper `ready` in 1.3 s; DuckDuckGo 2.7 s cold /
1.2-1.7 s warm, Bing 0.8-1.1 s, 10 results each, no challenge shown. Not yet
measured: macOS (WebKit) and Linux (GTK / Qt WebKit). Where pywebview or a GUI
backend is missing, the source says why and the other sources run (below).

When it is unavailable the `browser` source reports one of: "browser search is
switched off (IMURUN_BROWSER_SEARCH=0)", "pywebview is not installed (no
embedded browser)", "window.py has no --search-helper mode", "browser helper
failed recently: …" (it exited before `ready`, e.g. no GUI backend, or did
not report ready in 20 s; retried after 10 minutes), or "every search engine
showed a bot check in the last 10 minutes".

Server-side behaviour added on 2026-11-09:

- **One call per plan**: the plan's queries (≤ 3) go one after the other in
  one helper session, inside the source's time budget (the per-source
  timeout, 6 s); each request's `wait_ms` is what is left of that budget.
- **Engine order**: DuckDuckGo; Bing only when DuckDuckGo refused (`blocked`),
  failed or found nothing for that query. An engine whose page showed a bot
  check is skipped for 10 minutes (per-engine memory).
- **Redirects unwrapped**: DuckDuckGo `/l/?uddg=` and Bing `/ck/a?…&u=a1<base64url>`
  become the target URL; DuckDuckGo ad rows (`.result--ad`) are skipped.
- **Idle exit**: the helper (a WebView process holds ~100 MB) is shut down after
  `IMURUN_BROWSER_SEARCH_IDLE` seconds (default 600) without a search, and
  started again by the next one (~1.3 s).
- Tests never start a real WebView: `tests/conftest.py` sets
  `IMURUN_BROWSER_SEARCH=0`; the protocol tests use the fake helper.

## Why

A bare HTTP client is answered with bot-check pages by most search result
pages. The app already ships a real browser engine (pywebview over WebView2 /
WebKit). A hidden webview can load a result page the way the user's own
browser would and hand back what is on it.

What this is **not**: a way around refusals. If the page shows a challenge or
captcha, the helper reports `blocked` and the engine moves on. The helper
never clicks, types, submits, scrolls to trigger anything, or solves
anything. Search engines' terms restrict automated use of their result pages;
the product owner decided on 2026-11-09 to ship the source **on by default**
(`IMURUN_BROWSER_SEARCH=0` switches it off).

## Process model

The server and the UI window are separate processes, and the UI window's
stdout/stderr are already read by `start.py` for error reporting. So the
helper is its **own process**, started on demand by the server:

```
<python> window.py --search-helper <server_pid>
```

- one hidden pywebview window (`webview.create_window("imurun-search", "about:blank", hidden=True)`),
  `webview.start(private_mode=True)` so no cookies or storage persist;
- stdin: requests, one JSON object per line (UTF-8);
- stdout: replies, one JSON object per line, flushed after each line;
  **nothing else may be printed to stdout** (log to stderr, which the server
  discards);
- exit when stdin reaches EOF, on `{"op": "shutdown"}`, or when `<server_pid>`
  dies (reuse `_watch_parent`);
- the server serialises requests (one in flight at a time), so the helper may
  process them sequentially;
- if the helper fails to start or report ready within 20 s, the server marks
  it unavailable for 10 minutes.

## Messages

Startup (helper → server), as soon as the hidden window exists:

```json
{"type": "ready", "protocol": 1, "engines": ["duckduckgo", "bing"]}
```

The server refuses any other `protocol` value.

Search (server → helper):

```json
{"id": "a1b2c3d4e5f6", "op": "search",
 "url": "https://html.duckduckgo.com/html/?q=northhaven+port+strike",
 "script": "(function(){ ... return {status: 'ok', results: [...]}; })()",
 "wait_ms": 6000}
```

Helper behaviour for `search`:

1. Refuse anything but `http(s)` URLs: reply `{"id", "ok": false, "error": "only http(s)"}`.
2. `window.load_url(url)`; wait for the `loaded` event (DOM ready).
3. Every 250 ms, `window.evaluate_js(script)`. The script returns an object
   `{status, results?}`:
   - `"pending"` — keep polling;
   - `"ok"` / `"empty"` / `"blocked"` — stop and reply.
4. Stop at `wait_ms` regardless; reply with the last script value or
   `{"ok": false, "error": "timeout"}`.
5. Reply:

```json
{"id": "a1b2c3d4e5f6", "ok": true,
 "result": {"status": "ok", "results": [{"title": "...", "url": "...", "snippet": "..."}]},
 "final_url": "https://html.duckduckgo.com/html/?q=...", "elapsed_ms": 1234}
```

   or on failure `{"id": "...", "ok": false, "error": "navigation failed: ..."}`.
6. Load `about:blank` afterwards so the page stops running.

Other ops: `{"op": "ping"}` → `{"id": <same>, "type": "pong"}`;
`{"op": "shutdown"}` → exit 0. Unknown ops → `{"id", "ok": false, "error": "unknown op"}`.

The helper is engine-agnostic: the URL template and the extraction script
live in the server (`browser_search.py::ENGINES`), so adding or fixing an
engine never touches `window.py`. Scripts only read the DOM.

## Window-side sketch (implemented: `window.py::search_helper`)

The real implementation follows this sketch, plus: a `wait_ms` clamp to
0.5–30 s, a script error reported as the reply's `error`, and a fallback to
`webview.start(serve)` on a pywebview without `private_mode`.

```python
# window.py — add before the normal UI path in main():
#   if len(argv) > 1 and argv[1] == "--search-helper": return search_helper(argv)
def search_helper(argv):
    import json, threading, time
    import webview
    parent = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
    out_lock = threading.Lock()
    def send(obj):
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
    window = webview.create_window("imurun-search", "about:blank", hidden=True)
    loaded = threading.Event()
    window.events.loaded += lambda *_: loaded.set()

    def serve():
        send({"type": "ready", "protocol": 1, "engines": ["duckduckgo", "bing"]})
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except ValueError:
                continue
            op, rid = req.get("op"), req.get("id")
            if op == "shutdown":
                break
            if op == "ping":
                send({"id": rid, "type": "pong"}); continue
            if op != "search":
                send({"id": rid, "ok": False, "error": "unknown op"}); continue
            url = str(req.get("url") or "")
            if not url.startswith(("http://", "https://")):
                send({"id": rid, "ok": False, "error": "only http(s)"}); continue
            started = time.monotonic(); deadline = started + int(req.get("wait_ms") or 6000) / 1000
            loaded.clear(); window.load_url(url)
            value = None
            if loaded.wait(max(0.1, deadline - time.monotonic())):
                while time.monotonic() < deadline:
                    try:
                        value = window.evaluate_js(req["script"])
                    except Exception as exc:
                        value = {"status": "error", "error": str(exc)}
                    if isinstance(value, dict) and value.get("status") not in (None, "pending"):
                        break
                    time.sleep(0.25)
            if isinstance(value, dict) and value.get("status") not in (None, "pending", "error"):
                send({"id": rid, "ok": True, "result": value, "final_url": window.get_current_url(),
                      "elapsed_ms": int((time.monotonic() - started) * 1000)})
            else:
                send({"id": rid, "ok": False, "error": "timeout"})
            window.load_url("about:blank")
        window.destroy()

    _watch_parent(parent, window.destroy)
    webview.start(serve, private_mode=True)
    return 0
```

Acceptance (met 2026-11-09): `python -m pytest tests/test_research_sources.py
tests/test_research_keyless.py` passes; with pywebview installed,
`BrowserSearchSource().available()` returns `(True, "")` by default;
`test_real_window_script_marker_is_detected` sees the `--search-helper` marker
and the spec's limits (`hidden=True`, `private_mode=True`, `_watch_parent`) in
`window.py`.

Live check, for anyone repeating it: drive the helper with
`HelperClient()._start()` + `request({"op": "search", ...})` from a script and
`close()` it afterwards (that sends `shutdown`; the hidden window never shows).
Never run `start.py` with a window from an agent.
