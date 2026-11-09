# Server, bindings and tools changelog

The API contract itself is documented in `services/server/doc/README.md`.

## 2026-11-09 — bugs found driving the running server end to end

Found by running `python start.py --no-open` headless (provisioning state, then the
CPU engine with Llama 3.2 1B) and probing every main route.

- **Provisioning state said "ready" and accepted turns** (`model/fish_client.py`
  `SyncFishModelClient.configured`). Without an engine library the client falls back to
  the HTTP engine (`model/fish/src/server.cpp`), and `configured` was
  `bool(server_url)`, which is always true (default `http://127.0.0.1:8731`). So
  `/api/status` reported `runtime: "ready"`, `engine.available: true`, and
  `POST /api/turns` answered 200 instead of `503 model_unavailable`; the turn then failed
  ("Cannot connect to host 127.0.0.1:8731"). Now the HTTP engine counts as configured only
  when its `/fish/health` answers (cached 5 s). Test: `tests/test_fish_http_backend.py`.
- **HTTP engine: every turn after the first failed with "Event loop is closed"**
  (`model/fish_client.py`). The aiohttp session was cached, but each synchronous call and
  each streamed turn runs on its own short-lived event loop, and a session is bound to the
  loop that created it. Each request now opens and closes its own session.
- **Malformed requests dropped the connection with a traceback, or hung**
  (`services/server/api.py`). `Content-Length: -5` raised in `rfile.read`;
  `{"maxSteps": 1e309}` became `inf` and `POST /api/agent` raised `OverflowError`; a body
  shorter than its `Content-Length` held the handler thread forever. Now: a negative length
  is `400 invalid content length`; `NaN`, `Infinity` and out-of-range numbers are
  `400 invalid json` for every route; request reads time out after `REQUEST_TIMEOUT`
  (60 s) with `408 request timed out`; and any other unexpected exception in a route is a
  JSON `500 {"error": "internal error"}` logged with its path (never the query or headers)
  instead of a dropped connection.
- **A JSON `null` text field became the text "None"** (`services/server/api.py`):
  `{"first_command": null}` created a session named "None" (also queued prompts, rename,
  skills, clients, persona). `null` now counts as missing, so those requests get their
  usual 400. Tests: `tests/test_api_bad_requests.py`.
- **`POST /api/models/download` never finalised the download generator**
  (`services/server/api.py`): the route returned from inside `async for` and closed its
  event loop without `shutdown_asyncgens()`, so `ModelManager.download_model`'s cleanup
  (open `.part` file, HTTP response) never ran; the log showed "Task was destroyed but it
  is pending!" after every download request. Now finalised before the loop closes.
- **The tool workspace and the notes / facts stores ignored `IMURUN_DATA_ROOT`**
  (`services/tools/workspace.py` `default_workspace`, `services/tools/notes_tool.py`):
  they hard-coded `~/.imurun`, so a server on another data root reported
  `workspace: ~/.imurun/workspace` on `GET /api/permissions` and kept notes in the real
  home folder. They now use `services.paths.data_root()`. Test: `tests/test_data_root_paths.py`.
- **A download could never finish when the HF metadata call failed**
  (`services/model_manager.py` `_download_from_source`). The exact size came only from
  `HfApi.repo_info`; when that failed (rate limit, API outage) the catalogue size was used,
  and nine FISH-compatible entries were round approximations (checked today with HEAD and
  a 4-byte Range on every source: all 23 sources live, `GGUF` magic, but e.g. Qwen2.5 7B
  4.5e9 vs 4 683 074 240 served). A bigger file was deleted as "more bytes than expected"
  and re-downloaded up to six times; a smaller one was "resumed" past its end; a valid
  `.part` larger than the catalogue size was deleted before asking. Now the size comes from
  the response (`Content-Length` / `Content-Range`), only an exact size may delete or skip
  a `.part`, and a 416 whose total equals the bytes on disk counts as complete. The nine
  catalogue sizes are corrected to the served values. Test: `tests/test_download_served_size.py`.
