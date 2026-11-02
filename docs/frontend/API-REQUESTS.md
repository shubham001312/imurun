# API changes the frontend needs

The frontend works with the server as it is today and degrades honestly
where data is missing. These changes would let it show real numbers instead
of estimates or "not reported". Nothing here was changed on the server by
the frontend work. Paths refer to `services/server/`.

Priority: **P1** fixes something users see as wrong today, **P2** replaces an
estimate with real data, **P3** is new capability.

**Status (server owner, 2026-10-31 night):** done — #1 (`turn.end`/`step` stats),
`/api/status.engine` model info when loaded, #14 (window icon), #16 (`search`
settings section; keys write-only, exported to the tool environment), #17
(`turn.end.tools_used`), #20 (503 carries `reason: no_engine | no_model`),
#21 (`verification`/`sources` shapes confirmed as rendered), #23 (window tied
to the launcher's lifetime; no orphans). Open: customisation/voice defaults,
persona `tone`/`prompt`, per-session context tokens, download progress events,
session search, regenerate, `/api/voice/*`, #18 (persist tool activity with the
transcript), #19 (approval payload details), #22 (permission defaults listing).
See `docs/server/CHANGELOG.md` for details.

## P1

### 1. Engine counters in `turn.end`
`turns.py` already reads `inner.stats()` after each step (prefill/decode
counts) but does not publish them. Please add:

```json
{
  "type": "turn.end", "turn_id": "...", "status": "done", "steps": 1, "answer": "...",
  "stats": {
    "prompt_tokens": 512, "generated_tokens": 128,
    "prefill_seconds": 0.41, "decode_seconds": 3.2, "tokens_per_second": 40.0,
    "context_used": 640, "context_size": 8192
  }
}
```

The frontend already reads exactly these keys (`chat.js`
`Turn.prototype.addStats`) and labels them "engine"; without them it shows
browser-measured numbers labelled "measured here".

### 2. Default `customisation.accentColor` should be empty
`settings.py` `DEFAULTS["customisation"]["accentColor"] = "#b45309"` is
Liquid Paper's accent; any client that applies it pins every theme to amber.
Please default it to `""` (meaning "theme accent") and add
`"accentCustom": False`, `"fontScale": 1`. The frontend already handles the
current default by ignoring a colour unless `accentCustom` is true.

### 3. Persona field names
`PUT /api/persona` reads `{name, email_role, description}`; the Persona page
has Name, Tone and System prompt. The frontend now sends both
`{email_role: tone, description: prompt}` and `{tone, prompt}`. Please store
`tone` and `prompt` explicitly (or confirm the mapping) and return them from
`GET /api/persona`.

### 4. `voice` settings defaults
Add `"voice": {"lang": "", "voiceURI": "", "rate": 1, "sounds": True}` to
`settings.py` `DEFAULTS` so `GET /api/settings/voice` returns defaults and
`GET /api/settings` lists it. (It already works as an ad-hoc section.)

## P2

### 5. Model details in `/api/status`
`/api/status.engine` currently comes from `EngineClient.status()` and holds
only `{engine, available}`. `fish_native.py` already has `info()` with
`arch, layers, embd, ff, context, vocab, quant, chat_format, kernel`. Please
include it:

```json
"engine": {"engine": "fish", "available": true,
           "info": {"arch": "llama", "quant": "Q4_K_M", "kernel": "avx2",
                    "context": 8192, "layers": 28, "chat_format": "llama3"}}
```

`panels.js` reads `engine`, `engine.info` and `engine.model_info`, so either
nesting works.

### 6. Last-generation stats in `/api/status`
`"engine": {..., "stats": {<same keys as item 1>}}` so the performance
drawer can show context use for the current session after a reload.

### 7. Per-session context usage
`GET /api/sessions/{id}` could return `context_tokens` (tokens the next turn
will send, after compaction). The drawer currently estimates from the
visible transcript (characters / 4) and labels it "estimate".

### 8. Download progress on the event broker
Downloads report progress only on the `POST /api/models/download` response
stream, so a second tab or a reloaded page cannot see a running download.
Publishing `model.download.progress` frames (same payload as the POST
stream, plus `model_id`) on `/api/events` would let the shell restore the
progress pill. `api.js` already subscribes to that event name.

### 9. A list of active downloads
`GET /api/models/downloads` -> `[{model_id, status, downloaded_bytes,
total_bytes}]`, for the same reason.

## P3

### 10. Session search
`GET /api/sessions/search?q=...&limit=20` -> `[{session_id, name, snippet,
message_index}]` over transcript text. The palette searches names and ids
only; searching message text client-side would mean fetching every
transcript.

