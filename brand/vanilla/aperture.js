/* ============================================================
   aperture.js — the ImuTechz / IMURUN mark, dependency-free.

   A classic script (no modules, no build, no inline code needed), so it
   runs under the design-lab CSP (`default-src 'self'`) and from file://.
   It exposes one global, `ImuAperture`:

     ImuAperture.mount(el, { size: 26, live: true })   draw a mark into el
     ImuAperture.intro({ onDone })                     play the landing intro
     ImuAperture.bladePaths(p) / pupilPath(p) / law(t) the math
     ImuAperture.REST / TEMPO                          the icon frame, the tempo

   This is a line-for-line port of components/Mark.jsx and
   components/Intro.jsx from the ImuTechz website. The math must stay
   identical in both places — see ../docs/MATH.md.
   ============================================================ */
(function (root) {
  "use strict";

  var PHI = (1 + Math.sqrt(5)) / 2;
  var M = 6;                         /* six-fold symmetry */
  var W0 = (Math.PI * 2) / 8;        /* base breath: one cycle per 8 s of mark time */
  var TEMPO = 1.8;                   /* mark time runs 1.8x wall time */
  var EPOCH = (root.performance && performance.now()) || 0;   /* one clock per page */
  var REST = { n1: 0.38, n2: 1.2, gap: 11, cen: 0.5, rot: 0 }; /* the icon frame */

  function f(v) { return v.toFixed(2); }
  function lerp(a, b, u) { return a + (b - a) * u; }
  function elerp(a, b, u) { return Math.exp(lerp(Math.log(a), Math.log(b), u)); }

  /* Gielis's superformula, a = b = 1, n2 = n3 */
  function superR(deg, n1, n2) {
    var x = (M * deg * Math.PI) / 180 / 4;
    return Math.pow(Math.pow(Math.abs(Math.cos(x)), n2) + Math.pow(Math.abs(Math.sin(x)), n2), -1 / n1);
  }

  /* six blade paths in a 64x64 box; `cen` slides each blade from valley (0) to lobe (1) */
  function bladePaths(p, R) {
    R = R || 24;
    var lobe = p.n2 < 2 ? 0 : 30, valley = 30 - lobe;
    var rMax = superR(lobe, p.n1, p.n2);
    var c0 = valley + (lobe - valley) * p.cen;
    var out = [];
    for (var k = 0; k < 6; k++) {
      var c = c0 + k * 60, a0 = c - 30 + p.gap / 2, a1 = c + 30 - p.gap / 2;
      var d = "";
      for (var s = 0; s <= 40; s++) {
        var ang = lerp(a0, a1, s / 40);
        var rr = (superR(ang, p.n1, p.n2) / rMax) * R;
        var th = ((ang - c0 + p.rot - 90) * Math.PI) / 180;
        d += (s ? " L" : "M") + f(32 + Math.cos(th) * rr) + " " + f(32 + Math.sin(th) * rr);
      }
      out.push(d);
    }
    return out;
  }

  /* the pupil: the same curve, a quarter the size, rounder, counter-turning at 0.6x */
  function pupilPath(p, R) {
    R = R || 5.9;
    var m2 = 1 + (p.n2 - 1) * 0.5;
    var rMax = superR(m2 < 2 ? 0 : 30, p.n1, m2);
    var ccw = (-p.rot * 0.6 * Math.PI) / 180;
    var d = "";
    for (var i = 0; i <= 120; i++) {
      var th = (i / 120) * Math.PI * 2;
      var rr = (superR((th * 180) / Math.PI, p.n1, m2) / rMax) * R;
      d += (i ? " L" : "M") + f(32 + Math.cos(th + ccw) * rr) + " " + f(32 + Math.sin(th + ccw) * rr);
    }
    return d + " Z";
  }

  /* the breathing law: four breaths on frequencies 1 : PHI : PHI^2 : PHI^3 */
  function law(t) {
    var u1 = 0.5 + 0.5 * Math.sin(W0 * t + 0.4);
    var u2 = 0.5 + 0.5 * Math.sin(W0 * PHI * t + 2.1);
    var u3 = 0.5 + 0.5 * Math.sin(W0 * PHI * PHI * t + 4.0);
    var u4 = 0.5 + 0.5 * Math.sin(W0 * PHI * PHI * PHI * t + 1.0);
    var cen = u4 < 0.5 ? 2 * u4 * u4 : 1 - 2 * (1 - u4) * (1 - u4);
    /* never a plain ring: n2 stays under 2 (the circle), n1 stops at 6 */
    return { n1: elerp(0.3, 6, u1), n2: elerp(0.55, 1.5, u2), gap: lerp(5, 22, u3), cen: cen, rot: t * 14 };
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    return n;
  }
  function reducedMotion() {
    return !!(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* Draw a mark into `host`. Options:
       size   px (default 24)        live   breathe (default false)
       mono   all one colour          speed  tempo override (default TEMPO; keep it)
       title  accessible name (otherwise aria-hidden)
     Returns { svg, destroy() }. */
  function mount(host, opts) {
    opts = opts || {};
    var size = opts.size || 24;
    var speed = opts.speed || TEMPO;
    var ink = "currentColor";
    var acc = opts.mono ? ink : "var(--imu-accent, #d9481e)";
    var w = size <= 20 ? 7.6 : size <= 32 ? 6.4 : 5.2;    /* thicker blades when small */

    var svg = el("svg", { viewBox: "0 0 64 64", width: size, height: size, fill: "none", "class": "imu-mark" + (opts.className ? " " + opts.className : "") });
    if (opts.title) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", opts.title); }
    else svg.setAttribute("aria-hidden", "true");
    var g = el("g", { "stroke-linecap": "round", "stroke-linejoin": "round" });
    var pd = pupilPath(REST);
    var shadow = el("path", { d: pd, fill: ink, opacity: "0.16", transform: "translate(1.1 1.3)" });
    var body = el("path", { d: pd, fill: acc });
    g.appendChild(shadow); g.appendChild(body);
    var ds = bladePaths(REST), blades = [];
    for (var k = 0; k < 6; k++) {
      var b = el("path", { d: ds[k], stroke: k === 0 ? acc : ink, "stroke-width": w });
      blades.push(b); g.appendChild(b);
    }
    svg.appendChild(g);
    host.appendChild(svg);

    var raf = 0, running = false, last = 0, io = null;
    var minDt = size <= 40 ? 30 : 0;   /* small marks step at ~30 fps */
    function frame(now) {
      raf = 0;
      if (!running) return;
      if (minDt && now - last < minDt) { raf = requestAnimationFrame(frame); return; }
      last = now;
      var p = law(((now - EPOCH) / 1000) * speed);
      var d = bladePaths(p);
      for (var i = 0; i < 6; i++) blades[i].setAttribute("d", d[i]);
      var pp = pupilPath(p);
      body.setAttribute("d", pp); shadow.setAttribute("d", pp);
      raf = requestAnimationFrame(frame);
    }
    function start() { if (!running) { running = true; raf = requestAnimationFrame(frame); } }
    function stop() { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; }
    function onVis() { if (document.hidden) stop(); else if (!io || visible) start(); }
    var visible = true;

    if (opts.live && !reducedMotion()) {
      if ("IntersectionObserver" in root) {
        io = new IntersectionObserver(function (es) {
          visible = !!(es[0] && es[0].isIntersecting);
          if (visible) start(); else stop();
        });
        io.observe(svg);
      } else start();
      document.addEventListener("visibilitychange", onVis);
    }

    return {
      svg: svg,
      destroy: function () {
        stop();
        if (io) io.disconnect();
        document.removeEventListener("visibilitychange", onVis);
        if (svg.parentNode) svg.parentNode.removeChild(svg);
      }
    };
  }

  /* The landing intro: a cream curtain, the mark whooshes in large with two
     ripples, then the curtain lifts upward and is removed. Timing lives in
     aperture.css (see ../docs/LANDING-ANIMATION.md). Options:
       parent  where to attach (default document.body)
       size    mark size in px (default 250; CSS clamps it to 160..250)
       onDone  called after the curtain is gone
     Adds `has-intro` to <html> while it runs, so page content can stagger
     in behind it. Returns a cancel() function. */
  function intro(opts) {
    opts = opts || {};
    var reduce = reducedMotion();
    var docEl = document.documentElement;
    var wrap = document.createElement("div");
    wrap.className = "imu-intro";
    wrap.setAttribute("aria-hidden", "true");
    var inner = document.createElement("div");
    inner.className = "imu-intro-in";
    var r1 = document.createElement("span"); r1.className = "imu-intro-ring";
    var r2 = document.createElement("span"); r2.className = "imu-intro-ring imu-intro-ring-2";
    inner.appendChild(r1); inner.appendChild(r2);
    wrap.appendChild(inner);
    (opts.parent || document.body).appendChild(wrap);
    /* the intro mark breathes at speed 3 (the website's value), faster than
       the 1.8 tempo every other mark shares: the "signature" quick breath */
    var mark = mount(inner, { size: opts.size || 250, live: true, speed: 3, className: "imu-intro-mark" });

    docEl.classList.add("has-intro");
    /* start the clock on the next frame, so the initial state paints first */
    var go = requestAnimationFrame(function () { wrap.classList.add("imu-intro-go"); });
    var done = false;
    var t = setTimeout(finish, reduce ? 350 : 2050);
    function finish() {
      if (done) return;
      done = true;
      cancelAnimationFrame(go);
      clearTimeout(t);
      mark.destroy();
      if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      docEl.classList.remove("has-intro");
      if (opts.onDone) opts.onDone();
    }
    return finish;
  }

  root.ImuAperture = {
    REST: REST, TEMPO: TEMPO, PHI: PHI,
    superR: superR, bladePaths: bladePaths, pupilPath: pupilPath, law: law,
    mount: mount, intro: intro
  };
})(typeof window !== "undefined" ? window : globalThis);
