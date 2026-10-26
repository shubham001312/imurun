# Design lab - pick a style, then we refine it

Static mockups of the main chat screen, one style per file, all using the
same markup. Open `index.html` directly, or serve the folder:

```text
python -m http.server 8731
```

Then open <http://127.0.0.1:8731/>.

Direct links: `?style=neon` opens a style, `?style=paper&popup=1` opens the
approval popup on load, `?style=liquid&font=inter` picks the text font.

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

## Fonts

The lab bar has a **TEXT** switcher: Style default, Inter, Manrope, Grotesk.
All four are vendored in `fonts/` (SIL OFL) and work offline; JetBrains Mono
is used for code. The chosen font is remembered and can be linked with
`?font=inter`, `?font=manrope`, or `?font=grotesk`.

## Creating a new style

1. Copy `styles/obsidian.css` to `styles/<name>.css`.
2. Change the variables and any extra rules.
3. Add `{id, file, label, note}` to `THEMES` in `app.js`.

## How to ask for changes

Name the style and the change, e.g.:

- "Obsidian: warmer accent, more spacing in the transcript."
- "Paper: keep the serif brand but use sans for the transcript."
- "Neon: less glow on the composer, stronger focus ring."
- "Slate: sidebar in a slightly darker gray."

## What is mocked

The chat transcript, thinking block, tool row, queued prompt, composer,
session list, status line, and the email Send/Cancel approval popup. Buttons
switch the style and open/close the popup; nothing calls the API. These are
design mockups, not the production screens.
