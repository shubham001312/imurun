"use client";

import { useEffect, useRef } from "react";

/* ============================================================
   Mark — the ImuTechz symbol: the Aperture, in motion.

   Six blades, each one sixth of Gielis's superformula with m = 6:
     r(θ) = ( |cos(mθ/4)|^n₂ + |sin(mθ/4)|^n₃ )^(−1/n₁)
   At rest it is the frame caught half-way between six inward chevrons
   and a six-petal flower (n₁ 0.38, n₂ = n₃ 1.2, gap 11°, blades half
   way from valley to lobe) around a small filled centre. Live,
   the exponents breathe on golden-ratio frequencies — the blades
   become a star, a flower, a hexagon, an asterisk — while the mark
   turns, and a small filled "pupil" of the same curve counter-turns
   in the middle. The twelve-o'clock blade carries the brand red.

     <Mark size={24} tile="red" />            the logo (ink tile keeps the red blade)
     <Mark size={48} live />                  breathing, bare, on the page
     <Mark size={18} tile="red" live />       the loader — same breath, same clock
     <Mark mono />                            all ink

   Every live mark reads ONE shared clock at ONE tempo (TEMPO), so all
   of them on a page — nav, footer, loaders, the intro — breathe in
   lockstep, and the pupil is driven by the very same parameters as
   the blades. Pass `speed` only if you deliberately want a mark out
   of step. Server-renders the resting shape; the breathing starts on
   the client only when `live` is set and reduced motion is off.
   ============================================================ */

const PHI = (1 + Math.sqrt(5)) / 2;
const M = 6;
const W0 = (Math.PI * 2) / 8;
const TEMPO = 1.8;
/* the shared clock: one epoch for the whole page, kept across navigations */
const EPOCH = typeof performance !== "undefined" ? performance.now() : 0;
/* the icon is the frame half-way between the chevrons (n₁ .42, n₂ 1, c 0)
   and the flower (n₁ .34, n₂ 1.45, c 1): blades caught mid-slide */
const REST = { n1: 0.38, n2: 1.2, gap: 11, cen: 0.5, rot: 0 };
const f = (v) => v.toFixed(2);
const lerp = (a, b, u) => a + (b - a) * u;
const elerp = (a, b, u) => Math.exp(lerp(Math.log(a), Math.log(b), u));

function superR(deg, n1, n2) {
  const x = (M * deg * Math.PI) / 180 / 4;
  return Math.pow(Math.pow(Math.abs(Math.cos(x)), n2) + Math.pow(Math.abs(Math.sin(x)), n2), -1 / n1);
}

/* six blade paths; `cen` slides the blade centre from valley (0) to lobe (1) */
export function bladePaths({ n1, n2, gap, cen, rot }, R = 24) {
  const lobe = n2 < 2 ? 0 : 30, valley = 30 - lobe;
  const rMax = superR(lobe, n1, n2);
  const c0 = valley + (lobe - valley) * cen;
  const out = [];
  for (let k = 0; k < 6; k++) {
    const c = c0 + k * 60, a0 = c - 30 + gap / 2, a1 = c + 30 - gap / 2;
    let d = "";
    for (let s = 0; s <= 40; s++) {
      const ang = lerp(a0, a1, s / 40);
      const rr = (superR(ang, n1, n2) / rMax) * R;
      const th = ((ang - c0 + rot - 90) * Math.PI) / 180;
      d += (s ? " L" : "M") + f(32 + Math.cos(th) * rr) + " " + f(32 + Math.sin(th) * rr);
    }
    out.push(d);
  }
  return out;
}

/* the pupil: the same curve, a third the size, rounder, counter-turning */
export function pupilPath({ n1, n2, rot }, R = 5.9) {
  const m2 = 1 + (n2 - 1) * 0.5;
  const rMax = superR(m2 < 2 ? 0 : 30, n1, m2);
  const ccw = (-rot * 0.6 * Math.PI) / 180;
  let d = "";
  for (let i = 0; i <= 120; i++) {
    const th = (i / 120) * Math.PI * 2;
    const rr = (superR((th * 180) / Math.PI, n1, m2) / rMax) * R;
    d += (i ? " L" : "M") + f(32 + Math.cos(th + ccw) * rr) + " " + f(32 + Math.sin(th + ccw) * rr);
  }
  return d + " Z";
}

