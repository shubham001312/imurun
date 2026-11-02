/* Shared core for the IMURUN shell: one namespace, small utilities, an
 * internal event bus and the engine-status store.
 *
 * Every other module reads and writes through window.IMURUN so that load
 * order between modules only matters at call time, never at parse time.
 *
 *   IMURUN.util    formatting, escaping, DOM helpers, storage
 *   IMURUN.bus     on(type, fn) -> off(), emit(type, detail)
 *   IMURUN.state   sessions, activeSessionId, status, turn (see docs)
 *   IMURUN.status  refresh() -> Promise<status>, start polling
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  /* ---------------------------------------------------------------- util */

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatBytes(bytes) {
    var n = Number(bytes) || 0;
    if (n <= 0) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, index)).toFixed(index > 1 ? 1 : 0) + ' ' + units[index];
  }

  function formatTokens(value) {
    var count = Number(value) || 0;
    if (count >= 1e6) return (count / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (count >= 1e3) return (count / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(Math.round(count));
  }

  function formatSeconds(seconds) {
    var n = Number(seconds);
    if (!isFinite(n) || n < 0) return '—';
    if (n < 1) return Math.round(n * 1000) + ' ms';
    if (n < 60) return n.toFixed(n < 10 ? 2 : 1) + ' s';
    return Math.floor(n / 60) + 'm ' + Math.round(n % 60) + 's';
  }

  function formatTimeAgo(timestamp) {
    if (!timestamp) return 'unknown';
    var seconds = typeof timestamp === 'string' ? new Date(timestamp).getTime() / 1000 : Number(timestamp);
    if (!isFinite(seconds)) return 'unknown';
    var diff = Date.now() / 1000 - seconds;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.round(diff / 60) + ' min ago';
    if (diff < 86400) {
      var hours = Math.round(diff / 3600);
      return hours + (hours === 1 ? ' hour ago' : ' hours ago');
    }
    var days = Math.round(diff / 86400);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  /* True when the OS asks for reduced motion or the user forced it in
   * Settings > Customisation. Read at call time so the toggle is live. */
  function reducedMotion() {
    if (document.body && document.body.dataset.reducedMotion === '1') return true;
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (error) {
      return false;
    }
  }

  /* addEventListener that returns its own remover, so modules can keep a
   * list of disposers instead of leaking listeners. */
  function listen(target, type, handler, options) {
    if (!target) return function () {};
    target.addEventListener(type, handler, options);
    return function () { target.removeEventListener(type, handler, options); };
  }

  function debounce(fn, wait) {
    var timer = null;
    function run() {
      var args = arguments;
      var self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fn.apply(self, args);
      }, wait);
    }
    run.flush = function () {
      if (!timer) return;
      clearTimeout(timer);
      timer = null;
      fn();
    };
    run.cancel = function () {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    return run;
  }

  var store = {
    get: function (key, fallback) {
      try {
        var raw = window.localStorage.getItem(key);
        if (raw === null || raw === undefined) return fallback;
        return JSON.parse(raw);
      } catch (error) {
        return fallback;
      }
    },
    set: function (key, value) {
      try {
        window.localStorage.setItem(key, JSON.stringify(value));
        return true;
      } catch (error) {
        return false; /* file:// or private mode may refuse */
      }
    },
    remove: function (key) {
      try { window.localStorage.removeItem(key); } catch (error) { /* ignore */ }
    }
  };

  /* Elements that can receive keyboard focus inside a container. */
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function focusables(root) {
    if (!root) return [];
    return Array.prototype.filter.call(root.querySelectorAll(FOCUSABLE), function (node) {
      return node.offsetParent !== null || node === document.activeElement;
    });
  }

  /* Keep Tab inside `root` while it is open. Returns a release function that
   * also restores focus to whatever had it before. */
  function trapFocus(root) {
    var previous = document.activeElement;
    function onKey(event) {
      if (event.key !== 'Tab') return;
      var items = focusables(root);
      if (!items.length) {
        event.preventDefault();
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    root.addEventListener('keydown', onKey);
    return function release(restore) {
      root.removeEventListener('keydown', onKey);
      if (restore !== false && previous && typeof previous.focus === 'function' && document.contains(previous)) {
        try { previous.focus(); } catch (error) { /* ignore */ }
      }
    };
  }

  /* Announce text to screen readers through one polite live region. */
  var announcer = null;
  function announce(text) {
    if (!announcer) {
      announcer = document.getElementById('sr-announcer');
      if (!announcer) {
        announcer = document.createElement('div');
        announcer.id = 'sr-announcer';
        announcer.className = 'sr-only';
        announcer.setAttribute('aria-live', 'polite');
        announcer.setAttribute('aria-atomic', 'true');
        document.body.appendChild(announcer);
      }
    }
    announcer.textContent = '';
    setTimeout(function () { announcer.textContent = text; }, 30);
  }

  function isTypingTarget(node) {
    if (!node) return false;
    var tag = node.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || node.isContentEditable;
  }

  IM.util = {
    escapeHtml: escapeHtml,
    formatBytes: formatBytes,
    formatTokens: formatTokens,
    formatSeconds: formatSeconds,
    formatTimeAgo: formatTimeAgo,
    reducedMotion: reducedMotion,
    listen: listen,
    debounce: debounce,
    focusables: focusables,
    trapFocus: trapFocus,
    announce: announce,
    isTypingTarget: isTypingTarget
  };
  IM.store = store;

  /* ----------------------------------------------------------------- bus */

  var handlers = {};
  IM.bus = {
    on: function (type, fn) {
      (handlers[type] = handlers[type] || []).push(fn);
      return function off() {
        var list = handlers[type] || [];
        var index = list.indexOf(fn);
        if (index !== -1) list.splice(index, 1);
      };
    },
    emit: function (type, detail) {
      (handlers[type] || []).slice().forEach(function (fn) {
        try { fn(detail); } catch (error) { /* one bad handler must not stop the rest */ }
      });
      (handlers['*'] || []).slice().forEach(function (fn) {
        try { fn(type, detail); } catch (error) { /* ignore */ }
      });
    }
  };

  /* --------------------------------------------------------------- state */

  IM.state = IM.state || {
    sessions: [],          /* [{id, name, created_at, renamed_at}] from /api/sessions */
    activeSessionId: null, /* the session shown on the Chat page */
    status: null,          /* last /api/status payload, or null when unreachable */
    statusError: null,     /* Error from the last failed /api/status call */
    events: 'connecting',  /* SSE state: connecting | open | reconnecting | offline */
    turn: null             /* the live turn controller (chat.js) or null */
  };

  /* -------------------------------------------------------------- status */

  var inflight = null;
  var pollTimer = null;
  var POLL_MS = 15000;

  /* Fetch /api/status once; concurrent callers share the same request.
   * Emits 'status' with the payload (or null) on every completion. */
  function refreshStatus() {
    if (inflight) return inflight;
    var api = window.__imurun_api;
    if (!api) return Promise.resolve(null);
    inflight = api.getStatus().then(function (status) {
      IM.state.status = status || null;
      IM.state.statusError = null;
      IM.bus.emit('status', IM.state.status);
      return IM.state.status;
    }, function (error) {
      IM.state.status = null;
      IM.state.statusError = error;
      IM.bus.emit('status', null);
      return null;
    }).then(function (value) {
      inflight = null;
      return value;
    });
    return inflight;
  }

  /* Engine state in words the UI can show without lying:
   *   'unreachable'  the local server did not answer
   *   'provisioning' the server answered but the engine cannot generate
   *   'ready'        a turn can be started now
   * "No model downloaded" is decided from /api/models by the callers, since
   * some compute paths (the local core) never report a selected model. */
  function engineState(status) {
    if (!status) return 'unreachable';
    if (status.runtime !== 'ready') return 'provisioning';
    return 'ready';
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      if (document.visibilityState === 'hidden') return;
      refreshStatus();
    }, POLL_MS);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') refreshStatus();
    });
  }

  IM.status = {
    refresh: refreshStatus,
    engineState: engineState,
    startPolling: startPolling
  };
})();
