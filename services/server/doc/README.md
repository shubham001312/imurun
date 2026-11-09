# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\`

## Purpose

The loopback HTTP API that every frontend talks to. `api.py` is a
`ThreadingHTTPServer` with a token gate, a body cap, a loopback-only bind and a
static shell; `app.py` composes it from concrete stores and is the process
entrypoint (`python start.py` → `services.server.app.main`); the remaining
modules are the small services the handler exposes: turn lifecycle, SSE broker,
JSON settings sections, API clients, usage counters, resource sampling, Docker
stack control and an optional FastAPI models router.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\api.py`

Module constants: `MAX_BODY = 1_000_000`, `MAX_READ = 1_000`,
`LOOPBACK = {"127.0.0.1", "localhost", "::1"}`, `CSP` (a strict
`default-src 'self'` policy with `frame-ancestors 'none'`), `STATIC_TYPES` (the
only servable extensions), `_HAS_MODELS_API`.

| Symbol | Signature | Notes |
|---|---|---|
| `ApiServer.__init__` | keyword-only: `persona_store, persona_tool=None, connectors=None, queue=None, sessions_root=None, gate=None, host="127.0.0.1", port=0, max_body=MAX_BODY, mark=None, token=None, transcripts=None, events=None, skills=None, turns=None, model=None, static_root=None, oauth_flows=None, persona=None, kill=None, version="0.2", data_root=None, settings=None, clients=None, usage=None, resources=None, stack=None` | Binds immediately. Raises `ValueError` for a non-loopback `host` or `max_body <= 0`. Creates a default `SyncFishModelClient` only when both `model` and `turns` are `None`. |
| `ApiServer.token` | `-> str` (property) | The per-run bearer token (`secrets.token_urlsafe(24)` unless injected). |
| `ApiServer.port` | `-> int` (property) | Actual bound port (`0` picks a free one). |
| `ApiServer.add_oauth_flow` | `(connector_id: str, flow: OAuthFlow) -> None` | Thread-safe; empty id → `ValueError`. |
| `ApiServer.start` | `-> ApiServer` | Starts a daemon `serve_forever` thread; returns `self`. |
| `ApiServer.stop` | `-> None` | Closes the event broker, shuts the server down, joins the thread (5 s) and calls `model.close()` when present. |

Internal state worth knowing: `_sessions_root`, `_settings`, `_clients`,
`_usage`, `_resources`, `_stack`, `_oauth_flows`, `_oauth_states` (guarded by
`_oauth_lock`), `_home` (derived from `data_root`, else `sessions_root.parent`,
else `~/.imurun`).

The handler class built by `_handler()` implements:

| Method | Behaviour |
|---|---|
| `_reply(code, payload)` | JSON, `no-store`. |
| `_html(code, title, message)` | Escaped HTML with the CSP and `nosniff`/`DENY` headers. |
| `_local()` | Parses `Host` (with or without port, IPv6 brackets); a non-loopback host gets `403` and a `mark`. |
| `_same_token(a, b)` | `secrets.compare_digest`; non-ASCII input returns `False` instead of raising. |
| `_authorized()` | Requires `Authorization: Bearer <token>`; for `GET` only, accepts `?token=` (EventSource cannot set headers). Failure → `401` and a mark of the path (never the query). |
| `_drain(length)` | Consumes a refused body so Windows clients can read the `413`. |
| `_body()` | `Content-Length` must parse; over `max_body` → `413` (drain up to +64 KiB, else close); invalid JSON or non-object → `400`. Missing body is treated as `{}`. |
| `_static(path)` | Resolves under `static_root`, rejects traversal, serves only `STATIC_TYPES`, and substitutes `__IMURUN_TOKEN__` in `index.html`. |
| `_stream()` | SSE: subscribes, sends `retry: 3000`, forwards named frames, `: ping` every 15 s idle, exits on `CLOSE` or a broken pipe. |
| `_callback()` | `GET /oauth/callback`: pops the one-time state, completes the flow, renders an HTML result. |
| `_messages(session_id)` | Validates the session, clamps `limit` to `[1, MAX_READ]`, returns transcript messages. |

**`do_PUT`:** one definition handles `PUT /api/persona` and
`PUT /api/settings/{section}`. An earlier dead duplicate was removed on 2026-10-31.

**Agentic wiring (2026-10-31):** `__init__` now builds `_settings` first, then
`_policy = PermissionPolicy(_settings)`, `_approvals = ApprovalBroker(events.publish)`,
and, when `tools` has a `registry`, `_executor = ToolExecutor(...)`. It also
rebinds the workspace tools to `_policy.workspace` and sets `tools.policy`.
`TurnRunner` receives `executor`, `skills` and `settings`. Handlers for the
agentic routes live in `services/tools/routes.py` (`handle_get`,
`is_post_route`, `handle_post`). See `docs/agentic/README.md`.

#### Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` and any non-`/api/` path | none (loopback `Host` only) | Static shell/assets; `index.html` gets the token injected. |
| GET | `/health` | none | Liveness `{"ok": true}`. |
| GET | `/oauth/callback` | none (one-time `state` is the capability) | OAuth redirect target. When connectors are wired the page comes from `connectors/routes.handle_callback`: "Connected as <account> — you can close this tab", `window.close()` after 2 s (CSP `script-src 'sha256-…'`), `connector` event; otherwise the built-in page. |
| GET | `/api/status` | Bearer | Version, uptime, session count, queue depth, connector totals, model availability. `engine.warm_start` = `{state: idle\|loading\|ready\|error, model, seconds, error}` for the background load of the last chosen model at startup (`services/server/warmstart.py`, off with `IMURUN_WARM_START=0`). |
| GET | `/api/events` | Bearer, or `?token=` for GET | SSE broker stream. |
| GET | `/api/persona` | Bearer | Persona view. |
| GET | `/api/connectors` | Bearer | Connector cards. |
| GET | `/api/connectors/{id}` | Bearer | One connector status + `oauth` flag. |
| GET | `/api/clients` | Bearer | API client list (no hashes). |
| GET | `/api/usage` | Bearer | Usage panel view. |
| GET | `/api/resources` | Bearer | Memory/disk/CPU/folder sizes plus `governor` (resource governor view, see "Resource governor contract"). |
| GET | `/api/resources/budget` | Bearer | Resource governor view alone. |
| PUT/POST | `/api/resources` | Bearer | Change preset / dynamic / lowerPriority (`services/server/resources.py` `handle_update`; `PUT /api/settings/resources` does the same through the settings store). |
| GET | `/api/stack/status` | Bearer | Docker stack status. |
| GET | `/api/settings` | Bearer | Known section names. |
| GET | `/api/settings/{section}` | Bearer | One section, merged with defaults. |
| GET | `/api/sessions` | Bearer | Session identities. |
| GET | `/api/sessions/{id}` | Bearer | One identity. |
| GET | `/api/sessions/{id}/messages` | Bearer | Transcript, `?limit=` (default 200). |
| GET | `/api/skills` | Bearer | Skills without file paths. |
| GET | `/api/skills/{id}` | Bearer | One skill `{id, name, description, origin, body, invoke: "/<id>"}`; 404. |
| GET | `/api/tools` | Bearer | `{tools: [{name, description, parameters (JSON schema), category, requires_network, available, reason, permission: allow\|ask\|deny, permission_level: allow\|read\|write\|ask\|always}], network, last_results, permissions: {mode, workspace, workspaceExists}, format}`. |
| GET | `/api/tools/approvals` | Bearer | `{approvals: [{approval_id, tool, args, reasons, turn_id, session_id, call_id, created_at, expires_in}]}`. |
| GET | `/api/permissions` | Bearer | `{mode, workspaceRoot, workspace, workspaceExists, readFiles, writeFiles, runShell, network, approvalTimeout, modes, table, grants}`; 501 without a policy. |
| GET | `/api/agent` | Bearer | `{maxSteps, maxStepsLimit: 20, maxDepth: 2, subAgentSteps: 4, subAgentStepsLimit: 8}`. |
| GET | `/api/turns/{id}` | Bearer | Turn state. |
| GET | `/api/models` | Bearer | Model catalogue (501 if unavailable). |
| GET | `/api/models/{id}` | Bearer | One model (501/404). |
| PUT | `/api/persona` | Bearer | Save persona. |
| PUT | `/api/settings/{section}` | Bearer | Merge one section. |
| POST | `/api/prompts` | Bearer | Queue a prompt; publishes `queue`. |
| POST | `/api/system/pick-file` | Bearer | `{title?, patterns?}` → OS file dialog (Tk subprocess, `filedialog.py`) → `{path: string\|null, cancelled}`; 501 when Tk is missing. |
| GET | `/api/tuning/status` | Bearer | Tuning channel state: install, automation, counts, current job (`services/tuning/routes.py`; shapes in `docs/tuning/README.md`). |
| GET/POST | `/api/tuning/settings` | Bearer | `tuning` section (off by default) + write-only `hf_token` (masked on read). |
| GET | `/api/tuning/jobs`, `/api/tuning/jobs/{id}` | Bearer | Job list; one job with its `metrics`. |
| POST | `/api/tuning/jobs` | Bearer | Start a run `{model_id?, allow_slow?, confirm?}`; the feasibility gate may answer with a plan to confirm. |
| POST | `/api/tuning/jobs/{id}/cancel` | Bearer | Cancel a job. |
| GET | `/api/tuning/plan/{model_id}` | Bearer | Feasibility plan (ok / slow / refused, measured estimate, reason); 404 when no trainable original is known. |
| GET | `/api/tuning/datasets` | Bearer | Usable / dropped samples with reasons, preview, manifest, built datasets. |
| GET | `/api/tuning/feedback?session_id=` | Bearer | Latest rating per turn of a session: `{session_id, turns: {turn_id: {rating, consented, correction?}}}`; 400 without `session_id`. |
| POST | `/api/tuning/feedback` | Bearer | Consent: `{turn_id, session_id, rating: up\|down\|teach\|clear, correction?}`. |
| POST | `/api/tuning/setup` | Bearer | Install the separate training venv (progress on `tuning.setup`). |
| POST | `/api/sessions` | Bearer | Start a session from `first_command`. |
| POST | `/api/sessions/{id}/rename` | Bearer | Rename. |
| POST | `/api/turns` | Bearer | Start a turn (`session_id`, `text` or `prompt`, optional `language` ISO 639-1 the user spoke: adds one "answer in <language>" line to the system prompt, echoed in `Turn.view().language`); `503 model_unavailable` when no model is configured (now also when the engine reports `available: false`). |
| POST | `/api/turns/{id}/cancel` | Bearer | Cancel a running turn. |
| POST | `/api/skills` | Bearer | Create a skill (409 on duplicate). Invoke it with `/<id>` in a prompt. |
| POST | `/api/tools/run` | Bearer | `{name, args, approved?, session_id?}` → `200 {result: {call_id, tool, args, decision, reasons, approval_id, result: ToolResult}}` (a failed tool is still 200 with `result.ok: false`); `403 {decision: "ask", reasons, tool, args}` → repeat with `approved: true`; `403 {decision: "deny", reasons}`; `400` bad body; `503` no tools. |
| POST | `/api/tools/approvals/{id}` | Bearer | `{approved: bool, remember?: bool}` → `{approval_id, approved, remember}`; `404` unknown/answered. |
| POST | `/api/permissions` | Bearer | Any of `{mode, workspaceRoot (absolute, not a drive root), readFiles, writeFiles, runShell, network, approvalTimeout}` → the view; `400` on invalid; publishes `permissions`. Stored in settings section `permissions`. |
| POST | `/api/agent` | Bearer | `{maxSteps: 1..20}` → the view (settings section `agent`). |
| POST | `/api/tools/location` | Bearer | Browser-shared position for the `location` tool (`{}` clears). |
| POST | `/api/connectors/{id}/connect` | Bearer | Begin OAuth (handled by `services/connectors/routes.py`): body `{open?: bool}`; credentials resolve now (user → env → built-in); returns `{url, state, opened}` — with `open: true` the server opens `url` in the system browser. `404 {error, reason}` without a credential or for token services, `409` when already connected. |
| POST | `/api/connectors/{id}/token` | Bearer | Token services (Cloudflare, Render): `{token}` → verified with the provider first → `200 {connected, account, scopes_or_permissions, expires, token_id?}`; `401 {connected: false, error}` refused (nothing stored); `400` malformed; `409` for OAuth services. The token is never returned. Publishes `connector`. |
| DELETE | `/api/connectors/{id}/token` | Bearer | Forget the token → `{forgotten, connected: false}`; publishes `connector`. |
| GET | `/api/connectors/{id}/doc` | Bearer | `{id, path, format: "markdown", markdown}` — the service's page from `docs/connectors/`; 404. |
| POST | `/api/connectors/{id}/disconnect` | Bearer | Forgets the tokens (Google: revoked; Slack: `auth.revoke`) → `{disconnected: bool}` + `connector` event. |
| GET | `/api/connectors/{id}/config` | Bearer | `{id, label, clientId, credential_source: user\|env\|builtin\|none, clientIdSource, clientSecretSet, tools, availableTools, scopes, redirectUri, configured, connected, oauth, pkce, deviceFlow, console, doc, auth_mode: oauth\|token, has_token, store, store_encrypted, warning?, registration_todo?}`; token services add `token_console, token_permissions, account, scopes_or_permissions` + service options (Gmail `maxResults`, `bodyChars`; Supabase `keyRole`). The secret is never returned. 404 for an unknown id; 501 when not wired. |
| POST | `/api/connectors/{id}/config` | Bearer | Any of `{clientId, clientSecret, tools, <options>}` → the config view; re-registers the OAuth flow; `400` invalid, publishes `connector`. The secret goes to the token store, not the settings file. |
| POST | `/api/connectors/{id}/import-client` | Bearer | Gmail only: `{path}` (absolute path to a Google `client_secret_*.json`, `installed` or `web`, ≤ 64 KB) → `{client_id, project_id, redirect_uris, type, warnings}`; id → settings, secret → token store, never returned; `400` invalid (the file is never quoted), `404` for services without a client file. |
| GET | `/api/connectors/{id}/test` | Bearer | One small real read. Gmail: `{ok, account, threads: [{id, subject, from, date, messages, snippet}]}` (3 threads); others: `{ok, tool, content}`. `409` not connected, `502` provider error. |
| POST | `/api/connectors/{id}/device` | Bearer | Device flow (GitHub only): `{state: "pending", user_code, verification_uri, interval, expires_in}`; `400` no client id / refused, `404` no device flow, `409` connected. |
| GET | `/api/connectors/{id}/device` | Bearer | `{state: idle\|pending\|connected\|denied\|expired\|error, user_code?, verification_uri?, message?}`. |
| GET | `/api/mcp/servers` | Bearer | `{servers: [{id, name, transport, command, args, env (values "********"), cwd, url, headers ("********"), enabled, auto_approve, timeout, status, error, tools, protocolVersion, serverInfo, connectedAt}]}`. |
| GET | `/api/mcp/servers/{id}` | Bearer | One server view; 404. |
| GET | `/api/mcp/servers/{id}/tools` | Bearer | `{id, status, tools: [{name, remote_name, title, description, parameters, annotations, permission_level, auto_approve, available}]}`; 404. |
| POST | `/api/mcp/servers` | Bearer | Add/update one server (settings section `mcp`); `"********"` keeps a stored env/header value; `400` invalid. Re-discovers in the background. |
| POST | `/api/mcp/servers/{id}/connect` | Bearer | Connect now (initialize + tools/list) → view; `409` disabled, `502 {error, server}` failure. |
| POST | `/api/mcp/servers/{id}/disconnect` | Bearer | Close the connection → view (reconnects lazily on next use). |
| DELETE | `/api/mcp/servers/{id}` | Bearer | Remove → `{deleted}`; 404. |
| POST | `/api/connectors/{id}/actions/{action}` | Bearer | Gated action; `403` with the decision, `400` on provider error. |
| POST | `/api/stack/{build,start,ensure,stop}` | Bearer | Docker control; `500` when `ok` is false. |
| POST | `/api/models/download` | Bearer | SSE download progress. |
| POST | `/api/clients` | Bearer | Create a scoped client; plaintext token returned once (`201`). |
| POST | `/api/auth/rotate` | Bearer | Replace the per-run token. |
| POST | `/api/resources/clear` | Bearer | Delete `*.gguf` under the models root. |
| DELETE | `/api/sessions/{id}` | Bearer | Delete a session file. |
| DELETE | `/api/clients/{id}` | Bearer | Revoke a client. |
| DELETE | `/api/models/{id}` | Bearer | Delete a downloaded model. |
| DELETE | `/api/skills/{id}` | Bearer | Delete a skill. |
| GET | `/api/openai` | Bearer (shell token) | The "Use by API" panel's view of the OpenAI-compatible API: `{baseUrl, apiKey, model, available, contextWindow, maxTokens, keyFile, keyCreatedAt, endpoints}`. The persistent API key is returned in full (same trust as the shell token). |
| POST | `/api/openai/rotate` | Bearer (shell token) | Regenerate the persistent API key; same view. The old key fails at once. |
| GET | `/v1/models` | API key | OpenAI shape, see below. |
| GET | `/v1/models/{id}` | API key | One model; `404 model_not_found`. |
| POST | `/v1/chat/completions` | API key | OpenAI chat completions (JSON or SSE), see below. |

