# IMURUN frontend (`frontend/design-lab/`)

The shell is plain HTML, CSS and JavaScript with **no build step**. The Python
API (`services/server/api.py`) serves the folder as static files, replaces
`__IMURUN_TOKEN__` in `index.html` with the per-run bearer token, and sends a
strict Content Security Policy:

```text
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; font-src 'self'; connect-src 'self'
```

Consequences for anyone editing the frontend:

- No CDN, no remote font, no remote image, no inline `<script>`. Every script
  is a file loaded with `<script src>` from `index.html`.
- Inline `style="..."` attributes and `element.style` are allowed
  (`'unsafe-inline'` for styles).
- Network calls go only to the same origin (`/api/*`, `/health`).
- Sound must be synthesised (WebAudio); there is no `media-src`.
- Remote images in model output cannot load; the Markdown renderer turns them
  into link chips.

Related documents in this folder:

| File | What it holds |
|---|---|
| `CHANGELOG.md` | Everything changed in the frontend overhaul, and why |
| `BUGS-FIXED.md` | Each bug: symptom, root cause, fix, file:line |
| `API-REQUESTS.md` | Changes the frontend needs from the server |
| `VOICE.md` | Voice input/output today and the planned server engine |

## Running it

- Production: `python start.py` (or `python -m services.server`) serves the
  shell with the token injected. The app may also be shown inside a native
  window (pywebview); the layout needs no browser chrome (see "Native window").
- Design preview without the API: `python -m http.server 8731` inside
  `frontend/design-lab`, then open `http://127.0.0.1:8731/`. Without a server
  the shell shows its honest "server unreachable" states. `api.js` probes
  `/health` and then falls back to `http://<host>:8732`.

Useful links (query parameters):

| Parameter | Effect |
|---|---|
| `?demo=responses` | **Response gallery**: every kind of reply and reply state, rendered by the real chat code from canned frames, no API calls |
| `?intro=1` | Play the intro even when "Play the intro on launch" is off |
| `?page=sessions` (`connect`, `skills`, `persona`, `settings`) | Open a page |
| `?settings=customisation` (`voice`, `models`, `server`, `permissions`, `tools`, `agent`, `search`, `connectors`, `mcp`, `tuning`, ...) | Open a Settings section; `?settings=1` opens Customisation |
| `?style=imutechz-dark` / `?font=inter` | Preview a theme or font without saving it |
| `?notify=success` (`error`, `neutral`), `?popup=1`, `?rename=1`, `?menu=1` | Demo surfaces |

## Files and responsibilities

Scripts load in this order (`index.html`). Order only matters at start-up:
after that, modules talk through `window.IMURUN` at call time.

