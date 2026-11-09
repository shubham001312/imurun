# Frontend bugs fixed

Each entry: **symptom**, **root cause** (in the code before this change),
**fix**, and where the fix lives now (`file:line` in
`frontend/design-lab/`, lines as of this change). The old `app.js` (2,491
lines) was split; "old app.js:N" refers to the previous version.

## Streaming and turns

### 1. First tokens of a reply could be lost
- Symptom: replies sometimes started mid-word, especially the first reply after load.
- Cause: `streamTurn` created the `EventSource` and immediately sent `POST /api/turns`. The broker does not replay, so frames published before the stream connected were gone.
- Fix: wait for the stream's `open` event (bounded to 2.5 s) before posting.
- Where: `api.js:47` (`STREAM_OPEN_TIMEOUT_MS`), wait block after `source.onerror` (~`api.js:340`).

### 2. Another turn's tokens could appear in the answer
- Symptom: stray text from a different tab/session/turn at the start of a reply.
- Cause: frames that arrived before the POST reply were buffered without their turn id being checked, then replayed with `dispatch()`, which did not filter either (old api.js:259).
- Fix: replay only frames whose `turn_id` matches; `dispatch` also drops foreign ids.
- Where: `api.js:371`. Verified end to end with a mocked stream containing a foreign turn.

### 3. A dropped event stream failed a reply that was still running
- Symptom: "event stream disconnected" errors on Wi-Fi blips or laptop sleep, although the server finished the answer.
- Cause: `source.onerror` failed the turn unconditionally (old api.js:228).
- Fix: once the turn id is known, switch to polling `GET /api/turns/{id}` until the turn leaves `running`, then finish with the recorded answer (marked "Recovered after the stream dropped").
- Where: `api.js:284` (`pollTurn`), `api.js:322`.

### 4. A failed reply produced two error toasts and lost its partial text
- Symptom: two "Turn failed" toasts; text already streamed was replaced by "Error: ...".
- Cause: the `onError` callback rendered and toasted, then the rejected promise reached the outer `catch` which rendered and toasted again (old app.js:2269 and 2328).
- Fix: one failure path (`run()`); the partial answer is kept and the error is shown under it.
- Where: `chat.js:514` (`Turn.prototype.fail`), `run()` just above `provisionAndRetry` (`chat.js:667`).

### 5. Enter started a second, concurrent turn while one was streaming
- Symptom: two replies racing in one transcript, shared global state corrupted (stop button, labels).
- Cause: only the Send button was disabled; the Enter key handler still called `sendPrompt()`.
- Fix: while a reply runs, Enter and Send queue the prompt into the running agent loop with `POST /api/prompts` (shown as "Queued for the next step").
- Where: `chat.js:744` (`queue`), `chat.js:987` (`submit`), `api.js:398` (`queuePrompt`).

### 6. Stop re-enabled sending before the turn had actually stopped
- Symptom: after Stop, a new message could start while the old stream still wrote into the transcript.
- Cause: `cancelCurrentTurn` reset the composer immediately after the cancel request (old app.js:2354).
- Fix: the turn stays live until the server's `turn.end` (`status: "stopped"`); a 10 s fallback ends it locally if that frame never comes. Stop before the turn id exists is deferred until the POST replies.
- Where: `chat.js:556` (`Turn.prototype.cancel`), `onTurnId`.

### 7. Multi-step (tool) replies were concatenated into one blob
- Symptom: tool-call JSON and the final answer ran together in the bubble.
- Cause: every `token` was appended to one string regardless of `step`.
- Fix: text is kept per step; the last step is the answer, earlier steps go into the folded panel ("Worked through N steps"); `step` frames replace a step with the authoritative text.
- Where: `chat.js:325` (`onToken`), `Turn.prototype.parts`.

### 8. Engines without token frames showed an empty reply
- Symptom: "No reply." when the engine used the non-streaming path.
- Cause: the answer was built only from `token` frames; `turn.end.answer` and `step.text` were ignored.
- Fix: `step` text and `turn.end.answer` are used when no tokens arrived.
- Where: `chat.js:426` (`Turn.prototype.finish`), `chat.js:351` (`parts`).

### 9. The view jumped to the bottom on every token
- Symptom: impossible to read earlier text while a reply streamed.
- Cause: `transcript.scrollTop = scrollHeight` on each token.
- Fix: stick to the bottom only when the reader is already near it; otherwise show "Jump to latest".
- Where: `chat.js:52` (`nearBottom`, `scrollToEnd`), `bindTranscript`.

