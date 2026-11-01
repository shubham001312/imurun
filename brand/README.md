# Brand: the Aperture

The ImuTechz / IMURUN symbol, exactly as used on the ImuTechz website: the
logo, its math, its motion and the landing intro.

Six blades, each one sixth of Gielis's superformula with six-fold symmetry,
around a small red pupil of the same curve. The resting icon is caught half
way between six inward chevrons and a six-petal flower. The twelve-o'clock
blade carries the brand red. Live, the exponents breathe on golden-ratio
frequencies, so the blades pass through star, flower, hexagon and asterisk
while the mark turns and the pupil counter-turns.

**Meaning:** the prompt is where everything the studio ships begins, and the
aperture is open: built in public.

## What's here

| path | contents |
|---|---|
| `docs/USAGE.md` | Logo, lockup, colours, which file to use, sizes, do's and don'ts |
| `docs/MATH.md` | The superformula, blades, pupil, rest frame, breathing law, shared clock |
| `docs/LANDING-ANIMATION.md` | The intro timeline, rules and how to use it |
| `brand.json` | Every colour, size and timing above, machine-readable |
| `svg/` | Static logo: display, medium and small weights, mono, reversed |
| `icons/` | Favicons (`favicon.svg`, `favicon.ico` 16/32/48) and `apple-icon.png` 180 px |
| `vanilla/aperture.js` | Dependency-free live mark and intro, as global `ImuAperture` |
| `vanilla/aperture.css` | Brand tokens, wordmark lockup and intro animation |
| `vanilla/demo.html` | Every size live, the static files, and a replayable intro |
| `react/` | The website's own `Mark.jsx`, `Intro.jsx` and intro CSS, verbatim |
| `scripts/render-svg.js` | Regenerates `svg/` from the math |

## Quick start

Open `vanilla/demo.html` in a browser. It works from disk, no server needed.

In a page, including the design-lab UI, which forbids inline scripts and CDNs:

```html
<link rel="stylesheet" href="brand/vanilla/aperture.css">
<span id="logo"></span>
<script src="brand/vanilla/aperture.js"></script>
<script src="app.js"></script>
```

```js
// app.js
ImuAperture.mount(document.getElementById("logo"), { size: 26, live: true });
ImuAperture.intro();   // the landing animation
```

Regenerate the static SVGs after any change to the math:

```
node brand/scripts/render-svg.js
```

## Source of truth

The website's `frontend/components/Mark.jsx` is the original.
`vanilla/aperture.js` is a line-for-line port. The render script checks it:
`svg/aperture-small.svg` is byte-identical to the website's tab icon. If the
math changes, change both files and re-run the script. The `react/` copies are
snapshots and must be re-copied by hand after a change on the website.
