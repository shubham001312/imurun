/* Hardware card (Settings > Resource manager) and the topbar chip
 * "GPU 16/16 · 85 tok/s".
 *
 * Data: GET /api/resources -> {..., governor: <view>} (also accepted bare),
 * the `resources` SSE event (the governor view as the frame), and the last
 * turn's engine stats (bus `turn.stats`) for tok/s. Presets are written with
 * PUT /api/settings/resources {headroom, dynamic} through __imurun_settings.
 * Every field is optional; a 404 (older server) renders an honest
 * "not reported" card. Polls every 10 s only while the card is visible.
 * ES2017. See docs/frontend/README.md "Hardware card".
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util || {};
  var esc = util.escapeHtml || function (s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  };
  var POLL_MS = 10000;
  var PRESETS = [['gentle', 'Gentle'], ['balanced', 'Balanced'], ['performance', 'Performance'], ['custom', 'Custom']];
  var PRESET_NOTE = {
    gentle: 'Twice the headroom: the machine stays snappy for you.',
    balanced: 'Leaves 20-30% of CPU, RAM and VRAM free for your own use.',
    performance: 'Half the headroom: faster replies, a busier machine.',
    custom: 'Your own headroom percentages (set in the settings file).'
  };

  var view = null;          /* last governor view */
  var missing = false;      /* endpoint 404 / unreachable */
  var lastTps = null;
  var saving = false;

  function byId(id) { return document.getElementById(id); }
  function num(v) {
    var n = Number(v);
    return v === null || v === undefined || v === '' || !isFinite(n) ? null : n;
  }
  function bytes(v) {
    var n = num(v);
    if (n === null) return '';
    if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB';
    return Math.round(n / 1e6) + ' MB';
  }
  function pick(obj, path) {
    return path.split('.').reduce(function (o, k) { return o && typeof o === 'object' ? o[k] : undefined; }, obj);
  }

  /* ------------------------------------------------------------- derive */

  function derive(v) {
    var d = {};
    var engine = pick(v, 'imurun.engine') || {};
    var machine = v.machine || {};
    var budget = v.budget || {};
    d.gpuName = engine.gpu || machine.gpu || pick(v, 'live.vram.name') || '';
    d.vramTotal = num(machine.vramTotal) || num(budget.vram_total) || num(pick(v, 'live.vram.total')) || 0;
    d.vramBudget = num(budget.vram_budget);
    d.gpuBytes = num(engine.gpuBytes);
    d.gpuLayers = num(engine.gpuLayers);
    d.ramLayers = num(engine.ramLayers);
    d.diskLayers = num(engine.diskLayers);
    d.layers = (d.gpuLayers || 0) + (d.ramLayers || 0) + (d.diskLayers || 0);
    d.loaded = d.gpuLayers !== null && d.layers > 0;
    d.hasGpu = Boolean(d.gpuName) || d.vramTotal > 0;
    d.threads = num(engine.threads) || num(pick(v, 'imurun.engineThreads')) || num(pick(v, 'imurun.threadTarget'));
    d.physical = num(machine.physicalCores) || num(budget.physical_cores);
    d.headroomCores = num(budget.headroom_cores);
    d.ramBudget = num(budget.ram_budget);
    d.ramTotal = num(machine.ramTotal) || num(budget.ram_total);
    d.state = v.state || '';
    d.reason = v.reason || '';
    d.preset = v.preset || '';
    d.dynamic = v.dynamic;
    d.mix = engine.mix || '';
    return d;
  }

  function chipText() {
    if (!view) return '';
    var d = derive(view);
    var tps = lastTps ? Math.round(lastTps) + ' tok/s' : '';
    var head;
    if (d.loaded && d.gpuLayers > 0) head = 'GPU ' + d.gpuLayers + '/' + d.layers;
    else if (d.loaded) head = 'CPU' + (d.threads ? ' · ' + d.threads + ' threads' : '');
    else return d.hasGpu ? 'GPU ready' : 'CPU';
    return head + (tps ? ' · ' + tps : '');
  }

  function renderChip() {
    var chip = byId('hw-chip');
    if (!chip) return;
    var text = chipText();
    chip.hidden = !text;
    if (!text) return;
    var d = derive(view);
    chip.textContent = text;
    chip.title = (d.gpuName ? d.gpuName + '. ' : 'No GPU detected. ') + 'Open Settings > Resource manager for details.';
    chip.classList.toggle('is-gpu', Boolean(d.loaded && d.gpuLayers > 0));
    chip.classList.toggle('is-yield', d.state === 'yielding');
  }

  /* --------------------------------------------------------------- card */

  function meterRow(label, value, pctOf, note) {
    var pct = pctOf === null ? null : Math.max(0, Math.min(100, pctOf));
    return '<div class="hw-row"><div class="resource-head"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>' +
      (pct === null ? '' : '<div class="meter"><span data-hw-w="' + pct.toFixed(1) + '"></span></div>') +
      (note ? '<span class="muted">' + esc(note) + '</span>' : '') + '</div>';
  }

  function line(k, v) {
    return '<div class="settings-line"><span class="k">' + esc(k) + '</span><span class="v">' + v + '</span></div>';
  }

  function render() {
    var root = byId('hardware-root');
    if (!root) return;
    if (!view) {
      root.innerHTML = '<div class="panel settings-card"><h3 class="settings-card-title">Hardware</h3><p class="muted">' +
        (missing ? 'This server does not report hardware yet (no governor data from /api/resources). Update IMURUN to see GPU, threads and headroom here.' : 'Loading…') + '</p></div>';
      return;
    }
    var d = derive(view);
    var html = '<div class="panel settings-card"><h3 class="settings-card-title">Hardware</h3>';

    if (d.hasGpu) {
      var vendor = /nvidia|geforce|rtx|gtx/i.test(d.gpuName) ? 'NVIDIA' : /amd|radeon/i.test(d.gpuName) ? 'AMD' : /intel|arc|iris/i.test(d.gpuName) ? 'Intel' : '';
      html += line('GPU', esc(d.gpuName || 'GPU') + (vendor && d.gpuName.indexOf(vendor) === -1 ? ' <span class="chip neutral">' + vendor + '</span>' : ''));
    } else {
      html += line('GPU', 'No GPU detected: running on CPU');
    }

    if (d.vramTotal > 0) {
      var used = d.gpuBytes;
      var vtxt = (used !== null ? bytes(used) + ' by IMURUN' : 'no model loaded') +
        (d.vramBudget ? ' · budget ' + bytes(d.vramBudget) : '') + ' · ' + bytes(d.vramTotal) + ' total';
      html += meterRow('VRAM', used !== null ? bytes(used) : '—', used !== null ? used / d.vramTotal * 100 : 0, vtxt);
    }

    if (d.loaded) {
      var place = d.gpuLayers + ' of ' + d.layers + ' layers on GPU';
      var rest = [];
      if (d.ramLayers) rest.push(d.ramLayers + ' in RAM');
      if (d.diskLayers) rest.push(d.diskLayers + ' on disk');
      if (rest.length) place += ', ' + rest.join(', ');
      html += meterRow('Model placement', place, d.gpuLayers / d.layers * 100, d.mix);
    } else {
      html += line('Model placement', '<span class="muted">No model loaded yet</span>');
    }

    if (d.threads !== null || d.physical !== null) {
      var t = (d.threads !== null ? d.threads : '—') + ' of ' + (d.physical !== null ? d.physical : '—') + ' physical cores';
      if (d.headroomCores !== null) t += ' · ' + d.headroomCores + ' kept free for you';
      html += line('CPU threads', esc(t));
    }
    if (d.ramBudget) html += line('RAM budget', esc(bytes(d.ramBudget) + (d.ramTotal ? ' of ' + bytes(d.ramTotal) : '')));

    var gov;
    if (d.state === 'yielding') gov = '<span class="chip error">lowered</span> ' + esc(d.reason || 'the machine is busy');
    else if (d.state === 'normal' && d.reason) gov = '<span class="chip success">raised back</span> ' + esc(d.reason);
    else if (d.state) gov = '<span class="chip success">normal</span>';
    else gov = '<span class="chip neutral">unknown</span>';
    html += line('Governor', gov);

    html += '<div class="hw-preset"><div class="settings-label">Headroom preset</div><div class="hw-seg" role="radiogroup" aria-label="Headroom preset">' +
      PRESETS.map(function (p) {
        var on = p[0] === d.preset;
        var off = saving || (p[0] === 'custom' && !on);
        return '<button type="button" class="btn' + (on ? ' primary' : '') + '" role="radio" aria-checked="' + on +
          '" data-hw-preset="' + p[0] + '"' + (off ? ' disabled' : '') + '>' + p[1] + '</button>';
      }).join('') + '</div><span class="muted">' + esc(PRESET_NOTE[d.preset] || '') + '</span></div>';

    if (typeof d.dynamic === 'boolean') {
      html += '<label class="toggle-row"><span><strong>Lower threads while the machine is busy</strong><small>The governor gives cores back to you and raises them again when you stop.</small></span>' +
        '<input type="checkbox" data-hw-dynamic' + (d.dynamic ? ' checked' : '') + '></label>';
    }
    html += '</div>';
    root.innerHTML = html;
    root.querySelectorAll('[data-hw-w]').forEach(function (el) { el.style.width = el.getAttribute('data-hw-w') + '%'; });
  }

  /* --------------------------------------------------------------- data */

  function accept(payload) {
    if (!payload || typeof payload !== 'object') return;
    var v = payload.governor && typeof payload.governor === 'object' ? payload.governor : payload;
    if (!v.budget && !v.machine && !v.preset) return;
    view = v;
    missing = false;
    render();
    renderChip();
  }

  function refresh() {
    var api = window.__imurun_api;
    if (!api || !api.getResources) return Promise.resolve();
    return api.getResources().then(accept, function () {
      if (!view) { missing = true; render(); }
    });
  }

  function visible() {
    var root = byId('hardware-root');
    return Boolean(root && root.offsetParent !== null && document.visibilityState === 'visible');
  }

  function save(partial) {
    var s = window.__imurun_settings;
    if (!s) return;
    saving = true;
    s.set({ resources: partial });
    render();
    s.save('resources').then(function () { saving = false; return refresh(); }, function () { saving = false; render(); });
  }

  function bind() {
    var root = byId('hardware-root');
    if (!root || root.dataset.bound === '1') return;
    root.dataset.bound = '1';
    root.addEventListener('click', function (event) {
      var b = event.target.closest('[data-hw-preset]');
      if (b && !b.disabled) save({ headroom: b.getAttribute('data-hw-preset') });
    });
    root.addEventListener('change', function (event) {
      if (event.target.matches('[data-hw-dynamic]')) save({ dynamic: event.target.checked });
    });
  }

  function init() {
    bind();
    render();
    if (IM.bus) {
      IM.bus.on('server:resources', accept);
      IM.bus.on('turn.stats', function (s) {
        if (s && s.source === 'engine' && s.tokensPerSecond) { lastTps = s.tokensPerSecond; renderChip(); }
      });
    }
    var chip = byId('hw-chip');
    if (chip) {
      chip.addEventListener('click', function () {
        var nav = document.querySelector('.nav-item[data-page="settings"]');
        var tab = document.querySelector('.settings-tab[data-settings-section="resources"]');
        if (nav) nav.click();
        if (tab) tab.click();
      });
    }
    refresh();
    /* Poll only while the card is on screen; the SSE event keeps the chip live. */
    setInterval(function () { if (visible()) refresh(); }, POLL_MS);
  }

  window.__imurun_hardware = { refresh: refresh, accept: accept };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