### 10. Home quick start did not show the prompt and left it in the composer
- Symptom: starting from Home showed only the reply; the same text sat in the composer, ready to be sent twice.
- Cause: the home handler copied the text into the composer and called `sendToAPI` directly, skipping `appendPrompt` (old app.js:1097-1102).
- Fix: `IMURUN.chat.send()` appends the user bubble itself; the composer is not touched.
- Where: `app.js` `bindHome`, `chat.js` `send`.

### 11. Engine unavailable blocked the whole shell
- Symptom: a 503 covered the app with the boot overlay; "Continue anyway" was needed to read anything.
- Cause: the 503 path re-showed the full-screen boot overlay (old app.js:2300).
- Fix: provisioning progress is shown inline in the reply ("Starting the engine (12s)"); after three minutes an inline card offers Retry / Open Server settings. If no model is downloaded, the card says so and links to Models.
- Where: `chat.js:667` (`provisionAndRetry`).

## Event handling

### 12. Compaction, model-switch and web-search events never reached the UI
- Symptom: no compaction or model-switch notices, no web-search indicator.
- Cause: `EventSource` only delivers named events that have a listener; `subscribeEvents` registered just 11 types (old api.js:15).
- Fix: the list now covers every type the server publishes.
- Where: `api.js:21` (`EVENT_TYPES`).

### 13. Compaction and model-switch toasts could never update
- Symptom: (once events arrived) duplicate toasts that never changed to "done".
- Cause: handlers passed `id`/`persistent` options that `showNotification` ignored, then queried `[data-notification-id]` and `.notification-text`/`.notification-kicker` classes that do not exist (real classes are `notif-*`). The `model.switch.*` code keyed by `event.session_id`, which those events do not carry; the `spinner` icon did not exist.
- Fix: toasts support `id` (update in place), `timeout: 0`, `update()`, `dismiss()` and a spinner icon.
- Where: `toast.js:115` (`update`), `app.js:1292` (compaction), `models.js:216` (model switch).

### 14. Usage panel never updated live
- Symptom: Usage stayed stale after replies.
- Cause: the `usage` frame IS the usage view; the handler read `event.data` (undefined).
- Fix: use the frame itself.
- Where: `app.js:1287`.

### 15. `session` event could throw
- Cause: `event.session.name` read without a guard.
- Fix: refetch the session list instead.
- Where: `app.js` `handleServerEvent`.

### 16. Web-search indicator landed below the streaming answer and claimed "No results found"
- Cause: appended to the end of the transcript; the server always sends `results: []`.
- Fix: shown inside the live reply above the answer; "Web search finished" when there are no results.
- Where: `chat.js` `onServerEvent`.

### 17. Event stream gave up silently
- Symptom: after five failures live updates stopped with no sign.
- Fix: `subscribeEvents` reports `connecting/open/reconnecting/offline`; the status line shows it, clicking it reconnects, and `online`/tab-visible retry automatically.
- Where: `api.js:410` (`onStatus`), `app.js` `startEvents`.

## Markdown and reasoning

### 18. `<think>` blocks shown as literal text in reloaded sessions
- Cause: history went straight to `markdown.render`, which escaped the tags.
- Fix: shared `splitReasoning`; history renders a folded "Thought process" panel.
- Where: `markdown.js:399`, `chat.js:114`.

### 19. Reasoning edge cases
- Only the first `<think>` block was handled; a close tag without an open tag (templates that start inside the block) was shown raw; a tag split across tokens flashed (`<thi`) in the answer.
- Fix: all blocks, implicit opening, and held-back partial tags while streaming.
- Where: `markdown.js:389-440`.

### 20. Code fences with `c++`, `objective-c`, `shell-session`, or `~~~` were not code
- Cause: fence regex `^\s*```(\w*)\s*$`.
- Fix: ``` and ~~~ fences of any length with any info string; an unclosed fence renders as code (streaming).
- Where: `markdown.js:91`.

### 21. A table right after a paragraph line was swallowed into the paragraph; `:-:` separators were not tables
- Cause: the paragraph loop did not check for table starts; the separator regex needed two dashes.
- Fix: `isBlockStart` detects tables; separators accept one dash; alignment and escaped pipes (`\|`, pipes inside code) supported; tables scroll in a wrapper.
- Where: `markdown.js:94`, `markdown.js:106`, `markdown.js:330`.

