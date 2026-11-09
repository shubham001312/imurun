# Frontend changelog

All changes are in `frontend/design-lab/` unless noted. Bug fixes are listed
one by one, with causes, in `BUGS-FIXED.md`; this file says what changed and
why.

## 2026-11-09: no web-search keys in Settings (research owner)

The product owner's rule: web search never asks the user for an API key (the
website promises "Web search with no API key"). Removed, surgically: the
Settings > Web search tab and section (`mockup.js`), its panel code
(`agent-settings.js`: `SEARCH_KEYS`, `fillSearch`, `buildSearch`, `loadSearch`,
`saveSearch`, the `search` loader) and the "Web search keys" command-palette
entry (`panels.js`); the agent-gallery demo sources now say
`browser:duckduckgo` instead of `brave`. The Permissions > Network toggle (web
search on/off) is unchanged. The `.search-key` / `.search-form` CSS stays: the
connector and Hugging Face token fields reuse it.

## 2026-11-09: Chat messages read like a conversation, not a log

The user called the chat "not good": every reply carried an "IMURUN" label, a
stats line, three text buttons, a "Sources" heading with chips, a "Used
web_search" footer and a "Verified · 4 sources" badge, several saying the
same thing. New: `styles/chat.css` (after workspace.css) and `reply.js`
(chat.js is at its size limit); edits in `chat.js`, `activity.js`, `index.html`.

- **Layout.** One centred 760 px column. The user's turn is a subtle bubble on
  the right; the reply is plain text at full column width (prose capped at
  ~72ch, line height 1.65, more room between turns). The mono all-caps
  "IMURUN" / "YOU" labels are gone (still there for screen readers).
- **Numbers.** "114 tok • 28.5 tok/s • prefill 1.48 s • 8.45 s" is now a
  quiet "8.5 s" at the end of the action row. Its tooltip and accessible name
  hold every figure; click or Enter spells them out in place. Nothing was
  dropped, and `turn.stats` still feeds the Performance drawer.
- **Actions.** Copy, Read aloud, thumbs up / down and Teach are one row of
  28 px line icons with tooltips and aria-labels, shown on hover or keyboard
  focus of the message, always on touch screens, and kept visible while a
  reply is being read aloud. The icons are CSS masks, so voice.js /
  voice-server.js ("Stop reading"), copy ("Copied") and feedback.js
  ("Taught") still relabel the same buttons and the icon follows (stop, check).
- **Sources.** One collapsed row, "Searched the web · 4 sources", with a
  check when the server marked the answer verified (it replaces "Verified ·
  4 sources" and the "Sources" heading). Open, each source is one line:
  initial, title (ellipsis), domain; "Show snippets" adds the snippets. A
  `[n]` citation opens the list before jumping to item n. The live "Found N
  sources / Search finished" status is removed once the reply is in (a
  search that failed or found nothing still says so). "Not verified live"
  sits in the same place when there are no sources.
