# Server, bindings and tools changelog

The API contract itself is documented in `services/server/doc/README.md`.

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
