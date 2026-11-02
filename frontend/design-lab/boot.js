/* Startup overlay controller.
 *
 * Shown from first paint. It polls the local API until the shell and the FISH
 * engine answer, then fades out. Startup time is unknown, so the animation
 * loops and the copy changes as each stage reports in.
 */
(function () {
  'use strict';

  var el = null;
  var statusEl = null;
  var barEl = null;
  var actionsEl = null;
  var hintEl = null;
  var timer = null;
  var started = Date.now();
  var MAX_WAIT_MS = 120000;
  var SHELL_READY_GRACE_MS = 9000;
  var serverAnswered = false;

  function build() {
    if (el) return el;
    el = document.getElementById('boot');
    if (!el) return null;
    statusEl = el.querySelector('.boot-status');
    barEl = el.querySelector('.boot-bar');
    actionsEl = el.querySelector('.boot-actions');
    hintEl = el.querySelector('.boot-hint');
    return el;
  }

  function setStatus(text, isError) {
    if (!build() || !statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle('is-error', Boolean(isError));
  }

  function show(text) {
    if (!build()) return;
    el.classList.remove('is-hidden');
    if (barEl) barEl.hidden = false;
    if (actionsEl) actionsEl.hidden = true;
    if (hintEl) hintEl.hidden = true;
    if (text) setStatus(text);
  }

  function hide() {
    if (!build()) return;
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    el.classList.add('is-hidden');
    setTimeout(function () {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    }, 600);
  }

  function fail(text, onRetry) {
    if (!build()) return;
    setStatus(text, true);
    if (barEl) barEl.hidden = true;
    if (actionsEl) actionsEl.hidden = false;
    if (hintEl) {
      hintEl.hidden = false;
      hintEl.textContent = 'The shell is still usable. Chat will start the engine when you send a message.';
    }
    var retry = el.querySelector('[data-boot-retry]');
    if (retry) {
      retry.onclick = function () {
        show('Retrying…');
        if (onRetry) onRetry();
      };
    }
    var skip = el.querySelector('[data-boot-skip]');
    if (skip) skip.onclick = hide;
  }

  function poll() {
    if (timer) clearInterval(timer);
    var api = window.__imurun_api;
    if (!api) {
      fail('The API layer did not load. Reload the page.', function () { location.reload(); });
      return;
    }

    async function tick() {      var elapsed = Date.now() - started;
      try {
        var status = await api.getStatus();
        serverAnswered = true;
        if (status && status.runtime === 'ready') {
          setStatus('Ready');
          hide();
          return;
        }
        setStatus('Starting the FISH engine…');
        /* The shell is usable even while the engine warms up. Do not hold the
         * whole UI hostage: after a short grace period, fade out and let the
         * composer start the engine on demand. */
        if (elapsed > SHELL_READY_GRACE_MS) {
          setStatus('Shell ready · engine starting in the background');
          setTimeout(hide, 900);
          return;
        }
      } catch (error) {
        if (!serverAnswered) {
          setStatus('Connecting to the local server…');
        } else {
          setStatus('Waiting for the local server…');
        }
      }

      if (elapsed > MAX_WAIT_MS && !serverAnswered) {
        fail('The local server is not answering.', function () { location.reload(); });
      }
    }

    tick();
    timer = setInterval(tick, 1800);
  }

  window.__imurun_boot = {
    show: show,
    hide: hide,
    status: setStatus,
    fail: fail,
    poll: poll,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      build();
      show('Starting IMURUN…');
      poll();
    });
  } else {
    build();
    show('Starting IMURUN…');
    poll();
  }
})();