### 11. Delete a single message / regenerate
`POST /api/turns/{id}/retry` (re-run the last user prompt) would enable a
"Regenerate" action on replies.

### 12. Usage counters
`usage.py` notes that nothing calls `record_turn`/`record_tokens`; since
`turns.py` now calls `_record_usage`, please confirm the Usage panel numbers
are live. The frontend renders the `usage` frame directly.

### 13. Server-side speech (STT and TTS)
See `VOICE.md` for the contract the frontend expects:

- `GET /api/voice/status` -> `{stt: {available, engine, languages: [...]},
  tts: {available, engine, voices: [{id, name, lang, gender?}]}}`
- `POST /api/voice/transcribe` with `Content-Type: audio/webm;codecs=opus`
  (or `audio/wav`), query `?lang=hi-IN`, body = recorded audio, returns
  `{text, lang, segments?: [{start, end, text}], duration_seconds}`.
  Optionally a streaming variant on the event broker:
  `voice.partial {request_id, text}` and `voice.final {request_id, text}`.
- `POST /api/voice/speak` `{text, lang, voice_id?, rate?}` returns
  `audio/ogg` (or `audio/wav`) bytes.

CSP notes for the server: playing returned audio needs `media-src 'self'
blob:`; recording needs `navigator.mediaDevices.getUserMedia`, which is
allowed on `127.0.0.1`/`localhost` (secure context) and in WebView2 with
permission. No other CSP change is needed because uploads go to `'self'`.

### 14. Native window title and icon (launcher, `start.py`) - P1 for the product owner

**Done 2026-10-31** in `start.py` (`_brand_process`, `_apply_window_icon`,
`webview.start(icon=...)`), verified against pywebview 6.2.1 (`start(icon=)`
exists, `events.shown` exists, `logo.ico` loads via System.Drawing). Not yet
seen on screen — confirm the taskbar icon after restarting the server. The
`pythonw.exe` launch and the installer shortcut are still open.
The product owner reports the native window shows a Python logo, and wants
it named and branded IMURUN. The window title is already `"IMURUN"`
(`start.py:201`, `webview.create_window("IMURUN", ...)`) and the page
`<title>` is `IMURUN`. The Python logo comes from the host process
(`python.exe`): Windows groups the window under Python's AppUserModelID and
uses the interpreter's icon. The frontend now ships the assets:

- `frontend/design-lab/logo.ico` (16, 24, 32, 48, 64, 128, 256 px)
- `frontend/design-lab/logo-512.png`

Requested launcher change (setup scope; please verify against the installed
pywebview version):

```python
import sys, ctypes
from pathlib import Path
ICON = Path(__file__).parent / "frontend" / "design-lab"
if sys.platform == "win32":
    # Own taskbar group and icon instead of Python's (call before any window exists).
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ImuTechz.IMURUN")
    ctypes.windll.kernel32.SetConsoleTitleW("IMURUN")

window = webview.create_window("IMURUN", url, width=1200, height=800, min_size=(720, 560))

def _set_icon():
    # WinForms backend: the Form is window.native (pywebview >= 5).
    if sys.platform == "win32":
        import clr  # pythonnet, already a pywebview dependency on Windows
        clr.AddReference("System.Drawing")
        from System.Drawing import Icon
        window.native.Icon = Icon(str(ICON / "logo.ico"))

window.events.shown += _set_icon
webview.start(icon=str(ICON / "logo-512.png"))  # used by the GTK and Qt backends
```

Also worth doing: launch the window with `pythonw.exe` on Windows (no
console window), and have `install.py` create a desktop/Start-menu shortcut
pointing at it with `logo.ico`. A shortcut carrying the same AppUserModelID
makes the pinned taskbar icon IMURUN too. On macOS the Dock icon needs an
app bundle (`py2app`/`briefcase`) or `NSApplication.setApplicationIconImage_`.

### 15. CSP for the native window
If pywebview loads the shell from `http://127.0.0.1:<port>`, nothing
changes. If it ever loads files from disk (`file://`), the token injection
and `connect-src 'self'` will not work; keep serving it from the API.

## Agentic layer (added 2026-10-31 with the tool, approval and permission UI)

### 16. A `search` settings section the server reads (P1 for live answers)
`services/server/settings.py` has no section for the search keys, and
`services/tools/builtin/web_search.py` reads only environment variables. The
UI (Settings > Search providers, `agent-settings.js`) already reads and writes
`GET/PUT /api/settings/search` with these keys:

