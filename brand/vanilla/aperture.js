/* ============================================================
   aperture.js — the ImuTechz / IMURUN mark, dependency-free.

   ONE file, two places, byte for byte:
     brand/vanilla/aperture.js            the brand source (edit this one)
     frontend/design-lab/aperture.js      the copy the IMURUN UI serves
   (the UI's static root cannot reach brand/). Edit the kit, then copy it
   over; `frontend/design-lab/brand/make_icons.py --check` and the frontend
   unit checks fail while the two differ.

   A classic script (no modules, no build, no inline code needed), so it
   runs under a strict `default-src 'self'` CSP, from file://, and in Node
   (`require`: the math only, nothing touches the DOM at load). It is a
   line-for-line port of the ImuTechz website's components/Mark.jsx and
   components/Intro.jsx; the math must stay identical (see ../docs/MATH.md;
   the frontend checks run the website's own functions beside these).

   One global, `ImuAperture` (also `IMURUN.aperture` inside the IMURUN UI):

     mount(el, { size, live, speed, tile, mono, centre, title, className })
                               draw a mark into el -> { svg, destroy() }
     createMark(sameOptions)   -> a detached <svg> (live ones start breathing
                               once they are in the document)
     markup(sameOptions)       -> the same <svg> as an HTML string
     intro({ parent, size, onDone })   the landing intro -> cancel()
     bladePaths(p, R) / pupilPath(p, R) / law(t) / superR(deg, n1, n2)
     REST / TEMPO / PHI / EPOCH

   Six blades, each one sixth of Gielis's superformula with m = 6:
     r(t) = ( |cos(m t/4)|^n2 + |sin(m t/4)|^n3 )^(-1/n1)
   At rest it is the frame caught half-way between six inward chevrons and
   a six-petal flower (n1 0.38, n2 = n3 1.2, gap 11 deg, blades half way
   from valley to lobe) around a small filled pupil of the same curve. Live,
   the exponents breathe on golden-ratio frequencies while the mark turns
   and the pupil counter-turns. The twelve-o'clock blade carries the red.

   Every live mark reads ONE shared clock (EPOCH) at ONE tempo, so all of
   them on a page breathe in lockstep. Marks of 40 px or less step at
   ~30 fps; a mark off screen or in a hidden tab stops; under reduced
   motion only the resting frame is drawn.

   Colours: ink = currentColor, the red = var(--imu-accent, #d9481e)
   (the website's var(--color-accent)); tiles use var(--imu-ink).
   ES2017.
   ============================================================ */
