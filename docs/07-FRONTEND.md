# Frontend plan - TUI + web (proposed)

The frontend renders state and forwards input. It never sees the model, a
provider, a file or the OS (V10, 01-ARCHITECTURE.md). One core, two shells.

## 1. Shape

```text
frontend/
  core/        api client, store, SSE client, formatting, keymap, commands
  web/         index.html, app.js, components/, styles/ (Tailwind output)
  tui/         renderer (ANSI), input loop, screens
  vendor/      tailwind.css, gsap.min.js (offline, no CDN)
```

Both shells import the same `core/`. A screen is a pure `render(state) ->
nodes/strings` function plus a small event binder, so it can be tested
headless.

## 2. Delivery

| Piece | Proposal | Why |
|---|---|---|
| Web UI | static ES modules served by the Python agent API at `/`, opened in the system browser | no Electron, no second runtime, no signing, offline, $0 |
| TUI | JS on a bundled Node runtime (installer provisions it in the background) | one language for both UIs (approved stack); zero npm at runtime |
| Assets | Tailwind CSS output and GSAP vendored in `frontend/vendor/` | no CDN, clean-room safe |
| First paint | the web shell is static and paints before the backend/model is ready | clean-room rule: nothing blocks first paint |

## 3. Screens

| Screen | Content | API |
|---|---|---|
| Chat (main) | transcript (user / assistant / collapsed thinking / tool events), composer, queue badge, stop button, status line | `POST /api/turns`, `GET /api/events` (SSE), `POST /api/turns/{id}/cancel` |
| Sessions | readable id + name, rename, new, per-session memory note | `GET /api/sessions`, `GET /api/sessions/{id}/messages`, `POST /api/sessions/{id}/rename` |
| Connect | 8 provider cards, connected state, connect / disconnect, action approval | `GET /api/connectors`, `POST /api/connectors/{id}/connect`, `GET /oauth/callback`, `POST /api/connectors/{id}/actions/{action}` |
| Persona | name, email role, description (optional), save | `GET/PUT /api/persona` |
| Skills | list, create (name, description, instructions), origin badge | `GET/POST /api/skills` |
| Status | server/model state as designed messages, queue, runtime provisioning | `GET /api/status`, `GET /api/events` |

Chat rules: Enter sends, Shift+Enter adds a line; a queued prompt shows as
"queued - delivered at the next step"; the stop button is always reachable;
no model name is ever shown.

## 4. Popups

One modal at a time, focus trapped, Esc = cancel. Cancel never approves.

| Popup | Primary | Cancel | Rule |
|---|---|---|---|
| Persona | Save | Esc | fields validated, path never shown |
| Connect | Connect (opens OAuth URL) | Esc | waits for callback, then status |
| Action approval | Allow once | Deny | shows provider, action and the exact params |
| Email send | Send | Cancel | shows To / Subject / Body; Cancel is audited as refused |
| Session rename | Rename | Esc | name only; id stays stable |

The approval result is `{approved: true|false}` sent with the action call.
Keyboard: Esc always cancels; Enter activates only the focused button.

## 5. API work still needed

Existing: `/health`, `/api/persona`, `/api/connectors`, connector actions,
`/api/prompts`, `/api/sessions`.

Add (server layer, with Python tests):

```text
GET  /                      static shell; per-run token in a meta tag
GET  /api/status            server + model + runtime state
GET  /api/events            SSE: token, thinking, tool, queue, turn-end, status
POST /api/turns             {session_id, prompt} -> {turn_id}
POST /api/turns/{id}/cancel
GET  /api/sessions/{id}/messages
POST /api/sessions/{id}/rename
POST /api/connectors/{id}/connect   -> {url, state}
GET  /oauth/callback        state + code -> connected
GET/POST /api/skills
```

Security: loopback bind, `Host` must be `127.0.0.1:<port>`, per-run bearer
token (web: meta tag kept in memory, never localStorage; TUI: 0600 runtime
file), CSP `default-src 'self'`, no remote assets, model text rendered with
`textContent` only.

## 6. State and rendering

- `createStore(reducer, initial)` with pure reducers and selectors; no
  framework, no runtime dependency.
- State slices: session, transcript, turn, queue, connectors, persona,
  skills, modals, status.
- Rendering: template functions return DOM nodes (web) or strings (TUI);
  model text never goes through `innerHTML`.
- Transcript is capped (e.g. 500 nodes) with "load earlier"; streaming
  appends one text node, not a full re-render.

## 7. Design system

Dark-first tokens (background, surface, text, muted, accent, danger,
success), 4 px spacing scale, one radius, system font stack. Tailwind output
vendored; GSAP only for 120-200 ms transitions and disabled under
`prefers-reduced-motion`. Contrast AA, visible focus, `aria-live="polite"`
for streaming text.

## 8. Input map (proposed; F-keys stay parked)

| Key | Action |
|---|---|
| Enter | send (Shift+Enter: newline) |
| Esc | close modal / clear composer |
| Ctrl+C | stop the turn |
| Up / Down | prompt history |
| Tab | focus next control |
| `/persona`, `/connect`, `/rename-session`, `/skills` | open the matching screen |

## 9. Testing

- `test/frontend/` per 04-TESTING.md: pure logic with Node's built-in
  `node --test` (store, API client with a mocked fetch, SSE parser, keymap,
  reducers, render-to-string).
- DOM mount tests for the composer and every popup with `jsdom`
  (dev dependency only; runtime stays dependency-free).
- Python tests for the new server routes (`/`, SSE framing, status, turns).
- CI: add a Node job on Windows, macOS, Linux (`npm ci && node --test`);
  the Python job stays. No skips; a failing or skipped test fails the gate.

## 10. Build order

| Batch | Content | Evidence |
|---|---|---|
| F1 | `core/`: api client, store, SSE parser, formatting, keymap | node tests |
| F2 | server: shell route + token bootstrap, status, SSE, turns, skills, connect | Python tests |
| F3 | web shell: chat screen, composer, queue badge, stop, status | node + Python tests |
| F4 | popups: persona, connect, approval, email Send/Cancel, rename | node DOM tests |
| F5 | TUI: ANSI renderer, input loop, same screens | node tests |
| F6 | sessions and skills screens | node tests |
| F7 | polish: GSAP transitions, a11y, reduced motion, e2e turn flow | e2e |

## 11. Confirmed decisions (2026-10-26)

1. Web delivery: static shell served by the loopback API, opened in the
   system browser. No Electron (V32).
2. TUI runtime: JS on a bundled Node runtime, provisioned in the background;
   zero npm at runtime (V33).
3. Streaming: SSE over the loopback API (V34).
