/* Brand: the ImuTechz Aperture everywhere, the wordmark, the landing intro.
 *
 *   IMURUN.brand.logo({size, live, tile, mono, label, className}) -> <svg> markup
 *       (aperture.js; a live mark starts breathing once it is in the page)
 *   IMURUN.brand.wordmark(className)   "ImuTechz": red I and T, steel "echz"
 *   IMURUN.brand.playIntro()           replay the landing intro
 *   IMURUN.brand.introOnLaunch(bool?)  get / set "Play the intro on launch"
 *   IMURUN.brand.config                {mark, file, name}
 *
 * The intro is a 1:1 port of the website's components/Intro.jsx with the
 * .intro rules of app/styles/home.css (styles/brand.css): a cream curtain,
 * the mark at 250 px breathing at speed 3 whooshes from a blurred speck to
 * a slight overshoot and back, two ripples expand behind it, then the
 * curtain lifts upward and Home rises in behind it. 2050 ms end to end;
 * 350 ms fade under reduced motion. Like the website it plays on every real
 * load or reload (index.html carries the curtain so the first paint is
 * already cream), never on in-app navigation; Settings > Customisation
 * "Play the intro on launch" turns it off. A swell sounds as the curtain
 * lifts (sound.js).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  /* aperture.js (the brand kit, loaded before IMURUN exists) exposes window.ImuAperture */
  IM.aperture = IM.aperture || window.ImuAperture;

  /* ONE switch for the app mark (docs/frontend/README.md, "Brand").
   *   mark: 'aperture'  the live ImuTechz Aperture (aperture.js), default
   *   mark: 'file'      <img src="<file>"> everywhere instead */
  var BRAND = {
    mark: 'aperture',
    file: 'logo.svg',
    name: 'IMURUN'
  };

  var INTRO_KEY = 'imurun.intro.onLaunch.v1';
  var INTRO_MS = 2050;          /* Intro.jsx: setTimeout(..., reduce ? 350 : 2050) */
  var INTRO_REDUCED_MS = 350;
  var LIFT_AT_MS = 1300;        /* home.css: intro-up ... 1.3s */

  function attr(text) { return String(text).replace(/[<>&"]/g, ''); }

  function logo(options) {
    var opts = options || {};
    var size = opts.size || 28;
    if (BRAND.mark === 'file' || !IM.aperture) {
      var alt = opts.label === false || !opts.label ? '' : attr(opts.label);
      return '<img class="imu-mark imu-mark--file' + (opts.className ? ' ' + opts.className : '') + '" src="' + BRAND.file +
        '" width="' + size + '" height="' + size + '" alt="' + alt + '" draggable="false"' + (alt ? '' : ' aria-hidden="true"') + '>';
    }
    return IM.aperture.markup({
      size: size, live: opts.live, speed: opts.speed, tile: opts.tile, mono: opts.mono,
      centre: opts.centre, className: opts.className, title: opts.label === false ? '' : opts.label
    });
  }

  /* The website lockup (components/Shell.jsx Wordmark): Inter 700, -0.03em,
   * red I and T (.wmr), ink "mu", steel "echz" (.wms). */
  function wordmark(className) {
    return '<span class="imu-wordmark' + (className ? ' ' + className : '') + '" aria-label="ImuTechz">' +
      '<span class="wm-red" aria-hidden="true">I</span><span class="wm-ink" aria-hidden="true">mu</span>' +
      '<span class="wm-red" aria-hidden="true">T</span><span class="wm-steel" aria-hidden="true">echz</span></span>';
  }

  /* Replace every [data-logo] placeholder with a live mark (the website's
   * nav, footer and loaders are all `live`). data-logo="static" stays still. */
  function mountLogos(root) {
    (root || document).querySelectorAll('[data-logo]').forEach(function (node) {
      if (node.dataset.logoMounted === '1') return;
      node.dataset.logoMounted = '1';
      node.innerHTML = logo({
        size: Number(node.dataset.logoSize) || 28,
        tile: node.dataset.logoTile || null,
        live: node.dataset.logo !== 'static',
        label: false
      });
    });
  }

  /* ------------------------------------------------------------- intro */

  function reduced() {
    return IM.util && IM.util.reducedMotion ? IM.util.reducedMotion()
      : Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function introOnLaunch(value) {
    if (value === undefined) {
      try { return window.localStorage.getItem(INTRO_KEY) !== 'off'; } catch (error) { return true; }
    }
    try { window.localStorage.setItem(INTRO_KEY, value ? 'on' : 'off'); } catch (error) { /* ignore */ }
    return Boolean(value);
  }

  var running = null;

  /* `node` is the curtain from index.html on a real load, or a new one for
   * a replay. Everything is CSS-timed (styles/brand.css); JS only starts the
   * clock, sounds the swell and removes the node. */
  function playIntro(options) {
    var opts = options || {};
    if (running) return running;
    var node = opts.node;
    if (!node) {
      node = document.createElement('div');
      node.className = 'intro';
      node.setAttribute('aria-hidden', 'true');
      document.body.appendChild(node);
    }
    node.innerHTML = '<div class="intro-in"><span class="intro-ring"></span><span class="intro-ring intro-ring-2"></span>' +
      logo({ size: 250, live: true, speed: 3, className: 'intro-mark', label: false }) + '</div>';
    var reduce = reduced();
    var docEl = document.documentElement;
    var done = false;
    var timers = [];
    function finish() {
      if (done) return;
      done = true;
      timers.forEach(clearTimeout);
      if (node.parentNode) node.parentNode.removeChild(node);
      docEl.classList.remove('has-intro');
      running = null;
      if (opts.onDone) opts.onDone();
    }
    /* one clock for everything: the whoosh, the lift and the rise all start
       together; the curtain was plain cream until now */
    requestAnimationFrame(function () {
      if (done) return;
      node.classList.add('intro-go');
      docEl.classList.add('has-intro');
      timers.push(setTimeout(finish, reduce ? INTRO_REDUCED_MS : INTRO_MS));
      if (!reduce && IM.sound) timers.push(setTimeout(function () { IM.sound.swell(); }, LIFT_AT_MS));
    });
    running = { node: node, finish: finish };
    return running;
  }

  function startOnLoad() {
    var curtain = document.getElementById('intro');
    var forced = false;
    try { forced = new URLSearchParams(location.search).get('intro') === '1'; } catch (error) { forced = false; }
    if (!curtain) return;
    if (!forced && !introOnLaunch()) {
      curtain.parentNode.removeChild(curtain);
      return;
    }
    playIntro({ node: curtain });
  }

  function bindSettings() {
    var replay = document.getElementById('replay-intro');
    if (replay) replay.addEventListener('click', function () { playIntro(); });
    var toggle = document.getElementById('intro-on-launch');
    if (toggle) {
      toggle.checked = introOnLaunch();
      toggle.addEventListener('change', function () { introOnLaunch(toggle.checked); });
    }
  }

  function init() {
    document.documentElement.setAttribute('data-brand-mark', BRAND.mark);
    mountLogos();
    bindSettings();
    startOnLoad();
  }

  IM.brand = {
    config: BRAND,
    logo: logo,
    wordmark: wordmark,
    mountLogos: mountLogos,
    playIntro: playIntro,
    introOnLaunch: introOnLaunch,
    /* the old chime is gone: the website's sound palette replaces it */
    chime: function () { if (IM.sound) IM.sound.success(); return function () {}; }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