```json
{"provider": "", "braveApiKey": "", "tavilyApiKey": "", "serperApiKey": "", "searxngUrl": ""}
```

`provider` is `""` (automatic) or one of `brave`, `tavily`, `serper`,
`searxng`, `duckduckgo`. Requested server change:

1. `settings.py`: add `DEFAULTS["search"]` with the five keys above (empty strings).
2. `web_search.py`: a helper used by `_env` callers, read per search so a
   save applies without a restart, settings first and environment second:
   `IMURUN_SEARCH_PROVIDER` <- `provider`, `IMURUN_BRAVE_API_KEY` <- `braveApiKey`,
   `IMURUN_TAVILY_API_KEY` <- `tavilyApiKey`, `IMURUN_SERPER_API_KEY` <- `serperApiKey`,
   `IMURUN_SEARXNG_URL` <- `searxngUrl`. (The tool has no settings handle today;
   `ApiServer.__init__` can hand `self._settings` to the registry the same way
   it hands `_policy.workspace` to the workspace tools.)
3. **Never return the key values.** `GET /api/settings/search` (and
   `GET /api/settings`) should return each `*ApiKey` as `true`/`false` (set or
   not), and `PUT` should ignore a `true`/`false` value for those keys so a
   client that echoes the view back cannot wipe a key. The UI treats any
   non-empty value as "saved", never displays it and never caches the section
   in `localStorage`. Today the plaintext key comes back on GET.
4. Optional: `GET /api/tools` could report the active provider
   (`web_search.provider`) so the panel can say which one is in use.

### 17. `turn.end.tool_calls` is a count; the footer needs the list
`turns.py` publishes `tool_calls: <int>`. The "Used: ..." footer is built
from the `tool.call`/`tool.result` frames the page saw, so a recovered turn
(stream dropped) or a reloaded session can only say "N tool calls". Please add
`tools_used: [{tool, ok, decision, depth, latency_ms}]` to `turn.end` and to
`GET /api/turns/{id}` (keep `tool_calls` as the count).

### 18. Keep tool activity, verification and sources with the transcript
Only final answers are stored, so a reloaded session loses the tool rows,
skill chips, verification badge and sources. A `meta` object on assistant
messages from `GET /api/sessions/{id}/messages`
(`{tools_used, skills, verification, sources}`) would let `assistantMessage`
render the same footer.

### 19. `tool.approval_needed`: category, resolved paths, absolute deadline
The card works today from `tool` + `args` + `reasons` (category comes from a
built-in map or `GET /api/tools`). Better, and robust to clock skew:
`category`, `targets` (each path argument resolved to its absolute path inside
the workspace, as `Workspace.resolve` already computes), `workspace`, and
`expires_at` (epoch seconds) alongside `expires_in`.

### 20. 503 from `POST /api/turns`: say why
The body is `{"error": "model_unavailable", "message": "FISH engine is not
running. Use /api/stack/start ..."}` also when simply no model is selected,
which is the common case. Please add `reason: "no_model_selected" |
"engine_unavailable"` (and fix the message). The UI currently decides from
`/api/status.selected_model`: empty -> "Choose a model" (no toast, link to
Models); set -> the engine-start wait.

### 21. Verification and sources (coordinator item, already rendered)
The UI renders `verification` (`verified` | `unverified` | `n/a`) and
`sources: [{title, url, domain, provider, snippet?}]` from `step` and
`turn.end` (and from `GET /api/turns/{id}` for recovered turns). Until the
server sends `sources`, the pre-fetch `web_search.done.results` are shown as
sources; no badge is shown without `verification`. A `snippet` per source
enables the "Show snippets" toggle.

### 22. `permissions` defaults and `sendEmail`
Same as docs/agentic/REQUESTS.md item 6: list `mode`, `workspaceRoot`,
`approvalTimeout` in `DEFAULTS["permissions"]`. Also: `POST /api/permissions`
rejects `sendEmail` (not a policy key), so the connector toggle is written
with `PUT /api/settings/permissions {"sendEmail": bool}`; please keep that
merge-only PUT working, or accept `sendEmail` in `POST /api/permissions`.