`GET /api/skills/{id}` and `GET /api/models/{id}/path` exist only in
`models_api.py` / the JS layer, not in this handler. `POST /api/turns` accepts a
`mode` field but ignores it.

#### OpenAI-compatible API (`/v1/*`, `services/server/openai_api.py`)

For other apps on this machine (OpenClaw, the `openai` SDK, curl; user guide in
`docs/setup/README.md`). `api.py` hands every `/v1/*` and `/api/openai[/rotate]`
path to `openai_api.dispatch(server, handler, method)` after the loopback `Host`
check and before its own token gate (two hook lines, in `do_GET` and `do_POST`).

**Auth.** `Authorization: Bearer <key>` only (no `?token=`). Accepted: the
persistent API key (`ApiKeyStore`, `<data_root>/secrets/openai_api_key`, `sk-imurun-`
+ `token_urlsafe(32)`, created on first use, file `0600` from creation and folder
`0700` on POSIX, compared with `secrets.compare_digest`); the per-run shell token;
a `ClientStore` token (`imu_...`) whose scopes include `chat` or `admin` (the first
enforcement point for client tokens). Failure: `401 {error: {message, type:
"invalid_request_error", param: null, code: "invalid_api_key"}}`, a `G3-api` mark of
the path; the key and headers are never logged or marked.