- **Tools.** The folded pill above the answer names the tools ("Used
  web_search", "Used web_search and web_fetch", "Used 5 tools") and opens to
  the same tool rows with arguments and output. The "Used web_search" footer
  that repeated it is gone; the footer now only shows skills when no tool ran
  and the bare count when the tool frames were missed. The pill and the
  sources row share one style; trace and research labels are sentence case.
- **Markdown.** Headings, lists (muted markers), tables (sentence-case header
  row, hairline frame) and code blocks (language in plain words, quiet Copy,
  sideways scroll inside the block) restyled for the reading view.
- **Plan mode.** A reply generated while the composer was on Plan (read at
  send time, `Turn.mode`) gets a "Plan" tag and a "Switch to Build and do
  this" button under it. The button switches the composer to Build and sends
  "Carry out the plan above." in the same session; it shows only on the
  newest reply. Plan turn ids are remembered in `localStorage`
  (`imurun.chat.planTurns`, last 200) so the tag survives a reload.
- Empty state: a line icon instead of the diamond glyph. Thinking indicator,
  streaming caret, approval cards, queued / stopped / recovered chips and
  error cards are unchanged apart from sentence-case chips.
- Checked in headless Chrome on the response gallery and a replayed
  transcript (light and ImuTechz Console dark, 1280 px and 390 px, no
  horizontal page scroll).

## 2026-11-09: Access level beside the composer, /permission, type-to-focus

- `access.js`: an "Access" pill next to Plan / Build and a `/permission` (or
  `/permissions`) command open one popover with three levels: **Manual** (asks
  before every file read, change and command), **Ask for changes** (default:
  reads run, changes and commands ask) and **Auto** (runs without asking;
  deny-listed commands, protected files and credential folders stay blocked).
  It saves `POST /api/permissions {mode}`, the same setting as Settings >
  Permissions. Checked in the browser: Auto sets `allow-all` on the server,
  and switching back restores `allow-reads`.
- `typeahead.js`: typing on Home or Chat with nothing focused lands in the
  message box (it used to be lost).
- `hosted-models.js`: the model button and picker show the model the engine
  is running even when it was loaded from outside the models folder.

## 2026-11-09: Skills page and the Add skill dialog

- **Add skill** (`skills.js`, `styles/skills.css`) is one dialog with two tabs.
  *Write one*: name, description, instructions, a live preview of the `/<id>` it
  will get (same slug rules as the server) and inline checks that mirror the
  server's limits and catch an id that already exists. *From GitHub*: paste a
  link, Find skills, then a checklist of what the link holds (name, `/id`,
  path, size, status, warnings, files copied and files skipped), select all,
  and Install; each row then says installed, already installed or failed.
  Esc closes, focus is trapped while open and returned on close, arrow keys move
  between the tabs.
- **Cards** show the name, `/<id>`, description and the source (Written by you,
  or GitHub `owner/repo` linking to the commit), with Use in chat (puts `/<id>`
  in the composer), Copy command, Update (GitHub skills) and Delete (confirmed
  in a dialog). A search box appears with more than six skills; the empty state
  offers both ways to add one.
- Fixed: the old New skill button used two `window.prompt`s and sent no
  instructions, so the server refused every skill it tried to create (400
  "a skill needs instructions").
## 2026-11-09: API client dialog, visible API key, Performance panel

- **Add API client is a dialog**, not three browser prompts (`dialogs.js`,
  `styles/dialogs.css`). A name field with inline validation (required, at
  most 80 characters, not a duplicate), the scopes as checkboxes with one
  line each in plain words, grouped into "Works today" (chat, admin: the
  only scopes the server enforces, on `/v1`) and "Saved on the token, not
  enforced yet" (the rest), a warning when none of the chosen scopes can
  call anything, and Create. The token then shows once in a copy box with
  Copy, "You will not see this token again", and Done; a click outside does
  not close that step. Server errors show in the dialog. The client row
  shows "Not used yet" / "Used 3 min ago" instead of a status the server
  never sends.
- **In-app confirms** replace `window.confirm` for Revoke client, Rotate
  token, Clear cache (Settings > Resources) and Regenerate key (Use by API).
  Destructive confirms start on Cancel; Esc cancels; focus returns.
- **Use by API: the key is visible by default.** Hide masks it and is
  remembered (`imurun.openai.keyHidden`, try/catch). Base URL, key and model
  id are full width, monospace and wrap instead of being cut off; Copy stays.
  With no model loaded the panel still shows the base URL and key with a
  note that a model must be loaded to answer, and a link to Settings > Models.
  The cards no longer widen past a narrow column.
- **Performance panel redesigned** (`stats-view.js`, `styles/stats.css`):
  Model (name; size, quantisation, architecture; runs on CPU/GPU, GPU name,
  layers, threads, weight memory, each only when reported), Last reply
  (speed, time to first token or prompt read, tokens as stat tiles),
  Context, Speed trend (sparkline with latest/average/best and per-reply
  tooltips). Rows that were "not reported" and raw keys such as
  `loaded: false` or `Engine: none` are gone. With no model: one line and a
  single "Choose a model" button (Settings > Models). Esc closes the panel
  and focus returns to the Stats button; re-renders keep keyboard focus.
- **Context numbers are correct.** The window in use is
  `engine.model.context` (what the engine loaded with), shown next to the
  model maximum (`engine.model.context_train`, the GGUF context length) and
  the size saved in Settings > Engine; a note says when the run uses less
  than the model supports. The saved `server.contextSize` is not read by the
  server (the engine takes `FISH_CONTEXT_SIZE` or sizes the window to fit
  memory), so the panel labels it "saved, not applied yet" instead of
  showing it as the window (API-REQUESTS 35). No context number is shown
  while no model is loaded. Usage is for the current chat: exact when its
  last reply carried engine counters, else an estimate, labelled as such.

## 2026-11-09: Workspace layout, home launchpad, draft chats

- **Side panel** is a tool rail: New chat on top (Alt N), pages grouped under
  Workspace (Home, Chat, Sessions) and Agent (Skills, Persona, Apps) with line
  icons, Recent sessions, and Settings plus the engine status in the footer.
  Collapsed, it is a 56 px icon rail, where it used to show only the logo.
  Long names end in "…" instead of running past the edge.
- **Settings** is one sheet with an index of 4 groups instead of 17 flat tabs:
  Workspace (Profile, Appearance, Usage), Models (Models, Engine, Resources,
  Voice, Fine-tuning), Agent (Agent, Tools, Permissions, Web search) and
  Connections (Apps, MCP servers, Use by API, API clients). "API" and
  "Use by API" were easy to mix up; the first is now "API clients". Labels
  are in sentence case. Models shows one download button at a time, and line
  icons replace the emoji.
- **Home** is a launchpad instead of a landing page: a greeting, "What are we
  working on?" and the composer, then Recent work (name and time, without the
  internal id) and This machine (engine, model and apps, each with its state
  and the next action).
- **New chat no longer creates an empty session.** The button, Alt+N and the
  palette open a draft; the session is created when the first message is
  sent, so nothing is left behind by a click. Before this, every click saved
  a session called "New chat".
- **Session names read like titles** (`services/sessions/session.derive_name`):
  the first line of real text, without markdown or code fences, URLs shortened
  to their host, "please"/"can you" dropped, cut at a word with "…" (44
  characters at most). Before this: the raw first 60 characters, often cut
  mid-word. Tests: `tests/test_session_names.py`.
- All styling is in `styles/workspace.css` and uses only theme variables.
  Checked: the default light theme, ImuTechz Console (dark), mobile width,
  and the collapsed panel.

## 2026-11-03: Hardware card and GPU chip

- New `hardware.js`: a "Hardware" card at the top of Settings > Resource
  manager (GPU name, VRAM bar, layers on GPU/RAM/disk, CPU threads vs physical
  cores and cores kept free, RAM budget, governor state, headroom preset,
  "lower threads while busy" toggle) and a topbar chip such as
  `GPU 16/16 · 85 tok/s` / `CPU · 6 threads`. Reason: nothing in the UI said
  the 1B model runs fully on the RTX 3050.
- `api.js`: `resources` added to the SSE event list (it was dropped silently).
- Warm start: `model.load.start/done/error` are consumed (and in `EVENT_TYPES`).
  The chip reads "Loading <model>… N s" (also when the page opens mid-load, via
  `GET /api/status` engine.warm_start), then the GPU/CPU chip; errors show
  briefly. The composer stays enabled; `#composer-load-hint` explains the wait.