### 22. Nested lists flattened, quotes lost structure
- Fix: indentation-based nested lists, task items, ordered `start`; blockquotes render recursively; GitHub callouts.
- Where: `markdown.js:166`.

### 23. Code blocks drew a box inside a box
- Cause: themes style every `.bubble pre` as a glass card, including the one inside `.md-codeblock`.
- Fix: reset inside code blocks.
- Where: `styles/polish.css` (".bubble .md-codeblock pre.md-code").

## Models

### 24. "Use" gave no feedback and could raise an unhandled rejection
- Cause: `api.selectModel()` was called without awaiting or catching (old models.js:266); nothing showed the active model.
- Fix: one `selectModel` with a switching state (cards and composer picker), success/error toasts, catalogue refresh; cards show "Active" / "In use".
- Where: `models.js:242`.

### 25. Closing the download window cancelled the download
- Symptom: clicking X, the backdrop, or pressing Esc silently aborted a multi-GB download.
- Cause: all three called `disposeProgress(true)`.
- Fix: while running they minimise to the pill; "Cancel download" is explicit and confirmed; the pill's dismiss asks too.
- Where: `models.js:784`.

### 26. Declining to cancel from the pill still removed the pill
- Cause: `createPill` removed the pill in `finally`, whatever `onDismiss` decided.
- Fix: `onDismiss` returning `false` keeps it.
- Where: `models.js:1040`.

### 27. The models guide popped up over Home on first launch
- Cause: `initModelsTab` called `showFirstTimeGuide()` on page load.
- Fix: shown the first time the Models section opens; Esc closes it; focus is trapped.
- Where: `models.js:216`, `models.js:1205`.

### 28. Pill progress states stuck; download frames split oddly
- Cause: `downloading` class never removed on completion; SSE parsing ignored CRLF and multi-line `data:`.
- Fix: class reset; CRLF normalised; multi-line data joined.
- Where: `models.js` `updateProgressPill`, `api.js` `downloadModel`.

### 29. Composer picker said "No model downloaded" when the catalogue failed to load
- Fix: "Loading models…", "Models unavailable" or "No model downloaded", whichever is true.
- Where: `models.js` `renderPicker`.

## Settings and theme

### 30. Every Settings "Save" button did nothing
- Cause: the section was derived from the button label (`"Server settings"` -> `"Server settings"`), which matched no section, so the handler returned early.
- Fix: the section comes from the enclosing panel.
- Where: `settings.js:266`.

### 31. "Saved" toast shown after a failed save
- Cause: `persist` swallowed the error and resolved.
- Fix: resolves `true/false`; success only on `true`; failures share one toast per section.
- Where: `settings.js:215`.

### 32. Debounced settings lost on close
- Fix: pending saves flush on `pagehide` with `keepalive`.
- Where: `settings.js:234`, `api.js:628`.

### 33. Accent was pinned to amber in every theme
- Cause: `apply()` always set `--accent` on `<html>` from `accentColor`, whose server default is Liquid Paper's `#b45309`; Obsidian, Neon, etc. lost their accents, and soft/line variants were not derived.
- Fix: the accent overrides only when chosen (`accentCustom`), derived tokens are set too, and the old `<html>` override is cleared.
- Where: `theme.js:92`, `theme.js:189`.

### 34. Motion, density, glass and reduced-motion settings had no effect
- Cause: rules like `[data-motion="none"] body` never match (the attribute is on `body`); `--density-scale` and `--glass-opacity` were used nowhere; all of it lived in `liquid-paper.css`, so it vanished with any other theme.
- Fix: rewritten in `styles/customise.css`, theme-independent; density drives real spacing tokens.
- Where: `styles/customise.css`, `styles/liquid-paper.css` (note left in place).

### 35. Theme and font had two sources of truth (flash on load)
- Cause: app.js restored its own `localStorage` keys, then settings.js applied the cached server copy.
- Fix: theme.js is the only applier, migrating the old keys once.
- Where: `theme.js` `initialPrefs`.

### 36. Switching theme flashed unstyled
- Fix: the new stylesheet loads beside the old one, which is removed on `load`.
- Where: `theme.js:141`.

### 37. Persona form overwrote the saved persona
- Cause: the form never loaded the persona, and saved `{name, tone, prompt}` while the server reads `{name, email_role, description}`, so tone and prompt were stored as empty strings.
- Fix: load on start; send both naming schemes (see API-REQUESTS).
- Where: `app.js:945`, `app.js:975`.

