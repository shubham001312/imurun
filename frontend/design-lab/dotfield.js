/* DotField: the ImuTechz website's living background, ported 1:1 from
 * WebSite/frontend/components/DotField.jsx (constants, cost model and
 * drawing unchanged; React effect -> mount/unmount).
 *
 *   A fixed canvas of ink dots on a 24 px grid behind the app. Dots within
 *   reach of the pointer swell, darken, pick up the accent at the centre
 *   and are nudged away from the cursor, so the field bends around the
 *   mouse. Pointer-events: none. The full grid is painted once (and on
 *   resize); each frame repaints only the square around the pointer, and
 *   the loop stops when the bulge has settled. Reduced motion: a still grid.
 *
 * Shown with the ImuTechz theme (light) unless Settings > Customisation
 * "Ambient" is "none". Not on ImuTechz Console: the website's dark palette
 * sets --ink-rgb to 0,0,0, so its dots are black on black there too.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  var GAP = 24;
  var REACH = 200;
  var PUSH = 10;
  var BASE_R = 1.35;
  var BASE_A = 0.34;
  var PAD = REACH + PUSH + 6;   /* half-size of the square the bulge can touch */

  var current = null;

  function mount() {
    var cv = document.createElement('canvas');
    cv.className = 'dotfield';
    cv.setAttribute('aria-hidden', 'true');
    document.body.insertBefore(cv, document.body.firstChild);
    var ctx = cv.getContext('2d');
    if (!ctx) { cv.remove(); return null; }
    var reduce = IM.util && IM.util.reducedMotion ? IM.util.reducedMotion()
      : window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var css = getComputedStyle(document.documentElement);
    var INK = (css.getPropertyValue('--ink-rgb') || '28,25,20').trim();
    var hex = (getComputedStyle(document.body).getPropertyValue('--accent') || '#d9481e').trim();
    var m = hex.match(/^#([0-9a-f]{6})$/i);
    var n = m ? parseInt(m[1], 16) : 0xd9481e;
    var ACC = ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255);

    /* fill styles quantised to 1/100 alpha, built once */
    var inkStyle = [], accStyle = [];
    for (var i = 0; i <= 100; i++) { inkStyle.push('rgba(' + INK + ',' + (i / 100) + ')'); accStyle.push('rgba(' + ACC + ',' + (i / 100) + ')'); }
    var q = function (a) { return Math.max(0, Math.min(100, Math.round(a * 100))); };

    var W = 0, H = 0, dpr = 1, gap = GAP, raf = 0, running = false;
    var tx = -1e4, ty = -1e4, mx = -1e4, my = -1e4;   /* pointer target / eased position */
    var k = 0, kt = 0;                                /* bulge strength, eased toward kt */
    var prev = null;                                  /* last frame's dirty square */

    function dot(x, y) {
      var r = BASE_R, a = BASE_A, ox = 0, oy = 0, accent = false;
      if (k > 0.001) {
        var dx = x - mx, dy = y - my;
        var dist = Math.hypot(dx, dy);
        if (dist < REACH) {
          var f = 1 - dist / REACH;
          var e = f * f * (3 - 2 * f) * k;
          r += e * 2.3; a += e * 0.5;
          var inv = 1 / (dist || 1);
          ox = dx * inv * e * PUSH; oy = dy * inv * e * PUSH;
          accent = f > 0.55 && k > 0.5;
        }
      }
      ctx.fillStyle = accent ? accStyle[q(a)] : inkStyle[q(a)];
      ctx.beginPath(); ctx.arc(x + ox, y + oy, r, 0, Math.PI * 2); ctx.fill();
    }

    /* repaint every dot whose base position touches [x0,x1]x[y0,y1], clipped to it */
    function paint(x0, y0, x1, y1) {
      x0 = Math.max(0, Math.floor(x0)); y0 = Math.max(0, Math.floor(y0));
      x1 = Math.min(W, Math.ceil(x1)); y1 = Math.min(H, Math.ceil(y1));
      if (x1 <= x0 || y1 <= y0) return;
      ctx.save();
      ctx.beginPath(); ctx.rect(x0, y0, x1 - x0, y1 - y0); ctx.clip();
      ctx.clearRect(x0, y0, x1 - x0, y1 - y0);
      var h = gap / 2, m2 = BASE_R + 1;
      var i0 = Math.max(0, Math.floor((x0 - h - m2) / gap)), i1 = Math.ceil((x1 - h + m2) / gap);
      var j0 = Math.max(0, Math.floor((y0 - h - m2) / gap)), j1 = Math.ceil((y1 - h + m2) / gap);
      for (var j = j0; j <= j1; j++) for (var ii = i0; ii <= i1; ii++) dot(h + ii * gap, h + j * gap);
      ctx.restore();
    }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = window.innerWidth; H = window.innerHeight;
      gap = W < 700 ? 30 : GAP;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + 'px'; cv.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      prev = null;
      paint(0, 0, W, H);
    }

    function frame() {
      raf = 0;
      if (!running) return;
      /* snap straight to a pointer that just appeared, ease otherwise */
      if (k < 0.01 && kt > 0) { mx = tx; my = ty; }
      mx += (tx - mx) * 0.16; my += (ty - my) * 0.16;
      k += (kt - k) * 0.12;
      var cur = k > 0.001 ? [mx - PAD, my - PAD, mx + PAD, my + PAD] : null;
      var box = cur && prev
        ? [Math.min(cur[0], prev[0]), Math.min(cur[1], prev[1]), Math.max(cur[2], prev[2]), Math.max(cur[3], prev[3])]
        : cur || prev;
      if (box) paint(box[0], box[1], box[2], box[3]);
      prev = cur;
      var settled = Math.abs(tx - mx) < 0.3 && Math.abs(ty - my) < 0.3 && Math.abs(kt - k) < 0.004;
      if (settled) {
        if (kt === 0 && k !== 0) { k = 0; if (prev) { var p = prev; prev = null; paint(p[0], p[1], p[2], p[3]); } }
        running = false;
        return;
      }
      raf = requestAnimationFrame(frame);
    }
    function start() { if (running || reduce || document.hidden) return; running = true; raf = requestAnimationFrame(frame); }
    function stop() { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

    var onMove = function (e) { tx = e.clientX; ty = e.clientY; kt = 1; start(); };
    var onOut = function (e) { if (!e.relatedTarget) { kt = 0; start(); } };
    var onVis = function () { if (document.hidden) stop(); else start(); };
    var rz = 0;
    var onResize = function () { cancelAnimationFrame(rz); rz = requestAnimationFrame(resize); };

    resize();
    window.addEventListener('resize', onResize);
    if (!reduce) {
      window.addEventListener('pointermove', onMove, { passive: true });
      document.addEventListener('pointerout', onOut);
      document.addEventListener('visibilitychange', onVis);
    }
    return function unmount() {
      stop();
      cancelAnimationFrame(rz);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerout', onOut);
      document.removeEventListener('visibilitychange', onVis);
      cv.remove();
    };
  }

  function wanted() {
    var b = document.body;
    return b.dataset.theme === 'imutechz' && b.dataset.ambient !== 'none';
  }

  /* (Re)build on theme / preference changes: colours and reduced motion are
   * read once per mount, as in the component. */
  function sync() {
    var key = wanted() ? [document.body.dataset.theme, document.body.dataset.reducedMotion, document.body.style.getPropertyValue('--accent')].join('|') : '';
    if (current && current.key === key) return;
    if (current) { current.unmount(); current = null; }
    if (!key) return;
    /* wait one frame so a theme sheet that is swapping in has applied */
    requestAnimationFrame(function () {
      if (current || !wanted()) return;
      var unmount = mount();
      if (unmount) current = { key: key, unmount: unmount };
    });
  }

  function init() {
    sync();
    if (IM.bus) IM.bus.on('theme', function () { setTimeout(sync, 60); });
  }

  IM.dotfield = { sync: sync, active: function () { return Boolean(current); } };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
