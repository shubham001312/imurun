# The Aperture: the math

Everything here is implemented in `vanilla/aperture.js` and in the website's
`components/Mark.jsx`. The two must stay identical. `scripts/render-svg.js`
proves it: its `aperture-small.svg` is byte-identical to the website's tab icon.

## The curve

Each blade is one sixth of **Gielis's superformula** with six-fold symmetry:

```
r(θ) = ( |cos(mθ/4)|^n₂ + |sin(mθ/4)|^n₃ )^(−1/n₁)      a = b = 1, n₃ = n₂, m = 6
```

`n₁` and `n₂` decide the shape family. The chevron and flower rows are the
exact anchors of the icon; the others are approximate regions the breath
passes through:

| n₁ | n₂ | shape |
|---|---|---|
| small (≈0.3) | < 1 | sharp six-point star |
| ≈0.4 | ≈1 | inward chevrons |
| ≈0.35 | ≈1.45 | six-petal flower |
| large (→6) | ≈1–1.5 | soft hexagon |
| any | ≥ 2 | approaches a plain ring (never used) |

The curve is normalised by its largest radius, so every frame fills the same
24-unit radius inside the 64 × 64 box, centred on (32, 32).

## The blades

Six arcs of the curve, one per 60° sector. Each blade:

- is centred at `c = c₀ + k·60°` for blade `k = 0…5`;
- spans `c − 30° + gap/2` to `c + 30° − gap/2`, so `gap` is the angular
  space between neighbouring blades;
- is sampled at 41 points and drawn as a round-capped, round-joined stroke.

`cen` slides every blade's centre from the curve's **valley** (`cen = 0`) to
its **lobe** (`cen = 1`). That slide is what turns chevrons into petals.

Blade 0 sits at twelve o'clock and carries the brand red. The other five are
ink.

## The pupil

A filled shape of the same curve in the middle, radius 5.9:

- rounder than the blades: its exponent is `n₂′ = 1 + (n₂ − 1) · 0.5`;
- counter-rotates at 0.6× the blade rotation;
- sampled at 121 points and closed;
- has a soft shadow: the same path in ink at 16% opacity, offset (1.1, 1.3).

## The icon (rest frame)

The static logo and every favicon use one frame, caught half-way between the
chevrons (n₁ 0.42, n₂ 1, cen 0) and the flower (n₁ 0.34, n₂ 1.45, cen 1):

```
REST = { n1: 0.38, n2: 1.2, gap: 11°, cen: 0.5, rot: 0 }
```

## Stroke weight by size

Blades get thicker as the mark gets smaller, so they read at favicon sizes.

| rendered size | blade stroke (in 64-unit box) |
|---|---|
| ≤ 20 px | 7.6 |
| 21–32 px | 6.4 |
| > 32 px | 5.2 |

## The breathing law

A live mark is driven by mark-time `t = (now − EPOCH) / 1000 · TEMPO`. Four
sine waves on golden-ratio frequency ratios 1 : φ : φ² : φ³ drive four
parameters. Because φ is irrational, the combined motion never repeats.

```
W₀ = 2π / 8
u₁ = ½ + ½ sin(W₀ t + 0.4)          n₁  = exp-lerp(0.3, 6,   u₁)
u₂ = ½ + ½ sin(W₀ φ t + 2.1)        n₂  = exp-lerp(0.55, 1.5, u₂)
u₃ = ½ + ½ sin(W₀ φ² t + 4.0)       gap = lerp(5°, 22°, u₃)
u₄ = ½ + ½ sin(W₀ φ³ t + 1.0)       cen = ease-in-out-quad(u₄)
                                     rot = 14° · t
```

`exp-lerp` interpolates in log space, so the exponent spends equal time in
each octave instead of rushing through the small values.

**No ring in the loop.** `n₂` is capped at 1.5, well under 2 where the curve
becomes a circle, and `n₁` stops at 6. Earlier ranges passed through a plain
ring and read as a glitch.

**No shaking.** Lobe and valley positions are computed analytically and every
point is an exact evaluation of the formula, so frames don't jitter.

## One clock

Every live mark on a page reads one shared `EPOCH` at one `TEMPO` (1.8), so
the nav, footer, loaders and intro breathe in lockstep. Only the landing intro
deliberately runs faster, at speed 3. Don't pass `speed` otherwise.

Small marks (≤ 40 px) update at about 30 fps. They read the same clock, so they
stay in sync while repainting half as often. Marks pause when off screen or
when the tab is hidden. Reduced motion shows the still rest frame.