## Shell

### 38. "New skill" and "Add client" threw `TypeError: prompt is not a function`
- Cause: the IIFE declared `var prompt` (the composer textarea), shadowing `window.prompt`.
- Fix: `window.prompt` explicitly; composer variable lives in chat.js.
- Where: `app.js:899`, `app.js` `addClient`.

### 39. OAuth message listener leaked and checked the wrong origin
- Cause: the `message` listener was removed only on the postMessage path (not when the popup closed), so each attempt left one behind; origin was hardcoded `http://127.0.0.1:8731`; the "Complete authorization" toast never went away.
- Fix: one `finish()` removes listener and timer and dismisses the toast; origin is the API origin.
- Where: `app.js:772`, `app.js:799`.

### 40. "Clear cache" promised to keep models, then deleted them
- Cause: confirm text said models are kept; the server's clear deletes `*.gguf`.
- Fix: honest confirm and result text, model list refreshed.
- Where: `app.js:1021`.

### 41. Clients empty state disappeared; its "Add client" button was dead
- Cause: `renderClients` wiped the table including the empty state; only the first `[data-clients-action="add"]` got a listener.
- Fix: empty state restored; one delegated listener.
- Where: `app.js:1133`, `app.js:1193`.

### 42. CPU meter stuck on "Loading..."; null memory crashed nothing but stayed blank
- Fix: CPU rendered; unknown values say "Not reported on this system".
- Where: `app.js:1112`.

### 43. Server data inserted as HTML
- Cause: connector `label`/`account` and session ids were interpolated into `innerHTML` unescaped.
- Fix: escaped.
- Where: `app.js:732`, `renderSessions`.

### 44. Skills: clicking a card did nothing; details used a route that does not exist
- Cause: click listeners were bound at start-up to `[data-skill]` elements that did not exist yet; `GET /api/skills/{id}` is not served by the stdlib server.
- Fix: delegated listener using the list data; Delete added.
- Where: `app.js` skills section.

### 45. "New session" in the empty state was dead
- Fix: bound with the other new-session buttons.
- Where: `app.js` `bindHome`.

### 46. Rename showed success before the server agreed
- Fix: optimistic rename with rollback and the toast after the response.
- Where: `app.js:506`.

### 47. Mobile: no navigation at all below 860px
- Cause: `.sidebar { display: none }` with no alternative.
- Fix: a drawer with hamburger, scrim, focus trap and Esc.
- Where: `styles/polish.css` (mobile drawer), `app.js` `setDrawer`.

### 48. A collapsed sidebar squeezed narrow windows into a 56px column
- Cause: `.shell.sidebar-collapsed` (more specific) overrode the mobile grid.
- Fix: explicit mobile grid for both.
- Where: `styles/polish.css` (start of the 860px block).

### 49. CSV export could be cancelled
- Cause: object URL revoked synchronously after `click()`.
- Fix: revoked after 2 s; CSV fields quoted.
- Where: `app.js:1251`.

### 50. Rotating the token broke live updates
- Cause: the open event stream kept the old token in its URL.
- Fix: reconnect after rotation; the token is shown masked; confirm first.
- Where: `app.js:998`.

## Accessibility and leaks

### 51. Toasts announced twice; timers fired on removed nodes
- Cause: the container was `aria-live` and every card had its own role; timers were never cleared on dismiss.
- Fix: container is a plain labelled region; timers cleared and paused on hover/focus.
- Where: `toast.js:370`, `toast.js:247`.

### 52. Screen readers read every streamed token
- Fix: transcript `role="log"` with `aria-live="off"`; completion announced once.
- Where: `chat.js:1023`.

### 53. Dialogs did not trap or return focus; context menu was mouse-only
- Fix: `trapFocus` for rename, palette, shortcuts, download, guide, drawer; keyboard context menu.
- Where: `core.js` `trapFocus`, `app.js:492`, `app.js:320`.

### 54. Every finished message kept animating
- Cause: the thinking orb animation ran on all messages forever.
- Fix: animates only while the model works.
- Where: `styles/polish.css` (".msg:not(.is-thinking) .thinking-orb").