| File | Lines | Responsibility | Public surface |
|---|---|---|---|
| `mockup.js` | ~510 | Builds the whole DOM (shell, pages, Settings sections, dialogs) as one HTML string into `#app`. Pure markup, no behaviour. | none |
| `api.js` | ~1040 | All HTTP and SSE. Token handling, base URL resolution, `streamTurn`, `subscribeEvents`, every REST call (including tools, approvals, permissions, agent, connector config/test/doc, MCP). `EVENT_TYPES` must list every frame name the server publishes (EventSource drops unlisted names). | `window.__imurun_api` |
| `markdown.js` | ~450 | Safe Markdown renderer (escape first, then structure) and `splitReasoning` for `<think>` blocks. | `window.__imurun_markdown` |
| `core.js` | ~300 | Namespace, utilities, event bus, shared state, `/api/status` store and polling. | `IMURUN.util`, `.store`, `.bus`, `.state`, `.status` |
| `toast.js` | ~390 | Notifications: create, update by id, dismiss, hover-pause; `keep` cards are never pushed out; the Gmail demo approval and previews. | `IMURUN.toast`, `window.showNotification` |
| `boot.js` | ~140 | Start-up overlay: polls `/api/status` until the engine is ready or a grace period passes. | `window.__imurun_boot` |
| `theme.js` | ~440 | Theme system and all presentation preferences; owns Settings > Customisation controls. | `IMURUN.theme` |
| `settings.js` | ~360 | Loads/saves server settings sections; binds `data-setting` inputs and Save buttons. Never PUTs the whole `permissions` section (`NOT_PUT`). | `window.__imurun_settings` |
| `aperture.js` | ~365 | The ImuTechz Aperture: a byte-identical copy of the brand kit's `brand/vanilla/aperture.js` (edit the kit, copy here): the website's Mark.jsx math verbatim, `markup` / `createMark` / `mount` / `intro`, one shared clock, a hydrator that starts every `svg[data-mark-live]` (see "Brand"). Loads right after mockup.js. | `window.ImuAperture` = `IMURUN.aperture` |
| `sound.js` | ~225 | The website's synthesised sound palette and click tick (lib/sound.js); one remembered toggle; silent under reduced motion. | `IMURUN.sound` |
| `brand.js` | ~180 | Marks in the page (`logo()`, `[data-logo]`), the "ImuTechz" wordmark, the landing intro (Intro.jsx port), "Play the intro on launch"; `BRAND` can switch the mark to a file. | `IMURUN.brand` |
| `dotfield.js` | ~180 | The website's DotField background (ImuTechz light theme). | `IMURUN.dotfield` |
| `app.js` | ~1440 | Shell: navigation, drawer, sessions, rename/context menu, home, Connect page (OAuth, "Set up" -> Settings > Connectors), skills, persona, settings actions, resources, clients, usage, server-event routing, start-up. Shares the connector icons as `IMURUN.shell.connectorIcons`. | `IMURUN.shell` |
| `chat.js` | ~1490 | Transcript, composer, the streaming `Turn` controller (per-step trace blocks, tool steps by `kind`), stop/queue, the search indicator, the 503 "Choose a model" state, copy, response-gallery hooks. **At the size limit: put new reply features in `activity.js` or a new module.** | `IMURUN.chat` |
| `activity.js` | ~555 | Tool rows in the thinking panel (`tool.*`, `agent.*`, `skill.used`), the folded pill's tool label ("Used web_search"), the collapsed sources row (up to 12, "Searched the web · N sources", verified check), "Not verified live" note, the reduced footer (skills / missed tool count), `stripNotes` / `stripCalls`. Hands `research.*` frames to research.js. | `IMURUN.activity` |
| `reply.js` | ~240 | The quiet parts of a reply: the compact "8.5 s" stats button (every figure in its tooltip, spelled out on click), the sources toggle, the Plan tag and "Switch to Build and do this" (sets Build, sends "Carry out the plan above."), plan turn ids in `localStorage`. | `IMURUN.reply` |
| `research.js` | ~500 | The research engine in a reply: plan ("Looking up"), one lane per source family, pages being read, closing line; `[n]` citations -> superscript links to source chip n; gallery states. | `IMURUN.research` |
| `approvals.js` | ~600 | Approval cards: inline in the live reply or as a kept toast, countdown, Enter/Esc, restore from `GET /api/tools/approvals`, tab-title flag. Connector tools: "<Service> wants to ...", email preview, Send/Post/Deploy/Delete buttons, no "remember" for always-ask tools. | `IMURUN.approvals` |
| `slash.js` | ~220 | `/skill` autocomplete in the composer and chips for the skills a message names. | `IMURUN.slash` |
| `agent-settings.js` | ~615 | Settings > Permissions, Tools (schemas, manual runs), Agent, Search providers (keys under the server's `IMURUN_*` names); loaded when the section is first shown. | `IMURUN.agentSettings` |
| `connectors-settings.js` | ~930 | Settings > Connectors to docs/connectors/REQUESTS.md F1: `auth_mode` picks an OAuth card (one Connect that has the server open the system browser, device code for GitHub, "Not available yet" + `registration_todo`, credentials under a collapsed Advanced) or a token card (Verify & save / Forget token); storage badge, Test, Disconnect, tool levels, "How it works" from the doc route. | `IMURUN.connectors` |
| `feedback.js` | ~235 | Thumbs up / down / Teach on every assistant reply that has a turn id (`POST /api/tuning/feedback`), the one-time consent notice, sync from `tuning.feedback`. | `IMURUN.feedback` |
| `tuning-settings.js` | ~530 | Settings > Tuning (stack install, plan, samples, Train now with the 409 confirm / allow_slow round trip, the job state machine and Cancel, automation settings, history) and the "Use it" card on `tuning.ready`. | `IMURUN.tuning` |
| `mcp-settings.js` | ~510 | Settings > MCP servers: list with live status, add/edit form, connect/disconnect/remove, discovered tools with auto-approve, templates. | `IMURUN.mcp` |
| `models.js` | ~1320 | Models tab, downloads with progress modal and pill, model switching, composer model picker. | `IMURUN.models`, `window.__imurun_models` |
| `dialogs.js` | ~305 | In-app confirm dialog and the Add API client dialog (name with inline validation, scopes in plain words, token shown once with Copy), replacing `window.prompt`/`confirm` in Settings. | `IMURUN.dialogs` |
| `stats-view.js` | ~310 | Content of the Performance drawer: Model, Last reply (stat tiles), Context (meter: used / window in use, model maximum, saved size), Speed trend (sparkline). Only reported values; one-line empty states. | `IMURUN.statsView` |
| `panels.js` | ~720 | Performance drawer (open/close, Esc, focus return), command palette (session search), shortcuts dialog, sidebar filter, global shortcuts, response gallery (including the agentic states). | `IMURUN.panels` |
| `voice-server.js` | ~615 | Voice on the local server (`/api/voice/*`): MediaRecorder dictation with first-use download and retry, the spoken language for the next turn, sentence-by-sentence read aloud with prefetch and stop, Settings > Voice "Speech on this computer". Takes over the mic and Read aloud (capture-phase clicks) only when the server can do it; voice.js is the browser fallback. | `IMURUN.voiceServer` |
| `voice.js` | ~500 | Dictation (Web Speech recognition), read aloud (speechSynthesis), Settings > Voice. | `IMURUN.voice` |

Every file stays under ~1500 lines; when one grows past that, split it and
add a `<script src>` in `index.html` after its dependencies.

Stylesheets (`index.html` order; later wins on equal specificity):

| File | Holds |
|---|---|
| `fonts.css` | Vendored fonts (`fonts/`, SIL OFL) and `body[data-font]` |
| `base.css` | Layout and components (original design) |
| `styles/<theme>.css` | One theme: colour/radius/font custom properties plus flourishes (`#theme-link`, swapped at runtime). Default: `imutechz.css`; `imutechz-dark.css` imports it and only changes tokens |
| `styles/tokens.css` | Theme-independent scales: spacing, type, motion, density defaults, the mark's palette (`--logo-ray`, `--logo-accent`, `--logo-shadow`, wordmark ink/steel; light and `data-scheme="dark"`) |
| `styles/models.css`, `landing.css`, `skeleton.css`, `thinking.css`, `markdown.css`, `boot.css` | Feature styles (original, lightly fixed) |
| `styles/polish.css` | Chat details (enter animation, streaming caret, actions, errors), states, Markdown extras, Customisation controls, mobile drawer |
| `styles/panels.css` | Topbar actions, performance drawer, palette, shortcuts dialog |
| `styles/brand.css` | Mark placement, the "ImuTechz" wordmark, the landing intro (the website's home.css rules), the Home rise and top-bar drop behind it |
| `styles/integrations.css` | Settings > Connectors and MCP servers, the research block in the thinking panel, citation superscripts and numbered source chips |
| `styles/agent.css` | Tool rows, approval cards, reply footer, verification badge, sources, search indicator, `/` menu, Permissions / Tools / Agent / Search panels. Resets the legacy `.tool-row` / `.thinking summary` / `.thinking p` rules from `base.css` for the new rows. Two extra hues: `--ok-hue`, `--warn-hue` (dark variants under `body[data-scheme="dark"]`). |
| `styles/customise.css` | Preferences: text size, density, motion, ambient, glass, custom accent, reduced motion. Loaded last. |
| `styles/chat.css` | The chat reading view (after workspace.css): centred column, user bubble, plain-text replies with a 72ch measure, Markdown in replies, icon action row on hover/focus (icons are CSS masks so relabelled buttons keep working), folded rows (pill and sources), Plan tag and button. |

`add_models_tab.py` is a dead one-off patch script, see the note at the end.

## Brand (ImuTechz)

The brand is the company website's, ported 1:1. The website is a local
Next.js project at `C:\Users\shubh\Desktop\ImuTechz\WebSite\frontend\`
(read-only for this repo: code is ported, assets are copied, nothing is
linked). The repository-level brand kit (`brand/` at the repo root, see its
README) is the brand source for the website and other products too.

**Rule: edit the kit, copy to design-lab.** `frontend/design-lab/aperture.js`
is `brand/vanilla/aperture.js` byte for byte: the UI's static root cannot
reach `brand/`, so it serves a copy. After changing the kit file, copy it over
and run `py -3.15 frontend/design-lab/brand/make_icons.py --check` (exit 1
while the two differ); the frontend unit checks fail on drift too. The one
file serves both APIs: the kit's (`ImuAperture.mount` / `intro`, Node
`require` for `scripts/render-svg.js`) and the app's (`markup`,
`createMark`, the live-mark hydrator; `IMURUN.aperture` is the same
object). Colours: `--imu-accent` / `--imu-ink`, which `styles/tokens.css`
maps to `--logo-accent` / `--logo-ray` on `body` so the dark themes follow.

### Port record

| Website source | App file | Identical | Adapted (and why) |
|---|---|---|---|
| `components/Mark.jsx` (the Aperture) | `brand/vanilla/aperture.js` = `aperture.js` (byte-identical) | `superR` (M = 6), `bladePaths` (6 blades x 40 segments, gap, `cen` slide, rotation), `pupilPath` (R 5.9, m2 = 1 + (n2-1)*0.5, counter-turn -rot*0.6), `law(t)` (frequencies 1 : PHI : PHI^2 : PHI^3, phases 0.4 / 2.1 / 4.0 / 1.0, n1 0.3->6, n2 0.55->1.5, gap 5->22, `cen` ease-in-out of u4, rot = t*14), `REST` {n1 .38, n2 1.2, gap 11, cen .5, rot 0}, `TEMPO` 1.8, one `EPOCH` per page (every live mark in lockstep), stroke 7.6 / 6.4 / 5.2 by size, blade k = 0 and the pupil in the accent, pupil shadow translate(1.1 1.3) at .16, tiles (`red`: accent tile, white blades; any other tile: ink tile keeping the red blade; scale .78, rx 14), `mono`, `centre`, ~30 fps for marks <= 40 px, stop off screen (IntersectionObserver) and in a hidden tab, resting frame only under reduced motion | React effect -> a hydrator: `markup()` returns the same `<svg>` as a string (so `innerHTML` callers work) and a MutationObserver starts any `svg[data-mark-live]` added to the page. `var(--color-accent)` -> `var(--imu-accent)` (= `--logo-accent`: brand red in every theme, `#ff5c35` on dark like the website's dark palette). The thinking indicator (`live: 'thinking'`) breathes only while its reply is thinking. The in-app "Reduce motion" switch counts as reduced motion too. |
| `components/Intro.jsx` + `app/styles/home.css` (`.intro`, `.intro-in`, `.intro-ring`, `.intro-ring-2`, `.intro-mark`, `.intro-go`, `.has-intro`, keyframes) + the `html:has(.intro)` nav rule of `app/globals.css` | `brand.js` (`playIntro`), `styles/brand.css`, the curtain `<div id="intro">` in `index.html` | Every duration, delay, easing and keyframe: whoosh `intro-zoom` 1.05 s at 0.1 s `cubic-bezier(0.22, 1.2, 0.3, 1)` (scale .14 -> 1.1 at 78 % -> 1, blur 16 px -> 0 by 55 %); ripples `intro-ripple` 1.1 s at 0.45 s (ink, 35 %) and 0.62 s (red), .45 -> 2.4; lift `intro-up` 0.72 s at 1.3 s `cubic-bezier(0.8, 0, 0.2, 1)` to -104 % with the `intro-edge` shadow line; mark 250 px live at speed 3, `clamp(160px, 24vw, 250px)`; node removed at 2050 ms (350 ms and a 0.3 s fade under reduced motion); `has-intro` while it runs; content rises 18 px from 1.55 s + n x 0.09 s (0.75 s), the second block fades in from 1.6 s (0.9 s); the top bar waits above the viewport and drops in with `nav-drop` 0.55 s. Plays on every real load or reload, never on in-app navigation. | Selectors: `.hero3d-copy` -> `.home-hero`, `.hero3d-stage` -> `.home-actions`, `header.site-head` -> `.topbar`. z-index 1200 (website 70) to sit above the start-up card. The curtain is in `index.html`, so the first paint is already cream (the website's server render does the same). "Play the intro on launch" (Settings > Customisation, default on, kept in `localStorage`) and "Replay intro" were added; the website has neither. The old once-per-install splash flag is gone. customise.css shortens every animation under reduced motion, so the curtain's 0.3 s fade is exempted. |
| `lib/sound.js` + `initSound` in `components/initSite.js` | `sound.js` | `tick`, `open`, `close`, `success`, `error`, `blip`, `swell` with the same oscillator types, frequencies, sweeps, gains, durations and delays; the filtered-noise "paper" burst (bandpass, Q .9, cubic decay); master gain .3; unlock on the first gesture; one remembered toggle (switching on answers with `success`); the click tick on `button, a, summary, label, [role='tab']`, 60 ms throttle, skipping `.sound-toggle` and disabled controls | Storage key `imurun.sound` (website `imutechz sound`). The website's comment says sounds are silent under reduced motion but only the click tick checks; here every sound does. Where they play: open/close on the palette, shortcuts, performance drawer and mobile drawer (the website: menu, dropdowns); success / error when such a notice appears; swell as the intro curtain lifts. The old welcome chime is removed. The toggle is Settings > Voice "Interface sounds". |
| `components/DotField.jsx` | `dotfield.js` | GAP 24 (30 under 700 px), REACH 200, PUSH 10, BASE_R 1.35, BASE_A .34, the smoothstep bulge, accent at the centre, eased pointer, dirty-square repaint, loop stops when settled, dpr <= 2, still grid under reduced motion | Shown with the ImuTechz theme (light) unless Customisation "Ambient" is "none"; rebuilt on theme changes. Not on ImuTechz Console: the website's dark palette makes its dots black on black, so the effect is off there. |
| `components/Shell.jsx` `Wordmark` + `.wordmark/.wmr/.wms` | `brand.js` `wordmark()`, `.imu-wordmark` in `styles/brand.css` | Inter 700, -0.03em, red I and T, steel "echz" | Shown on Home as "IMURUN \| by ImuTechz". |
| `app/icon.svg` | `logo.svg`, `brand/icon.svg` | Byte for byte; it is exactly `bladePaths(REST)` / `pupilPath(REST)` at 7.6 px (checked) | - |
| `components/OrbitalHero.jsx` | not ported | - | No page of the website uses it (no import anywhere). |
| `components/EntryLogo.jsx` | not ported | - | Repository avatars for the agents directory, not part of the brand or the landing page. |
| `components/SoundToggle.jsx` | Settings > Voice "Interface sounds" | toggles the same remembered state | The app has no top bar slot for it; the switch lives in Settings. |
| `app/globals.css` tokens | `styles/imutechz.css`, `styles/imutechz-dark.css` | see "Token mapping" | - |
| Fraunces (site build, OFL) | `fonts/fraunces.woff2` | - | - |

Live marks in the app: sidebar 26 px, Home 40 px, the start-up card 84 px,
the thinking indicator 18 px (the website's loader size), the intro 250 px.
`BRAND.mark = 'file'` in `brand.js` swaps every mark for `<img src="logo.svg">`.

### Token mapping

Website -> shell: canvas `--color-canvas #f3ede2` -> `--bg`;
`--color-surface #faf7f0` -> `--surface`; `surface-2 #ebe4d6` -> `--surface-2`;
`surface-3 #e1d9c8` -> `--surface-3`; `line rgba(28,25,20,.10/.22)` ->
`--line` / `--line-strong`; `fg #1c1915` -> `--text`; `fg-muted #58524a` ->
`--muted`; `fg-dim #847c70` -> `--muted-soft`; `accent #d9481e` (hi `#b93a14`,
soft `rgba(217,72,30,.12)`) -> `--accent`, `--accent-hi`, `--accent-soft`, and
`--logo-accent` for the mark; `ok #1f8a46` -> `--success`, `--ok-hue`; `warn
#9a6a12` -> `--warn-hue`; `err #b2352a` -> `--danger`; `teal #15858d`, `steel
#7e858d` -> `--teal`, `--steel`, `--logo-word-steel`; `--ink-rgb 28,25,20`
kept; radius 14 / 9 / 20 px -> `--radius`, `--radius-sm`, `--radius-lg`;
`--shadow-card` / `--shadow-pop` -> `--shadow-card` / `--shadow`; ease
`cubic-bezier(.2,.8,.2,1)` -> `--ease-std`; Inter / JetBrains Mono / Fraunces
500 as on the site; eyebrows mono 11 px uppercase .14em; paper grain at 6 %.

### Raster icons

`logo.svg` is the resting frame, so the raster files never show a moment of
the breath. After the website's `app/icon.svg` changes:

```text
copy WebSite\frontend\app\icon.svg  frontend\design-lab\logo.svg
copy WebSite\frontend\app\icon.svg  frontend\design-lab\brand\icon.svg
py -3.15 frontend\design-lab\brand\make_icons.py
```

This writes `logo.ico` (16, 24, 32, 48, 64, 128, 256 px, 32-bit BMP entries,
so Win32 `LoadImage` and System.Drawing load them; PNG entries crashed an
icon loader before) and `logo-512.png`, each size rendered separately by
headless Edge (Playwright + Pillow, installed for `py -3.15` here). If
`Mark.jsx` changes, port the change into `aperture.js`; the V8 check below
fails until the two agree. Restart the app to see a new taskbar icon.
## State model

All shared state lives in `IMURUN.state` (core.js):

| Field | Written by | Meaning |
|---|---|---|
| `sessions` | app.js | Array from `GET /api/sessions` (`{id, name, created_at, renamed_at}`) |
| `activeSessionId` | app.js (`setActiveSession`) | The session shown on Chat, or `null` |
| `status` | core.js (`IMURUN.status.refresh`) | Last `/api/status` payload, `null` when unreachable |
| `statusError` | core.js | Error from the last failed status call |
| `events` | app.js | SSE state: `connecting`, `open`, `reconnecting`, `offline` |
| `turn` | chat.js | The live `Turn` while a reply streams, else `null` |
| `models`, `modelsDownloaded`, `selectedModel` | models.js (`publish`) | Model catalogue, count of runnable downloaded models, the active one |

Engine state shown to the user is derived, never invented
(`IMURUN.status.engineState`): `unreachable` (no answer), `provisioning`
(`runtime !== 'ready'`), `ready`. "No model downloaded" comes from the model
catalogue, because some compute paths never report a selected model.

Persistent client-side keys (`localStorage`, all wrapped in try/catch):

| Key | Owner | Content |
|---|---|---|
| `imurun.prefs.customisation.v1` | theme.js | Presentation preferences (first-paint copy) |
| `imurun.shell.settings.v1` | settings.js | Last copy of every server settings section |
| `imurun.intro.onLaunch.v1` | brand.js | `off` when "Play the intro on launch" is unticked |
| `imurun.sound` | sound.js | `off` when interface sounds are muted |
| `imurun.tuning.consent.v1` | feedback.js | The rating consent notice was shown |
| `imurun.stats.history.v1` | panels.js | Last 40 per-reply stats |
| `imurun.openai.keyHidden` | openai-settings.js | `1` when the API key is hidden in Settings > Use by API (shown by default) |
| `imurun.stats.open`, `imurun.design.sidebar`, `imurun.chat.mode` | panels/app/chat | UI conveniences |
| `imurun.voice.consent.v1` | voice.js | User acknowledged that browser dictation uses the vendor's service |
| `imurun.models.guided` | models.js | Models guide dismissed |

Server-side settings sections: `profile`, `permissions`, `server`,
`customisation`, `resources`, `voice` (`/api/settings/{section}`, cached
in `localStorage` by settings.js), and `search` (agent-settings.js only;
holds API keys, so it is never cached). `permissions` is loaded by
settings.js for the first paint but written through `POST /api/permissions`
(and `sendEmail` alone through a merge-only PUT); the agent step budget goes
through `GET/POST /api/agent` (stored by the server in section `agent`).

Per-turn state on the chat.js `Turn` (not in `IMURUN.state`): `kinds`
(step -> `tool` | `answer` from the `step` frame), `blocks` (step -> trace
block), `activity` (activity.js controller), `verification`, `sources`,
`searchResults`.

### Internal bus events (`IMURUN.bus`)

| Event | Emitted by | Payload |
|---|---|---|
| `status` | core.js | `/api/status` payload or `null` |
| `models` | models.js | model list |
| `sessions` | app.js | session list |
| `session.active` | app.js | session id |
| `page` | app.js | page name |
| `settings.section` | app.js | Settings section shown |
| `theme` | theme.js | effective preferences |
| `turn.stats` | chat.js | per-reply stats object |
| `turn.settled` | chat.js | (none) the live turn ended |
| `events.status` | app.js | SSE state |
| `server:<type>` | app.js | every SSE frame from `/api/events`, e.g. `server:model.switch.done`. approvals.js listens to `server:tool.approval_needed`, `server:tool.approved`, `server:tool.denied`, `server:permissions`; agent-settings.js to `server:permissions`; connectors-settings.js to `server:connector`; feedback.js to `server:tuning.feedback`; tuning-settings.js to `server:tuning.setup`, `server:tuning.job`, `server:tuning.ready`; mcp-settings.js to `server:mcp.connected`, `server:mcp.disconnected`, `server:mcp.error` |

## Event flow

```text
 user presses Enter in the composer
        |
 chat.js submit()
        |   (while typing "/...": slash.js lists skills; Enter/Tab insert "/<id> ")
        |-- "/rename" ............................. app.js handleCommand -> rename dialog
        |-- no session ............................ app.js createSession -> POST /api/sessions
        |-- a reply is running .................... chat.js queue() -> POST /api/prompts (shown as "Queued")
        v
 chat.js send(): append user bubble, new Turn (assistant node exists NOW)
        |
 api.js streamTurn()
   1. new EventSource(/api/events?token=...)       one stream per turn
   2. wait for `open` (max 2.5 s)                  so the first tokens are not lost
   3. POST /api/turns {session_id, text, mode}     -> {turn_id}
        503 -> chat.js provisionAndRetry: no model downloaded -> "No model
               downloaded"; /api/status.selected_model empty -> "Choose a
               model" (inline, Open Models / Try again, no toast); else wait
               for the engine (stack ensure, up to 3 min) and retry once
   4. replay frames buffered before the reply, keeping only turn_id == ours
      (tool.*, agent.*, skill.used must carry exactly our turn_id: manual
      runs publish them with an empty one)
   5. dispatch in arrival order:
        turn.start / step.start / step  -> Turn.onEvent (step.kind -> Turn.kinds;
                                           step.verification/sources kept)
        tool.call / tool.result /
        tool.approval_needed / tool.approved / tool.denied /
        agent.start / agent.end / skill.used
                                        -> Turn.onEvent -> activity.js (rows in
                                           the step's trace block; approval
                                           frames also -> approvals.js)
        research.plan / .search / .fetch / .done
                                        -> Turn.onEvent -> activity.js ->
                                           research.js (block at the top of the
                                           thinking panel; frames without a
                                           turn_id are kept)
        token                           -> Turn.onToken (per step)
        turn.end                        -> Turn.finish(event)   then stream closes
        turn.error                      -> Turn.fail            then stream closes
   6. stream drops mid-turn -> poll GET /api/turns/{id} until not running
        |
 Turn.render() (at most once per animation frame; backs off to ~30/~16 fps
 when a render costs more than 5/12 ms)
   last step text -> markdown.splitReasoning -> thinking panel + answer bubble
     (unless that step's kind is "tool": then it is trace, the bubble is
     cleared and a folded pill unfolds)
   earlier steps -> one trace block per step (Turn.traceBlock), its
     reasoning text first, then its tool rows; "Using `tool` {...}" lines
     are dropped once rows exist
   answer HTML -> patchHtml(): unchanged blocks kept, the growing block
   updated in place (code: only <code> text; tables: only <tbody>), new
   blocks fade in. Never assign innerHTML to a streaming bubble.
   panel open/close/fold -> animateDetails / collapseAway (height animations)
        |
 Turn.finish: final text cleaned (activity.stripNotes drops "[note: unable
 to verify]"-style notes), stats line (engine counters if turn.end carries
 `stats`, otherwise measured in the browser), Copy / Read aloud, then
 activity.finish: sources row, "Used ..." footer with skill chips,
 verification badge next to the stats; announce "Imurun replied"
        |
 bus 'turn.stats' -> panels.js performance drawer and sparkline

 Separately, app.js subscribeEvents(/api/events) receives every frame:
   usage            -> Usage panel
   compaction.*     -> one toast, updated in place (id "compaction")
   model.switch.*   -> models.js toast "model-switch", refresh catalogue
   web_search.*     -> chat.js: "Searching the web…" (dots + query) under the
                       pill, then "Found N sources" (results kept as sources)
   connector        -> connector card + toast
   turn.*           -> session list "replying" chip
   tool.approval_needed -> approvals.js add(): card inline in the live reply
                       when it is on screen, else a kept toast; countdown;
                       tab title "(N) Approval needed"
   tool.approved / tool.denied -> approvals.js: card shows the outcome, leaves
   permissions      -> Settings > Permissions and Tools refresh
   events 'open'    -> approvals.js restore(): GET /api/tools/approvals

 Answering a card: Approve / Approve and remember / Deny (Enter, Esc) ->
 POST /api/tools/approvals/{id} {approved, remember}; the server then
 publishes tool.approved / tool.denied and the waiting turn continues.

 Settings > Tools run form: POST /api/tools/run {name, args}
   200 -> result; 403 {decision: "ask"} -> "Run it once" -> same POST with
   approved: true; 403 {decision: "deny"} -> the reasons.
```

Stop: the Stop button (or Esc on Chat) calls `POST /api/turns/{id}/cancel`.
If the turn id is not known yet, the cancel is sent as soon as the POST
replies. The reply ends when the server sends `turn.end` with
`status: "stopped"`; if it never does, the UI gives up after 10 s.

## Theme system

A theme is `styles/<id>.css` that defines, on `:root`, at least:
`--bg --surface --surface-2 --text --muted --line --line-strong --accent
--accent-text --accent-soft --accent-line --code-bg --success --success-soft
--danger --danger-line --danger-text --backdrop --focus-ring --shadow --radius
--radius-sm --font --mono`, plus optional flourishes (body background,
glass on `.bubble`, `.send` gradient).

The default theme is `imutechz` (see "Brand"). Preferences carry a `chosen`
flag set when the user picks a theme or font; without it the pre-2026-11
defaults (`liquid-paper`, `manrope`) count as "no choice" and become the
current defaults (BUGS-FIXED 66).

`theme.js` swaps `#theme-link` by loading the new sheet next to the old one
and removing the old one on `load` (no unstyled flash), sets
`body[data-theme]` and `body[data-scheme]` (`light`/`dark`, from the registry,
used for the logo palette and callout colours), and applies preferences:

| Preference | Mechanism | Styled in |
|---|---|---|
| Accent colour | inline `--accent`, `--accent-soft`, `--accent-line`, `--focus-ring`, `--accent-text` on `<body>` (only when `accentCustom`) | everywhere + `customise.css` for theme gradients |
| Chat text size | `--font-scale` (0.9 to 1.25) | `customise.css` |
| Density | `body[data-density]` compact / comfortable / spacious, via `--msg-gap --bubble-pad --row-py --page-pad --card-pad --composer-pad` | `tokens.css`, `customise.css` |
| Reduce motion | `body[data-reduced-motion="1"]`; the OS setting is always honoured too | `customise.css` |
| Background motion / ambient / glass | `body[data-motion]`, `[data-ambient]`, `[data-glass]` | `customise.css` |
| Notice duration | `window.__imurun_notify_timeout` | toast.js |

Persistence: every change is written to `localStorage` at once and pushed
(debounced) to the server `customisation` section. When the server copy
arrives (`imurun:settings-hydrated`), it is adopted unless the user already
changed something on this page, in which case the local copy is pushed.

To add a theme: copy `styles/obsidian.css` to `styles/<id>.css`, change the
variables, and add `{id, label, note, swatch: [bg, surface, accent], dark}`
to `THEMES` in `theme.js`. It appears in the picker and the command palette.

## Adding a panel (Settings section or page)

Settings section:

1. In `mockup.js`, add a tab button
   `<button class="settings-tab" type="button" data-settings-section="mysection">My section</button>`
   to `.settings-subnav`, and a panel
   `<section class="settings-section" data-settings-panel="mysection" hidden>...</section>`
   inside `.settings-content`. app.js wires roles, arrow keys and the crumb.
2. For values stored on the server, give inputs `data-setting="mysection.key"`
   and add `mysection` with defaults to `SECTIONS`/`defaults` in
   `settings.js`. The server accepts any section name matching
   `^[a-z0-9][a-z0-9-]{0,63}$`.
3. For behaviour, add a module (new file + `<script src>`), and react to
   `IMURUN.bus.on('settings.section', fn)` if it should load lazily.
4. Add a palette entry in `actions()` in `panels.js`.

Page: add a `nav-item` with `data-page="x"` and a
`<div class="page-view page-scroll" id="page-x" hidden>` in `mockup.js`;
`IMURUN.shell.setPage('x')` then works, including Alt+number if you extend
`PAGES` in `panels.js`.

Conventions: render lists with `innerHTML` and handle clicks with ONE
delegated listener on the container, bound once; escape all server text with
`IMURUN.util.escapeHtml`; report failures with `IMURUN.toast({tone: 'error',
...})` saying what happened and what to do; never show a number the server
did not give without saying it is an estimate.

## Hardware card

`hardware.js` (loaded after `feedback.js`) owns `#hardware-root`, the first
block of Settings > Resource manager (markup in `mockup.js`), and the topbar
chip `#hw-chip` (click opens that tab).

Data flow: `GET /api/resources` (the `governor` key, a bare governor view is
also accepted) on load; the `resources` SSE event (routed by `app.js` as bus
`server:resources`) after that; polling every 10 s only while the card is
visible. tok/s comes from bus `turn.stats` (engine-sourced only). Fields read
(all optional): `preset`, `dynamic`, `state` (`normal`/`yielding`), `reason`,
`budget.{threads,physical_cores,headroom_cores,ram_budget,ram_total,vram_budget,vram_total}`,
`machine.{gpu,vramTotal,physicalCores,ramTotal}`,
`imurun.engine.{gpuLayers,ramLayers,diskLayers,gpuBytes,mix,threads,gpu}`.
Preset buttons and the dynamic toggle call `__imurun_settings.set/save('resources')`
(PUT `/api/settings/resources {headroom, dynamic}`; presets gentle / balanced /
performance; `custom` is shown but only selectable from the settings file).
There is no process-priority toggle: the server contract has none (see
API-REQUESTS #34).

## Accessibility

- Skip link, landmarks (`nav`, `main`, labelled regions), `aria-current` on
  the active page and session, Settings as a vertical `tablist` (arrow keys,
  Home/End).
- Dialogs (rename, palette, shortcuts, download, guide, drawer) trap focus
  and return it on close; Esc closes the innermost surface first.
- Sidebar sessions: Enter/Space open, F2 rename, Delete delete, arrows move,
  Shift+F10 or the Menu key opens the context menu (arrow keys inside).
- The transcript is `role="log"` with `aria-live="off"`: tokens are not read
  one by one; completion is announced once through `#sr-announcer`.
- Toast cards carry their own `status`/`alert` role (the container is no
  longer a second live region). Timers pause on hover and focus.
- Every animation stops under reduced motion (OS or the Customisation
  toggle); spinners slow down rather than vanish.

## Keyboard shortcuts

`Ctrl/Cmd K` search, `Enter` send (queues while a reply runs), `Shift Enter`
new line, `Esc` stop or close, `/` focus the message box, `Alt N` new chat,
`Ctrl/Cmd B` sidebar, `Ctrl/Cmd .` performance panel, `Alt 1`-`7` pages,
`?` or `Ctrl /` the list.

Approval cards: `Enter` approves when the card has focus (ignored for
0.7 s after it appears), `Esc` denies the focused card, or the card in the
reply on screen when focus is on the page or the composer. That Esc runs in
the capture phase, so with a card waiting Esc denies instead of stopping the
reply (a second Esc then stops it). In the composer's `/` list: arrows,
`Enter`/`Tab` insert, `Esc` closes.

## Native window (pywebview)

- Title: `start.py` names the window "IMURUN"; the page title is "IMURUN".
  The taskbar/window icon is set by the launcher from `logo.ico` (item 14 in
  `API-REQUESTS.md`, done); it now shows the ImuTechz Aperture (resting frame).
- OAuth sign-in: `window.open` may be blocked in the native window; the
  Connectors panel then offers "Open sign-in page" / "Copy link" and finishes
  by polling (API-REQUESTS 31 asks the launcher to open such links in the
  system browser).

- No history/back-forward use anywhere; navigation is in-app state.
- Works from 720px wide: below 860px the sidebar becomes a drawer (hamburger
  in the topbar, scrim, focus trap). Window resizes reflow the composer,
  close menus and drop the drawer when widening.
- `window.confirm`/`window.prompt` are used for destructive confirmations and
  quick inputs; WebView2 shows them natively.
- Links in replies use `target="_blank"`; whether they open in the system
  browser depends on the pywebview host configuration.
- WebView2 exposes `webkitSpeechRecognition` but the service usually refuses;
  dictation then explains why and disables itself (see `VOICE.md`).

## Verification tooling used

No Node on the build machine. Checks used during this work (scripts were
kept out of the repo):

- Syntax: every `*.js` parsed with `esprima` (Python package, ES2017). Code is
  written to ES2017 (no optional catch binding, no object spread, no `?.`).
  On this machine esprima, playwright and mini-racer are installed for the
  Python 3.15 interpreter (`py -3.15`), not in `.venv`.
- Agentic UI (2026-10-31): headless Edge with the production CSP, every
  `/api/*` route mocked in-process and `EventSource` replaced by a fake the
  test drives; 49 checks: gallery states, a live turn (tool step preamble
  cleared, manual-run frame kept out, inline card, toast when Chat is hidden
  and gone when it is back, countdown, title flag, Enter approves, Esc denies
  without stopping, delete card has no "remember"), finish (row latency,
  footer, verified badge, note stripped, title restored, `n/a` has no badge),
  503 -> "Choose a model" without a toast, Permissions (POST, never PUT),
  Tools 403 ask -> approved re-post, Agent, Search (key never in
  `localStorage`), plus light, dark and narrow screenshots under reduced
  motion. `stripNotes` / `stripCalls` / `argSummary` unit-checked in V8.
- Connectors / MCP / research / brand (2026-11-01): headless Edge, same
  set-up (production CSP, mocked `/api/*` including the connector config,
  test, connect, device and import routes and the MCP routes, fake
  `EventSource`, `window.open` returning null as in the native window);
  148 checks (2026-11-02; feedback, Tuning and server voice added; Edge runs with a fake microphone): ImuTechz default theme, canvas colour, the Aperture in the
  sidebar (6 blades + pupil, live, red 12 o'clock blade; resting frame under
  reduced motion, breathing otherwise), home lockup, Fraunces loads; the intro
  measured against Intro.jsx / home.css (curtain `intro-up 0.72s` at 1.3 s
  `cubic-bezier(0.8, 0, 0.2, 1)`, mark `intro-zoom 1.05s` at 0.1 s
  `cubic-bezier(0.22, 1.2, 0.3, 1)`, ripples 1.1 s at 0.45 / 0.62 s, the edge
  shadow, cream, 250 px at speed 3, `has-intro`, Home rise delays, top bar
  hidden then dropped in, the mark breathing across frames, curtain removed at
  ~2050 ms (measured 2032), not replayed on in-app navigation, the launch
  toggle persists; reduced motion: 0.3 s fade, no ripples, removed at ~350 ms,
  measured 336) with frame screenshots at 120 / 400 / 700 / 1000 / 1300 /
  1600 / 1900 ms; the dot field on (light) and off (dark); a press ticks
  (oscillator count), muted and reduced motion stay silent; search keys PUT
  under `IMURUN_*` names and never in `localStorage`; 8 connector cards with
  the right status, levels ("reads", "always asks"), tool toggle POST, Test
  output, client id + secret save (secret cleared from the field and never
  in `localStorage`), exact-port note, Gmail import posts only the path,
  blocked pop-up -> sign-in link -> poll finishes "Connected as", the
  `connector` frame updates a card; MCP built-in entry, tools with
  destructive auto-approve disabled, enable POST keeps `********`,
  `mcp.connected` / `mcp.error` frames, form validation and parsed args/env,
  templates; a live turn driven by engine-shaped `research.*` frames (lanes
  wait, a refused source says failed, a page chip fills in, unreported lanes
  go quiet, verified badge, 3 citation links to numbered chips, the panel
  survives the fold, citation click highlights the chip without
  navigating); gallery research states and the Gmail approval card; dark
  theme blades; no horizontal overflow at 720 px; no CSP violations,
  no console errors. Screenshots reviewed for light, dark, narrow, intro.
- Connectors to the F1 contract of 2026-11-01 (in the 98, including "Choose file…" through `POST /api/system/pick-file` and its 501 fallback): built-in /
  env / user / none credential sources (one Connect, or "Not available yet" +
  `registration_todo` and "Use your own app"), credential fields hidden until
  Advanced is opened, Connect posts `{open: true}` and shows "Finish signing
  in in your browser" with no status polling, the `connector` frame flips the
  card and a cancelled sign-in gives exactly one notice, token cards (password
  field, Create a token link + minimum permissions, 401 keeps the field, 200
  shows account + permissions and clears it, Forget token -> DELETE), the
  storage badge, "How it works" renders the doc route's Markdown, the Gmail
  import result; approvals honour the native `rememberable` / `level`;
  the MCP edit form picks auto-approve from the discovered tools.
- Tuning (in the 127): the history reply carries `data-turn-id` and three
  buttons; up posts `{turn_id, rating, session_id}`, a second click posts
  `clear`, the consent notice appears exactly once with the local-only
  wording, Teach is prefilled with the answer and posts the correction,
  a `tuning.feedback` frame syncs the buttons, a finished live reply gets
  the buttons too; the honest copy; Install posts `/api/tuning/setup` and
  `tuning.setup` frames move the bar; the plan for the target and for an 8B
  ("refused", reason verbatim); samples and skip reasons; Train now: 409
  asks first and only the click resends with `confirm: true`; `tuning.job`
  drives the stepper, progress, loss and "paused", Cancel posts the cancel;
  Settings post only changed keys plus the write-only token; history with
  metrics on open; `tuning.ready` shows the card, never switches by itself,
  and Use it posts `/api/models/select {id}`; no overflow at 720 px.
- Aperture vs the website (V8, 21 checks; the three new ones: the served copy
  is byte-identical to `brand/vanilla/aperture.js`, the kit file loads with
  no DOM and exposes the kit API, `ImuAperture === IMURUN.aperture`): the website's own `Mark.jsx`
  math is extracted and run next to `aperture.js`; the resting frame and
  `law(t)` / blade / pupil paths at 400 instants are identical strings; the
  resting frame equals `logo.svg` (the website's `app/icon.svg`) and the repo
  kit's `brand/svg/aperture-small.svg`; stroke weights, accent blade, shadow,
  tiles, mono, centre, live markup; the sound palette and toggle.
- `logo.ico` loaded through Win32 `LoadImage` (16, 24, 32, 48, 256 px) and
  System.Drawing.
- Unit: `markdown.js` run in V8 (`mini-racer`) against rendering and
  `splitReasoning` cases; 2026-11-01: 24 checks of `research.expand`
  (citation groups and ranges), `laneOf`, `phase`, `mcp.configFrom` /
  `parsePairs` (ids, stdio/http, redacted env kept, headers, timeout clamp).
- Render: headless Microsoft Edge via Playwright with the production CSP
  injected, screenshots of the splash, gallery, settings, dark theme, narrow
  window, palette, stats and shortcuts; and an end-to-end chat against a
  mocked API whose event stream delivers frames before the POST reply and
  includes another turn's frames.

## Dead file: `add_models_tab.py`

A one-off patch script that inserted the Models tab into `mockup.js`. The tab
is already in `mockup.js`, and the script hardcodes another machine's path
(`C:\Users\imu\...`). On this machine it fails on the path; pointed at the
right file it would print "Could not find subnav pattern" and then rewrite
`mockup.js` anyway, reading and writing with the platform default encoding
(cp1252 on Windows), which risks corrupting the file's UTF-8 characters. Do
not run it. It is kept (not deleted) per the task rules; it is safe to
remove.
