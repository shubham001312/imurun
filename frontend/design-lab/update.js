/* Update flow: launch pop-up, Settings card, and the /update command.
 *
 *   IMURUN.update.state()        last known GET /api/update object
 *   IMURUN.update.check()        POST {action:"check"} -> object
 *   IMURUN.update.apply()        POST {action:"apply"} and show progress
 *   IMURUN.update.later() / dismiss()
 *   IMURUN.update.open()         show the pop-up now
 *   IMURUN.update.command()      what `/update` does (check, then pop-up or toast)
 *
 * Contract (services/server/doc/README.md):
 *   GET /api/update -> {current, latest, available, source, releaseNotesUrl,
 *     prompt, state: idle|checking|updating|restart_pending|error, error}
 *   POST /api/update {action: check|apply|dismiss|later}
 *   SSE `update` carries the same object on every state change.
 * The server decides when to ask again (`prompt`); the pop-up is shown at
 * most once per page load. "Not now" posts `dismiss` (asks again after 3 launches).
 * Esc or a click outside posts `later` (this launch only).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var info = null;
  var modal = null;
  var releaseTrap = null;
  var shownThisLoad = false;
  var applying = false;
  var waiting = false;
  var cardEl = null;

  function api() { return window.__imurun_api; }
  function toast(o) { if (IM.toast) IM.toast(o); }
  function esc(s) { return util.escapeHtml(String(s == null ? '' : s)); }
  function safeUrl(u) { return /^https?:\/\//i.test(u || ''); }

  function post(action) {
    return api().fetchAPI('/api/update', { method: 'POST', body: JSON.stringify({ action: action }) });
  }

  function set(next) {
    if (next && next.update && typeof next.update === 'object') next = next.update; /* POST wraps: {ok, update} */
    if (!next || typeof next !== 'object') return;
    info = next;
    renderModal();
    renderCard();
    if (IM.bus) IM.bus.emit('update', info);
    if (applying && info.state === 'restart_pending') waitForRestart();
    if (applying && info.state === 'error') applying = false;
    renderModal();
  }

  function load() {
    return api().fetchAPI('/api/update').then(function (data) { set(data); return data; });
  }

  /* POST check answers state "checking"; the result arrives as an `update`
   * event. Resolve on the first non-checking state, or poll GET as a backup. */
  function check() {
    return post('check').then(function (data) {
      set(data);
      return new Promise(function (resolve) {
        var tries = 0;
        (function wait() {
          if (!info || info.state !== 'checking' || tries++ > 12) { resolve(info); return; }
          setTimeout(function () { load().then(wait, wait); }, 1250);
        })();
      });
    });
  }

  function summary(i) {
    if (!i) return 'Update status unavailable.';
    if (i.state === 'error' && i.error) return 'Update check failed: ' + i.error;
    if (i.available && i.canApply === false && i.applyBlocked) return 'Version ' + (i.latest || '?') + ' is available, but it cannot be installed from here: ' + i.applyBlocked + '.';
    if (i.available) return 'Version ' + (i.latest || '?') + ' is available (you have ' + (i.current || '?') + ').';
    return 'You are up to date (' + (i.current || 'unknown') + ').';
  }

  /* ------------------------------------------------------------- modal */

  function build() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.className = 'modal update-modal';
    modal.id = 'update-modal';
    modal.hidden = true;
    modal.innerHTML =
      '<div class="dialog update-dialog" role="dialog" aria-modal="true" aria-labelledby="update-title" aria-describedby="update-body">' +
      '<h2 id="update-title">Update available</h2>' +
      '<p class="sub" id="update-body"></p>' +
      '<p class="update-progress" id="update-progress" role="status" aria-live="polite" hidden></p>' +
      '<p class="update-cmd" id="update-cmd" hidden></p>' +
      '<p class="update-notes" id="update-notes"><a id="update-notes-link" href="#" target="_blank" rel="noopener noreferrer">Release notes</a></p>' +
      '<div class="dialog-actions update-actions">' +
      '<button class="cancel" id="update-later" type="button">Not now</button>' +
      '<button class="send" id="update-now" type="button">Update now</button>' +
      '</div></div>';
    document.body.appendChild(modal);
    modal.querySelector('#update-now').addEventListener('click', apply);
    modal.querySelector('#update-later').addEventListener('click', dismiss); /* "Not now": the server counts launches */
    modal.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !applying) { e.stopPropagation(); closeLater(); }
    });
    modal.addEventListener('mousedown', function (e) { if (e.target === modal && !applying) closeLater(); });
    return modal;
  }

  function progressText() {
    var s = info && info.state;
    if (s === 'restart_pending') return 'Restarting…';
    if (s === 'updating') {
      if (info.phase === 'downloading') return 'Downloading…';
      if (info.phase === 'installing') return 'Installing…';
      return 'Downloading and installing…';
    }
    if (s === 'error') return 'Update failed: ' + (info.error || 'unknown error');
    return applying ? 'Starting…' : '';
  }

  function renderModal() {
    if (!modal || modal.hidden) return;
    modal.querySelector('#update-body').textContent = summary(info);
    var link = modal.querySelector('#update-notes-link');
    var url = info && info.releaseNotesUrl;
    modal.querySelector('#update-notes').hidden = !safeUrl(url);
    if (safeUrl(url)) link.href = url;
    var cmdEl = modal.querySelector('#update-cmd');
    cmdEl.hidden = !(info && info.command);
    cmdEl.textContent = info && info.command ? 'This will run: ' + info.command : '';
    var text = progressText();
    var prog = modal.querySelector('#update-progress');
    prog.hidden = !text;
    prog.textContent = text;
    modal.querySelector('#update-now').disabled = applying || !info || info.canApply === false;
    modal.querySelector('#update-later').disabled = applying;
  }

  function open() {
    build();
    if (!modal.hidden) return;
    modal.hidden = false;
    renderModal();
    if (util.trapFocus) releaseTrap = util.trapFocus(modal);
    modal.querySelector('#update-now').focus();
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    if (releaseTrap) { releaseTrap(); releaseTrap = null; }
  }

  function apply() {
    open();
    applying = true;
    renderModal();
    return post('apply').then(set, function (error) {
      applying = false;
      info = Object.assign({}, info || {}, { state: 'error', error: error.message });
      renderModal();
      toast({ tone: 'error', icon: 'alert', kicker: 'Update', title: 'Could not start the update', text: error.message });
    });
  }

  /* Esc / click outside: hide for this launch only, counts nothing. */
  function closeLater() {
    closeModal();
    return post('later').then(set, function () {});
  }

  /* "Not now": the server asks again after 3 more launches. */
  function dismiss() {
    closeModal();
    return post('dismiss').then(set, function () {});
  }

  /* After restart_pending the old server goes away. Poll /health until the
   * new one answers, then reload to pick up the new frontend files. */
  function waitForRestart() {
    if (waiting) return;
    waiting = true;
    var started = Date.now();
    var sawDown = false;
    var previous = info && info.current;
    (function poll() {
      api().healthCheck().then(function (up) {
        var elapsed = Date.now() - started;
        if (!up) sawDown = true;
        if (up && (sawDown || elapsed > 6000)) {
          api().fetchAPI('/api/update').then(function (d) {
            if (sawDown || (d && d.current !== previous) || elapsed > 20000) location.reload();
            else setTimeout(poll, 1500);
          }, function () { setTimeout(poll, 1500); });
          return;
        }
        if (elapsed > 180000) {
          waiting = false;
          applying = false;
          info = Object.assign({}, info || {}, { state: 'error', error: 'The app did not come back. Close it and start it again.' });
          renderModal();
          return;
        }
        setTimeout(poll, 1500);
      });
    })();
  }

  /* ------------------------------------------------------ settings card */

  function ensureCard() {
    if (cardEl && document.body.contains(cardEl)) return cardEl;
    var host = document.querySelector('[data-settings-panel="profile"]');
    if (!host) return null;
    cardEl = document.createElement('div');
    cardEl.className = 'panel settings-card update-card';
    cardEl.innerHTML =
      '<h3 class="settings-card-title">About and updates</h3>' +
      '<p class="update-card-text" id="update-card-text" role="status"></p>' +
      '<div class="settings-actions" id="update-card-actions"></div>';
    host.appendChild(cardEl);
    cardEl.querySelector('#update-card-actions').addEventListener('click', function (e) {
      var b = e.target.closest('[data-update-act]');
      if (!b) return;
      var act = b.dataset.updateAct;
      if (act === 'check') {
        b.disabled = true;
        check().then(null, function (err) { toast({ tone: 'error', icon: 'alert', kicker: 'Update', title: 'Check failed', text: err.message }); })
          .then(function () { b.disabled = false; });
      } else if (act === 'apply') apply();
    });
    return cardEl;
  }

  function renderCard() {
    var el = ensureCard();
    if (!el) return;
    el.querySelector('#update-card-text').textContent =
      summary(info) + (info && info.source ? ' Source: ' + info.source + '.' : '');
    var html = '<button class="btn" type="button" data-update-act="check">Check for updates</button>';
    if (info && info.available) {
      html = '<span class="chip neutral update-badge">Update available</span>' +
        '<button class="btn primary" type="button" data-update-act="apply"' + (info.canApply === false ? ' disabled' : '') + '>Update now</button>' +
        html;
      if (safeUrl(info.releaseNotesUrl)) {
        html += '<a class="btn ghost" href="' + esc(info.releaseNotesUrl) + '" target="_blank" rel="noopener noreferrer">Release notes</a>';
      }
    }
    el.querySelector('#update-card-actions').innerHTML = html;
  }

  /* --------------------------------------------------------------- init */

  function maybePrompt() {
    if (shownThisLoad || !info || !info.prompt || !info.available) return;
    shownThisLoad = true;
    open();
  }

  function init() {
    if (!api() || !api().fetchAPI) return;
    if (IM.bus) {
      IM.bus.on('server:update', function (e) {
        var data = Object.assign({}, e);
        delete data.type;
        set(data);
        maybePrompt();
      });
    }
    load().then(maybePrompt, function () { /* server without /api/update */ });
  }

  /* `/update` typed in the composer (app.js handleCommand): check now, then
   * show the pop-up when a newer version exists, else a toast with the result. */
  function command() {
    toast({ tone: 'neutral', icon: 'info', kicker: 'Update', title: 'Checking for updates…', timeout: 2500 });
    return check().then(function (i) {
      if (i && i.available) { open(); return; }
      toast({ tone: i && i.state === 'error' ? 'error' : 'neutral', icon: i && i.state === 'error' ? 'alert' : 'info',
        kicker: 'Update', title: summary(i), timeout: 7000 });
    }, function (err) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Update', title: 'Check failed', text: err.message });
    });
  }

  IM.update = {
    state: function () { return info; },
    load: load, check: check, apply: apply, later: closeLater, dismiss: dismiss, open: open, summary: summary,
    command: command
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