### 56. Fast generation felt glitchy: the whole answer was rebuilt every frame
- Symptom: with a fast model the reply flickered, code blocks jumped back to the left edge of their horizontal scroll, a "Copied" state vanished mid-stream, text selection was lost, and the page could stutter on long answers.
- Cause: `Turn.render` assigned `bubble.innerHTML = renderMarkdown(answer)` on every animation frame, recreating every block.
- Fix: `patchHtml` keeps unchanged blocks, updates only the block being written (for code blocks only the `<code>` text, for tables only the body), and fades genuinely new blocks in (`.md-enter`). Rendering backs off to ~30 or ~16 per second when a render costs more than 5 or 12 ms. Measured in headless Edge at ~2 ms per token: 4 block replacements over the whole reply, and the first paragraph and the code node were the same DOM nodes from start to end.
- Where: `chat.js` `patchHtml`, `updateBlock`, `Turn.prototype.schedule`, `Turn.prototype.render`.

### 57. The thinking box snapped and jittered
- Symptoms: the panel slammed shut when the answer started; with no reasoning it vanished and the answer jumped up by its height; the answer bubble popped in from `display: none`; the timer's text width changed every 100 ms; the label re-rendered every frame, restarting its dots animation; the trace jumped to the bottom even when the reader had scrolled up in it; the summary was a tiny all-caps mono line that read as an error code.
- Fix: open/close and fold-away are height animations (`animateDetails`, `collapseAway`, Web Animations API, instant under reduced motion); the bubble eases in (`.is-revealing`); tabular digits with a fixed width; the label only changes when its text changes, with a short cross-fade; the trace stays pinned only while the reader is at its end; and the panel was redesigned as a quiet pill (spinning brand mark, sentence-case label with a light shimmer while live, timer, a one-line preview of the newest reasoning fading in from the left, chevron) over a capped trace card whose oldest lines fade under the top edge.
- Where: `chat.js` `animateDetails`, `collapseAway`, `Turn.prototype.setLabel`, `foldThinking`; `styles/thinking.css` (rewritten); `styles/polish.css` (`.md-enter`, `.is-revealing`).

### 55. Dead code removed
- `THINK_MAP`/`thinkingComment` and its `MutationObserver` targeted `.thinking.is-thinking p`, which no markup produces (old app.js:706-744).
- The duplicate `composerSend` declaration and the `[data-mock-action]` handler bound to nothing.

## Agentic layer (2026-10-31, second pass)

### 58. Every new agentic frame was silently dropped
- Symptom: no tool activity, no approval card; with the default mode a write or shell call was denied after 180 s ("nobody answered in time") even though a window was open.
- Cause: `EventSource` only delivers named frames that have a listener, and `EVENT_TYPES` / `TURN_EVENT_TYPES` in `api.js` did not list `tool.*`, `agent.*`, `skill.used` or `permissions`.
- Fix: both lists extended. Tool frames for a turn must now carry that exact `turn_id` (`STRICT_TURN_TYPES`), because a manual run from Settings > Tools publishes the same frames with an empty `turn_id`, which the old filter (`event.turn_id && ...`) let through into the live reply.
- Where: `api.js` `EVENT_TYPES`, `STRICT_TURN_TYPES`, `dispatch()`.

### 59. A tool step's text showed as the answer, then stayed in the bubble
- Symptom: "Let me search for that." appeared as the reply, then (with tools) the bubble kept that text while the next step ran, or the pill had folded away so the trace was hidden.
- Cause: `Turn.parts()` always treated the newest step as the answer; the `step` frame's new `kind: "tool"` was ignored; when the answer became empty again nothing cleared the bubble or re-opened the folded pill; and a running fold-away animation hid the pill again after it was shown.
- Fix: `kinds[step]` from the `step` frame; a tool step is never the answer; the bubble is cleared when the answer turns out to be a tool preamble; `Turn.unfold()` restores the live pill and cancels the fold animation. If `turn.end` carries the answer while the last step is a tool step, the answer becomes a new step instead of overwriting the trace.
- Where: `chat.js` `Turn.prototype.parts`, `render`, `unfold`, `finish`.

### 60. Permission toggles could be written back stale
- Symptom (latent, would appear once the mode is changed through `POST /api/permissions`): a later save of Settings > Permissions restored old toggles.
- Cause: `settings.js` PUT the whole cached `permissions` section on every change or Save, overwriting fields written by another path.
- Fix: `permissions` is never PUT as a whole (`NOT_PUT`); the panel writes through `POST /api/permissions` (validated server-side), and `sendEmail` (connector-only) is PUT alone.
- Where: `settings.js` `NOT_PUT`, `persist()`; `agent-settings.js`.