**Errors** always use `{error: {message, type, param, code}}`: `400`
`invalid_request_error` (bad JSON, missing/empty `messages`, unknown role, a
non-text content part, `max_tokens`/`max_completion_tokens` < 1, `temperature`
outside 0..2, `top_p` outside (0, 1], more than 4 `stop` strings, non-boolean
`stream`, `n` != 1; `param` names the field), `401`, `404 model_not_found` /
`unknown_url`, `405`, `413 request_too_large` (the server's `max_body`), `503`
`server_error`/`model_not_available` when `TurnRunner.available` is false, `500`
`generation_failed`.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/v1/models` | - | `{object: "list", data: [{id, object: "model", created, owned_by: "imurun"}]}`; `id` = the selected model file name without `.gguf`; `data: []` when no model is available. |
| POST | `/v1/chat/completions` | `{model, messages: [{role: system\|developer\|user\|assistant\|tool\|function, content: string\|null\|[{type: "text", text}]}], stream?, stream_options?: {include_usage}, max_tokens? \| max_completion_tokens?, temperature?, top_p?, stop?: string\|string[≤4], n?: 1}` | Non-streaming: `{id: "chatcmpl-…", object: "chat.completion", created, model, choices: [{index: 0, message: {role: "assistant", content, refusal: null}, logprobs: null, finish_reason: "stop"\|"length"}], usage: {prompt_tokens, completion_tokens, total_tokens}, system_fingerprint: null}`. Streaming: `text/event-stream`, `data: {…object: "chat.completion.chunk", choices: [{index, delta, logprobs, finish_reason}]}` frames: first `delta: {role: "assistant", content: ""}`, then `{content}` pieces, then `delta: {}` with `finish_reason`, then (with `include_usage`) a `choices: []` frame carrying `usage`, then `data: [DONE]`. A failure after the headers is sent in-band as `data: {error: {…}}` followed by `[DONE]`. |

Behaviour: `model` is accepted as given (one model is served; the reply names it).
`developer` becomes `system`; `tool`/`function` messages become user text
`"Tool result (<name>):\n…"`; `tools`, `tool_choice`, `response_format`, penalties and
other fields are ignored. Generation calls the same model client as the chat
(`TurnRunner._model`, i.e. `SyncFishModelClient.generate_stream(messages,
FishParams(temperature, top_p, max_tokens))`), so the native engine serialises it
with chat turns on `FishNativeClient._gen_lock`. Unset sampling fields fall back to
the engine config (`FISH_TEMPERATURE`, `FISH_TOP_P`, `FISH_MAX_TOKENS`). `stop` is
applied to the decoded text on the server (the engine has no stop strings); the
first match ends the reply, closes the generator and cancels the engine
(`FishNativeClient._stream`'s `finally`, reached through `loop.shutdown_asyncgens()`).
A client that disconnects mid-stream is cancelled the same way. `finish_reason` is
`length` when the generated count reached the effective `max_tokens`. `usage` comes
from the engine's last-generation stats (best effort: a chat turn finishing in the
same instant could overwrite them), else a chars/4 estimate and the chunk count;
completion tokens are added to `UsageTracker`. No CORS headers and no `OPTIONS`
handler (`501`): SDK and agent clients are not browsers, and a cross-origin page
should not drive the local model.

#### Voice (`/api/voice/*`, handled by `services/voice/routes.py`)

Owner: voice (`docs/voice/README.md`). `api.py` forwards every path under
`/api/voice/` (GET and POST, after the Bearer check) to
`voice_routes.dispatch(server, handler, path)`, because voice needs audio
bodies above the 1 MB JSON cap and binary replies. Without the optional
packages every route answers **503** `{error, reason, message}`; bad input is
400 / 413 / 415 `{error: "bad_request" | "too_large", reason}`; never a traceback.
`/api/status` gains `voice`: `"ready"`, `"partial (<reason>)"` or
`"unavailable (<reason>)"` (never starts the worker).

`/api/status` → `model` (FISH engine, ABI 3) has a new `adapter` field. It is
`null`, or `{active, pending, name, rank, alpha, scale, effective_scale,
tensors, merged_bytes, merge_seconds, retention, residual, blocks,
blocks_refit}` when a LoRA adapter is merged into the loaded weights
(`docs/engine/ARCHITECTURE.md` → "LoRA adapters"). No route sets an adapter
yet; `FishNativeClient.select(model, adapter=..., scale=...)` is the binding
(`docs/tuning/REQUESTS.md` E1).

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/voice/status` | - | `{summary, stt: {available, reason, engine: "faster-whisper-small", model, model_installed, auto_install, languages[99], auto_detect, candidates, formats: {wav, webm, ogg, mp3, flac, mp4, decoder}}, tts: {available, engine, engines: {piper: {available, reason, installed_voices}, system: {available, backend, reason, voices}, online: {available, enabled, installed, privacy}}, voices: [{id, name, lang, locale, engine, quality, offline}], format: "audio/wav"}, worker: {running, pid, threads, idle_unload_s, starts, priority}, jobs: [...], settings: {...}, paths: {root}}` |
| GET | `/api/voice/voices[?refresh=1]` | - | `{languages: {<code>: {name, native, stt, selected, voices: [{engine, voice, name, locale, quality, speakers?, installed, engine_available, offline, download_mb?, default?, privacy?}]}}, catalogue: "builtin" \| "cache" \| "network", stt_model, stt_model_installed, stt_models: [{name, download_mb, installed}]}` |
| GET | `/api/voice/jobs` | - | `{jobs: [job]}` |
| POST | `/api/voice/transcribe[?lang=hi-IN&cleanup=0\|1&polish=0\|1&context=<json>]` | raw audio with its `Content-Type` (`audio/webm;codecs=opus`, `audio/wav` incl. float32, `audio/ogg`, `audio/mpeg`, ...) **or** `multipart/form-data` (first file part = audio, text fields `lang`, `context`). `context` = JSON list of the last messages (`["..."]` or `[{role, content}]`, max 8 x 4000 chars) or plain text; names in it bias recognition. Max 32 MB / 300 s. `Content-Length` required (411 for chunked). | `{text, text_raw, cleanup: {applied, changes[], model: {used, reason, ms}}, language, lang, language_name, language_probability, detected_raw, script_fixed, confidence, duration, duration_seconds, speech_seconds, dropped, segments: [{start, end, text}], engine, model, device: "cuda"\|"cpu", compute_type, device_reason, prompted, elapsed, timings: {load, decode, transcribe, redecode?}, answer_language, model_pending?: {model, error, message, job?}}`. `text` is the cleaned dictation (fillers, self-corrections, spoken punctuation, numbers; `voice.cleanup`), `text_raw` what Whisper wrote. `model` = the Whisper model that produced the text (`sttModelGpu` on the GPU, `sttModel` on the CPU, or `sttModelByLanguage`). 503 `model_downloading` + `job` when the model is missing (the download starts), `stt_unavailable`, `decoder_unavailable`, `stt_failed`. |
| POST | `/api/voice/stream?id=<s>&seq=<n>[&final=1&lang=&context=]` | the next audio chunk (MediaRecorder timeslice, same `Content-Type` each time); chunks in order from `seq=0`; a repeated `seq` is ignored | not final: `{id, seq, accepted, final: false, partial: null \| {id, seq, text, text_raw, stable, language, endpoint, audio_s, elapsed, device, model, partial: true}}`; final: the transcription shape above + `{id, final: true, stream: {passes, committed_s}}`. 409 chunk out of order, 413 over 32 MB, 400 no id. |
| POST | `/api/voice/stream/cancel?id=<s>` | - | `{ok}` |
| GET | `/api/voice/vocabulary` | - | `{vocabulary[], corrections: [{heard, fixed}], product_terms[], context_biasing, prompt_preview}` |
| POST | `/api/voice/vocabulary` | `{add: [...], remove: [...]}` or `{vocabulary: [...]}` (replace) | same as GET |
| POST | `/api/voice/correction` | `{heard, fixed}` (the "fix this" button; `fixed` also joins the vocabulary) or `{remove: heard}` | same as GET; 400 when a field is missing or both are equal |
| POST | `/api/voice/speak` | `{text, language \| lang, voice \| voice_id, rate (0.5-2), raw}`; text is Markdown unless `raw`; max 5000 chars after cleaning - send one sentence per call | `audio/wav` (or `audio/mpeg` from the online engine) with `X-Voice-Engine` (`piper` \| `system` \| `online`), `X-Voice-Id` (`piper:<key>[#speaker]`, `system:<name>`, `online:<name>`), `X-Voice-Language`, `X-Voice-Offline`, `X-Voice-Duration`, `X-Voice-Fallback` (why earlier tiers were skipped). 503 `voice_downloading` + `job`, `no_voice` + `tried`. |
| POST | `/api/voice/voices/install` | `{engine: "whisper" \| "piper", name}` (or `{voice: "piper:<key>"}`) | 202 `{job}`; 400 for unknown names. |
| POST | `/api/voice/unload` | - | `{ok, worker}`; stops the worker process now. |

`job` = `{id, kind: "whisper" | "piper", name, state: "queued" | "downloading" | "done" | "error", done, total, percent, file, error}`.
Event: **`voice.model.download`** with the same `job` shape, at most every 0.5 s per job and on every state change.
Events: **`voice.partial`** (a streamed utterance's live text, the `partial` shape above) and **`voice.final`** `{id, text, text_raw, language}`.
`GET /api/voice/status` also carries `gpu: {cuda, reason, free_mb, total_mb, source, loaded}` (from the worker's start-up probe; `{}` before the worker ran), `worker.on_gpu`, `worker.gpu_releases`, `streams[]` and `vocabulary: {words, corrections}`.
Settings: section `voice` (keys in `docs/voice/README.md`); `onlineFallback` (edge-tts, sends text to Microsoft) defaults to false.
CSP: `media-src 'self' blob:` was added so the shell can play fetched audio from a `blob:` URL.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\app.py`

| Symbol | Signature | Notes |
|---|---|---|
| `default_root` | `() -> Path` | `~/.imurun`. |
| `default_static_root` | `() -> Path \| None` | `<repo>/frontend/design-lab` when it exists, else `None`. |
| `runtime_path` | `(root=None) -> Path` | `<root>/runtime/api.json`. |
| `write_runtime` | `(root, port, token, *, pid=None) -> Path` | Validates token and `0 < port < 65536`; writes `{url, token, pid, started_at}` atomically, `0700` folder / `0600` file. |
| `clear_runtime` | `(root=None) -> None` | Unlinks the runtime file. |
| `build_server` | `(*, root=None, model=None, connectors=None, oauth_flows=None, static_root=None, port=0, token=None, kill=None, persona=None, mark=None, config=None) -> ApiServer` | Wires `PersonaStore`, `ConnectorRegistry`, `PromptQueue`, `SkillsStore`, `TranscriptStore`, the model client, and calls `wire_server` only when `connectors is None`. |
| `main` | `(argv: list[str] \| None = None) -> int` | CLI: `--port`, `--root`, `--static`, `--no-browser`, `--no-stack`. |

`main` sequence: parse args → `load_config` → `build_server` → `server.start()` →
`write_runtime` → print URL and runtime path → start `imurun-stack` thread
(`server._stack.ensure()`, skipped by `--no-stack` or `IMURUN_AUTOSTART=0`) →
start `imurun-browser` thread (`webbrowser.open` after 0.6 s, skipped by
`--no-browser` or `IMURUN_NO_BROWSER=1`) → sleep loop → on `KeyboardInterrupt`
`clear_runtime` + `server.stop()`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\stack.py`

| Symbol | Signature | Notes |
|---|---|---|
| `locate_root` | `(start=None) -> Path` | `IMURUN_ROOT` if it holds `docker-compose.yml`, else the nearest ancestor with one (also checks `<dir>/imurun/docker-compose.yml`). |
| `StackController.__init__` | `(root=None, fish_health_url=None)` | Health default `http://127.0.0.1:8731/fish/health` or `FISH_HEALTH_URL`. |
| `StackController.docker_path` | `() -> str \| None` | `shutil.which("docker")`. |
| `StackController.status` | `() -> dict` | `{docker, compose_file, root, services, running, total, fish_healthy, last_action}`. |
| `StackController.build` | `(timeout=1800.0) -> dict` | `docker compose build --parallel`. |
| `StackController.start` | `(timeout=300.0) -> dict` | `docker compose up -d --remove-orphans`. |
| `StackController.stop` | `(timeout=120.0) -> dict` | `docker compose down`. |
| `StackController.ensure` | `(wait_seconds=60.0) -> dict` | Healthy → `already-running`; no services → build; then start; polls health every 2 s; returns `started` or `starting`. |

`_run` returns `{"ok", "returncode", "stdout", "stderr", "command"}` and never
raises; stdout/stderr are truncated to the last 8000 characters. Missing Docker,
a missing compose file, a timeout and an `OSError` all become `ok: False` with an
`error` string. `ensure` treats "containers up but engine still warming" as
`ok: True, status: "starting"`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\turns.py`

| Symbol | Signature | Notes |
|---|---|---|
| `Turn` | `@dataclass Turn(id, session_id, prompt, state="running", started_at, finished_at=None, status=None, answer="", error=None, steps=0)` | `state` is `running`/`done`/`stopped`/`error`. |
| `Turn.view` | `() -> dict` | Drops `prompt`; used by `GET /api/turns/{id}`. |
| `TurnRunner.__init__` | `(model: ModelClient \| None, *, queue=None, transcripts=None, events=None, audit=None, kill=None, persona=None, tools=None, max_steps=None, mark=None, usage=None, executor=None, skills=None, settings=None)` | `max_steps` defaults to `IMURUN_MAX_STEPS` or 6, capped at 20; `<= 0` → `ValueError`. Per turn, `settings.read("agent")["maxSteps"]` overrides it. When `mark` is given and `audit` is not, wraps it in `_MarkAudit`. |
| `TurnRunner.available` | `-> bool` (property) | `False` without a model; otherwise calls the model's `available` (callable or value) and treats an exception as unavailable. |
| `TurnRunner.start` | `(session_id: str, prompt: str, mode="build") -> Turn` | `RuntimeError("model unavailable")` without a model or when `available` is false; `ValueError` for a missing session id or blank prompt. Resolves `/skill` tokens, creates an `AgentLoop` (with `executor`, `raw_model`, `turn_id`), registers the turn and starts a daemon thread. |
| `TurnRunner.spawn_agent` | `(task, tools=None, steps=4, session_id="manual") -> LoopResult` | A depth-1 sub-agent outside a turn (used by `POST /api/tools/run` for `run_agent`). |
| `TurnRunner.cancel` | `(turn_id: str) -> bool` | Calls `loop.stop()`; `False` for an unknown or finished turn. |
| `TurnRunner.get` | `(turn_id: str) -> Turn \| None` | |
| `TurnRunner.list` | `() -> list[Turn]` | |

A turn thread appends the user prompt to the transcript, publishes
`turn.start`, `queue`, `step.start`/`token`/`step` (from `_publishing_model`),
then `queue` and `turn.end` (`turn_id`, `status`, `steps`, `answer`). Exceptions
become `state="error"`, a `G3-turn` mark and a `turn.error` event. The `finally`
block sets `finished_at`, calls `loop.close()` and removes the loop from
`_loops`; the `Turn` itself stays in `_turns` (there is no eviction).

`_publishing_model` prefers `inner.generate_stream` (running it on a fresh event
loop and publishing each token) and falls back to `inner.generate`. While tools
are on offer, a `StreamGuard` hides tool-call blocks from the `token` stream.
The `step` event carries `kind` (`"answer"` | `"tool"`); for a tool step its
`text` is readable (``Using `tool` {args}``, `<think>` kept). Only
`LoopResult.new_messages` (final answers plus delivered queued prompts) are
appended to the transcript.

#### Events

Unchanged names: `queue`, `step.start`, `token`, `step`, `turn.start`,
`turn.end`, `turn.error`, `connector`, `session`, `usage`, `compaction.*`,
`model.switch.*`, `model.download.progress`, `tools.start`, `tools.done`,
`web_search.start`, `web_search.done`. Additions on 2026-10-31:

| Event | Payload |
|---|---|
| `research.plan` | `{turn_id, needs_web, queries, freshness, entities, reason, source: "model"\|"fallback"}` — the AI's search plan for the message (`docs/research/README.md`) |
| `research.search` | `{turn_id, query, source, count, elapsed_ms, error?}` — one per source per query (wikipedia, searxng, rss, direct, browser, keyed) |
| `research.fetch` | `{turn_id, url, status, chars?, elapsed_ms}` — a result page being read and cleaned |
| `research.done` | `{turn_id, sources, elapsed_ms, budget_exceeded}` — final cited source list (same shape as `turn.end.sources`) |
| `step` | adds `kind` |
| `turn.end` | adds `tool_calls` (count), `tools_used` (names in call order), `skills`, `verification`, `sources` |
| `step` (final answer) | adds `verification`, `sources` |

`verification` is decided by the server, never written by the model:
`"verified"` — a live tool result (search, page, clock, weather) fed the
answer; `"unverified"` — the question was about the present and no live
result existed; `"n/a"` — not a live-data question. `sources` is
`[{title, url, domain, provider, snippet}]` collected from web_search /
wikipedia / web_fetch results used in the turn (max 12, de-duplicated by URL).
The UI renders these as a badge and source chips; the model is instructed not
to write disclaimers or bracketed notes about verification.
| `tool.call` | `{turn_id, session_id, depth, step, source: model\|agent:N\|user, call_id, tool, args, category}` |
| `tool.result` | `{turn_id, session_id, depth, step, source (+ prefetch), call_id, tool, ok, decision, error, offline, latency_ms, approval_id, content (≤ 2000 chars)}` |
| `tool.approval_needed` | `{approval_id, tool, args, reasons, turn_id, session_id, call_id, created_at, expires_in}` |
| `tool.approved` / `tool.denied` | `{approval_id, tool, turn_id, call_id, reason, remember}` |
| `agent.start` | `{turn_id, depth, task, tools, max_steps}` |
| `agent.end` | `{turn_id, depth, status, steps, tool_calls, elapsed, summary}` (or `{status: "error", error}`) |
| `skill.used` | `{turn_id, session_id, skills}` |
| `permissions` | `{mode, workspace, readFiles, writeFiles, runShell, network}` |
| `connector` | extended: `{connector, id?, connected, message, account?, configured?, via?: "oauth"\|"device"\|"token"}` — published after the OAuth callback (success, cancel, failure), device flow end, token save/forget, config save |
| `tool.approval_needed` | carries `level` (`allow\|read\|write\|ask\|always\|deny`, built-ins mapped from their category) and `rememberable` (false for always-ask tools and deletes) for every tool; also on `GET /api/tools/approvals` |
| `mcp.connected` | `{id, name, tools, protocolVersion, server}` |
| `mcp.disconnected` | `{id, name, reason}` |
| `mcp.error` | `{id, name, error}` |
| `tuning.job` | job view `{id, model_id, parent, trigger, state, progress, message, eta_s, loss, eval_loss, base_eval_loss, step, max_steps, paused, plan, build, result, export, new_model_id, error}` on every state change, ≤1/s during progress |
| `tuning.ready` | `{job_id, model_id, parent, name, path, quant, size_bytes, eval{…}, action: {label: "Use it", method: "POST", path: "/api/models/select", body: {id}}}` — the UI offers the switch; the server never switches on its own |
| `tuning.setup` | `{running, stage, percent, message, ok?, error?, venv_bytes?}` |
| `tuning.feedback` | `{turn_id, session_id, rating, consented}` |

Connector and MCP tools appear in `GET /api/tools` with categories
`connector-read`, `connector-write` and `mcp`; their permission levels are in
`docs/connectors/README.md` and `MCP.md`. Settings sections `connectors` and
`mcp` are documented there too.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\events.py`

| Symbol | Signature | Notes |
|---|---|---|
| `CLOSE` | `"__close__"` | Sentinel event type. |
| `Event` | `@dataclass(frozen=True) Event(type: str, data: dict)` | |
| `Subscription.__init__` | `(max_queue: int = 512, on_drop=None)` | `max_queue > 0`. |
| `Subscription.dropped` | `-> int` (property) | |
| `Subscription.put` | `(event: Event) -> None` | Bounded queue: drops the oldest and calls `on_drop` when full; a closed subscription ignores writes. |
| `Subscription.get` | `(timeout=None) -> Event \| None` | `None` on timeout. |
| `Subscription.close` | `() -> None` | Enqueues `CLOSE` (evicting one item if needed). |
| `EventBroker.__init__` | `(*, max_queue=512, mark=None)` | |
| `EventBroker.subscribe` | `() -> Subscription` | Registers and returns a new subscription. |
| `EventBroker.unsubscribe` | `(sub) -> None` | Removes then closes. |
| `EventBroker.publish` | `(event_type: str, data: dict) -> int` | Empty type → `ValueError`; copies `data` per subscriber; returns the subscriber count. |
| `EventBroker.close` | `() -> None` | Closes every subscriber and clears the list. |

A drop is reported through `mark("G3-events", "subscriber", "dropped N event(s): slow reader")`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\settings.py`

| Symbol | Signature | Notes |
|---|---|---|
| `SECTION_RE` | `re.Pattern` | `^[a-z0-9][a-z0-9-]{0,63}$`; blocks traversal. |
| `DEFAULTS` | `dict[str, dict]` | Sections: `general`, `profile`, `server`, `permissions`, `customisation`, `resources`, plus `connectors` (per-service `clientId`, Gmail `tools`/`maxResults`/`bodyChars`, Supabase `keyRole`) and `mcp` (`servers`: the built-in `gmail` and `connectors` entries, both disabled). `read` deep-copies defaults. |
| `SettingsStore.__init__` | `(root: Path \| str)` | Creates the directory. |
| `SettingsStore.read` | `(section: str) -> dict` | Invalid name → `ValueError`; missing/corrupt/non-object file → defaults; stored keys overlay the defaults. |
| `SettingsStore.write` | `(section, values: dict) -> dict` | Non-dict → `ValueError`; merge-on-read, atomic `.json.tmp` + `os.replace`, chmod `0600`. |
| `SettingsStore.sections` | `() -> list[str]` | Sorted union of `DEFAULTS` keys and valid `*.json` stems. |

`DEFAULTS` keys: `general` (`profileName`, `emailRole`, `personaDescription`),
`profile` (`name`, `email`, `role`, `timezone`), `server` (`port`, `logLevel`,
`contextSize`, `gpuLayers`, `threads`, `loadMode`, `lazyMode`, `mmap`,
`flashAttention`, `kvCacheType`, `maxTokens`, `temperature`, `topP`,
`repeatPenalty`, `seed`, `thinkingLevel`, `startWithShell`), `permissions`
(`readFiles`, `writeFiles`, `runShell`, `network`, `sendEmail`), `customisation`
(`theme`, `font`, `backgroundMotion`, `ambientMode`, `glassIntensity`, `density`,
`accentColor`, `notificationDuration`, `reducedMotion`), `resources`
(`cacheLimitGb`, `transcriptDays`, `logDays`).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\clients.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ALLOWED_SCOPES` | `frozenset` | `chat`, `sessions`, `skills`, `models`, `connectors`, `read-only`, `admin`. |
| `ClientStore.__init__` | `(path: Path \| str)` | Creates the parent folder. |
| `ClientStore.list` | `() -> list[dict]` | Views only; no hash. |
| `ClientStore.create` | `(name: str, scopes: list[str]) -> dict` | Name required, ≤ 80 chars; unknown scopes are dropped; no valid scope → `ValueError`; token is `"imu_" + secrets.token_urlsafe(32)` and is returned once. |
| `ClientStore.revoke` | `(client_id: str) -> bool` | Removes the record; `False` when unknown. |
| `ClientStore.authenticate` | `(token: str) -> dict \| None` | Constant-time hash compare; updates `last_used_at`. |

Only the SHA-256 hash is stored; the plaintext token exists only in the `create`
response. **Note:** the only enforcement point is `/v1/*` (`openai_api.py`), which
accepts a client token with the `chat` or `admin` scope; the `/api/*` routes still
accept only the per-run token, so other scopes are labels.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\usage.py`

| Symbol | Signature | Notes |
|---|---|---|
| `UsageTracker.__init__` | `(path: Path \| str)` | Creates the parent folder. |
| `UsageTracker.record_turn` | `(mode: str = "build", wait_seconds: float = 0.0) -> None` | Increments `build` or `plan` for today; keeps at most 500 waits per day. |
| `UsageTracker.record_tokens` | `(count: int) -> None` | Ignores `count <= 0`. |
| `UsageTracker.view` | `() -> dict` | `turnsToday`, `buildTurns`, `planTurns`, `tokensToday`, `queueWaitP50`, `history` (7 bars, 0–100), `historyLabels`, `historyTokens`, `generatedAt`. |

Per-day buckets are keyed by `date.today().isoformat()`; writes are atomic and
chmod `0600`. **Note:** nothing in the server currently calls `record_turn` or
`record_tokens`, so the panel reads zeros unless another caller records usage.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\resources.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ResourceSampler.__init__` | `(data_root, models_root=None)` | `models_root` defaults to `<data_root>/models`. |
| `ResourceSampler.view` | `() -> dict` | `{memory, disk, cpu, cache, transcripts, logs, sampledAt}`. |
| `ResourceSampler.clear_cache` | `() -> dict` | Unlinks `*.gguf` under `models_root`; returns `{"removed_bytes": int}`. |
| `view_with_governor` | `(server) -> dict` | The `GET /api/resources` body (sampler view + `governor`). |
| `handle_update` | `(server, payload) -> (status, body)` | ResourcesUpdate: rejects empty / unknown keys / invalid values (400), writes the `resources` section, `governor.refresh()` (publishes `resources`), returns `{ok, governor}`. |

`_memory` uses `psutil`, then `GlobalMemoryStatusEx` on Windows, then
`/proc/meminfo`; `_cpu_percent` uses `psutil` then `getloadavg`; `_directory_size`
walks at most 20 000 files. A missing probe becomes `None`/`0`, never an
exception. `clear_cache` deletes model weights (its docstring also mentions
transcripts, but the code only removes `*.gguf`).

#### Resource governor contract (`/api/resources`, `resources` event)

Owner: resources (`services/resources/`, `docs/resources/README.md`). This is
the contract the frontend builds against; every field below is produced by
`ResourceGovernor.view()` (`services/resources/governor.py`).

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/resources` | - | Sampler view `{memory, disk, cpu, cache, scratch, transcripts, logs, sampledAt}` plus `governor`: **ResourcesView**. |
| GET | `/api/resources/budget` | - | **ResourcesView** alone. |
| PUT / POST | `/api/resources` | **ResourcesUpdate** | `200` `{ok: true, governor: ResourcesView}`; `400 {error}` for an invalid value, an unknown key or an empty body. Handler: `services/server/resources.handle_update(server, payload)`. **Wiring pending** in `api.py` (not the resources owner's file, see `docs/resources/README.md` "Requests"); until then use the next row, which takes the same body. |
| PUT | `/api/settings/resources` | **ResourcesUpdate** (plus the panel's own `cacheLimitGb`, `transcriptDays`, `logDays`) | `{section, values: {..., computed: ResourcesView}}`. Same validation (`services/resources.validate_settings`); `preset` is accepted as an alias of the stored key `headroom`. Works today. |

**ResourcesUpdate** (every key optional, at least one required):

| Key | Type | Meaning |
|---|---|---|
| `preset` (alias `headroom`) | `"gentle" \| "balanced" \| "performance" \| "custom"` | `gentle` doubles every headroom, `performance` halves it, `custom` uses `custom`. `eco` is accepted as an alias of `gentle` (stored and reported as `gentle`). |
| `custom` | `{cpu?, ram?, vram?, disk?}` numbers 0..90 (percent) | Headroom per resource for `custom`; missing keys take the balanced value. `{}` clears. |
| `dynamic` | bool, default `true` | Lower the next reply's threads while other programs are busy. |
| `lowerPriority` | bool, default `true` | Run IMURUN below normal priority while a reply, voice request or tuning step runs. |

**ResourcesView** (also the data of every `resources` SSE event):

```jsonc
{
  "preset": "balanced", "presets": ["gentle", "balanced", "performance", "custom"],
  "custom": {"cpu": 30.0, "ram": 20.0, "vram": 10.0, "disk": 10.0},   // effective custom % (balanced when unset)
  "dynamic": true, "lowerPriority": true,
  "deviceClass": "laptop",               // laptop | desktop | unknown (battery present = laptop)
  "state": "normal",                     // normal | yielding (threads lowered for the user's other load)
  "reason": "",                          // one human sentence when yielding
  "priority": {"enabled": true, "lowered": false, "mode": "below_normal",  // below_normal | nice | unsupported | off
               "holders": [], "error": ""},          // holders: "chat" | "voice" | "tuning" while lowered
  "gpu": {                               // null only when no source found any GPU
    "detected": true, "vendor": "nvidia", // nvidia | amd | intel | apple | other
    "name": "NVIDIA GeForce RTX 3050 Laptop GPU", "shortName": "RTX 3050 Laptop GPU",
    "source": "nvidia-smi",              // nvidia-smi | rocm-smi | sysfs | registry | cim | system_profiler | engine
    "live": true,                        // usage numbers are measured (false: identity only)
    "vramTotal": 4294967296, "vramFree": 3900000000, "vramUsedByOthers": 300000000,  // bytes; null when unknown
    "utilization": 0.04,                 // 0..1 or null
    "vramHeadroom": 509000000, "vramBudget": 3390000000,   // bytes kept free / IMURUN may use at the next load
    "engine": {"gpuLayers": 16, "totalLayers": 16, "gpuBytes": 858993459, "mix": "q4_k->int8"},  // null before a load
    "label": "GPU: RTX 3050 Laptop GPU - 16/16 layers, 0.8 GB of 3.2 GB"
  },
  "engine": {                            // null until a model is loaded; last report is kept during a reply
    "loaded": true, "gpu": "NVIDIA GeForce RTX 3050 Laptop GPU", "gpuLayers": 16, "ramLayers": 0,
    "diskLayers": 0, "totalLayers": 16, "gpuBytes": 858993459, "mix": "q4_k->int8",
    "threads": 6, "peakRssMb": 1400, "tokensPerSecond": 20.1, "prefillTokensPerSecond": 180.0
  },
  "budget": { "threads": 6, "headroom_cores": 2, "physical_cores": 8, "logical_cores": 16,
              "ram_budget": 0, "ram_budget_gb": 10.7, "vram_headroom": 0, "vram_budget": 0,
              "vram_used_by_others": 0, "disk_min_free": 0, "notes": ["..."],
              "headroom": {"preset": "balanced", "cpu": 30.0, "ram": 20.0, "vram": 11.9, "disk": 10.0,
                           "diskFloorGb": 10.0}, "...": "every size also as *_gb" },
  "machine": {"physicalCores": 8, "logicalCores": 16, "ramTotal": 0, "vramTotal": 0, "deviceClass": "laptop",
              "gpu": "NVIDIA GeForce RTX 3050 Laptop GPU",   // name string ("" = none); kept a string for hardware.js
              "gpuInfo": { /* same as gpu above without engine/label/vramHeadroom/vramBudget, or null */ },
              "diskTotal": 0, "errors": []},
  "live": {"cpu": {"system": 0.12, "imurun": 0.02, "other": 0.10},   // fractions of all logical CPUs, null until 2 samples
           "ram": {"total": 0, "available": 0}, "vram": { /* nvidia-smi / rocm-smi card view or null */ },
           "sampledAt": 1762130000.0},
  "imurun": {"threadTarget": 6, "engineThreads": 6, "engineAppliesTarget": false,
             "ledger": {"total": 6, "held": {"chat": 6}, "free": 0}, "loadEnv": {"FISH_THREADS": "6"},
             "engine": {"gpu": "...", "gpuLayers": 16, "ramLayers": 0, "diskLayers": 0, "totalLayers": 16,
                        "gpuBytes": 0, "mix": "", "threads": 6, "peakRssMb": 0,
                        "tokensPerSecond": 20.1, "prefillTokensPerSecond": 180.0}},  // {} before a load
  "headroomState": {"cpu": "ok", "ram": "ok", "vram": "ok", "disk": "ok"},  // ok | busy | tight | none | unknown
  "disk": {"ok": true, "freeGb": 270.0, "minFreeGb": 40.0, "usableGb": 230.0, "...": "DiskCheck.view()"}
}
```

**Event cadence.** `resources` is published immediately when the settings
change, the thread target changes, the priority is lowered or restored, or a
model load reports its placement; otherwise as a heartbeat every 10 s **only
while at least one `/api/events` client is connected** (no listener: no view
is built and the GPU is not polled). A client that just connected should GET
`/api/resources` once instead of waiting for the heartbeat.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\models_api.py`

