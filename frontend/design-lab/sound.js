/* The interface sound: a 1:1 port of the ImuTechz website's lib/sound.js
 * (WebSite/frontend/lib/sound.js) plus its click wiring (initSound in
 * components/initSite.js).
 *
 * Not a single audio file: every sound is synthesised with the Web Audio
 * API (the CSP has no media-src anyway). The palette is deliberately small
 * and quiet - a mechanical tick, a soft confirm, a low error. Tones,
 * frequencies, sweeps, gains, durations, the master gain .3 and the
 * filtered-noise "paper" burst are the website's numbers, unchanged.
 *
 * Rules (the website's):
 *   - nothing plays before the user has interacted with the page (browsers
 *     block audio until then, and we respect that);
 *   - one toggle, remembered between visits (localStorage, key below);
 *   - silent when reduced motion is asked for. The website states this rule
 *     but only applies it to the click tick; here every sound follows it
 *     (OS setting or Settings > Customisation "Reduce motion").
 *
 *   IMURUN.sound.tick() open() close() success() error() blip() swell()
 *   IMURUN.sound.isMuted() / setMuted(bool) / toggleMuted()
 *
 * Wiring in this app: a tick on every press of a button, link, summary,
 * label or tab (as initSite.js); open/close for the palette, shortcuts,
 * performance drawer and mobile drawer (panels.js, app.js); success / error
 * when such a notice appears (toast.js); a swell as the intro curtain lifts
 * (brand.js). ES2017.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var KEY = 'imurun.sound';
  var ctx = null;
  var master = null;
  var unlocked = false;
  var muted = false;

  try {
    muted = window.localStorage.getItem(KEY) === 'off';
  } catch (error) { /* no storage: sound stays on */ }

  function quiet() {
    if (IM.util && IM.util.reducedMotion) return IM.util.reducedMotion();
    return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function audio() {
    if (muted || quiet()) return null;
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      /* Creating a context before any gesture only yields a blocked one
       * (and a console warning): wait for the first press. */
      if (!unlocked && navigator.userActivation && !navigator.userActivation.hasBeenActive) return null;
      try {
        ctx = new AC();
      } catch (error) {
        return null;
      }
      master = ctx.createGain();
      master.gain.value = 0.3;
      master.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') {
      /* browsers keep the context suspended until a real gesture */
      ctx.resume().catch(function () {});
    }
    return ctx;
  }

  /* the first click anywhere unlocks the audio */
  function unlock() {
    if (unlocked) return;
    if (navigator.userActivation && !navigator.userActivation.hasBeenActive) return;
    unlocked = true;
    var c = audio();
    if (c && c.state === 'suspended') c.resume().catch(function () {});
  }

  function isMuted() { return muted; }

  function setMuted(next) {
    var was = muted;
    muted = Boolean(next);
    try { window.localStorage.setItem(KEY, muted ? 'off' : 'on'); } catch (error) { /* ignore */ }
    /* switching the sound on answers with a note, so the switch proves
       itself instead of looking like it does nothing */
    if (!muted) {
      unlock();
      if (was) success();
    }
    if (IM.bus) IM.bus.emit('sound', { muted: muted });
    return muted;
  }

  function toggleMuted() { return setMuted(!muted); }

  function tone(o) {
    var freq = o.freq === undefined ? 440 : o.freq;
    var type = o.type || 'sine';
    var dur = o.dur === undefined ? 0.08 : o.dur;
    var gain = o.gain === undefined ? 0.5 : o.gain;
    var sweep = o.sweep || 0;
    var delay = o.delay || 0;
    var c = audio();
    if (!c || !master) return;
    var t0 = c.currentTime + delay;
    var osc = c.createOscillator();
    var g = c.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t0);
    if (sweep) osc.frequency.exponentialRampToValueAtTime(Math.max(40, freq + sweep), t0 + dur);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain, t0 + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g);
    g.connect(master);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  }

  /* a very short filtered noise burst: the "paper" part of the tick */
  function noise(o) {
    var dur = o.dur === undefined ? 0.035 : o.dur;
    var gain = o.gain === undefined ? 0.16 : o.gain;
    var freq = o.freq === undefined ? 2600 : o.freq;
    var delay = o.delay || 0;
    var c = audio();
    if (!c || !master) return;
    var t0 = c.currentTime + delay;
    var len = Math.max(1, Math.floor(c.sampleRate * dur));
    var buf = c.createBuffer(1, len, c.sampleRate);
    var data = buf.getChannelData(0);
    for (var i = 0; i < len; i++) {
      /* a decaying noise burst */
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
    }
    var src = c.createBufferSource();
    src.buffer = buf;
    var filter = c.createBiquadFilter();
    filter.type = 'bandpass';
    filter.frequency.value = freq;
    filter.Q.value = 0.9;
    var g = c.createGain();
    g.gain.value = gain;
    src.connect(filter);
    filter.connect(g);
    g.connect(master);
    src.start(t0);
  }

  /* ---------- the sounds (the website's numbers) ---------- */

  /** the everyday tick: a link or a button was pressed */
  function tick() {
    unlock();
    noise({ dur: 0.03, gain: 0.14, freq: 3200 });
    tone({ freq: 1180, type: 'triangle', dur: 0.035, gain: 0.16, sweep: -260 });
  }

  /** a panel opened */
  function open() {
    unlock();
    tone({ freq: 520, type: 'sine', dur: 0.1, gain: 0.16, sweep: 220 });
    noise({ dur: 0.05, gain: 0.08, freq: 1800, delay: 0.01 });
  }

  /** something closed */
  function close() {
    unlock();
    tone({ freq: 640, type: 'sine', dur: 0.09, gain: 0.12, sweep: -220 });
  }

  /** it worked */
  function success() {
    unlock();
    tone({ freq: 660, type: 'sine', dur: 0.09, gain: 0.18 });
    tone({ freq: 990, type: 'sine', dur: 0.12, gain: 0.16, delay: 0.075 });
  }

  /** it did not */
  function error() {
    unlock();
    tone({ freq: 220, type: 'square', dur: 0.13, gain: 0.1, sweep: -60 });
    tone({ freq: 165, type: 'square', dur: 0.16, gain: 0.08, delay: 0.1 });
  }

  /** a code was sent, a file was copied */
  function blip() {
    unlock();
    tone({ freq: 880, type: 'square', dur: 0.05, gain: 0.08 });
    tone({ freq: 1320, type: 'square', dur: 0.05, gain: 0.06, delay: 0.055 });
  }

  /** a long soft swell */
  function swell() {
    unlock();
    tone({ freq: 220, type: 'sine', dur: 0.5, gain: 0.07, sweep: 120 });
  }

  /* ---- one soft tick for every press (initSite.js initSound) ---- */
  function initClicks() {
    var last = 0;
    document.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('.sound-toggle')) return;
      var now = Date.now();
      if (now - last < 60) return;
      last = now;
      var el = t.closest("button, a, summary, label, [role='tab']");
      if (!el) return;
      if (el.getAttribute('aria-disabled') === 'true' || el.disabled) return;
      tick();
    }, { passive: true });
  }

  IM.sound = {
    tick: tick, open: open, close: close, success: success, error: error, blip: blip, swell: swell,
    isMuted: isMuted, setMuted: setMuted, toggleMuted: toggleMuted, unlock: unlock
  };

  initClicks();
})();
