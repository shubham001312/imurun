/* Panels: the performance/engine drawer, the command palette (session
 * search), the keyboard-shortcuts dialog, the sidebar session filter, and
 * the global keyboard shortcuts.
 *
 *   IMURUN.panels.toggleStats(force?)
 *   IMURUN.panels.openPalette(query?)
 *   IMURUN.panels.openShortcuts()
 *
 * Numbers are honest about their source: engine-reported counters are
 * labelled "engine", browser-measured ones "measured here", and anything the
 * server does not report says so instead of showing a made-up value.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var state = IM.state;
  var escapeHtml = util.escapeHtml;
  var HISTORY_KEY = 'imurun.stats.history.v1';
  var STATS_OPEN_KEY = 'imurun.stats.open';
  var HISTORY_MAX = 40;

  function byId(id) { return document.getElementById(id); }
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || '');
  var MOD = isMac ? '⌘' : 'Ctrl';

  /* ------------------------------------------------------------ history */

  var history = IM.store.get(HISTORY_KEY, []);
  if (!Array.isArray(history)) history = [];

  IM.bus.on('turn.stats', function (stats) {
    history.push(stats);
    if (history.length > HISTORY_MAX) history = history.slice(-HISTORY_MAX);
    IM.store.set(HISTORY_KEY, history);
    renderStats();
  });

  /* ------------------------------------------------------- stats drawer */

  var panel = null;
  var toggleButton = null;

  function toggleStats(force) {
    if (!panel) return;
    var open = typeof force === 'boolean' ? force : panel.hidden;
    if (IM.sound && panel.hidden === open) { if (open) IM.sound.open(); else IM.sound.close(); }
    panel.hidden = !open;
    document.body.classList.toggle('stats-open', open);
    if (toggleButton) toggleButton.setAttribute('aria-expanded', open ? 'true' : 'false');
    IM.store.set(STATS_OPEN_KEY, open);
    if (open) {
      renderStats();
      IM.status.refresh();
    }
  }

  function row(label, value, hint) {
    return '<div class="stat-row"><dt>' + escapeHtml(label) + '</dt><dd' +
      (hint ? ' title="' + escapeHtml(hint) + '"' : '') + '>' + value + '</dd></div>';
  }

  function unknown(text) {
    return '<span class="stat-unknown">' + escapeHtml(text || 'not reported') + '</span>';
  }

  /* Engine fields the shell knows how to label. Anything else scalar that
   * the engine reports is listed after these, so new fields show up without
   * a frontend change. */
  var ENGINE_FIELDS = [
    ['arch', 'Architecture'],
    ['quant', 'Quantisation'],
    ['kernel', 'Kernel'],
    ['context', 'Context window'],
    ['context_size', 'Context window'],
    ['chat_format', 'Chat format'],
    ['layers', 'Layers'],
    ['threads', 'Threads'],
    ['backend', 'Backend'],
    ['version', 'Engine version']
  ];

  function engineInfo(status) {
    var engine = (status && status.engine) || {};
    var info = Object.assign({}, engine.info || {}, engine.model_info || {}, engine);
    return info;
  }

  function sparkline(values) {
    var points = values.filter(function (v) { return typeof v === 'number' && isFinite(v); });
    if (points.length < 2) return '<p class="stat-note">Send a few messages to see the trend.</p>';
    var max = Math.max.apply(null, points) || 1;
    var w = 248;
    var h = 44;
    var step = w / (points.length - 1);
    var coords = points.map(function (v, i) {
      return (i * step).toFixed(1) + ',' + (h - 4 - (v / max) * (h - 10)).toFixed(1);
    });
    var last = coords[coords.length - 1].split(',');
    return '<svg class="sparkline" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-label="Tokens per second over the last ' +
      points.length + ' replies, peak ' + max.toFixed(1) + '">' +
      '<polyline points="0,' + h + ' ' + coords.join(' ') + ' ' + w + ',' + h + '" class="spark-fill"/>' +
      '<polyline points="' + coords.join(' ') + '" class="spark-line"/>' +
      '<circle cx="' + last[0] + '" cy="' + last[1] + '" r="2.6" class="spark-dot"/></svg>';
  }

  /* Context use: exact when the engine reports it, otherwise an estimate
   * from the visible transcript (about 4 characters per token). */
  function contextBlock(lastStats) {
    var used = lastStats && lastStats.contextUsed;
    var size = lastStats && lastStats.contextSize;
    var source = 'engine';
    if (!used || !size) {
      var settings = window.__imurun_settings ? window.__imurun_settings.get() : null;
      size = (settings && settings.server && Number(settings.server.contextSize)) || 0;
      var transcript = byId('transcript');
      var chars = transcript ? (transcript.textContent || '').length : 0;
      used = Math.round(chars / 4);
      source = 'estimate';
    }
    if (!size) return row('Context', unknown('size not known'));
    var pct = Math.min(100, Math.round((used / size) * 100));
    return '<div class="stat-context">' +
      '<div class="stat-context-head"><span>Context</span><span>' + util.formatTokens(used) + ' / ' + util.formatTokens(size) +
      (source === 'estimate' ? ' <em>estimate</em>' : '') + '</span></div>' +
      '<div class="meter' + (pct >= 85 ? ' is-high' : '') + '" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" aria-label="Context used">' +
      '<span style="width:' + pct + '%"></span></div></div>';
  }

  function renderStats() {
    if (!panel || panel.hidden) return;
    var body = panel.querySelector('.stats-body');
    if (!body) return;
    var status = state.status;
    var engineState = IM.status.engineState(status);
    var info = engineInfo(status);
    var selected = state.selectedModel;
    var sessionId = state.activeSessionId;
    var sessionHistory = history.filter(function (item) { return item.sessionId === sessionId; });
    var last = sessionHistory[sessionHistory.length - 1] || history[history.length - 1] || null;
    var stateLabel = { ready: 'Online', provisioning: 'Provisioning', unreachable: 'Unreachable' }[engineState];

    var html = '';
    /* Model card */
    html += '<section class="stats-card model-card-info" aria-labelledby="stats-model-title">' +
      '<div class="stats-card-head"><h3 id="stats-model-title">Model</h3>' +
      '<span class="state-pill" data-state="' + engineState + '">' + stateLabel + '</span></div>';
    var modelName = (status && status.selected_model) || (selected && (selected.name || selected.filename)) || '';
    html += '<p class="stats-model-name">' + (modelName ? escapeHtml(modelName) : (state.modelsDownloaded === 0 ? 'No model downloaded' : 'No model loaded')) + '</p>';
    html += '<dl class="stat-list">';
    var engineName = info.engine || '';
    html += row('Engine', engineName ? escapeHtml(engineName === 'fish' ? 'FISH' : engineName) : unknown());
    var shown = {};
    ENGINE_FIELDS.forEach(function (pair) {
      var key = pair[0];
      if (shown[pair[1]]) return;
      var value = info[key];
      if (value === undefined || value === null || value === '') return;
      shown[pair[1]] = true;
      html += row(pair[1], escapeHtml(key.indexOf('context') === 0 ? util.formatTokens(value) + ' tokens' : value));
    });
    if (!shown.Architecture) html += row('Architecture', unknown());
    if (!shown['Quantisation']) {
      var tag = selected && Array.isArray(selected.tags) ? selected.tags.filter(function (t) { return /^q\d|^f16|^bf16|^iq\d/i.test(t); })[0] : null;
      html += row('Quantisation', tag ? escapeHtml(tag) + ' <em>from catalogue</em>' : unknown());
    }
    if (!shown.Kernel) html += row('Kernel', unknown());
    if (!shown['Context window']) {
      var settings = window.__imurun_settings ? window.__imurun_settings.get() : null;
      var ctx = settings && settings.server ? settings.server.contextSize : null;
      html += row('Context window', ctx ? util.formatTokens(ctx) + ' tokens <em>configured</em>' : unknown());
    }
    if (selected && (selected.size_bytes || selected.size_gb)) {
      html += row('File size', escapeHtml(selected.size_gb ? Number(selected.size_gb).toFixed(1) + ' GB' : util.formatBytes(selected.size_bytes)));
    }
    Object.keys(info).forEach(function (key) {
      if (['engine', 'available', 'info', 'model_info'].indexOf(key) !== -1) return;
      if (ENGINE_FIELDS.some(function (pair) { return pair[0] === key; })) return;
      var value = info[key];
      if (value === null || typeof value === 'object') return;
      html += row(key.replace(/_/g, ' '), escapeHtml(String(value)));
    });
    html += '</dl>';
    if (engineState !== 'ready') {
      html += '<p class="stat-note">' + (engineState === 'unreachable'
        ? 'The local server is not answering. Numbers here are from earlier replies.'
        : 'The engine is still starting. It loads the model when it is ready, or when you send a message.') + '</p>';
    }
    html += '</section>';

    /* Last reply */
    html += '<section class="stats-card" aria-labelledby="stats-turn-title"><div class="stats-card-head"><h3 id="stats-turn-title">Last reply</h3>' +
      (last ? '<span class="stat-source">' + (last.source === 'engine' ? 'engine' : 'measured here') + '</span>' : '') + '</div>';
    if (!last) {
      html += '<p class="stat-note">No replies yet on this page. Speed and timing appear after the first answer.</p>';
    } else {
      html += '<dl class="stat-list">' +
        row('Speed', last.tokensPerSecond ? last.tokensPerSecond.toFixed(1) + ' tok/s' : unknown('too short to measure'),
          last.source === 'engine' ? 'Decode speed reported by the engine' : 'Streamed chunks per second after the first one') +
        row(last.prefillSeconds !== null && last.prefillSeconds !== undefined ? 'Prefill' : 'First token',
          last.prefillSeconds !== null && last.prefillSeconds !== undefined ? util.formatSeconds(last.prefillSeconds) : (last.firstTokenSeconds !== null ? util.formatSeconds(last.firstTokenSeconds) : unknown()),
          last.prefillSeconds !== null ? 'Prompt processing time' : 'Time from the server accepting the turn to the first token (includes prefill and queueing)') +
        row('Total time', util.formatSeconds(last.totalSeconds)) +
        row('Tokens out', last.tokens ? util.formatTokens(last.tokens) : unknown()) +
        (last.promptTokens ? row('Tokens in', util.formatTokens(last.promptTokens)) : '') +
        (last.steps > 1 ? row('Steps', String(last.steps)) : '') +
        '</dl>';
    }
    html += contextBlock(last);
    html += '</section>';

    /* Trend */
    var speeds = history.map(function (item) { return item.tokensPerSecond; });
    var measured = speeds.filter(function (v) { return typeof v === 'number' && isFinite(v); });
    var avg = measured.length ? measured.reduce(function (a, b) { return a + b; }, 0) / measured.length : null;
    html += '<section class="stats-card" aria-labelledby="stats-trend-title"><div class="stats-card-head"><h3 id="stats-trend-title">Speed trend</h3>' +
      (avg ? '<span class="stat-source">avg ' + avg.toFixed(1) + ' tok/s</span>' : '') + '</div>' +
      sparkline(speeds) +
      '<button class="btn ghost stats-clear" type="button" data-stats-clear' + (history.length ? '' : ' disabled') + '>Clear history</button>' +
      '</section>';

    body.innerHTML = html;
  }

  function bindStats() {
    panel = byId('stats-panel');
    toggleButton = byId('stats-toggle');
    if (!panel) return;
    if (toggleButton) toggleButton.addEventListener('click', function () { toggleStats(); });
    panel.addEventListener('click', function (event) {
      if (event.target.closest('[data-stats-close]')) toggleStats(false);
      if (event.target.closest('[data-stats-clear]')) {
        history = [];
        IM.store.set(HISTORY_KEY, history);
        renderStats();
      }
    });
    IM.bus.on('status', renderStats);
    IM.bus.on('models', renderStats);
    IM.bus.on('session.active', renderStats);
    if (IM.store.get(STATS_OPEN_KEY, false) && window.innerWidth > 1100) toggleStats(true);
  }

  /* ---------------------------------------------------- command palette */

  var palette = null;
  var paletteInput = null;
  var paletteList = null;
  var paletteItems = [];
  var paletteIndex = 0;
  var releasePalette = null;

  function actions() {
    var list = [
      { kind: 'Action', label: 'New chat', hint: 'Alt+N', run: function () { IM.shell.createSession('New chat'); } },
      { kind: 'Action', label: 'Show performance and model', hint: MOD + '+.', run: function () { toggleStats(true); } },
      { kind: 'Action', label: 'Keyboard shortcuts', hint: '?', run: openShortcuts },
      { kind: 'Action', label: 'Rename current session', hint: 'F2', run: function () { IM.shell.renameActive(); } },
      { kind: 'Action', label: 'Response gallery', hint: 'preview every reply style', run: openGallery },
      { kind: 'Action', label: 'Replay the intro', run: function () { if (IM.brand) IM.brand.playIntro(); } },
      { kind: 'Page', label: 'Home', run: function () { IM.shell.setPage('home'); } },
      { kind: 'Page', label: 'Sessions', run: function () { IM.shell.setPage('sessions'); } },
      { kind: 'Page', label: 'Connect', run: function () { IM.shell.setPage('connect'); } },
      { kind: 'Page', label: 'Skills', run: function () { IM.shell.setPage('skills'); } },
      { kind: 'Page', label: 'Persona', run: function () { IM.shell.setPage('persona'); } },
      { kind: 'Settings', label: 'Permissions', hint: 'what tools may do', run: function () { IM.shell.openSettings('permissions'); } },
      { kind: 'Settings', label: 'Tools', hint: 'schemas and manual runs', run: function () { IM.shell.openSettings('tools'); } },
      { kind: 'Settings', label: 'Agent', hint: 'steps per reply', run: function () { IM.shell.openSettings('agent'); } },
      { kind: 'Settings', label: 'Search providers', hint: 'web search keys', run: function () { IM.shell.openSettings('search'); } },
      { kind: 'Settings', label: 'Connectors', hint: 'OAuth apps, connect, test, tools', run: function () { IM.shell.openSettings('connectors'); } },
      { kind: 'Settings', label: 'MCP servers', hint: 'add, connect, discovered tools', run: function () { IM.shell.openSettings('mcp'); } },
      { kind: 'Settings', label: 'Tuning', hint: 'teach the local model from your ratings', run: function () { IM.shell.openSettings('tuning'); } },
      { kind: 'Settings', label: 'Models', run: function () { IM.shell.openSettings('models'); } },
      { kind: 'Settings', label: 'Customisation', run: function () { IM.shell.openSettings('customisation'); } },
      { kind: 'Settings', label: 'Voice', run: function () { IM.shell.openSettings('voice'); } },
      { kind: 'Settings', label: 'Server settings', run: function () { IM.shell.openSettings('server'); } },
      { kind: 'Settings', label: 'Usage', run: function () { IM.shell.openSettings('usage'); } }
    ];
    if (IM.theme) {
      IM.theme.themes.forEach(function (theme) {
        list.push({ kind: 'Theme', label: theme.label, hint: theme.note, run: function () { IM.theme.set({ theme: theme.id }); } });
      });
    }
    return list;
  }

  function score(text, query) {
    var hay = text.toLowerCase();
    var q = query.toLowerCase();
    if (!q) return 1;
    var at = hay.indexOf(q);
    if (at === 0) return 100;
    if (at > 0) return 60 - Math.min(at, 50);
    /* subsequence match: "nwc" finds "new chat" */
    var i = 0;
    for (var j = 0; j < hay.length && i < q.length; j += 1) if (hay[j] === q[i]) i += 1;
    return i === q.length ? 10 : 0;
  }

  function buildItems(query) {
    var q = String(query || '').trim();
    var sessions = state.sessions.map(function (session) {
      return {
        kind: 'Session', label: session.name || 'Untitled', hint: session.id,
        score: Math.max(score(session.name || '', q), score(session.id || '', q)),
        run: function () { IM.shell.activateSession(session.id); }
      };
    });
    var rest = actions().map(function (item) {
      item.score = score(item.label + ' ' + item.kind, q);
      return item;
    });
    var all = sessions.concat(rest).filter(function (item) { return item.score > 0; });
    if (q) all.sort(function (a, b) { return b.score - a.score; });
    else all = sessions.slice(0, 6).concat(rest.slice(0, 8));
    return all.slice(0, 40);
  }

  function renderPalette() {
    paletteItems = buildItems(paletteInput.value);
    paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
    if (!paletteItems.length) {
      paletteList.innerHTML = '<li class="palette-empty" role="option" aria-disabled="true">Nothing matches. Sessions are searched by name and id.</li>';
      paletteInput.removeAttribute('aria-activedescendant');
      return;
    }
    paletteList.innerHTML = paletteItems.map(function (item, index) {
      return '<li class="palette-item' + (index === paletteIndex ? ' is-active' : '') + '" role="option" id="palette-opt-' + index + '"' +
        ' aria-selected="' + (index === paletteIndex ? 'true' : 'false') + '" data-index="' + index + '">' +
        '<span class="palette-kind">' + escapeHtml(item.kind) + '</span>' +
        '<span class="palette-label">' + escapeHtml(item.label) + '</span>' +
        (item.hint ? '<span class="palette-hint">' + escapeHtml(item.hint) + '</span>' : '') +
        '</li>';
    }).join('');
    paletteInput.setAttribute('aria-activedescendant', 'palette-opt-' + paletteIndex);
    var active = paletteList.querySelector('.is-active');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function openPalette(query) {
    if (!palette) return;
    if (palette.hidden && IM.sound) IM.sound.open();
    palette.hidden = false;
    paletteInput.value = query || '';
    paletteIndex = 0;
    renderPalette();
    releasePalette = util.trapFocus(palette);
    paletteInput.focus();
  }

  function closePalette() {
    if (!palette || palette.hidden) return;
    if (IM.sound) IM.sound.close();
    palette.hidden = true;
    if (releasePalette) {
      releasePalette();
      releasePalette = null;
    }
  }

  function runPalette(index) {
    var item = paletteItems[index];
    if (!item) return;
    closePalette();
    item.run();
  }

  function bindPalette() {
    palette = byId('palette');
    if (!palette) return;
    paletteInput = byId('palette-input');
    paletteList = byId('palette-list');
    paletteInput.addEventListener('input', function () {
      paletteIndex = 0;
      renderPalette();
    });
    paletteInput.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (!paletteItems.length) return;
        paletteIndex = (paletteIndex + (event.key === 'ArrowDown' ? 1 : -1) + paletteItems.length) % paletteItems.length;
        renderPalette();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        runPalette(paletteIndex);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closePalette();
      }
    });
    paletteList.addEventListener('click', function (event) {
      var item = event.target.closest('[data-index]');
      if (item) runPalette(Number(item.dataset.index));
    });
    palette.addEventListener('click', function (event) {
      if (event.target === palette) closePalette();
    });
    var trigger = byId('palette-toggle');
    if (trigger) trigger.addEventListener('click', function () { openPalette(); });
  }

  /* -------------------------------------------------- shortcuts dialog */

  var SHORTCUTS = [
    [MOD + ' K', 'Search sessions and commands'],
    ['Enter', 'Send the message (queues it while a reply runs)'],
    ['Shift Enter', 'New line in the message'],
    ['Esc', 'Stop the reply, or close the open dialog'],
    ['/', 'Focus the message box'],
    ['Alt N', 'New chat'],
    [MOD + ' B', 'Show or hide the sidebar'],
    [MOD + ' .', 'Performance and model panel'],
    ['Alt 1 – 7', 'Home, Chat, Sessions, Connect, Skills, Persona, Settings'],
    ['F2', 'Rename the focused session in the sidebar'],
    ['Delete', 'Delete the focused session in the sidebar'],
    ['?', 'This list']
  ];

  var shortcuts = null;
  var releaseShortcuts = null;

  function openShortcuts() {
    if (!shortcuts) return;
    var list = shortcuts.querySelector('.shortcut-list');
    if (list && !list.children.length) {
      list.innerHTML = SHORTCUTS.map(function (pair) {
        var keys = pair[0].split(' ').map(function (key) { return '<kbd>' + escapeHtml(key) + '</kbd>'; }).join('');
        return '<div class="shortcut-row"><dt>' + keys + '</dt><dd>' + escapeHtml(pair[1]) + '</dd></div>';
      }).join('');
    }
    if (shortcuts.hidden && IM.sound) IM.sound.open();
    shortcuts.hidden = false;
    releaseShortcuts = util.trapFocus(shortcuts);
    var close = shortcuts.querySelector('[data-shortcuts-close]');
    if (close) close.focus();
  }

  function closeShortcuts() {
    if (!shortcuts || shortcuts.hidden) return;
    if (IM.sound) IM.sound.close();
    shortcuts.hidden = true;
    if (releaseShortcuts) {
      releaseShortcuts();
      releaseShortcuts = null;
    }
  }

  function bindShortcutsDialog() {
    shortcuts = byId('shortcuts');
    if (!shortcuts) return;
    shortcuts.addEventListener('click', function (event) {
      if (event.target === shortcuts || event.target.closest('[data-shortcuts-close]')) closeShortcuts();
    });
    shortcuts.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeShortcuts();
      }
    });
    var trigger = byId('shortcuts-toggle');
    if (trigger) trigger.addEventListener('click', openShortcuts);
  }

  /* ------------------------------------------------ sidebar session filter */

  function bindSessionFilter() {
    var input = byId('session-filter');
    if (!input) return;
    function apply() {
      var q = input.value.trim().toLowerCase();
      var visible = 0;
      document.querySelectorAll('#sidebar-sessions .session').forEach(function (row) {
        var hay = ((row.dataset.sessionName || '') + ' ' + (row.dataset.sessionId || '')).toLowerCase();
        var show = !q || hay.indexOf(q) !== -1;
        row.hidden = !show;
        if (show) visible += 1;
      });
      var none = byId('session-filter-empty');
      if (none) none.hidden = !q || visible > 0;
    }
    input.addEventListener('input', apply);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && input.value) {
        event.preventDefault();
        event.stopPropagation();
        input.value = '';
        apply();
      } else if (event.key === 'ArrowDown') {
        var first = document.querySelector('#sidebar-sessions .session:not([hidden])');
        if (first) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    IM.bus.on('sessions', function (sessions) {
      apply();
      var wrap = input.closest('.session-filter');
      if (wrap) wrap.hidden = !sessions || sessions.length < 4;
    });
  }

  /* ---------------------------------------------------- global shortcuts */

  var PAGES = ['home', 'chat', 'sessions', 'connect', 'skills', 'persona', 'settings'];

  function anyDialogOpen() {
    return ['palette', 'shortcuts', 'rename'].some(function (id) {
      var node = byId(id);
      return node && !node.hidden;
    });
  }

  document.addEventListener('keydown', function (event) {
    var mod = event.ctrlKey || event.metaKey;
    var key = event.key;
    var typing = util.isTypingTarget(document.activeElement);

    if (mod && !event.altKey && (key === 'k' || key === 'K')) {
      event.preventDefault();
      if (palette && !palette.hidden) closePalette();
      else openPalette();
      return;
    }
    if (mod && key === '.') {
      event.preventDefault();
      toggleStats();
      return;
    }
    if (mod && !event.shiftKey && (key === 'b' || key === 'B') && !(typing && document.activeElement.isContentEditable)) {
      event.preventDefault();
      IM.shell.toggleSidebar();
      return;
    }
    if (mod && key === '/') {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (event.altKey && !mod && (key === 'n' || key === 'N' || event.code === 'KeyN')) {
      event.preventDefault();
      IM.shell.createSession('New chat');
      return;
    }
    if (event.altKey && !mod && /^Digit[1-7]$/.test(event.code || '')) {
      event.preventDefault();
      IM.shell.setPage(PAGES[Number(event.code.slice(5)) - 1]);
      return;
    }
    if (typing || mod || event.altKey || anyDialogOpen()) return;
    if (key === '?') {
      event.preventDefault();
      openShortcuts();
    } else if (key === '/') {
      event.preventDefault();
      if (state.activeSessionId && IM.shell.currentPage() !== 'chat') IM.shell.setPage('chat');
      if (IM.shell.currentPage() === 'chat' && IM.chat) IM.chat.focusComposer();
      else {
        var home = byId('home-prompt');
        if (home && IM.shell.currentPage() === 'home') home.focus();
      }
    }
  });

  /* ---------------------------------------------------- response gallery */

  /* Every kind of reply the chat can show, rendered by the real chat code
   * from canned frames (no API calls, nothing saved). Open with
   * ?demo=responses or "Response gallery" in the command palette. */
  var FENCE = '```';
  var RICH = [
    '# Release checklist',
    'Here is what changed and what to verify. **Bold**, *italic*, ~~struck~~, `inline code` and a link to https://example.com/docs?x=1&y=2.',
    '',
    '## Steps',
    '1. Pull the branch',
    '2. Run the tests',
    '   - unit tests',
    '   - end-to-end tests',
    '3. Tag the release',
    '',
    '- [x] Changelog written',
    '- [ ] Screenshots updated',
    '',
    '| Check | Owner | Time (s) |',
    '|:------|:-----:|--------:|',
    '| Build | CI | 182 |',
    '| Lint, with a much longer cell that wraps \\| escapes | Dana | 9 |',
    '',
    '> Keep the release notes short; link the full diff.',
    '',
    '> [!WARNING]',
    '> The migration rewrites the sessions table. Back it up first.',
    '',
    '---',
    '',
    '$$',
    'T_{total} = T_{prefill} + n \\cdot T_{token}',
    '$$',
    '',
    '![Throughput chart](https://example.com/chart.png)'
  ].join('\n');

  var CODE = [
    'Two ways to do it:',
    '',
    FENCE + 'python',
    'def tokens_per_second(tokens: int, seconds: float) -> float:',
    '    """Decode speed, guarding the empty case."""',
    '    return tokens / seconds if seconds else 0.0',
    FENCE,
    '',
    FENCE + 'c++',
    'for (int i = 0; i < n; ++i) { out[i] = std::fma(a[i], b[i], c[i]); } // one very long line that has to scroll sideways instead of breaking the layout of the reply bubble',
    FENCE,
    '',
    FENCE + 'json',
    '{ "turn_id": "t_42", "status": "done", "steps": 2 }',
    FENCE
  ].join('\n');

  /* Agentic states: live search, tool rows, an approval card, sub-agents,
   * skills, the "Used" footer, verified / not verified, sources. Frames have
   * the exact shape services/server publishes (doc/README.md). */
  function agentGallery(demo, label, nodes) {
    var T = 'demo';
    function call(id, tool, args, extra) {
      return Object.assign({ type: 'tool.call', turn_id: T, call_id: id, tool: tool, args: args, depth: 0, step: 1, source: 'model' }, extra || {});
    }
    function result(id, tool, ok, extra) {
      return Object.assign({ type: 'tool.result', turn_id: T, call_id: id, tool: tool, ok: ok, decision: 'allow', depth: 0, step: 1, latency_ms: 640 }, extra || {});
    }
    var SOURCES = [
      { title: 'Python 3.14.0 release notes', url: 'https://docs.python.org/3/whatsnew/3.14.html', domain: 'docs.python.org', provider: 'brave',
        snippet: 'Python 3.14 is the latest stable release, with template strings, deferred annotations and a free-threaded build.' },
      { title: 'Python Release Python 3.14.0', url: 'https://www.python.org/downloads/release/python-3140/', domain: 'python.org', provider: 'brave',
        snippet: 'Download installers for Windows, macOS and source tarballs.' },
      { title: 'What is new in CPython', url: 'https://realpython.com/python314-new-features/', domain: 'realpython.com', provider: 'brave' }
    ];

    label('Searching the web right now (the query, then "Found N sources")');
    nodes.push(demo.turn({ state: 'searching', search: { query: 'latest Python release' } }));

    label('Live tool activity: one row per call, updated by its result');
    nodes.push(demo.turn({
      state: 'thinking', kinds: ['tool'],
      steps: ['<think>The question is about the present, so I should search.</think>\n\nUsing `web_search` {"query": "latest Python release"}'],
      events: [call('c1', 'web_search', { query: 'latest Python release' })]
    }));

    label('Approval needed: inline card with countdown (Enter approves, Esc denies)');
    nodes.push(demo.turn({
      state: 'thinking', kinds: ['tool'],
      steps: ['I will run the test suite to check the change.\n\nUsing `shell` {"command": "python -m pytest tests -q"}'],
      events: [call('c2', 'shell', { command: 'python -m pytest tests -q' }),
        { type: 'tool.approval_needed', turn_id: T, call_id: 'c2', approval_id: 'demo', tool: 'shell', expires_in: 142 }],
      approval: { approval_id: 'demo', tool: 'shell', args: { command: 'python -m pytest tests -q', timeout: 120 }, reasons: ['runs a command on this computer'], expires_in: 142 }
    }));

    label('Connector approval: email preview, "Gmail wants to send an email", Send button, no "remember" (always asks)');
    nodes.push(demo.turn({
      state: 'thinking', kinds: ['tool'],
      steps: ['I will send the summary to Priya.\n\nUsing `gmail_send` {"to": "priya@example.com"}'],
      events: [call('g1', 'gmail_send', { to: 'priya@example.com', subject: 'Release notes' }),
        { type: 'tool.approval_needed', turn_id: T, call_id: 'g1', approval_id: 'demo-mail', tool: 'gmail_send', expires_in: 160 }],
      approval: { approval_id: 'demo-mail', tool: 'gmail_send', expires_in: 160,
        args: { to: 'priya@example.com', cc: 'team@example.com', subject: 'Release notes 0.3', body: 'Hi Priya,\n\nThe 0.3 release adds a fused attention kernel and a q8 KV cache.\n\nThanks' },
        reasons: ['sends an email to priya@example.com with subject "Release notes 0.3"; sent mail cannot be unsent'] }
    }));

    label('Finished agent reply: ok, failed and denied calls, a sub-agent, a skill, "Used" footer, verified with sources');
    nodes.push(demo.turn({
      kinds: ['tool', 'answer'],
      steps: [
        '<think>Search first, then read the release page, then save a note.</think>\n\nUsing `web_search` {"query": "latest Python release"}',
        'The latest stable release is **Python 3.14.0** [note: unable to verify the exact date]. It adds template strings and a free-threaded build.'
      ],
      events: [
        { type: 'skill.used', turn_id: T, skills: ['release-notes'] },
        call('a1', 'web_search', { query: 'latest Python release' }),
        result('a1', 'web_search', true, { latency_ms: 812, content: '1. Python 3.14.0 release notes - docs.python.org\n2. Python Release Python 3.14.0 - python.org' }),
        call('a2', 'web_fetch', { url: 'https://example.invalid/python' }),
        result('a2', 'web_fetch', false, { latency_ms: 8000, error: 'FAILED: the page did not answer within 8 s. Do not guess what the tool would have returned.' }),
        call('a3', 'file_write', { path: 'notes/python.md', content: '# Python 3.14' }),
        { type: 'tool.approval_needed', turn_id: T, call_id: 'a3', approval_id: 'demo-2', tool: 'file_write' },
        { type: 'tool.denied', turn_id: T, call_id: 'a3', approval_id: 'demo-2', tool: 'file_write', reason: 'declined by the user' },
        result('a3', 'file_write', false, { decision: 'deny', error: 'Permission denied for file_write: declined by the user' }),
        call('a4', 'run_agent', { task: 'Summarise the changelog in notes/changelog.md', tools: ['file_read'] }),
        { type: 'agent.start', turn_id: T, depth: 1, task: 'Summarise the changelog in notes/changelog.md', tools: ['file_read'], max_steps: 4 },
        call('b1', 'file_read', { path: 'notes/changelog.md' }, { depth: 1, source: 'agent:1' }),
        result('b1', 'file_read', true, { depth: 1, source: 'agent:1', latency_ms: 3, content: '## 0.3\n- fused attention kernel\n- q8 KV cache' }),
        { type: 'agent.end', turn_id: T, depth: 1, status: 'done', steps: 2, tool_calls: 1, elapsed: 4.1, summary: 'Two changes: a fused attention kernel and a q8 KV cache.' },
        result('a4', 'run_agent', true, { latency_ms: 4120, content: 'Sub-agent result (done, 2 steps, 1 tool calls):\nTwo changes: a fused attention kernel and a q8 KV cache.' })
      ],
      end: { tool_calls: 4, skills: ['release-notes'], verification: 'verified', sources: SOURCES }
    }));

    label('Not verified live: no source was reachable (the model\'s bracketed note is removed)');
    nodes.push(demo.turn({
      steps: ['As far as I know the latest release is Python 3.13. [unverified]'],
      events: [call('n1', 'web_search', { query: 'latest Python release' }),
        result('n1', 'web_search', false, { latency_ms: 1200, error: 'DuckDuckGo refused the automated search (bot check), so the web could not be searched.' })],
      end: { tool_calls: 1, verification: 'unverified' }
    }));
  }

  function galleryNodes() {
    var demo = IM.chat.demo;
    var nodes = [];
    function label(text) {
      var node = document.createElement('div');
      node.className = 'gallery-label';
      node.textContent = text;
      nodes.push(node);
    }
    label('Short answer');
    nodes.push(demo.user('What is the capital of Australia?'));
    nodes.push(demo.turn({ steps: ['Canberra. Sydney is the largest city, but Canberra has been the capital since 1913.'] }));

    label('Rich Markdown: headings, nested and task lists, aligned table, quote, callout, rule, formula, image');
    nodes.push(demo.user('Give me the release checklist.'));
    nodes.push(demo.turn({ steps: [RICH] }));

    label('Code blocks: copy buttons, long lines scroll, any language tag');
    nodes.push(demo.turn({ steps: [CODE] }));

    label('Reasoning model: thought process folded above the answer');
    nodes.push(demo.turn({ steps: ['<think>The user wants a quick estimate. 7B at q4 is about 4.1 GB of weights; KV cache at 8k context adds roughly 1 GB.</think>About **5 GB** of RAM for a 7B model at q4 with an 8k context.'] }));

    agentGallery(demo, label, nodes);
    if (IM.research && IM.research.gallery) IM.research.gallery(demo, label, nodes);

    label('Thinking right now (reasoning streams into the panel)');
    nodes.push(demo.turn({ state: 'thinking', steps: ['<think>Comparing the two quantisations. q8 keeps more precision in the attention weights'] }));

    label('Streaming: the drop caret marks the live end of the text');
    nodes.push(demo.turn({ state: 'streaming', steps: ['The model loads once and stays resident, so the second reply starts much faster than the'] }));

    label('Prompt queued while a reply runs');
    nodes.push(demo.user('Also include the Windows steps.', { queued: true }));

    label('Stopped by the user');
    nodes.push(demo.turn({ status: 'stopped', steps: ['Here are the first three steps:\n\n1. Install'] }));

    label('Recovered after the live stream dropped');
    nodes.push(demo.turn({ recovered: true, steps: ['The answer arrived through the turn record after the event stream reconnected.'] }));

    label('Empty reply');
    nodes.push(demo.turn({ steps: [''] }));

    label('Engine still provisioning');
    nodes.push(demo.turn({ state: 'error', error: {
      title: 'The engine is still provisioning',
      text: 'It did not become ready within three minutes. Check Settings → Server, or retry once it reports online.',
      actions: [{ label: 'Retry', primary: true, onClick: function () {} }, { label: 'Open Server settings', onClick: function () { IM.shell.openSettings('server'); } }]
    } }));

    label('No model downloaded');
    nodes.push(demo.turn({ state: 'error', error: {
      title: 'No model downloaded',
      text: 'The engine needs a model before it can answer. Download one, then send your message again.',
      actions: [{ label: 'Open Models', primary: true, onClick: function () { IM.shell.openSettings('models'); } }]
    } }));

    if (IM.chat.chooseModelState) {
      label('No model selected (POST /api/turns answered 503)');
      var choose = IM.chat.chooseModelState(function () {});
      nodes.push(demo.turn({ state: 'error', error: choose }));
    }

    label('Failed mid-answer: partial text is kept');
    nodes.push(demo.turn({ state: 'error', steps: ['The first part of the answer made it through before'], error: { title: 'The reply failed', text: 'RuntimeError: the engine closed the connection.' } }));

    label('Markup in model output is shown as text, never run');
    nodes.push(demo.turn({ steps: ['<script>alert("x")</script> and <b>tags</b> stay literal. A very long unbroken token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'] }));
    return nodes;
  }

  function openGallery() {
    if (!IM.chat || !IM.chat.demo) return;
    state.activeSessionId = null;
    IM.shell.setPage('chat', { force: true });
    IM.chat.demo.mount(galleryNodes());
    var crumb = byId('page-crumb');
    if (crumb) crumb.textContent = 'Response gallery (preview only, nothing is sent)';
  }

  function init() {
    bindStats();
    bindPalette();
    bindShortcutsDialog();
    bindSessionFilter();
    var demoParam = null;
    try { demoParam = new URLSearchParams(location.search).get('demo'); } catch (error) { demoParam = null; }
    if (demoParam === 'responses') setTimeout(openGallery, 0);
  }

  IM.panels = {
    toggleStats: toggleStats,
    openPalette: openPalette,
    closePalette: closePalette,
    openShortcuts: openShortcuts,
    openGallery: openGallery
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