/* the breathing law: four breaths on frequencies 1 : Φ : Φ² : Φ³ */
export function law(t) {
  const u1 = 0.5 + 0.5 * Math.sin(W0 * t + 0.4);
  const u2 = 0.5 + 0.5 * Math.sin(W0 * PHI * t + 2.1);
  const u3 = 0.5 + 0.5 * Math.sin(W0 * PHI * PHI * t + 4.0);
  const u4 = 0.5 + 0.5 * Math.sin(W0 * PHI * PHI * PHI * t + 1.0);
  const cen = u4 < 0.5 ? 2 * u4 * u4 : 1 - 2 * (1 - u4) * (1 - u4);
  /* no ring in the loop: n₂ stays well under 2 (the circle), n₁ stops at 6 */
  return { n1: elerp(0.3, 6, u1), n2: elerp(0.55, 1.5, u2), gap: lerp(5, 22, u3), cen, rot: t * 14 };
}

export default function Mark({
  size = 24, tile = null, live = false, speed = TEMPO, centre = "pupil", mono = false, className = "", title,
}) {
  const ref = useRef(null);

  useEffect(() => {
    if (!live) return;
    const svg = ref.current;
    if (!svg || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const blades = svg.querySelectorAll("[data-b]");
    const body = svg.querySelector("[data-pb]");
    const shadow = svg.querySelector("[data-ps]");
    let raf = 0, running = false, last = 0;
    /* small marks (nav, footer, loaders) step at ~30 fps: still smooth at
       that size, half the repaints inside the blurred glass bar. Same
       shared clock, so they stay in sync with the large ones. */
    const minDt = size <= 40 ? 30 : 0;
    const loop = (now) => {
      raf = 0;
      if (!running) return;
      if (minDt && now - last < minDt) { raf = requestAnimationFrame(loop); return; }
      last = now;
      const p = law(((now - EPOCH) / 1000) * speed);     /* the shared clock */
      const ds = bladePaths(p);
      blades.forEach((b, k) => b.setAttribute("d", ds[k]));
      if (body) { const d = pupilPath(p); body.setAttribute("d", d); if (shadow) shadow.setAttribute("d", d); }
      raf = requestAnimationFrame(loop);
    };
    const start = () => { if (!running) { running = true; raf = requestAnimationFrame(loop); } };
    const stop = () => { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; };
    const io = new IntersectionObserver((es) => (es[0]?.isIntersecting ? start() : stop()));
    const onVis = () => (document.hidden ? stop() : start());
    io.observe(svg);
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); io.disconnect(); document.removeEventListener("visibilitychange", onVis); };
  }, [live, speed, size]);

  const w = size <= 20 ? 7.6 : size <= 32 ? 6.4 : 5.2;
  const onTile = !!tile;
  const ink = onTile ? "#ffffff" : "currentColor";
  const acc = tile === "red" ? "#ffffff" : mono ? ink : "var(--color-accent)";
  const tileFill = tile === "red" ? "var(--color-accent)" : "var(--color-fg)";
  const ds = bladePaths(REST);
  const pd = pupilPath(REST);

  return (
    <svg
      ref={ref}
      viewBox="0 0 64 64"
      width={size}
      height={size}
      fill="none"
      className={`mark${onTile ? " mark-tile" : ""} ${className}`.trim()}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : "true"}
    >
      {onTile && <rect width="64" height="64" rx="14" fill={tileFill} />}
      <g transform={onTile ? "translate(32 32) scale(0.78) translate(-32 -32)" : undefined} strokeLinecap="round" strokeLinejoin="round">
        {centre === "pupil" && (
          <>
            <path data-ps d={pd} fill={ink} opacity="0.16" transform="translate(1.1 1.3)" />
            <path data-pb d={pd} fill={acc} />
          </>
        )}
        {ds.map((d, k) => (
          <path key={k} data-b d={d} stroke={k === 0 ? acc : ink} strokeWidth={w} />
        ))}
      </g>
    </svg>
  );
}