- **Selecting a model file that will not load left it as "the model"**
  (`model/fish_native.py` `FishNativeClient.select`). With no previous model to restore,
  the broken path stayed in `_model_path`, so `/api/status` said `runtime: "ready"` with
  `selected_model` = the broken file, and every turn was accepted (200) only to fail with
  "not a GGUF file". Now the client is left with no model: status says provisioning and a
  turn gets `503 model_unavailable / no_model`. `POST /api/models/select` answers such a
  file with **422** `{error: <engine message>, reason: "load_failed"}` instead of 500
  (`services/server/turns.py` `_select_model`, `api.py`). Tests:
  `tests/test_fish_adapter_binding.py`, `tests/test_api_bad_requests.py`.
- **`GET /api/stack/status` reported a file as the stack root** (`services/server/stack.py`
  `locate_root`): with no `docker-compose.yml` anywhere it returned `stack.py` itself, so
  `root` was `.../services/server/stack.py` and `compose_file`
  `.../stack.py/docker-compose.yml`. Now its folder. Test: `tests/test_stack_root.py`.
- **After the first launch that selected a model, every later launch showed no model
  downloaded** (`services/config.py` `resolve_model_path`, called by the warm start). It
  created the process-wide model manager on `<data root>/models`; in a source checkout the
  models live in `<repo>/models` (`services.paths.models_dir()`). So on the second start
  the warm start found nothing (`warm_start.state` stayed `idle`), `GET /api/models` listed
  every model as not downloaded and `POST /api/models/select` answered 404 for the whole
  session. It now uses the default manager. Measured on restart: warm start loads the 1B
  with no user action, `downloaded: true`. Installed copies were not affected (`start.py`
  pins the manager to the data root first). Test: `tests/test_last_model_resolve.py`.

## 2026-11-03 — resource governor (resources owner)

- `/api/resources` governor view grows `gpu` (detected with fallbacks, engine placement
  merged, `label`), `engine`, `priority`, `presets`, `custom`, `deviceClass`,
  `lowerPriority`, `machine.gpuInfo`; `imurun.engine` gains `gpu`, `totalLayers`,
  `tokensPerSecond`, `prefillTokensPerSecond`. Contract: README "Resource governor contract".
- `resources` settings: `lowerPriority` (default true; the server runs below normal
  priority while a reply / voice / tuning step runs); `preset` and `eco` accepted on input.
- `resources` event also on model load / placement change; heartbeat 10 s, only while a
  client listens.
- `services/server/resources.handle_update` ready for `PUT/POST /api/resources`
  (not routed in `api.py` yet). Details: `docs/resources/CHANGELOG.md`.

## 2026-11-03 — warm start (first token no longer pays the model load)

- `services/server/warmstart.py` (new), `app.py`: right after the server is up, the model
  the user last chose (`--model`/`FISH_MODEL`, else `<home>/last_model.json` written by
  `POST /api/models/select`) is loaded in a background thread at below-normal process
  priority with the governor's budget. Before, the weights were loaded by the first
  message of every launch (owner measured 225 s to the first token on the 8B). Measured:
  1B resident 2.0 s after start with no user action. `IMURUN_WARM_START=0` disables it.
  Progress: `/api/status` → `engine.warm_start {state: idle|loading|ready|error, model,
  seconds, error}` and events `model.load.start` / `model.load.done` / `model.load.error`
  (`reason: "warm_start"`). A message sent during the load waits for it (the engine's
  select holds the generation lock) instead of failing.
- Removed `models/.imurun-selected.json`: a stale copy from the previous machine, read by
  nothing.

## 2026-11-03 — keep the model loaded (cold start)

- `services/config.py`, `services/server/app.py`: `idle_timeout_seconds` now defaults to
  `0` (never unload) and the idle watchdog thread is not started at all when it is 0.
  The old 300 s default unloaded the weights after five quiet minutes, so every return
  to the app paid a full cold load (8B: read 4.9 GB + re-quantise + upload to VRAM) before
  the first token — the owner's "cold start is the main culprit". `FISH_IDLE_TIMEOUT_SECONDS=<s>`
  restores the old behaviour for machines that need the RAM back.

## 2026-11-02 — voice hook (voice owner)

