# The landing intro

The opening of the ImuTechz home page. A cream curtain covers the page. The
Aperture whooshes in large and breathes. Then the whole curtain lifts upward
and the page is revealed underneath, its content rising in behind it.

There is no wordmark or text in the intro. It is the mark alone.

## Timeline

| time (s) | what happens |
|---|---|
| 0.00 | Curtain is up: plain cream (`#f3ede2`), full viewport. |
| 0.10 – 1.15 | **Whoosh in.** The mark scales from 0.14 to a 1.1 overshoot at 78%, then settles at 1. It starts blurred 16 px and is sharp by 55%. Easing `cubic-bezier(0.22, 1.2, 0.3, 1)`. |
| 0.45 | First ripple: an ink ring at 35% opacity expands from 0.45× to 2.4× the mark and fades. |
| 0.62 | Second ripple, in the brand red. |
| 1.30 – 2.02 | **Drop-up reveal.** The curtain translates to −104% over 0.72 s with `cubic-bezier(0.8, 0, 0.2, 1)`. A soft ink shadow on its bottom edge makes it read as a lifting sheet. |
| 1.55 + n·0.09 | Page content rises 18 px and fades in, staggered. |
| 2.05 | The curtain is removed from the DOM. The nav bar drops into place. |

The mark breathes throughout at speed 3, faster than the 1.8 tempo every other
mark uses. That quicker breath is the intro's signature.

Sizes: the mark is `clamp(160px, 24vw, 250px)`, centred. Ripples share that
size.

## Rules

- **Plays on a real load only.** The website plays it on first visit and on
  reload, never on in-app navigation back to Home. Replaying a two-second
  curtain on every click made navigation feel slow.
- **The clock starts after the page is interactive.** The curtain stays plain
  cream until JavaScript is ready, then all timers start together. A slow
  network only lengthens the blank cream screen. It never desyncs the whoosh,
  the lift and the content rise.
- **Nothing peeks through.** On the website the fixed nav bar is hidden while
  the curtain exists, using `html:has(.intro)`, and drops in after it.
- **Reduced motion:** a 0.3 s fade of the curtain with a still mark, no
  ripples, removed at 0.35 s.
- `html.has-intro` is set while it runs, so page content can hook its stagger
  to it.

## Using it

Vanilla, for the CLI UI or any page:

```html
<link rel="stylesheet" href="brand/vanilla/aperture.css">
<script src="brand/vanilla/aperture.js"></script>
<script src="your-page.js"></script>
```

```js
// your-page.js
ImuAperture.intro({ onDone: () => console.log("revealed") });
```

Give elements the class `imu-rise` to stagger them in behind the curtain. Set
`--n: 0, 1, 2…` from a class in your stylesheet for the order.

React, on the website: `react/Intro.jsx` with `react/intro.css`.