An optional FastAPI router (`prefix="/api/models"`), imported by `api.py` only to
obtain `get_model_manager`. It is **not mounted** by `ApiServer`; the stdlib
handler reimplements the same routes.

| Symbol | Signature |
|---|---|
| `DownloadRequest` | Pydantic model `{model_id: str, token: str \| None}` |
| `ModelResponse` | Pydantic model `{models: list[ModelInfo]}` |
| `list_models` | `async () -> ModelResponse` |
| `get_model` | `async (model_id) -> ModelInfo` (404) |
| `download_model` | `async (request: DownloadRequest, req: Request) -> StreamingResponse` (SSE) |
| `delete_model` | `async (model_id) -> dict` (404) |
| `get_model_path` | `async (model_id) -> dict` (404) |

Import fails cleanly when `fastapi`, `pydantic` or `huggingface_hub` is absent;
`api.py` then falls back to `services.model_manager.get_model_manager`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\server\__init__.py` / `__main__.py`

`__init__.py` re-exports `ApiServer`. `__main__.py` runs `main()` from `app.py`,
so `python -m services.server` is equivalent to `python start.py`.

## Connections

Imports from the rest of the tree: `..config.load_config`,
`..connectors.{base,gate,oauth,registry}`, `..persona.{store,tool}`,
`..prompts.queue`, `..safety.decisions`, `..sessions.{session,transcript}`,
`..skills.store`, and `model.fish_client.SyncFishModelClient` (with a
package-relative fallback). `app.py` additionally imports
`..connectors.wiring.wire_server` lazily.

Imported by: `start.py`, `test/services/server/*`, `test/e2e/*`, and the Docker
`python-api` service. The frontend consumes it through
`frontend/design-lab/api.js`.

## Invariants

- The socket binds loopback only. `ApiServer.__init__` raises on any other host;
  the handler also rejects a non-loopback `Host` header (`403`).
- Every `/api/*` route requires the bearer token, except `/health`; `/v1/*` requires the persistent API key (or the bearer token, or a `chat`/`admin` client token). Static files
  are reachable without the token but only on loopback and only from
  `static_root` with an allow-listed extension.
- The token is compared with `secrets.compare_digest`; the query-string form is
  accepted for `GET` only. A denied request marks the path, never the query.
- Request bodies are capped (`max_body`) and must be a JSON object; a refused body
  is drained so the client sees `413`.
- The shell's `index.html` token substitution must stay in sync with the
  `meta[name="api-token"]` read in `api.js`; a leaked token is rotated only by
  `POST /api/auth/rotate`.
- `EventBroker` subscribers are bounded; a slow reader drops events and is
  marked, never allowed to grow memory.
- One turn runs per `TurnRunner.start`; cancelling sets the loop's stop flag and
  the thread cleans up in `finally`. `Turn` records are never evicted.
- Settings section names must match `SECTION_RE`; writes are atomic and private.
- `StackController` never runs a shell; it invokes `docker compose` with an
  argument list and a timeout, and reports failures as data.

`model.load.start` / `model.load.done` / `model.load.error` — the warm start (`{model_name, reason: "warm_start", seconds?, error?}`). A message sent while it runs waits for the weights.