- Older server: the card says hardware is not reported; no errors.

## 2026-11-02 (follow-up): three server answers used

- Voice F1 is served: the Bengali `large-v3-turbo` choice now applies (the
  "applies once the server supports it" copy is gone). The transcribing
  notice names the model it expects and a finished one says which model
  produced the text (`model`); `model_pending` shows "Downloading
  large-v3-turbo…" and which model was used meanwhile.
- Tuning 4: ratings of a reloaded transcript are restored from
  `GET /api/tuning/feedback?session_id=`.
- Tuning 5: the "Use it" card shows the tuned model's size (`size_bytes`).
- Headless suite: 151 checks.

## 2026-11-02 (later): voice on this computer

The voice owner shipped server-side speech (`docs/voice/README.md`). New
module `voice-server.js`; `voice.js` stays as the browser fallback. Request
#33 in `API-REQUESTS.md` is done except the input-device picker; one server
request is filed (`docs/voice/REQUESTS.md` F1, a speech model per language).

- **Engine choice:** `GET /api/voice/status` decides. Automatic uses this
  computer when the server says speech is available, else the browser's
  speech service; "This computer only" and "The browser" can be forced
  (`voice.engine`). Reasons for unavailability are shown verbatim.
- **Dictation on this computer:** the mic records with MediaRecorder
  (WebM/Opus) and posts the blob to `POST /api/voice/transcribe`; the button
  shows recording, then "transcribing" (4-6 s for a short phrase with the
  default model, said in the notice). On first use the server answers 503
  `model_downloading`: the notice shows the download from
  `voice.model.download` frames and the same recording is transcribed when
  the model is in. The detected `answer_language` rides once on the next
  `POST /api/turns` as `language` (api.js `turnBody`), so the model can
  answer in the language you spoke.
- **Read aloud on this computer:** replies are split into sentences (the
  server's rules) and sent to `POST /api/voice/speak` one by one; sentence
  n+1 is fetched while n plays (`audio/wav` from a `blob:` URL). The button
  turns into "Stop reading"; the tooltip names the voice and whether it was
  online. "Read replies aloud" speaks every new reply.
- **Settings > Voice, "Speech on this computer"** (first card): status; the
  first-run "Which languages do you speak?" picker (English/German only ->
  the fast `base` model, otherwise `small`) with a Bengali `large-v3-turbo`
  option that states its cost (1.62 GB, 23-42 s per utterance here) and that
  it waits on the server (F1); the speech model with sizes; a voice per
  language with Install buttons and download sizes, progress in the panel;
  read replies aloud; detect the spoken language; answer in it; threads (2
  by default; 4 is about 30 % faster but takes CPU from the model); "Online
  voices (sends text to Microsoft)", off by default; the measured accuracy
  note: synthetic speech only, Bengali usable but unreliable (fails roughly
  1 run in 6). The old note promising "a local speech engine is planned" now
  says what stays on the machine.
- Headless suite: 148 checks (fake microphone; voice added). The harness's
  CSP now mirrors the server's (`media-src 'self' blob:`).

## 2026-11-02: the tuning channel in the UI

The tuning owner finished `services/tuning/` (`docs/tuning/README.md`). New
modules: `feedback.js`, `tuning-settings.js`; `api.js` gained the
`/api/tuning/*` calls and the `tuning.job`, `tuning.ready`, `tuning.setup`,
`tuning.feedback` event names. Two API needs are filed as items 4 and 5 in
`docs/tuning/REQUESTS.md`.

- **Thumbs up / thumbs down / Teach on every assistant reply** (live and
  history). They attach themselves to any reply that carries a turn id;
  `chat.js` only tags replies with `data-turn-id` (two lines changed in
  place, still 1492 lines). Up = "learn this answer", down = recorded but
  never trained on, Teach opens an editor prefilled with the answer and sends
  the correction (it replaces the answer in training); clicking the active
  rating again sends `clear` and withdraws consent. The first rating shows,
  once, the consent wording: only rated replies are used, only to fine-tune
  the model on this machine, stored next to the transcripts, never sent
  anywhere. A `tuning.feedback` frame from another window updates the buttons.
  Ratings given before a reload are restored from
  `GET /api/tuning/feedback?session_id=` (tuning REQUESTS 4, served).
- **Settings > Tuning:**
  - what fine-tuning can and cannot do, in plain words: style, format and
    habits yes; facts about people, current events and prices no (the
    research tools do that); an 8B needs a GPU with at least 6 GB and is
    refused here;
  - the training stack: installed or not, "Install training stack
    (~4.9 GB)", progress from `tuning.setup`;
  - the plan for the target model or any other model: fits / slow / refused,
    method, memory needed and available, estimated time (and whether it is
    measured), download size, disk, warnings, the reason verbatim;
  - approved samples (usable of N) and why turns were skipped;
  - **Train now**: a 409 (`needs: confirm | allow_slow`) shows the server's
    message and asks first ("Download 2.5 GB and train", "Train anyway (~41
    h)"); only that click resends with `confirm` / `allow_slow`;
  - the current run: the state machine `queued → preparing → training →
    evaluating → exporting → done` (or failed / rejected / cancelled),
    progress, step, loss, held-out loss, time left, "paused while you chat",
    Cancel;
  - automation (`off | nightly | when-idle | every-N-approved-samples`) and
    the training options, posting only the changed keys; a write-only
    Hugging Face token;
  - run history with held-out loss before → after, sanity checks, the
    result model ("Use it") and the metrics of each run on demand.
- **"Use it" card** on `tuning.ready`: the tuned model's name, quant, size
  when the event carries it (tuning REQUESTS 5), held-out loss and sanity
  checks, and a **Use it** button that sends `POST /api/models/select {id}`.
  Nothing switches by itself.
- Fixed before release: the run-length field had `min=0.1 step=0.5`, which
  made the default 4.0 "invalid" and silently blocked the Tuning form's Save
  (native validation); the field is `step="any"` and the form `novalidate`
  (the server validates). The plan's model list also kept only the model last
  looked at; it now always offers the tuning target too.
- Headless suite: 127 checks (feedback and tuning added). The harness had to
  be rebuilt this session: another session sharing the scratchpad replaced it.

## 2026-11-01 (later): connector contract update, approvals, one Aperture file

- **One Aperture file.** `frontend/design-lab/aperture.js` and the brand
  kit's `brand/vanilla/aperture.js` are now byte-identical (rule: edit the
  kit, copy it to design-lab). The two had diverged: the kit had
  `mount`/`intro`, the app had `markup`/`createMark`/the hydrator and tile
  variants. The merged file keeps both APIs, has the same verbatim
  Mark.jsx math (still identical to the website at 400 sampled instants),
  touches no DOM at load (so `scripts/render-svg.js` can `require` it), and
  reads `--imu-accent` / `--imu-ink` (mapped to the theme's `--logo-*` in
  `styles/tokens.css`). `brand/make_icons.py --check` and the unit checks
  fail when the copies drift.
