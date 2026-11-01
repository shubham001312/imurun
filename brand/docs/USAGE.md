# Using the Aperture

## The logo

The logo is the **bare mark**: five ink blades, the twelve-o'clock blade and the
pupil in brand red, on a transparent ground. No tile, no circle, no outline.

Beside text it forms the **lockup**: the mark, an 8 px gap, then the wordmark
"ImuTechz" in Inter 700, tracking −0.03em, with a red **I** and **T** and a steel
**echz**. In the website nav the mark is 26 px and the wordmark is 17 px.

## Colours

| role | hex |
|---|---|
| Blades, text | `#1c1915` ink |
| Red blade, pupil, I and T | `#d9481e` accent (`#ff5c35` on dark UI) |
| "echz" | `#7e858d` steel |
| Ground | `#f3ede2` cream |

One accent only. Never recolour individual ink blades.

## Which file

| need | file |
|---|---|
| Logo on a page, > 32 px | `svg/aperture.svg` |
| Logo at 21–32 px | `svg/aperture-medium.svg` |
| Logo at ≤ 20 px, favicons | `svg/aperture-small.svg` |
| Single-ink print, stamps | `svg/aperture-mono.svg` |
| Dark grounds | `svg/aperture-reversed.svg` |
| Browser tab | `icons/favicon.svg`, `icons/favicon.ico` (16/32/48) |
| iOS home screen | `icons/apple-icon.png` (180 × 180) |
| Live, breathing mark | `vanilla/aperture.js` → `ImuAperture.mount(el, { size, live: true })` |
| Landing intro | `ImuAperture.intro()` + `vanilla/aperture.css` |
| React (website) | `react/Mark.jsx`, `react/Intro.jsx`, `react/intro.css` |

The three logo weights differ only in blade stroke. Pick by rendered size so
the blades stay legible small and light large.

## Sizes on the website

| size | where |
|---|---|
| 18 px | loaders and skeleton notes |
| 24 px | wordmark component |
| 26 px | nav bar |
| 30 px | footer |
| 160–250 px | landing intro |

## Motion rules

- `live` marks breathe on one shared clock. Don't pass `speed`, except the
  landing intro, which runs at 3.
- Live marks pause off screen and in hidden tabs. Reduced motion shows the
  still icon frame.
- Static files and favicons always use the rest frame. Never export a random
  moment of the breath as the logo.

## Don't

- Put the mark on a coloured tile or in a circle.
- Change the number of blades, the red blade's position, or the pupil.
- Rotate the static logo. The red blade sits at twelve o'clock.
- Stretch it. The box is always square.
- Add an outline, glow or gradient.
- Show the old wordmark-typing intro. The intro is the mark alone.

## Clear space (recommended)

Keep at least a quarter of the mark's width clear on every side. The smallest
size in use is the 16 px favicon, drawn with the small weight.
