# Design lab - pick a style, then we refine it

> **This folder is now the production IMURUN shell**, served by the Python
> API. Start with the engineering docs in `docs/frontend/`:
>
> - [`docs/frontend/README.md`](../../docs/frontend/README.md): architecture,
>   files and modules, event flow, state model, theme system, how to add a
>   panel, keyboard shortcuts, native-window notes
> - [`docs/frontend/CHANGELOG.md`](../../docs/frontend/CHANGELOG.md): what
>   changed and why
> - [`docs/frontend/BUGS-FIXED.md`](../../docs/frontend/BUGS-FIXED.md): every
>   bug fixed, with cause and location
> - [`docs/frontend/API-REQUESTS.md`](../../docs/frontend/API-REQUESTS.md):
>   server changes the frontend needs
> - [`docs/frontend/VOICE.md`](../../docs/frontend/VOICE.md): dictation, read
>   aloud, and the planned local speech engine
>
> Quick previews: `?demo=responses` shows every reply type and state;
> `?intro=1` replays the first-launch intro. Theme files are registered in
> `THEMES` in `theme.js` (not `app.js` any more). Parts of the text below
> describe the original mock-up ("nothing calls the API"); the shell now
> calls the real API everywhere except the notification previews and the
> email approval demo. `add_models_tab.py` is a dead one-off script; do not
> run it (see the docs).

**Finalised: Liquid Paper, with Manrope as the UI text font.** The lab opens
on that pair; the other styles and fonts stay available for comparison.
Liquid Paper keeps its warm serif display face for the brand and dialog
headings; Manrope is used for the working text, ids and controls.

Static mockups of the chat screen and the Sessions, Connect, Skills, Persona
and Settings pages, one style per file, all using the same markup. Open
`index.html` directly, or serve the folder:

```text
python -m http.server 8731
```

Then open <http://127.0.0.1:8731/>.

Direct links: `?style=liquid-paper` is the default; `?style=neon` opens another
style; `?page=sessions`, `?page=connect`, `?page=skills`, `?page=persona` or
`?page=settings` opens a page (Chat is the default). `?settings=1` opens the
Settings page on Customisation; `?settings=api`, `permissions`, `server`,
`resources`, `use-by-api`, `usage`, `customisation` or `profile` opens that
section. `?style=liquid-paper&popup=1` opens the email approval notification;
`?style=liquid-paper&notify=success` (or `error` / `neutral`) shows a demo
notification; `?style=liquid-paper&rename=1` opens the rename popup;
`?style=liquid-paper&menu=1` shows a session's right-click menu;
`?style=liquid&font=inter` picks a comparison font.

## The nine styles

| Style | File | Direction |
|---|---|---|
| Obsidian | `styles/obsidian.css` | near-black, warm amber, dense, technical |
| Paper | `styles/paper.css` | warm light, serif headings, calm editorial |
| Neon | `styles/neon.css` | deep blue, cyan/violet glow, glassy |
| Slate | `styles/slate.css` | cool light, blue accent, clean SaaS |
| Phosphor | `styles/phosphor.css` | CRT black/green, monospace, square |
| Aurora | `styles/aurora.css` | soft gradients, glass, rounded, friendly |
| Liquid | `styles/liquid.css` | frosted glass, specular edges, pointer light |
| Liquid Paper | `styles/liquid-paper.css` | warm light glass, serif identity, amber accent |
| Liquid Slate | `styles/liquid-slate.css` | cool light glass, blue accent, clean spacing |

Every style sets the same variables (`--bg`, `--surface`, `--text`,
`--muted`, `--accent`, `--line`, `--radius`, `--font`, ...). The layout and
all components live in `base.css`; a style file is only a variable block plus
a few flourishes.

Liquid Paper keeps three warm colour washes behind the glass. They drift
slowly across the page rather than sitting still; the motion stops when the OS
requests reduced motion.

## Fonts

**Manrope is the selected UI text font.** The Settings page has a **Text**
switcher under Customisation (Manrope, Inter, Grotesk, Style default) so the
alternatives can be compared; the default and the remembered choice is
Manrope. All four are vendored in `fonts/` (SIL OFL) and work offline;
JetBrains Mono is used for
code and ids. The choice can be linked with `?font=manrope` (default),
`?font=inter`, `?font=grotesk`, or `?font=default` for the style's own font.

## Composer

The prompt starts at two lines, grows to five as you type, then scrolls with
the wheel or trackpad. Its scrollbar is hidden, and focus is shown by the
caret rather than a yellow ring. The caret is a 3px accent bar, not the
native 1px line. Ctrl+A (Cmd+A on macOS) selects only the prompt text. Enter
sends the prompt and Shift+Enter adds a line. In the lab, sending appends a
local user message and clears the prompt; it does not call the turn API or
raise a notification. The left side of the action row shows Plan/Build and the
model effort.

The thinking block is a rounded glass panel with softer text; opening it
plays a short droplet animation. A completed row is labelled "Thought" and has
no step count. While a thinking step is live, a fast blue/pink Möbius-like orb
sits to the right of "Thinking"; it is not shown beside "Imurun" and
disappears when the step ends. The transcript and sidebar scroll without a
visible scrollbar.

## Connect

The Connect page mirrors the eight connectors implemented by the backend:
Gmail, GitHub, Slack, Vercel, Render, Cloudflare, Supabase and Canva. Each card
has a proper brand icon, a connected / needs-auth / not-connected state, and a
Connect or Disconnect action. Reads run free; writes and admin actions ask
first. There is no Google Calendar card because there is no backend connector
for it.

## Settings

Settings is a full page, not a sidebar panel. Its section rail holds Profile,
API, Permissions, Server settings, Resource manager, Use by API, Usage and
Customisation. Customisation is one section of that page and contains the
style, text and notification previews. Save buttons and section actions are
mock interactions; they confirm locally and never call the server.

## Notifications and approval

Everything that needs attention enters from the top-right as a stacked card.
The card slides in with a liquid-drop morph and can be dismissed with the
close control; the email approval stays until Send or Cancel. Success is
green, failure is red and everything else is cream. The icons are different
per tone: a check for success, an alert for failure, a clock/info for neutral,
and a mail icon for the email approval.

The email approval shows editable To / Subject / Body fields. Send validates
that all three are present and reports the result; Cancel is recorded as
refused. The notification previews under Settings / Customisation, or the
`notify` direct links, show the success / error / neutral variants.

## Rename

There is no topbar Rename button. Right-click a session under **Sessions** and
choose **Rename**, or type `/rename` in the composer and press Enter, to open
the rename popup for the current session (`/rename-session` is an alias).
Saving updates the session name and the current crumb; the short session id
stays stable and a success notification confirms the change.

## Creating a new style

1. Copy `styles/obsidian.css` to `styles/<name>.css`.
2. Change the variables and any extra rules.
3. Add `{id, label, note, swatch: [bg, surface, accent], dark}` to `THEMES`
   in `theme.js`.

## How to ask for changes

Name the style and the change, e.g.:

- "Obsidian: warmer accent, more spacing in the transcript."
- "Paper: keep the serif brand but use sans for the transcript."
- "Neon: less glow on the composer, stronger focus ring."
- "Slate: sidebar in a slightly darker gray."

## What is mocked

The chat transcript, thinking block, tool row, queued prompt, composer send
(including Enter-to-send), session list, status line, notification stack
(including the editable email approval), rename popup / session context menu,
the eight branded connector cards, and the full Settings page. Buttons switch
the style, show notifications, save mock settings, or open the approval and
rename surfaces; nothing calls the API. These are design mockups, not the
production screens.