(function (root) {
  "use strict";

  /* ------------------------------------------- the math, as in Mark.jsx */
  var PHI = (1 + Math.sqrt(5)) / 2;
  var M = 6;                         /* six-fold symmetry */
  var W0 = (Math.PI * 2) / 8;        /* base breath: one cycle per 8 s of mark time */
  var TEMPO = 1.8;                   /* mark time runs 1.8x wall time */
  /* the shared clock: one epoch for the whole page */
  var EPOCH = typeof performance !== "undefined" ? performance.now() : 0;
  /* the icon is the frame half-way between the chevrons (n1 .42, n2 1, c 0)
     and the flower (n1 .34, n2 1.45, c 1): blades caught mid-slide */
  var REST = { n1: 0.38, n2: 1.2, gap: 11, cen: 0.5, rot: 0 };
  var f = function (v) { return v.toFixed(2); };
  var lerp = function (a, b, u) { return a + (b - a) * u; };
  var elerp = function (a, b, u) { return Math.exp(lerp(Math.log(a), Math.log(b), u)); };

  function superR(deg, n1, n2) {
    var x = (M * deg * Math.PI) / 180 / 4;
    return Math.pow(Math.pow(Math.abs(Math.cos(x)), n2) + Math.pow(Math.abs(Math.sin(x)), n2), -1 / n1);
  }

  /* six blade paths; `cen` slides the blade centre from valley (0) to lobe (1) */
  function bladePaths(p, R) {
    if (R === undefined) R = 24;
    var n1 = p.n1, n2 = p.n2, gap = p.gap, cen = p.cen, rot = p.rot;
    var lobe = n2 < 2 ? 0 : 30, valley = 30 - lobe;
    var rMax = superR(lobe, n1, n2);
    var c0 = valley + (lobe - valley) * cen;
    var out = [];
    for (var k = 0; k < 6; k++) {
      var c = c0 + k * 60, a0 = c - 30 + gap / 2, a1 = c + 30 - gap / 2;
      var d = "";
      for (var s = 0; s <= 40; s++) {
        var ang = lerp(a0, a1, s / 40);
        var rr = (superR(ang, n1, n2) / rMax) * R;
        var th = ((ang - c0 + rot - 90) * Math.PI) / 180;
        d += (s ? " L" : "M") + f(32 + Math.cos(th) * rr) + " " + f(32 + Math.sin(th) * rr);
      }
      out.push(d);
    }
    return out;
  }

  /* the pupil: the same curve, a quarter the size, rounder, counter-turning at 0.6x */
  function pupilPath(p, R) {
    if (R === undefined) R = 5.9;
    var n1 = p.n1, n2 = p.n2, rot = p.rot;
    var m2 = 1 + (n2 - 1) * 0.5;
    var rMax = superR(m2 < 2 ? 0 : 30, n1, m2);
    var ccw = (-rot * 0.6 * Math.PI) / 180;
    var d = "";
    for (var i = 0; i <= 120; i++) {
      var th = (i / 120) * Math.PI * 2;
      var rr = (superR((th * 180) / Math.PI, n1, m2) / rMax) * R;
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
  /* ------------------------------------------------- end of the math */

  var ACCENT = "var(--imu-accent, #d9481e)";
  var INK_TILE = "var(--imu-ink, #1c1915)";
  var hasDOM = typeof document !== "undefined";

  function attr(text) { return String(text).replace(/[<>&"]/g, ""); }

  function reducedMotion() {
    var im = root.IMURUN;
    if (im && im.util && im.util.reducedMotion) return im.util.reducedMotion();
    return !!(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* The <svg> exactly as Mark.jsx renders it (attributes, order, colours). */
  function markup(options) {
    var o = options || {};
    var size = o.size || 24;
    var tile = o.tile || null;
    var centre = o.centre === undefined ? "pupil" : o.centre;
    var w = size <= 20 ? 7.6 : size <= 32 ? 6.4 : 5.2;    /* thicker blades when small */
    var onTile = !!tile;
    var ink = onTile ? "#ffffff" : "currentColor";
    var acc = tile === "red" ? "#ffffff" : (o.mono ? ink : ACCENT);
    var tileFill = tile === "red" ? ACCENT : INK_TILE;
    var ds = bladePaths(REST);
    var pd = pupilPath(REST);
    var label = o.title ? ' role="img" aria-label="' + attr(o.title) + '"' : ' aria-hidden="true"';
    var live = o.live ? ' data-mark-live="' + (o.live === "thinking" ? "thinking" : "1") + '" data-mark-speed="' + Number(o.speed || TEMPO) + '"' : "";
    var html = '<svg viewBox="0 0 64 64" width="' + size + '" height="' + size + '" fill="none" class="' +
      ("mark imu-mark" + (onTile ? " mark-tile" : "") + " " + (o.className || "")).trim() + '"' + label + live +
      ' data-mark-size="' + size + '" focusable="false">';
    if (onTile) html += '<rect width="64" height="64" rx="14" fill="' + tileFill + '"/>';
    html += "<g" + (onTile ? ' transform="translate(32 32) scale(0.78) translate(-32 -32)"' : "") + ' stroke-linecap="round" stroke-linejoin="round">';
    if (centre === "pupil") {
      html += '<path data-ps d="' + pd + '" fill="' + ink + '" opacity="0.16" transform="translate(1.1 1.3)"/>';
      html += '<path data-pb d="' + pd + '" fill="' + acc + '"/>';
    }
    for (var k = 0; k < ds.length; k++) {
      html += '<path data-b d="' + ds[k] + '" stroke="' + (k === 0 ? acc : ink) + '" stroke-width="' + w + '"/>';
    }
    return html + "</g></svg>";
  }

  /* ------------------------------------------------------- the breathing */

  var marks = [];   /* live controllers */

  function draw(ctl, p) {
    var ds = bladePaths(p);
    for (var k = 0; k < ctl.blades.length; k++) ctl.blades[k].setAttribute("d", ds[k]);
    if (ctl.body) {
      var d = pupilPath(p);
      ctl.body.setAttribute("d", d);
      if (ctl.shadow) ctl.shadow.setAttribute("d", d);
    }
  }

  /* A "thinking" mark (IMURUN's reply indicator) breathes only inside a
     reply that is still thinking. */
  function wanted(ctl) {
    if (!ctl.svg.isConnected) return false;
    if (ctl.thinking && !ctl.svg.closest(".msg.is-thinking")) return false;
    return !reducedMotion();
  }

  function rest(ctl) {
    if (ctl.moved) { draw(ctl, REST); ctl.moved = false; }
  }

  function loop(ctl, now) {
    ctl.raf = 0;
    if (!ctl.running) return;
    if (!wanted(ctl)) { stop(ctl); rest(ctl); return; }
    if (ctl.minDt && now - ctl.last < ctl.minDt) { ctl.raf = requestAnimationFrame(function (t) { loop(ctl, t); }); return; }
    ctl.last = now;
    draw(ctl, law(((now - EPOCH) / 1000) * ctl.speed));   /* the shared clock */
    ctl.moved = true;
    ctl.raf = requestAnimationFrame(function (t) { loop(ctl, t); });
  }

  function start(ctl) {
    if (ctl.running || document.hidden || !ctl.visible || !wanted(ctl)) return;
    ctl.running = true;
    ctl.raf = requestAnimationFrame(function (t) { loop(ctl, t); });
  }

  function stop(ctl) {
    ctl.running = false;
    if (ctl.raf) cancelAnimationFrame(ctl.raf);
    ctl.raf = 0;
  }

  function release(ctl) {
    stop(ctl);
    if (ctl.io) ctl.io.disconnect();
    var i = marks.indexOf(ctl);
    if (i !== -1) marks.splice(i, 1);
    ctl.svg._aperture = null;
  }

  function hydrate(svg) {
    if (svg._aperture) return svg._aperture;
    var size = Number(svg.getAttribute("data-mark-size")) || 24;
    var ctl = {
      svg: svg,
      blades: svg.querySelectorAll("[data-b]"),
      body: svg.querySelector("[data-pb]"),
      shadow: svg.querySelector("[data-ps]"),
      speed: Number(svg.getAttribute("data-mark-speed")) || TEMPO,
      thinking: svg.getAttribute("data-mark-live") === "thinking",
      /* small marks step at ~30 fps, same shared clock */
      minDt: size <= 40 ? 30 : 0,
      raf: 0, running: false, last: 0, visible: true, moved: false, io: null
    };
    svg._aperture = ctl;
    marks.push(ctl);
    if ("IntersectionObserver" in root) {
      ctl.visible = false;
      ctl.io = new IntersectionObserver(function (entries) {
        var e = entries[entries.length - 1];
        ctl.visible = !!(e && e.isIntersecting);
        if (ctl.visible) start(ctl); else stop(ctl);
        if (!svg.isConnected) release(ctl);
      });
      ctl.io.observe(svg);
    } else {
      start(ctl);
    }
    return ctl;
  }

  function scan(node) {
    if (!node || !node.querySelectorAll) return;
    if (node.matches && node.matches("svg[data-mark-live]")) hydrate(node);
    var list = node.querySelectorAll("svg[data-mark-live]");
    for (var i = 0; i < list.length; i++) hydrate(list[i]);
  }

  /* Re-check every live mark: a reply finished thinking, reduced motion
     changed, marks left the document. */
  function recheck() {
    marks.slice().forEach(function (ctl) {
      if (!ctl.svg.isConnected) { release(ctl); return; }
      if (wanted(ctl)) start(ctl); else { stop(ctl); rest(ctl); }
    });
  }

  function createMark(options) {
    var holder = document.createElement("div");
    holder.innerHTML = markup(options);
    var svg = holder.firstChild;
    if (svg.getAttribute("data-mark-live")) hydrate(svg);
    return svg;
  }

  /* Draw a mark into `host` -> { svg, destroy() }. */
  function mount(host, opts) {
    var svg = createMark(opts || {});
    host.appendChild(svg);
    return {
      svg: svg,
      destroy: function () {
        if (svg._aperture) release(svg._aperture);
        if (svg.parentNode) svg.parentNode.removeChild(svg);
      }
    };
  }

  /* The landing intro (Intro.jsx): a cream curtain, the mark whooshes in
     large with two ripples, then the curtain lifts upward and is removed.
     Timing lives in aperture.css (see ../docs/LANDING-ANIMATION.md).
       parent  where to attach (default document.body)
       size    mark size in px (default 250; CSS clamps it to 160..250)
       onDone  called after the curtain is gone
     Adds `has-intro` to <html> while it runs. Returns cancel(). */
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

  /* Any svg[data-mark-live] that appears in the page starts breathing. */
  function watch() {
    scan(document);
    new MutationObserver(function (records) {
      var check = false;
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === "attributes") { check = true; continue; }
        for (var j = 0; j < r.addedNodes.length; j++) {
          if (r.addedNodes[j].nodeType === 1) scan(r.addedNodes[j]);
        }
        if (r.removedNodes.length) check = true;
      }
      if (check) recheck();
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "data-reduced-motion"] });
    document.addEventListener("visibilitychange", function () {
      marks.forEach(function (ctl) { if (document.hidden) stop(ctl); else start(ctl); });
    });
    if (root.matchMedia) {
      var mq = root.matchMedia("(prefers-reduced-motion: reduce)");
      if (mq.addEventListener) mq.addEventListener("change", recheck);
    }
  }

  var api = {
    REST: REST, TEMPO: TEMPO, PHI: PHI, EPOCH: EPOCH,
    superR: superR, bladePaths: bladePaths, pupilPath: pupilPath, law: law,
    markup: markup, createMark: createMark, hydrate: hydrate, mount: mount, intro: intro,
    liveCount: function () { return marks.filter(function (c) { return c.running; }).length; }
  };
  root.ImuAperture = api;
  if (root.IMURUN) root.IMURUN.aperture = api;

  if (hasDOM) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watch);
    else watch();
  }
})(typeof window !== "undefined" ? window : globalThis);