- `api.py`: `/api/voice/*` (GET/POST) is forwarded to `services/voice/routes.dispatch`;
  `/api/status` gains `voice` (one summary line); CSP adds `media-src 'self' blob:` for
  audio playback. `settings.py`: `voice` section in `DEFAULTS`. Details: `docs/voice/CHANGELOG.md`.

## 2026-11-02 — installed-copy paths, reply language, per-language speech model

- `services/paths.py`: `installed()`, `data_root()`, `models_dir()`, `engine_dir()`,
  `local_file()`. A pip-installed copy (`site-packages/imurun/`) never writes into
  site-packages: models and tuning exports go to `~/.imurun/models`, registered
  app credentials (`apps.local.json`) to `~/.imurun/`, and the bindings also find
  the engine in `~/.imurun/bin` (`imurun --get-engine`). CODE-REQUESTS 9-11.
- `POST /api/turns {language}` → one "answer in <language>" line (agentic REQUESTS #13).
- `GET /api/tuning/feedback?session_id=`, `size_bytes` on `tuning.ready`,
  `voice.sttModelByLanguage` with `model` / `model_pending` on transcriptions.
- `POST /api/system/pick-file`: OS file dialog via a Tk subprocess (frontend 32).
- Research: definitional questions ("what is X", "how does X work") never search
  (`docs/research/CHANGELOG.md`).

## 2026-11-01 — research wired in, answer hygiene, MCP shutdown

- **Research engine wired** (`docs/research/README.md`): the agent loop hands
  the raw model and recent history to the planner for the pre-fetch
  (`AgentLoop._run_prefetch`), so the AI decides whether and what to search;
  `_TurnState.needs_web` counts as a live question for the verification badge.
  `services/server/app.py` sets the research publisher and planner model at
  startup. `IMURUN_LOOP_PLANNER_MODEL=0` withholds the model on that path
  (the test suite sets it; scripted fake models would spend a reply on the
  routing generation). `research.plan/search/fetch/done` documented in the API README.
- **Browser-search helper** (`window.py --search-helper <server_pid>`,
  protocol v1 in `docs/research/BROWSER-SEARCH.md`): hidden private-mode
  webview, read-only page scripts, reports `blocked` on challenge pages, exits
  with the server. Off by default (`IMURUN_BROWSER_SEARCH=1`) — search
  engines' terms restrict automated use of result pages; the owner decides.
- **Answer hygiene** (symptom: a reply that *was* a fake
  `[tool result: web_search] …` plus a knowledge-cutoff paragraph):
  `looks_like_fake_tool_result` → one retry with `FAKE_RESULT_NOTE`;
  `scrub_disclaimers` removes knowledge-cutoff / "I'm an AI" / "no real-time
  access" sentences and bracket notes from the final visible answer only
  (reasoning untouched, code untouched, never empties an answer).
  Tests: `tests/test_response_style.py`, loop test
  `test_invented_tool_result_is_retried_then_scrubbed`.
- `ApiServer.stop()` closes MCP servers before the model (connectors request).
- Permission levels for connector/MCP tools are native in `PermissionPolicy`;
  approval cards carry `level` + `rememberable`; connector tools offered per
  service (`docs/agentic/CHANGELOG.md` 2026-11-01).
- **Launcher window tests are opt-in** (`IMURUN_WINDOW_TESTS=1`): they start
  the real app with a native window, and a suite run by an agent popped
  IMURUN windows on the owner's screen (2026-11-02). Rule added to CLAUDE.md:
  never run `start.py` with a window from an agent or script.
- `tests/test_launcher.py::test_native_window_stays_up_when_available` failed
  once in a full run (launcher exit 0 = the window closed itself after the
  grace period) and passes alone and on reruns; the test now prints the
  launcher output and `~/.imurun/logs/window.log` when it fails so the next
  occurrence can be diagnosed.
- Suite: 203 passing + 1 network-skipped at the time of writing (one
  connectors test is mid-rename by the connectors agent).

## 2026-10-31 (night) — search keys in Settings, honest 503, tools_used

- New `search` settings section (`services/server/settings.py`): provider +
  `IMURUN_BRAVE_API_KEY` / `IMURUN_TAVILY_API_KEY` / `IMURUN_SERPER_API_KEY` /
  `IMURUN_SEARXNG_URL`. Keys are **write-only through the API**: `GET
  /api/settings/search` returns `••••••••` for a set key, a masked value posted
  back is ignored, and `POST` exports the keys to the process environment that
  `web_search` reads (`apply_search_env`); stored keys are applied at startup.
  (frontend request #16)
- `POST /api/turns` 503 now carries `reason`: `no_engine` (library missing)
  or `no_model` (nothing selected) with a matching message; it no longer
  claims "FISH engine is not running" when the user simply has no model. (#20)
- `turn.end` / `Turn.view()` carry `tools_used` (tool names in call order)
  next to the `tool_calls` count. (#17)
- `verification` / `sources` shapes confirmed as documented in
  `services/server/doc/README.md`. (#21)
- Launcher (setup scope, `window.py` / `start.py`): the window process watches
  the launcher's PID and closes itself when the launcher dies; the launcher
  terminates the window on every exit path. A hard-killed `start.py` leaves no
  window behind (verified: 2 window processes before, 0 five seconds after). (#23)

## 2026-10-31 (evening) — verification as data, not prose

Symptom (product owner): "Who is the current PM of India" produced a
paragraph about training data and a knowledge cutoff, and another answer had
"[note: unable to verify]" in the middle of the text.

Root causes: (1) the keyless DuckDuckGo path is bot-blocked on this network,
so no live source was available; (2) the house rule and the "live-data nudge"
told the model to *say* it could not verify, and small models render that as
disclaimers and bracket notes.

Changes:
- **Verification is decided by the server**, not written by the model.
  `LoopResult` / `Turn` carry `verification` = `verified` (a live tool result
  fed the answer) | `unverified` (live question, no live result) | `n/a`, and
  `sources` = `[{title, url, domain, provider, snippet}]` collected from
  web_search / wikipedia / web_fetch results (`response/loop.py::collect_sources`).
  Both are on the `step` (final) and `turn.end` events and in `Turn.view()`;
  the frontend renders a badge and source chips (`docs/frontend/`).
- **House style** (`response/style.py`): answer first; facts about the present
  come from a tool result when there is one, otherwise slowly-changing facts get
  an "as of <year>" qualifier and fast-changing values are not stated; **never**
  disclaimers about training data / being an AI / bracketed notes — the app
  shows verification itself. The offline/failed instructions in
  `tools/policy.py` use the same rule; `OFFLINE_SENTENCE` is one short line.
- **Live-data nudge retired** (`response/loop.py::LIVE_NUDGE = False`): it was
  the direct source of "[note: unable to verify]".
- **Keyless search fallback**: when DuckDuckGo refuses (bot check) or is empty,
  Wikipedia's public API answers encyclopaedic questions — direct article
  lookup first ("current prime minister of India" → *Prime Minister of India*,
  whose summary names the incumbent), then full-text search; identified with
  an explicit User-Agent as Wikimedia asks (`tools/builtin/web_search.py`).
  `IMURUN_SEARCH_PROVIDER=wikipedia` selects it outright. News, prices and
  scores still need a keyed provider (Brave has a free tier).
- **Live-question detection** now covers offices and roles ("current prime
  minister / president / CEO / chief minister …", "who is the … now",
  "current government / cabinet / standings …") in both the pre-fetch intents
  and `is_realtime` (`tools/policy.py`).
- Tests: `tests/test_agentic_loop.py` asserts the new contract (83 passing).

Verified on this machine: `WebSearchTool().run({"query": "who is the current
prime minister of India"})` → DuckDuckGo refused → Wikipedia returned *Prime
Minister of India* first with a summary naming the incumbent; `sources` populated.

## 2026-10-31

### Security
- **Bearer tokens were written to `auth_debug.txt` / `http_debug.txt` in the
  working directory** on every request (`services/server/api.py::_authorized`,
  `do_GET`). Removed. The two files in the repo root contain live tokens from
  earlier runs — delete them and treat those tokens as burned.

### Bindings (`model/fish_native.py`, `model/fish_client.py`, `model/engine_client.py`)
- ABI 2 structs (`FishStats`, `FishModelInfo`) and `fish_generate_chat`; older
  DLLs still load (new fields read as zero).
- `select()`, `close()`, `unload_if_idle()` take `_gen_lock`: the engine context
  can no longer be freed while a decode is running. A failed switch restores the
  previous model instead of leaving the engine empty.
- `health()` / `available` no longer load the weights as a side effect
  (polling `/api/status` used to load a multi-GB model and defeat idle unload).
- No implicit model: the largest `.gguf` in `models/` is no longer auto-selected;
  only `FISH_MODEL` or an explicit selection.
- Sampling params: `temperature=0` (greedy) and `top_p=0` are honoured
  (`x or default` was turning them into 0.35/0.9).
- `generate_stream(messages)` sends the conversation to the engine's structural
  chat path; raw-prompt path kept as `generate_prompt_stream`.
- `aiohttp` is imported lazily (native path does not need it).
- HTTP path posts `{"messages": [...]}` with `ensure_ascii=False`.
- `context_limit()` asks the loaded model for its real window.
- `peak_rss_mb` measured in Python on Windows (the DLL deliberately avoids psapi).

### Turn runner (`services/server/turns.py`)
- **Stop now stops**: `cancel()` calls the engine's `cancel()`, not just the loop flag.
- **Queued model switch deadlock**: `request_model_switch` waited on an event while
  holding `_switch_lock`, which `_process_model_switch_queue` needed — a 5-minute
  hang. Waits outside the lock now.
- **`AttributeError` after a successful queued switch**: `self._record` does not
  exist on `TurnRunner`. → `_mark_turn`.
- Context limit comes from the model (`context_limit()`), falling back to config.
- Finished turns are pruned (keeps 200) so `_turns` no longer grows forever.
- `step` and `turn.end` events carry engine `stats` (tokens, prefill/decode time,
  tok/s, context used/size, cached tokens, truncated) — frontend request P1.

### Config (`services/config.py`)
- `FishConfig.context_size` default 0 = let the engine's planner size the context
  from measured RAM; `FISH_CONTEXT_SIZE` overrides.

### Tools
- **`time_now` reported UTC labelled as local time.** → real local time with
  UTC offset (`services/tools/builtin/time_now.py`).
- **Web search fired on `what is` / `explain` / `how does` / `why is`…** so most
  questions paid a 15 s network round-trip before the model started. Intents
  narrowed to live-world questions (`services/tools/policy.py`).
- **DuckDuckGo instant-answer API returned nothing for most queries.** Default
  provider now reads the DuckDuckGo HTML results page (real web results, no key)
  and falls back to the instant-answer API (`services/tools/builtin/web_search.py`).
- Search timeout 8 s (`IMURUN_SEARCH_TIMEOUT`); tools run concurrently with a
  10 s overall deadline (`IMURUN_TOOL_DEADLINE`) (`services/tools/runner.py`).
- Loop publishes `tools.start/done`, and `web_search.start/done` only when a
  search actually runs, with the real hits (`services/response/loop.py`).

### Model manager (`services/model_manager.py`)
- Compatibility is by architecture (llama, qwen2, qwen3, phi3, gemma2 + MoE
  variants) → every catalogue entry is now selectable.
- `httpx` / `huggingface_hub` imported lazily; listing works offline.

### Housekeeping
- `pyproject.toml` pytest `testpaths` fixed (`tests`, not `test`).
- One-off `fix_*.py` / `check_*.py` scripts moved to `archive/scratch-scripts/`.
- `models/.imurun-selected.json` pointed at another user's home directory; it
  is regenerated on selection (delete the stale one if still present).

### Open requests from other owners
- `docs/frontend/API-REQUESTS.md`: P1 items done — `turn.end`/`step` stats;
  `/api/status.engine` now includes model info and last-generation stats when
  the model is loaded. Still open: customisation/voice settings defaults,
  persona `tone`/`prompt` fields, per-session context count, download progress
  on `/api/events`, session search, regenerate, `/api/voice/*`.
- `docs/setup/CODE-REQUESTS.md`: #1 (lazy aiohttp), #3 (lazy httpx/hf), #7
  (`ServerConfig` import) done. #5 (`--host` not forwarded to the bind address)
  still open.