### 61. The Permissions panel claimed "Every write opens an approval card"
- Cause: copy written before permission modes existed; in `allow-all` writes run without asking, and "Commands stay inside the workspace sandbox" overstated what shell confinement does.
- Fix: the panel was rebuilt around the real modes, with the decision table from `GET /api/permissions` and the hard denials in plain language ("Shell is not a sandbox").
- Where: `mockup.js` (Permissions section), `agent-settings.js`.

### 62. A new notice could push a pending approval toast off screen
- Cause: the stack limit protected only the single demo approval (`approvalNode`); any other card could be dismissed as "oldest".
- Fix: `keep: true` cards are skipped when trimming, however many arrive.
- Where: `toast.js` `showNotification` (stack trimming loop).

### 63. Legacy `.tool-row` / `.thinking summary` / `.thinking p` styles leaked into the new rows
- Symptom (found in headless screenshots while building the tool rows): a sub-agent row laid its note, result and children out side by side; result toggles were uppercase mono; notes got the reasoning-bubble background and drop-in animation.
- Cause: `base.css` still carries the original mockup's `.tool-row { display: flex }` and descendant rules on `.thinking summary` / `.thinking p`.
- Fix: scoped resets in `styles/agent.css` (top of the trace section). The legacy rules were left in place (themes still style `.tool-badge`).

### 64. Smaller fixes
- `api.js` `getSkill` said the server had no `GET /api/skills/{id}`; it has one now (comment corrected).
- A recovered turn (stream dropped, outcome polled) now carries `tool_calls`, `skills`, `verification` and `sources` from `GET /api/turns/{id}` when the server provides them (`api.js` `pollTurn`).
- The response gallery's "tool step" example was a raw JSON line the server never sends; it now uses the real frames.

## 2026-11-01: connectors, MCP, research, ImuTechz brand

### 65. Search keys saved in Settings were never used
- Symptom: Settings > Search providers said "saved" for a Brave / Tavily / Serper key, but `web_search` still ran keyless (and was bot-blocked on some networks).
- Cause: the panel wrote `braveApiKey`, `tavilyApiKey`, `serperApiKey` and `searxngUrl`; the server's `search` section (and `apply_search_env`) only knows `IMURUN_BRAVE_API_KEY`, `IMURUN_TAVILY_API_KEY`, `IMURUN_SERPER_API_KEY`, `IMURUN_SEARXNG_URL`. The UI's keys were stored and ignored. The plain SearXNG URL field also PUT `""` on every save, which would have cleared a saved URL.
- Fix: the panel uses the server's names; the SearXNG URL is write-only like the keys (the server masks it) and is only sent when typed. Checked in headless Edge: the PUT body carries `IMURUN_TAVILY_API_KEY` and nothing named `tavilyApiKey`; the key never reaches `localStorage`.
- Where: `agent-settings.js` `SEARCH_KEYS`, `buildSearch`, `loadSearch`.

### 66. A new default theme would have been overwritten by the old one on every load
- Symptom (latent, would appear the moment the default changed): with ImuTechz as the default, a fresh install would still flip to Liquid Paper after `/api/settings/customisation` loaded.
- Cause: every earlier install stored `theme: "liquid-paper", font: "manrope"` locally, and the server's `customisation` defaults return the same; `theme.js` adopts the server copy on hydration, so a default looks exactly like a choice.
- Fix: a `chosen` flag. Picking a theme or font sets it; without it the old defaults are read as "no choice" and become the new defaults (ImuTechz, theme font). A deliberate Liquid Paper choice made from now on sticks. API-REQUESTS.md item 28 asks the server to change its defaults too.
- Where: `theme.js` `LEGACY_DEFAULTS`, `normalise`, `set`.

### 67. Found while building (fixed before release)
- The MCP form's `pattern="[a-z0-9][a-z0-9_-]{0,31}"` is an invalid regular expression under the `v` flag current browsers use for `pattern`, which logs an error on every keystroke and blocks native validation; the attribute was removed (`mcp-settings.js` validates the id itself).
- Integration chips used `--danger-text` as a red text colour; themes define it as text *on* a danger fill (white), so "always asks" was nearly invisible. Now `--danger` (`styles/integrations.css`).
- The connector status chip upper-cased e-mail addresses (chip style of the ImuTechz theme); the status chip keeps its case.
- A reply whose only activity was research (no tool rows, no reasoning) folded its thinking panel away when the answer started, taking the research trace with it; `chat.js` `foldThinking` now keeps it and labels the pill "Researched · read N pages".