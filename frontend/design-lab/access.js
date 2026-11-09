/* Access level next to the composer, and the /permission command.
 *
 *   IMURUN.access.open()     show the access popover (also /permission)
 *   IMURUN.access.refresh()  re-read GET /api/permissions
 *
 * Three levels, the permission policy's modes (services/tools/permissions.py):
 *   Manual          ask      - every file read, change and command asks
 *   Ask for changes allow-reads (default) - reads run, changes/commands ask
 *   Auto            allow-all - runs without asking; deny-listed commands,
 *                               protected files and credential folders stay blocked
 * The choice is saved with POST /api/permissions {mode}, the same setting as
 * Settings > Permissions, so both stay in sync.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var LEVELS = [
    { mode: 'ask', label: 'Manual', short: 'Manual',
      text: 'Asks before every file read, change and command.' },
    { mode: 'allow-reads', label: 'Ask for changes', short: 'Ask for changes',
      text: 'Reads files on its own. Asks before it changes files or runs a command.' },
    { mode: 'allow-all', label: 'Auto', short: 'Auto',
      text: 'Runs without asking. Blocked commands, protected files and credential folders stay blocked.' }
  ];
  var current = 'allow-reads';
  var pill = null;
  var pop = null;
  var opener = null;

  function api() { return window.__imurun_api; }
  function level(mode) {
    for (var i = 0; i < LEVELS.length; i += 1) if (LEVELS[i].mode === mode) return LEVELS[i];
    return LEVELS[1];
  }
  function toast(opts) { if (IM.toast) IM.toast(opts); }

  var SHIELD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5 5 6.5v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9v-5z"/></svg>';

  function ensurePill() {
    if (pill && document.body.contains(pill)) return pill;
    var meta = document.querySelector('#page-chat .composer-meta');
    if (!meta) return null;
    var divider = document.createElement('span');
    divider.className = 'meta-divider';
    divider.setAttribute('aria-hidden', 'true');
    pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'access-pill';
    pill.setAttribute('aria-haspopup', 'dialog');
    pill.addEventListener('click', function () { open(pill); });
    meta.appendChild(divider);
    meta.appendChild(pill);
    render();
    return pill;
  }

  function render() {
    if (!pill) return;
    var l = level(current);
    pill.innerHTML = SHIELD + '<span>Access <strong>' + l.short + '</strong></span>';
    pill.dataset.mode = current;
    pill.title = 'Access: ' + l.label + '. ' + l.text + ' (/permission)';
  }

  function refresh() {
    if (!api() || !api().getPermissions) return Promise.resolve(current);
    return api().getPermissions().then(function (view) {
      var mode = view && (view.mode || (view.values && view.values.mode));
      if (mode) current = mode;
      render();
      return current;
    }, function () { return current; });
  }

  function choose(mode) {
    if (!api() || !api().setPermissions) return;
    var previous = current;
    current = mode;
    render();
    paint();
    api().setPermissions({ mode: mode }).then(function () {
      toast({ tone: 'success', kicker: 'Access', title: level(mode).label, text: level(mode).text });
      close();
    }, function (error) {
      current = previous;
      render();
      paint();
      toast({ tone: 'error', kicker: 'Access', title: 'Could not change access', text: error && error.message });
    });
  }

  function paint() {
    if (!pop) return;
    pop.querySelectorAll('[data-access-mode]').forEach(function (row) {
      var on = row.dataset.accessMode === current;
      row.setAttribute('aria-checked', on ? 'true' : 'false');
      row.tabIndex = on ? 0 : -1;
    });
  }

  function build() {
    pop = document.createElement('div');
    pop.className = 'access-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Access level');
    pop.hidden = true;
    pop.innerHTML =
      '<div class="access-head"><strong>How much may IMURUN do on its own?</strong>' +
      '<button class="access-x" type="button" aria-label="Close">&times;</button></div>' +
      '<div class="access-list" role="radiogroup" aria-label="Access level">' +
      LEVELS.map(function (l) {
        return '<button class="access-row" type="button" role="radio" data-access-mode="' + l.mode + '">' +
          '<span class="access-dot" aria-hidden="true"></span><span class="access-copy"><strong>' + l.label +
          '</strong><span>' + l.text + '</span></span></button>';
      }).join('') + '</div>' +
      '<p class="access-foot">Folders it may use and the on/off switches are in ' +
      '<button class="access-link" type="button">Settings, Permissions</button>.</p>';
    document.body.appendChild(pop);
    pop.addEventListener('click', function (event) {
      var row = event.target.closest('[data-access-mode]');
      if (row) { choose(row.dataset.accessMode); return; }
      if (event.target.closest('.access-x')) { close(); return; }
      if (event.target.closest('.access-link')) {
        close();
        if (IM.shell && IM.shell.openSettings) IM.shell.openSettings('permissions');
      }
    });
    pop.addEventListener('keydown', function (event) {
      var rows = Array.prototype.slice.call(pop.querySelectorAll('[data-access-mode]'));
      var index = rows.indexOf(document.activeElement);
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && index !== -1) {
        event.preventDefault();
        rows[(index + (event.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length].focus();
      }
    });
    document.addEventListener('mousedown', function (event) {
      if (!pop.hidden && !pop.contains(event.target) && event.target !== pill && !(pill && pill.contains(event.target))) close();
    });
  }

  function open(from) {
    if (!pop) build();
    opener = from || document.activeElement;
    refresh().then(function () {
      paint();
      pop.hidden = false;
      var anchor = (pill && pill.offsetParent !== null) ? pill : null;
      if (anchor) {
        var rect = anchor.getBoundingClientRect();
        pop.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - 372)) + 'px';
        pop.style.top = '';
        pop.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
        pop.classList.remove('is-centred');
      } else {
        pop.style.left = pop.style.bottom = '';
        pop.classList.add('is-centred');
      }
      var active = pop.querySelector('[aria-checked="true"]') || pop.querySelector('[data-access-mode]');
      if (active) active.focus();
    });
  }

  function close() {
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    if (opener && opener.focus && document.body.contains(opener)) opener.focus();
    opener = null;
  }

  function init() {
    ensurePill();
    refresh();
    if (IM.bus) IM.bus.on('page', function (page) { if (page === 'chat') ensurePill(); });
  }

  IM.access = { open: function () { open(null); }, refresh: refresh, close: close };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else setTimeout(init, 0);
})();