### 23. Orphaned native windows with no server behind them (launcher, setup scope)
Seen on 2026-10-31 17:36: `window.py` (PIDs 21788 and its child 19588)
was still showing `http://127.0.0.1:57865/` after its parent `start.py`
(PID 18216) had exited, so the window showed the shell with no API (nothing
listened on 57865). `run_window` starts `window.py` with `subprocess.Popen`
and only terminates it on `KeyboardInterrupt`; if `start.py` is killed (a
timeout in a script or an agent run, closing the console), the window
survives. Suggested: pass the parent PID to `window.py` and have it close
itself when the parent is gone or `/health` stops answering for a few
seconds (and, on Windows, put the child in a job object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`). The frontend side already shows
"server unreachable" honestly, but it cannot close the window.
## Connectors, MCP, research and brand (added 2026-11-01)

The UI for these is built against the routes in `services/connectors/routes.py`,
`services/mcp/routes.py` and the events in `services/research/engine.py`
(docs/connectors/, docs/research/). Everything below degrades honestly today.

### 24. Serve the connector set-up docs (P2)
Settings > Connectors shows the set-up steps of each service inline. The
docs live in `docs/connectors/<SERVICE>.md`, outside the static root, so the
UI shows short built-in steps and the file path. Please add
`GET /api/connectors/{id}/doc` -> `{"markdown": "<contents of the doc named
in setup.SERVICES[id].doc>"}` (read-only, only those eight files). The UI
already calls it (`api.js` `getConnectorDoc`) and renders the Markdown with
the safe renderer when it answers; a 404 keeps the built-in steps.

### 25. `/oauth/callback` should publish `connector` (P1)
After a successful (or failed) exchange the callback only renders an HTML
page. The UI finishes a sign-in by polling `GET /api/connectors/{id}` every
2.5 s; a `connector` frame `{connector, connected, account?, error?}` from the
callback would finish it at once, also in a second window. The UI already
listens for it (`connectors-settings.js` `onConnectorEvent`).

### 26. `account` in the connector status (P2)
`GET /api/connectors` and `/api/connectors/{id}` do not name the signed-in
account, so a card says "Connected" until the user presses Test (Gmail's test
returns `account`). Please add `account` (address / login / workspace name,
whatever the provider's profile call gives) to the status view.

### 27. `permission_level` in `GET /api/tools` (P2)
**Done 2026-11-01:** every entry of `GET /api/tools` has `permission_level`
(`allow|read|write|ask|always|deny`; built-ins mapped from their category,
connector/MCP tools from `permission_level({})`), decided natively by
`PermissionPolicy` (`docs/agentic/CHANGELOG.md`).

Original request: Connector and MCP tools have a level (`read` / `write` / `always` / ...,
`connectors/tool_policy.py`), but `/api/tools` reports only `permission`
(`allow|ask|deny`). The Connectors panel shows "reads / asks / always asks"
per tool and today derives it from the category plus a small Gmail table
(`connectors-settings.js` `GMAIL_LEVELS`). `GET /api/mcp/servers/{id}/tools`
already has `permission_level`; the same field in `/api/tools` (from
`tool.permission_level({})`) would make it exact.

### 28. `customisation` defaults: ImuTechz (P1, product owner)
The default theme is now ImuTechz (the company website's look). Please set
`DEFAULTS["customisation"]` to `"theme": "imutechz"`, `"font": "default"`,
`"accentColor": ""`, and keep the extra key `chosen` (bool) that the UI
writes. The UI already reads the old defaults as "no choice"
(BUGS-FIXED.md 66), so this is for other clients and tidiness.

### 29. `tool.approval_needed.rememberable` (P2, = docs/connectors/REQUESTS.md L2)
The approval card hides "Approve and remember" for tools that always ask;
without the flag it guesses from the tool name and category
(`approvals.js` `rememberable`). Please send `rememberable: false` for level
`always`.

### 30. Research frames (confirmations, P3)
The thinking panel renders `research.plan / search / fetch / done` exactly as
`engine.py` publishes them (one frame per finished search and page). Two
optional additions would make the strip livelier on slow networks:
`research.search {state: "start", source, query}` when a source is asked
(the UI shows a spinner per source until its frame arrives either way), and
`pages` (pages read) in `research.done`. Please keep `turn_id` on every frame
(the stream filters on it) and keep `sources` in the order the answer's `[n]`
markers refer to: the UI numbers chips 1..n in that order and turns `[n]`
into links to chip n.

### 31. Native window: external browser for OAuth (launcher, P2)
`window.open` inside pywebview may open an embedded window or nothing. The
Connectors panel detects a blocked popup and shows "Open sign-in page" and
"Copy link"; better would be for the launcher to route `target=_blank` /
`window.open` to the system browser (pywebview `OPEN_EXTERNAL_LINKS_IN_BROWSER`
or a `js_api.open_external(url)` the page can call).
### 32. A file dialog for "Import client file…" — done (server)
Served as `POST /api/system/pick-file {title?, patterns?}` (the server opens
the OS file dialog in a Tk subprocess, so it works in the native window and
in a browser) -> `{path: string|null, cancelled: bool}`; `501 {error}` when
the machine has no Tk (Linux without python3-tk). Used by the Gmail card
("Choose file…", `api.js` `pickFile`): it only fills in the path; on a 501
the button is hidden for the session and the typed path remains.
**Status 2026-11-01 (later), from the connector owner's updated contract:**
24 (doc route), 25 (`/oauth/callback` publishes `connector`), 26 (`account`
in the status) and 29 (`rememberable` / `level` on approvals) are done and
used by the UI. 27 (`permission_level` for every tool in `GET /api/tools`) is done; the UI uses it. 31 is covered by the
server opening the system browser (`POST .../connect {"open": true}`).
## Voice (added 2026-11-02 by the voice owner)

### 33. Use the server voice layer (`/api/voice/*`) - done in the shell (2026-11-02)
Built in `voice-server.js` (see `CHANGELOG.md`): engine choice, MediaRecorder
dictation, first-use downloads with retry, `language` on `POST /api/turns`,
sentence-by-sentence speaking with prefetch and stop, the Settings panel.
Not done yet: the input-device picker (`inputDevice`, `enumerateDevices`) and
the speaking-rate control for the server voice (the browser rate slider is
reused). The per-language model (`docs/voice/REQUESTS.md` F1) is served and used.
The original request follows.
#13 and #4 are done on the server: the exact contract is in
`services/server/doc/README.md` ("Voice") and the design in
`docs/voice/README.md`. Field names follow `VOICE.md` (`lang`,
`duration_seconds`, `voice_id` are accepted/returned alongside `language`,
`duration`, `voice`). What the shell needs to do:

1. **Pick the engine.** On start and after Settings changes,
   `GET /api/voice/status`. Use the server when `voice.engine` is `auto` or
   `server` and `stt.available` / `tts.available` are true; otherwise keep
   the browser path. Show `stt.reason` / `tts.engines.*.reason` verbatim
   when unavailable (e.g. "faster-whisper is not installed ...").
2. **Dictation.** `getUserMedia({audio: {deviceId: voice.inputDevice || undefined}})`
   + `MediaRecorder` (default `audio/webm;codecs=opus` is fine; WAV works too).
   On stop, `POST /api/voice/transcribe?lang=<voice.lang>` with the blob as
   the body and its `Content-Type`. Put `text` in the composer. Expect
   **4-6 s** for a short utterance with the default model on 2 threads -
   show a "transcribing..." state. The server never opens a microphone.
3. **First use downloads.** A 503 with `error: "model_downloading"` or
   `"voice_downloading"` carries `job`; show progress from the SSE event
   `voice.model.download` (`{name, state, done, total, percent}`) and retry
   when `state == "done"`. Whisper `small` is 486 MB, a Piper voice 63-77 MB.
   An explicit "Download" button can call `POST /api/voice/voices/install
   {engine, name}` (202 `{job}`).
4. **Answer in the spoken language.** Send `answer_language` from the
   transcription as `language` on `POST /api/turns` (server/loop change
   requested in `docs/agentic/REQUESTS.md` #13; harmless to send before it
   lands).
5. **Read aloud / speak replies.** Split the finished (or streaming) reply
   into sentences (same rules as `voice.js`; the server's reference is
   `services/voice/text.py`) and call `POST /api/voice/speak {text, lang,
   voice_id: voice.voices[lang] || "", rate: voice.rate}` per sentence,
   prefetching sentence n+1 while n plays. Play with
   `new Audio(URL.createObjectURL(blob))` (CSP now has `media-src 'self'
   blob:`) or `AudioContext.decodeAudioData`. Show `X-Voice-Engine` /
   `X-Voice-Id` in the player tooltip; if `X-Voice-Offline: false` the text
   went to Microsoft (only possible when the user enabled it). Warm Piper
   takes 0.15-0.3 s per sentence. `voice.speakReplies` = read every reply.
6. **Settings > Voice** (section `voice`, all keys in `docs/voice/README.md`):
   engine (Auto / Server / Browser), speech model (`sttModel`, with sizes
   from `GET /api/voice/voices` -> `stt_models`), per-language voice picker
   from `GET /api/voice/voices` (`languages[code].voices`, with installed /
   download size / offline), auto-detect language, reply in the same
   language, speak replies aloud, input device (`enumerateDevices`), speed.
   **"Online voices (sends text to Microsoft)"** for `onlineFallback` must be
   off by default and say exactly that.
