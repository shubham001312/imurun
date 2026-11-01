#!/usr/bin/env node
/* render-svg.js — regenerate the static SVGs in ../svg from the math in
   ../vanilla/aperture.js, so the files can never drift from the live mark.

     node brand/scripts/render-svg.js

   No dependencies. Every file is the REST frame (the icon): the frame
   caught half-way between six inward chevrons and a six-petal flower. */
"use strict";
const fs = require("fs");
const path = require("path");

require(path.join(__dirname, "..", "vanilla", "aperture.js"));
const A = globalThis.ImuAperture;

const INK = "#1c1915";
const CANVAS = "#f3ede2";
const ACCENT = "#d9481e";

/* blade stroke by rendered size, exactly as the live mark picks it */
const STROKE = { small: 7.6, medium: 6.4, large: 5.2 };

function svg({ ink, accent, shadow, stroke }) {
  const pd = A.pupilPath(A.REST);
  const blades = A.bladePaths(A.REST)
    .map((d, k) => `    <path d="${d}" stroke="${k === 0 ? accent : ink}" stroke-width="${stroke}"/>`)
    .join("\n");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" fill="none">
  <g stroke-linecap="round" stroke-linejoin="round">
    <path d="${pd}" fill="${shadow}" opacity="0.16" transform="translate(1.1 1.3)"/>
    <path d="${pd}" fill="${accent}"/>
${blades}
  </g>
</svg>
`;
}

const out = path.join(__dirname, "..", "svg");
fs.mkdirSync(out, { recursive: true });
const files = {
  /* the logo for display sizes (> 32 px) */
  "aperture.svg": svg({ ink: INK, accent: ACCENT, shadow: INK, stroke: STROKE.large }),
  /* the logo for 21–32 px */
  "aperture-medium.svg": svg({ ink: INK, accent: ACCENT, shadow: INK, stroke: STROKE.medium }),
  /* the logo for ≤ 20 px: identical to the website's tab icon (app/icon.svg) */
  "aperture-small.svg": svg({ ink: INK, accent: ACCENT, shadow: INK, stroke: STROKE.small }),
  /* one colour, for engraving, stamps, single-ink print */
  "aperture-mono.svg": svg({ ink: INK, accent: INK, shadow: INK, stroke: STROKE.large }),
  /* for dark grounds: cream blades, the red stays red */
  "aperture-reversed.svg": svg({ ink: CANVAS, accent: ACCENT, shadow: "#000000", stroke: STROKE.large }),
};
for (const [name, body] of Object.entries(files)) {
  fs.writeFileSync(path.join(out, name), body);
  console.log("wrote svg/" + name);
}
