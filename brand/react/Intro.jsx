"use client";

import { useEffect, useState } from "react";
import Mark from "./Mark";

/* ============================================================
   Intro — the home page's opening, played on every load of "/".

   A cream curtain. The mark whooshes in large — from a blurred speck
   to a slight overshoot and back — breathing fast, with one ripple
   expanding behind it. Then the whole curtain drops upward and the
   site is revealed, its hero copy rising in behind it (see
   .has-intro in home.css). All CSS-timed; JS only removes the node
   once the curtain has gone. About 2 s end to end; reduced motion
   gets a short fade.
   ============================================================ */

/* true once the curtain has played in this page load: a client-side click
   back to Home should be instant, only a real load (or reload) replays it */
let played = false;

export default function Intro() {
  const [gone, setGone] = useState(() => played);
  const [go, setGo] = useState(false);

  useEffect(() => {
    if (gone) return;            /* in-app navigation: no curtain */
    played = true;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    /* one clock for everything: the whoosh, the curtain lift and the hero
       rise all start at hydration, so a slow network can only lengthen
       the plain cream screen before the sequence, never desync it */
    setGo(true);
    document.documentElement.classList.add("has-intro");
    const t = setTimeout(() => {
      setGone(true);
      document.documentElement.classList.remove("has-intro");
    }, reduce ? 350 : 2050);
    return () => {
      clearTimeout(t);
      document.documentElement.classList.remove("has-intro");
    };
  }, []);

  if (gone) return null;

  return (
    <div className={"intro" + (go ? " intro-go" : "")} aria-hidden="true">
      <div className="intro-in">
        <span className="intro-ring"></span>
        <span className="intro-ring intro-ring-2"></span>
        <Mark size={250} live speed={3} className="intro-mark" />
      </div>
    </div>
  );
}