- **Settings > Connectors to the updated F1 contract**
  (docs/connectors/REQUESTS.md, 2026-11-01):
  - `auth_mode` picks the card. OAuth cards: a single Connect when an app
    exists (`credential_source` builtin / env / user); "Not available yet"
    with the `registration_todo` text and "Use your own app" when it is
    `none`. Connect posts `{open: true}`, so the server opens the **system**
    browser (Google refuses sign-in inside the embedded window); the card
    says "Finish signing in in your browser…" and flips on the `connector`
    frame, with no polling (one status read when the window regains focus,
    in case the event stream was down). A cancelled or failed sign-in shows
    the server's `message`. GitHub keeps "Connect without a browser".
  - Every credential field moved under a collapsed **Advanced**: credential
    source, client id (empty = back to the built-in app), write-only secret,
    redirect URI, scopes, options (Supabase `service_role` warns that it
    bypasses row-level security), tool levels, Gmail "Import client file…"
    ("Choose file…" asks the server to open the OS file dialog,
    `POST /api/system/pick-file`, and fills in the path; with no Tk on the machine
    the button goes away and a typed path is used), and the port note for the built-in Slack / Vercel / Supabase / Canva
    apps (port 8732).
  - **Token cards** (Cloudflare, Render): password field, Verify & save
    (`POST .../token`): 200 shows the account and permissions and clears
    the field, 401 shows the provider's error and keeps it; "Create a token"
    with the minimum permissions per tool; Forget token (`DELETE .../token`).
  - Storage badge on every card ("Encrypted with Windows DPAPI", "macOS
    Keychain", "libsecret", "AES-GCM file", "OS keychain", or "⚠ not
    encrypted" with the warning). Status line with granted scopes and token
    expiry. "How it works" renders `GET .../doc`.
  - The Connect page uses the same server-opened browser flow, and a
    connector frame and the panel share one notice (no duplicate "vercel
    disconnected" after a cancelled sign-in).
- **Approval cards** use the server's native `rememberable` and `level`
  (on every frame and on `GET /api/tools/approvals`): "remember" is hidden
  when `rememberable` is false or the level is `always`; the old name-based
  guess is only a fallback for older servers. Titles, verb buttons and the
  Gmail email preview are as before.
- **MCP servers (F3 re-checked):** auto-approve in the edit form is now a
  multi-select of the server's discovered tools (destructive ones cannot be
  ticked), plus a free list for tools not discovered yet. Everything else in
  F3 was already in place.
- Headless suite: 98 checks (connector contract, the server file dialog and its 501 fallback, approvals, MCP picker
  added); V8: 21 (drift and kit-API checks added).

## 2026-11-01: connectors, MCP servers, research, ImuTechz brand

New modules: `connectors-settings.js`, `mcp-settings.js`, `research.js`,
`aperture.js`, `sound.js`, `dotfield.js`, `styles/integrations.css`,
`styles/imutechz.css`, `styles/imutechz-dark.css`, `brand/make_icons.py`.
Bugs: entries 65 to 67 in `BUGS-FIXED.md`. Server requests: items 24 to 31 in
`API-REQUESTS.md`.

### Brand: the ImuTechz website, ported 1:1

The website's landing animation and logo animation are the brand
signature, so they are ported, not interpreted. The record of what is
identical and what was adapted is the "Brand" section of `README.md`.

- **The Aperture** (`aperture.js`, from the website's `components/Mark.jsx`):
  six superformula blades around a red pupil of the same curve, the
  twelve-o'clock blade in brand red. Live marks breathe on golden-ratio
  frequencies on one shared clock (every mark on the page in lockstep), turn
  while the pupil counter-turns, step at 30 fps when small, pause off screen
  and in hidden tabs, and hold the resting frame under reduced motion. The
  math is verbatim; a V8 check runs the website's own functions beside the
  port and compares the output strings. It replaces every mark: sidebar
  (26 px), Home (40 px), start-up card, the thinking indicator (18 px, the
  website's loader; it breathes only while its reply is thinking) and the
  intro. The earlier "sunburst rays" animation of this release is gone.
- **The landing intro** (`brand.js` + `styles/brand.css`, from
  `components/Intro.jsx` and the `.intro` rules of `app/styles/home.css`):
  cream curtain, the mark at 250 px breathing at speed 3 whooshes from a
  blurred speck to an overshoot and back, an ink and a red ripple, the
  curtain lifts upward and Home rises in behind it while the top bar drops
  in; 2050 ms, or a 350 ms fade under reduced motion. Every timing is the
  website's (measured in the headless suite). It plays on every real start or
  reload, like the website, never on in-app navigation; Settings >
  Customisation "Play the intro on launch" turns it off. The once-per-install
  splash and its slab-serif wordmark are gone.
- **Sound** (`sound.js`, from `lib/sound.js`): the website's synthesised
  palette (tick, open, close, success, error, blip, swell; master gain .3;
  the filtered-noise "paper" burst), unlocked by the first press, one
  remembered toggle (Settings > Voice "Interface sounds"), silent under
  reduced motion. A tick on every press, open/close on the palette,
  shortcuts and drawers, success/error with notices, a swell as the intro
  curtain lifts. The old chime is gone.
- **Dot field** (`dotfield.js`, from `components/DotField.jsx`): the
  website's living background of ink dots that bend around the pointer,
  behind the app with the ImuTechz theme (off with Ambient "none"; not on the
  dark theme, where the website's dots are black on black).
- **Wordmark**: the website's lockup, Inter 700 -0.03em, red I and T, steel
  "echz", on Home ("IMURUN | by ImuTechz").
- `logo.svg` / `logo.ico` / `logo-512.png` are the website's `app/icon.svg`,
  which is exactly the Aperture's resting frame (checked).
- Theme **ImuTechz** (new default) maps the website's tokens
  (`app/globals.css`): canvas `#f3ede2`, surfaces `#faf7f0` / `#ebe4d6` /
  `#e1d9c8`, hairlines, ink `#1c1915`, muted `#58524a` / `#847c70`, one
  accent `#d9481e`, status ok/warn/err, radius 14 / 9 / 20 px, the matte
  shadows and the faint paper grain; no glass, no ambient orbs. Inter for
  text, JetBrains Mono for eyebrows and chips, **Fraunces** (vendored, OFL)
  for titles; ink primary buttons, red only for Send; code blocks on the
  website's dark code ground. **ImuTechz Console** is the website's dark
  palette. The other themes stay as alternatives.
- Existing installs move to the new default unless the user had picked a
  theme or font (BUGS-FIXED 66).
- Raster icons: `logo.ico` now holds 32-bit BMP (DIB) entries at 16 to 256 px,
  which Win32 `LoadImage` and System.Drawing both load (checked); earlier
  PNG entries had crashed an icon loader. One command regenerates both files.

### Settings > Connectors (`connectors-settings.js`)

- One card per service (Gmail, GitHub, Slack, Vercel, Render, Cloudflare,
  Supabase, Canva) with an honest status: Not configured / Configured · not
  connected / Connected as <account> / Needs sign-in again / Signing in….
- OAuth app: client id (shown; "from the environment" when it came from
  `IMURUN_OAUTH_<ID>_CLIENT_ID`), client secret write-only like the search
  keys (never shown, never in `localStorage`, "Remove" clears it), service
  options (Gmail results / characters per email, Supabase key role), the
  redirect URI with Copy, the scopes the next sign-in will ask for, the
  exact-port caveat for Slack / Vercel / Supabase / Canva, a link to the
  provider's console, and for Gmail "import the client file" by path (the
  server reads it, the page never does).
- Connect: `POST .../connect`, the provider page in a window; if pop-ups are
  blocked (the native window), "Open sign-in page" and "Copy link". The card
  finishes on the `connector` event or a status poll (10 minutes, the
  server's state lifetime), and says why when it does not. GitHub also gets
  "Sign in with a code" (device flow: the code shown large, polled until
  connected / denied / expired). Disconnect asks first. Test shows the
  account and three recent threads (Gmail) or the test tool's output.
- Tools: every tool the service enables, with what it does and how it asks
  ("reads", "asks", "always asks"); unticking one saves the tool list (which
  also drops its scope from the next sign-in). Unavailable tools say why.
- Set-up steps inline (built-in key steps until the server serves the docs,
  API-REQUESTS 24). The Connect page cards gained "Set up", which opens the
  card here.

### Settings > MCP servers (`mcp-settings.js`)

- Each server: status dot and chip (off, connecting, connected, not
  connected, error with the reason), transport and command / URL, tool
  count, server name and version, protocol version, auto-approved count.
  Enable switch, Connect / Disconnect, Edit, Remove, Tools.
- Tools: discovered tools with their level ("read-only", "asks", "always
  asks"); auto-approve per tool (disabled for tools the server marks
  destructive).
- Add / edit form: id, name, stdio (command, one argument per line,
  `NAME=value` environment, working folder) or HTTP (URL, `Name: value`
  headers), auto-approve list, timeout, enabled. Saved env and header values
  stay `********` (= unchanged) unless retyped. Validation mirrors the
  server's.
- The two built-in servers (Gmail, IMURUN connectors) are marked as such
  and explained; templates for both plus a filesystem server and a remote
  HTTP server.
- Live `mcp.connected` / `mcp.disconnected` / `mcp.error` frames update the
  card; errors also toast.

### Research in a reply (`research.js`)

- At the top of the thinking panel: the AI's plan ("Looking up" with each
  query, the reason, "needs live data", who planned it), one lane per source
  family (Wikipedia, public search, news, direct) that spins until its
  result arrives and then shows the number of results or "failed" (with the
  reason on hover), the pages being read as domain chips that fill in (a
  page that could not be read is struck through), and a closing line ("Read
  3 pages · 3 sources kept · 3.4 s", or offline / bot-blocked / nothing
  found). A question that needs no lookup shows one quiet "No lookup" line.
  The older "Searching the web…" line is hidden while this block exists.
- The pill follows along ("Looking up …", "Reading docs.python.org") and,
  when research was the only activity, folds to "Researched · read 3 pages".
- Sources are numbered; `[1]`, `[1][2]`, `[1, 3]` and `[2-4]` in the final
  answer become superscript links to the matching chip (click scrolls to and
  highlights the chip; Ctrl/middle click opens the source). Numbers without
  a source, and anything in code, stay as text.
- Gallery: planning, searching (one source refused), reading, done with
  citations, and "no lookup needed".

### Approval cards for connector tools (`approvals.js`, docs/connectors/REQUESTS.md F2)

- "Gmail wants to send an email. Allow it?", with an email preview (To, Cc,
  Subject, Body) instead of raw JSON; the button says Send / Post / Deploy /
  Restart / Delete when the tool does that; tools that always ask hide
  "Approve and remember" and say so.

### Also

- `api.js`: connector config / test / doc, the MCP routes, and the
  `mcp.*`, `research.*`, `verification`, `sources` event names (the stream
  silently drops names it does not list).
- Command palette: Connectors, MCP servers.

## 2026-10-31 (later): the agentic layer in the UI

The server gained tool calling, permissions, approvals, skills and
sub-agents (`docs/agentic/README.md`). The UI now shows all of it. New
modules: `activity.js`, `approvals.js`, `slash.js`, `agent-settings.js`,
`styles/agent.css`. Bugs found on the way are entries 58 to 64 in
`BUGS-FIXED.md`; server changes the UI would like are items 16 to 23 in
`API-REQUESTS.md`.

### Approval cards (`approvals.js`)

- On `tool.approval_needed` a card appears at once: the tool, what it will
  do in plain words, exactly what it touches (the command and where it runs;
  the file, how it is written and a preview of the content; source and
  destination; the folder to delete and whether everything inside goes too;
  the workspace folder), and a risk level (low / changes files / high: shell,
  deletes and overwrites).
- Approve, Deny, and "Approve and remember for this session" (hidden for
  deletes, which the server never remembers). Answers go to
  `POST /api/tools/approvals/{id}`; a 404 (answered elsewhere or expired) is
  shown as such.
- A countdown bar and "Denied automatically in 2:41" mirror the server's
  timeout; under 20 s it turns red.
- Hard to miss: inline in the live reply when that reply is on screen (and
  focused unless you are typing); otherwise a toast that newer notices
  cannot push out and that has no close button, with "Open chat". The tab
  title reads "(1) Approval needed" while anything waits. Cards resolved by
  the server (timeout, Stop, another window) update from
  `tool.approved` / `tool.denied`. Pending cards are restored from
  `GET /api/tools/approvals` on load and on every reconnect.
- Keys: Enter approves (ignored for 0.7 s after the card appears, so an
  Enter meant for the composer cannot approve), Esc denies (before Esc
  reaches "stop the reply").

### Live tool activity (`activity.js`, `chat.js`)

- One row per `tool.call` in the thinking panel, inside the block of the
  step that called it: "web_search latest Python release", then "done ·
  812 ms" / "failed · 8.0 s" / "refused" / "waiting for your approval", with
  a collapsible Result / Error / Why preview. Pre-fetched lookups appear in a
  "Looked up first" block.
- Sub-agents (`agent.start` / `agent.end`) nest their calls under the
  `run_agent` row, with the task, the tools it was given, and "done · 2
  steps · 1 tool call · 4.1 s" plus its summary.
- `skill.used` shows `/skill` chips at the top of the trace.
- The pill says what is happening ("Searching the web", "Running a
  command", "Waiting for your approval") and opens once while tools run;
  when the answer starts it folds to "Used 3 tools · 2 steps" (or "Thought
  for 4.2s · used 3 tools").
- A `step` frame with `kind: "tool"` is never shown as the answer; its
  "Using `tool` {...}" lines are replaced by the rows.
- Reply footer: "Used web_search ×2 · web_fetch · /release-notes", each chip
  marked ok / failed / denied. If the frames were missed, it says only what
  `turn.end.tool_calls` reports ("2 tool calls").

### Verification and sources (coordinator item)

- Searching: "Searching the web…" with three soft pulsing dots and the
  query under the pill, replaced by "Found 4 sources" (pre-fetch) or "Search
  finished" (a search the model asked for). Static under reduced motion.
- Badge next to the stats line from `verification`: "Verified · 3 sources"
  (teal, check) or "Not verified live" (amber, info; tooltip: no live source
  could be reached, the answer comes from the model's knowledge, which may be
  out of date). Nothing for `n/a` or when the server does not say.
- Sources row under the reply from `sources` (or the pre-fetch results):
  chips with a domain-initial avatar (the CSP blocks remote favicons), the
  title and the domain, opening in a new tab; "Show snippets" when snippets
  exist. Chips arrive staggered (not under reduced motion).
- Bracketed notes such as "[note: unable to verify]" or "[unverified]" are
  removed from the final text (never mid-stream; square brackets only; a
  Markdown link is left alone). History messages are cleaned the same way.

### Settings

- **Permissions** (rebuilt, `GET/POST /api/permissions`): the three modes
  as cards with plain-language explanations, a table of what runs / asks /
  is off per mode (the current mode highlighted), the workspace folder (the
  path in use and whether it exists), category switches with what "off"
  means, the approval timeout, remembered approvals, and "Always refused"
  (outside the workspace, protected files, whole drives, the destructive
  command list, switched-off categories, no window connected). Each change
  saves at once and is announced; a `permissions` frame from another window
  refreshes the panel. "Send email" stays a connector setting.
- **Tools** (`GET /api/tools`): every tool with category, what it does in the
  current mode (runs / asks / off), availability, an argument table, the raw
  JSON schema, and a run form generated from the schema. `POST
  /api/tools/run` answering 403 `ask` shows "needs your approval" with the
  reasons and "Run it once" (posts again with `approved: true`); `deny` shows
  the reasons.
- **Agent** (`GET/POST /api/agent`): steps per reply (1 to 20) with what one
  step means, and the sub-agent limits.
- **Search providers**: provider choice, Brave / Tavily / Serper keys and a
  SearXNG URL, with the reason (the keyless DuckDuckGo path is bot-blocked on
  some networks). Keys are write-only in the UI (shown as "saved"), never
  cached in the browser. Stored in settings section `search`, which the
  server does not read yet: see `API-REQUESTS.md` item 16.
- Command palette: Permissions, Tools, Agent, Search providers.

### Composer and chat

- `/` autocomplete: typing `/` at the start or after a space lists skills
  from `GET /api/skills` (and `/rename`); arrows, Enter or Tab inserts
  `/<id> `, Esc closes. Skills the message names show as chips under the
  composer.
- `POST /api/turns` answering 503 with no model selected now shows an inline
  "Choose a model" card (Open Models, Try again) and no error toast. With a
  model selected it still waits for the engine to start.
- Response gallery: searching, live tool rows, an approval card, a finished
  agent reply (ok / failed / denied, sub-agent, skill, footer, verified with
  sources and snippets), "Not verified live" with the note removed, and
  "Choose a model".

## 2026-10-31: frontend overhaul

### Structure

- **Split `app.js` (2,491 lines) into modules** under the 1,500-line limit:
  `core.js` (namespace, utilities, bus, status store), `toast.js`,
  `theme.js`, `chat.js`, `app.js` (shell only), `panels.js`, `brand.js`,
  `voice.js`. Load order and responsibilities are in `README.md`.
- **One namespace, `window.IMURUN`**, so modules call each other at call
  time; the old globals (`window.__imurun_api`, `__imurun_settings`,
  `__imurun_markdown`, `__imurun_models`, `__imurun_boot`,
  `showNotification`) still exist for compatibility.
- **Delegated listeners**: lists render with `innerHTML` and each container
  has one listener bound once, so re-renders never stack handlers.
- **ES2017 syntax everywhere** (no `?.`, no optional catch binding, no object
  spread) so the offline parser check covers every file.
- New stylesheets: `styles/tokens.css` (spacing, type, motion, density
  scales, logo palette), `styles/polish.css`, `styles/panels.css`,
  `styles/brand.css`, `styles/customise.css`.

### Streaming chat (`chat.js`, `api.js`)

- A `Turn` object owns each reply from the moment you press Enter; the
  assistant node exists before the request, so no frame can arrive before
  its UI.
- `streamTurn` waits for the stream to open before posting, filters buffered
  frames by turn id, passes every turn frame (`onEvent`) and the turn id
  (`onTurnId`) to the UI, and recovers a dropped stream by polling
  `GET /api/turns/{id}`.
- Per-step text: the last step is the answer, earlier tool steps fold into
  the thinking panel ("Worked through 2 steps").
- Reasoning (`<think>`, `<thinking>`, `<reasoning>`) streams into a
  collapsible panel with a live one-line preview and timer, folds to
  "Thought for 3.4s" when the answer starts, and renders the same way when a
  session is reloaded.
- Streaming caret: a small liquid drop pulsing at the end of the live text
  (paragraph, list item, table cell or code).
- Messages slide in; the view sticks to the bottom only when you are there,
  otherwise "Jump to latest" appears.
- **Stop** calls `POST /api/turns/{id}/cancel` (button or Esc), even before
  the turn id is known; the reply ends on the server's `stopped` frame.
- **Queue**: Enter during a reply queues the prompt with `POST /api/prompts`
  (button reads "Queue"; the prompt shows "Queued for the next step").
- Per-reply stats line: tokens, tok/s, prefill or first-token time, total
  time, steps. Engine-reported numbers are used when `turn.end` carries
  `stats`; otherwise browser measurements are labelled "measured here", and
  bursts under 0.25 s report no rate.
- Copy (code blocks and whole replies) with visible and announced
  confirmation, and an `execCommand` fallback for non-secure origins.
- Honest engine states: inline "Starting the engine (Ns)" with stack ensure
  and polling; a "No model downloaded" card linking to Models; a
  "still provisioning" card with Retry.
- Chat empty state with suggestions; loading skeleton bubbles; error card for
  sessions that fail to load.

### Smooth streaming and the thinking box (follow-up)

- Answers are patched, not rebuilt: unchanged blocks stay as they are, the
  block being written is updated in place, new blocks fade up once, and
  rendering backs off automatically on long answers. Fast models no longer
  flicker, and code blocks keep their scroll and copy state while streaming.
- The thinking box is redesigned. Collapsed, it is a pill holding the
  spinning mark, a shimmering "Thinking" label, a steady timer and a
  one-line live preview. Opened, the full trace sits in a card that follows
  the newest line unless you scroll up. Opening, closing and folding away
  are animated, and the answer bubble eases in as the first words arrive.
  All of it is instant under reduced motion.

### Every response type (response gallery)

- `?demo=responses`, or "Response gallery" in the command palette, renders
  every reply kind and state with the real code and no API calls: short
  answer, rich Markdown, code (long lines, any language), reasoning, tool
  steps with web search, live thinking, streaming, queued prompt, stopped,
  recovered, empty, provisioning, no model, failed mid-answer, and escaped
  markup with an unbreakable token.
- Markdown renderer: ``` and ~~~ fences with any info string (unclosed fence
  renders while streaming); nested and task lists; ordered list start;
  tables after a paragraph line, `:-:` alignment, escaped pipes, scroll
  wrapper; recursive blockquotes; GitHub callouts (`> [!NOTE]`, TIP,
  IMPORTANT, WARNING, CAUTION); display math (`$$ $$`, `\[ \]`) shown as a
  labelled formula block; images become link chips (the CSP blocks remote
  images); bare URLs autolink; long tokens wrap.

### Theme system and Customisation

- `theme.js` is the only owner of presentation. Live switching without a
  flash; persisted to `localStorage` immediately and to the server
  `customisation` section (debounced); local changes made before the server
  answers win.
- Settings > Customisation rebuilt: theme cards with swatches (all nine
  themes, arrow-key radio group), accent colour (presets, picker, "Use the
  theme accent"), text font, chat text size (90-125%), density (compact /
  comfortable / spacious, driving real spacing tokens), reduce motion (on top
  of the OS setting), background motion, ambient light, glass, notice
  duration, "Replay intro" and "Reset to defaults".
- Theme registry knows light/dark (`body[data-scheme]`), used by the logo and
  callouts.
- Theme changes cross-fade colours (skipped under reduced motion).

### Brand

- Original IMURUN mark, "Confluence" (no letters; it replaced a first
  letter-I draft the product owner rejected): three liquid currents in the
  Liquid Paper washes (amber, rose, sky; your machine, your model, your
  agent) swirl into one local core, and their colours mix where they
  overlap. One arm path, drawn once and rotated 0/120/240 degrees, so every
  part can animate. Assets: `logo.svg` (favicon, own light/dark styles),
  `logo.ico` (16 to 256 px, for the native window, taskbar and shortcuts),
  `logo-512.png` (launchers, touch icon), and the inline version
  (`IMURUN.brand.logo()`) recoloured by `--logo-*` tokens.
- The mark is used and animated in four places: the sidebar (turns by one
  current on hover), the start-up card (slow spin with breathing currents
  and a pulsing core, replacing the atom illustration), every live reply
  (spins as the "thinking" indicator, still once finished) and the intro.
- First-launch intro (~2.9 s, once, flag `imurun.splash.seen.v1`): the three
  currents unfurl out of the core one after another, the vortex settles with
  a small overshoot, the core pops and a ripple leaves it, the wordmark rises
  letter by letter, the tagline settles. Click or any key skips. A
  synthesised WebAudio chime (E5, B5, E6 partials, about 1.4 s, quiet) plays
  as the currents meet; if autoplay is blocked it waits for the first
  gesture during the intro and is skipped afterwards. "Sound on/off" on the
  splash and "Interface sounds" in Settings > Voice persist to the server
  `voice` section. Reduced motion: no intro, no sound. Replay from Settings
  or `?intro=1`.

### Voice (first step, browser engines)

- Mic button in the composer: dictation with interim results via
  `SpeechRecognition`/`webkitSpeechRecognition`, one-time disclosure that
  Chrome/Edge send audio to the vendor's service, clear messages for denied
  microphone, no speech, unsupported language, refused service (WebView2),
  and a disabled button with the reason when unavailable (Firefox).
- "Read aloud" on replies with `speechSynthesis`: Markdown stripped to
  speakable text, code blocks announced as "(code block)", spoken in sentence
  chunks (Chrome stalls on long utterances), toggles to "Stop reading".
- Settings > Voice: language (en-IN, en-US, en-GB, hi-IN, bn-IN, de-DE plus
  every language the installed voices offer, or follow the browser), voice
  (automatic picks a natural/neural voice first), speaking rate, interface
  sounds, sample playback, support status. Stored in the server `voice`
  section. Details in `VOICE.md`.

### Performance and model panel (`panels.js`)

- Topbar "Stats" (Ctrl/Cmd .) opens a drawer: model card (name, engine,
  architecture, quantisation, kernel, context window, file size; every other
  scalar the engine reports; "not reported" where it says nothing; values
  from the catalogue or settings are labelled as such), last reply (speed,
  prefill or first token, total, tokens in/out, steps, source), context use
  (exact from engine stats, else an estimate labelled "estimate"), and a
  speed sparkline over the last 40 replies. On wide windows the page makes
  room instead of being covered.

### Navigation and search

- Command palette (Ctrl/Cmd K): sessions by name and id, pages, Settings
  sections, themes and actions, fuzzy matching, full keyboard support,
  combobox/listbox semantics.
- Sidebar session filter (appears with 4+ sessions).
- Keyboard shortcuts dialog (`?` or Ctrl/Cmd /) and global shortcuts listed
  in `README.md`.
- Mobile and narrow windows: navigation drawer with hamburger, scrim and
  focus trap; topbar labels collapse; works from 720px (pywebview).

### Models

- Model switching is one flow with a visible "switching" state on cards and
  the composer picker, toasts for queued/done/failed, and "Active / In use"
  on the active model.
- Download window: closing minimises while the download runs; "Cancel
  download" is explicit and confirmed; "Keep downloading in the background";
  focus trapped and restored; skeleton cards while loading.
- The guide opens the first time the Models section is shown, not on first
  page load.

### Status, toasts, accessibility

- Status line: engine online / provisioning / unreachable plus live-update
  state, colour-coded; click to re-check and reconnect.
- Toasts update in place by id (compaction, model switch, stack build,
  OAuth), pause on hover and focus, never drop a pending approval, and use a
  spinner icon for work in progress.
- Skip link, landmarks, `aria-current`, Settings tablist, focus traps,
  keyboard context menu, single completion announcement, labelled controls,
  reduced motion everywhere.

### Other

- Persona loads from the server and saves fields the server reads.
- Skills: delete, details from the list, real empty state.
- Resources: CPU meter, "not reported" for missing probes, honest cache
  clearing.
- Server settings: Docker stack status line; build/start progress in one
  updating toast.
- API section: endpoint shows the resolved API base; token rotation confirms,
  masks the new token and reconnects the event stream.
- `frontend/design-lab/README.md` points to these documents.
