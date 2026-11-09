/* The Performance panel's content (topbar "Stats", #stats-panel). panels.js
 * owns opening, closing and the reply history; this file turns the status,
 * the selected model and that history into four groups:
 *
 *   Model       name; size, quantisation, architecture; where it runs
 *   Last reply  speed, time to first token, tokens (three stat tiles)
 *   Context     a meter: used of the window
 *   Speed trend a sparkline of tokens per second over recent replies
 *
 * Rules: only values the server or the browser actually reported are shown
 * (nothing says "not reported"); raw keys such as `loaded: false` become
 * words; empty states are one line each, and with no model there is one
 * call to action ("Choose a model" -> Settings > Models). Colours come from
 * the theme variables only (styles/stats.css).
 *
 *   IMURUN.statsView.html(ctx) -> string
 *   ctx = {status, engineState, selected, modelsDownloaded, history, last, settings, transcriptChars}
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  function util() { return IM.util || {}; }
  function esc(text) { return util().escapeHtml(String(text)); }
  function tokens(n) { return util().formatTokens(n); }
  function seconds(n) { return util().formatSeconds(n); }
  function num(value) { var n = Number(value); return isFinite(n) ? n : null; }
  function has(value) { return value !== undefined && value !== null && value !== ''; }

  /* engine.model / engine.last_generation are the current names; info and
   * model_info are older ones some compute paths may still send. */
  function engineParts(status) {
    var engine = (status && status.engine) || {};
    var model = engine.model && typeof engine.model === 'object' ? engine.model : {};
    var info = Object.assign({}, engine.info || {}, engine.model_info || {}, model);
    var gen = engine.last_generation && typeof engine.last_generation === 'object' ? engine.last_generation : {};
    return { engine: engine, info: info, gen: gen, placement: gen.placement || {} };
  }

  function sizeLabel(selected) {
    if (!selected) return '';
    if (selected.size_gb) return Number(selected.size_gb).toFixed(1) + ' GB';
    if (selected.size_bytes) return util().formatBytes(selected.size_bytes);
    return '';
  }

  function quantLabel(info, selected) {
    if (has(info.quant)) return String(info.quant);
    var tags = selected && Array.isArray(selected.tags) ? selected.tags : [];
    return tags.filter(function (t) { return /^q\d|^f16|^bf16|^iq\d/i.test(t); })[0] || '';
  }

  var ARCH_NAMES = { llama: 'Llama', qwen2: 'Qwen 2', qwen3: 'Qwen 3', phi3: 'Phi-3', gemma2: 'Gemma 2' };

  function detail(label, value) {
    return '<div class="sv-row"><dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd></div>';
  }

  /* Where it runs, from last_generation.placement (the residency planner)
   * and threads; rows only for what was reported. */
  function placementRows(parts) {
    var engine = parts.engine;
    var p = parts.placement;
    if (engine.remote) return detail('Runs on', 'A hosted provider');
    if (!engine.loaded) return '';
    var gpu = num(p.gpu_layers) || 0;
    var ram = num(p.ram_layers) || 0;
    var disk = num(p.disk_layers) || 0;
    var total = (gpu + ram + disk) || num(parts.info.layers) || 0;
    var threads = num(parts.gen.threads);
    var rows = detail('Runs on', gpu > 0 ? (total && gpu >= total ? 'GPU' : 'GPU and CPU') : 'CPU');
    if (gpu > 0 && p.gpu) rows += detail('GPU', String(p.gpu).replace(/\s*\((vulkan|cuda|metal)\)\s*$/i, ''));
    if (total) {
      var bits = [];
      if (gpu > 0) bits.push(gpu + ' of ' + total + ' on the GPU');
      if (ram > 0) bits.push(ram + (gpu ? '' : ' of ' + total) + ' on the CPU');
      if (disk > 0) bits.push(disk + ' read from disk');
      if (!bits.length || (!gpu && !disk)) bits = ['All ' + total + ' on the CPU'];
      rows += detail('Layers', bits.join(', '));
    }
    if (threads) rows += detail('CPU threads', String(threads));
    return rows;
  }

  /* --------------------------------------------------------------- model */

  function modelSection(ctx, parts) {
    var status = ctx.status;
    var selected = ctx.selected;
    var info = parts.info;
    var engine = parts.engine;
    var remoteName = engine.remote && (info.name || info.id);
    var name = (status && status.selected_model) || remoteName || (selected && (selected.name || selected.filename)) || '';
    var pill = {
      ready: ['ready', name ? 'Ready' : 'No model'],
      provisioning: ['provisioning', name ? 'Starting' : 'No model'],
      unreachable: ['unreachable', 'Offline']
    }[ctx.engineState] || ['provisioning', 'Starting'];
    if (ctx.engineState === 'ready' && !name) pill = ['idle', 'No model'];

    var html = '<section class="sv-card" aria-labelledby="sv-model-title">' +
      '<div class="sv-head"><h3 id="sv-model-title">Model</h3>' +
      '<span class="sv-pill" data-state="' + pill[0] + '">' + esc(pill[1]) + '</span></div>';

    if (!name && ctx.engineState === 'unreachable') {
      return html + '<p class="sv-empty-title">Not connected</p>' +
        '<p class="sv-note">The local server is not answering. Restart IMURUN if this lasts.</p></section>';
    }
    if (!name) {
      var noFiles = ctx.modelsDownloaded === 0;
      html += '<p class="sv-empty-title">' + (noFiles ? 'No model downloaded' : 'No model loaded') + '</p>' +
        '<p class="sv-note">' + (noFiles
          ? 'Download a model to start chatting on this computer.'
          : 'Pick one of your downloaded models to start chatting.') + '</p>' +
        '<button class="btn primary sv-cta" type="button" data-stats-models>Choose a model</button>';
      return html + '</section>';
    }

    var facts = [sizeLabel(selected), quantLabel(info, selected), ARCH_NAMES[info.arch] || info.arch || '']
      .filter(Boolean);
    html += '<p class="sv-model-name">' + esc(name) + '</p>';
    if (facts.length) html += '<p class="sv-model-facts">' + esc(facts.join(' · ')) + '</p>';

    var rows = placementRows(parts);
    var weights = num(parts.gen.weights_bytes);
    if (weights) rows += detail('Memory for weights', util().formatBytes(weights));
    if (rows) html += '<dl class="sv-list">' + rows + '</dl>';

    if (ctx.engineState === 'unreachable') {
      html += '<p class="sv-note">The local server is not answering. Numbers below are from earlier replies.</p>';
    } else if (!engine.loaded && !engine.remote) {
      html += '<p class="sv-note">Not in memory yet. It loads when you send a message.</p>';
    }
    return html + '</section>';
  }

  /* ---------------------------------------------------------- last reply */

  function tile(label, value, unit, hint) {
    return '<div class="sv-tile"' + (hint ? ' title="' + esc(hint) + '"' : '') + '>' +
      '<dt>' + esc(label) + '</dt>' +
      '<dd><span class="sv-tile-value">' + esc(value) + '</span>' + (unit ? '<span class="sv-tile-unit">' + esc(unit) + '</span>' : '') + '</dd></div>';
  }

  function splitSeconds(value) {
    var text = seconds(value);
    var at = text.lastIndexOf(' ');
    return at > 0 ? [text.slice(0, at), text.slice(at + 1)] : [text, ''];
  }

  function lastReplySection(last) {
    var html = '<section class="sv-card" aria-labelledby="sv-reply-title"><div class="sv-head"><h3 id="sv-reply-title">Last reply</h3>' +
      (last ? '<span class="sv-source">' + (last.source === 'engine' ? 'from the engine' : 'measured in this window') + '</span>' : '') + '</div>';
    if (!last) return html + '<p class="sv-note">No replies yet. Speed and timing show up after the first answer.</p></section>';

    var tiles = '';
    var speed = num(last.tokensPerSecond);
    tiles += tile('Speed', speed ? speed.toFixed(1) : '—', speed ? 'tok/s' : '',
      speed ? (last.source === 'engine' ? 'Decode speed reported by the engine' : 'Streamed chunks per second after the first one') : 'Too short to measure');
    var usePrefill = has(last.prefillSeconds);
    var first = usePrefill ? last.prefillSeconds : last.firstTokenSeconds;
    if (has(first)) {
      var parts = splitSeconds(first);
      tiles += tile(usePrefill ? 'Prompt read' : 'First token', parts[0], parts[1],
        usePrefill ? 'Time the engine spent reading the prompt' : 'From sending to the first word (includes reading the prompt and waiting)');
    }
    if (num(last.tokens)) tiles += tile('Tokens', tokens(last.tokens), 'out', last.promptTokens ? util().formatTokens(last.promptTokens) + ' tokens in' : '');
    var extra = [];
    if (has(last.totalSeconds)) extra.push(seconds(last.totalSeconds) + ' in total');
    if (num(last.promptTokens)) extra.push(tokens(last.promptTokens) + ' tokens read');
    if (last.steps > 1) extra.push(last.steps + ' steps');
    return html + '<dl class="sv-tiles">' + tiles + '</dl>' +
      (extra.length ? '<p class="sv-note">' + esc(extra.join(' · ')) + '</p>' : '') + '</section>';
  }

  /* ------------------------------------------------------------- context */

  function full(n) { return Math.round(Number(n) || 0).toLocaleString(); }

  /* Three different numbers, never mixed up:
   *   in use    engine.model.context (the n_ctx the engine loaded with: the
   *             smallest of the request, the model maximum and what fits in
   *             memory), else last_generation.context_size;
   *   maximum   engine.model.context_train (the GGUF context_length);
   *   saved     settings server.contextSize (Settings > Engine). The server
   *             does not pass it to the engine yet (services/config.py reads
   *             FISH_CONTEXT_SIZE), so it is shown as saved, never as real.
   * engine.model is only reported while the model is in memory, so with no
   * model loaded no context number is shown at all.
   * Usage: exact when this session's last reply carried engine counters,
   * otherwise an estimate from the visible transcript (~4 chars per token). */
  function contextSection(ctx, parts) {
    var engine = parts.engine;
    var info = parts.info;
    var html = '<section class="sv-card" aria-labelledby="sv-context-title"><div class="sv-head"><h3 id="sv-context-title">Context</h3>';
    var inUse = (engine.loaded || engine.remote) ? (num(info.context) || num(parts.gen.context_size)) : null;
    if (!inUse) {
      return html + '</div><p class="sv-note">How much of the model\'s memory this chat uses shows here once a model is loaded.</p></section>';
    }
    var maximum = num(info.context_train);
    var saved = num(ctx.settings && ctx.settings.server && ctx.settings.server.contextSize);
    var own = ctx.sessionLast;
    var used = own && num(own.contextUsed);
    var exact = !!(used && num(own.contextSize));
    if (!exact) used = Math.round((ctx.transcriptChars || 0) / 4);
    used = Math.min(used, inUse);
    html += (used ? '<span class="sv-source">' + (exact ? 'from the engine' : 'estimate') + '</span>' : '') + '</div>';

    var pct = Math.max(0, Math.min(100, (used / inUse) * 100));
    var level = pct >= 90 ? 'high' : (pct >= 70 ? 'mid' : 'low');
    html += '<div class="sv-meter-head"><span><strong>' + esc(full(used)) + '</strong> of ' + esc(full(inUse)) + ' tokens used</span>' +
      '<span>' + Math.round(pct) + '%</span></div>' +
      '<div class="sv-meter" data-level="' + level + '" role="meter" aria-valuemin="0" aria-valuemax="' + inUse + '" aria-valuenow="' + used + '" ' +
        'aria-valuetext="' + esc(full(used) + ' of ' + full(inUse) + ' tokens used in this chat') + '" aria-label="Context used by this chat">' +
        '<span class="sv-meter-fill" data-pct="' + pct.toFixed(1) + '"></span></div>';
    var usage;
    if (!used) usage = 'This chat is empty.';
    else if (level === 'high') usage = 'Nearly full: the oldest messages are dropped next. A new chat starts fresh.';
    else usage = 'Room for about ' + full(inUse - used) + ' more tokens in this chat.';
    html += '<p class="sv-note">' + esc(usage) + '</p>';

    var rows = detail('Window in use', full(inUse) + ' tokens');
    if (maximum) rows += detail('Model maximum', full(maximum) + ' tokens');
    if (saved && !engine.remote) rows += detail('Saved in Settings', full(saved) + ' tokens');
    html += '<dl class="sv-list">' + rows + '</dl>';

    var notes = [];
    if (maximum && inUse < maximum) {
      notes.push('The model supports up to ' + full(maximum) + ' tokens; this run uses ' + full(inUse) + '.');
    }
    if (saved && saved !== inUse && !engine.remote) {
      notes.push('The saved size (' + full(saved) + ') is used when the model next loads; the engine may use less to fit this computer\'s memory or the model\'s maximum.');
    }
    if (notes.length) {
      html += '<p class="sv-note">' + esc(notes.join(' ')) +
        ' <button class="sv-link" type="button" data-stats-engine>Settings → Engine</button></p>';
    }
    return html + '</section>';
  }

  /* --------------------------------------------------------- speed trend */

  /* Single series, so no legend: the title names it. Line in the muted ink,
   * the latest reply as an accent dot with a surface ring; each reply has a
   * hit area wider than the mark with a native tooltip. */
  function sparkline(points) {
    var w = 280;
    var h = 56;
    var pad = 5;
    var max = Math.max.apply(null, points);
    var min = Math.min.apply(null, points);
    var lo = Math.max(0, min - (max - min) * 0.25);
    var span = (max - lo) || max || 1;
    var step = (w - pad * 2) / (points.length - 1);
    var xy = points.map(function (v, i) {
      return [pad + i * step, h - pad - ((v - lo) / span) * (h - pad * 2)];
    });
    var line = xy.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
    var lastPt = xy[xy.length - 1];
    var hits = xy.map(function (p, i) {
      return '<rect class="sv-spark-hit" x="' + Math.max(0, p[0] - step / 2).toFixed(1) + '" y="0" width="' + Math.max(step, 8).toFixed(1) + '" height="' + h + '">' +
        '<title>Reply ' + (i + 1) + ' of ' + points.length + ': ' + points[i].toFixed(1) + ' tok/s</title></rect>';
    }).join('');
    return '<svg class="sv-spark" viewBox="0 0 ' + w + ' ' + h + '" role="img" ' +
      'aria-label="Tokens per second over the last ' + points.length + ' replies: lowest ' + min.toFixed(1) + ', highest ' + max.toFixed(1) +
      ', latest ' + points[points.length - 1].toFixed(1) + '">' +
      '<polyline class="sv-spark-line" points="' + line + '"/>' +
      '<circle class="sv-spark-dot" cx="' + lastPt[0].toFixed(1) + '" cy="' + lastPt[1].toFixed(1) + '" r="4"/>' +
      hits + '</svg>';
  }

  function trendSection(history) {
    var points = (history || []).map(function (item) { return num(item && item.tokensPerSecond); })
      .filter(function (v) { return v !== null && v > 0; });
    var html = '<section class="sv-card" aria-labelledby="sv-trend-title"><div class="sv-head"><h3 id="sv-trend-title">Speed trend</h3>';
    if (points.length < 2) {
      return html + '</div><p class="sv-note">Send a few messages to see how speed changes from reply to reply.</p>' +
        ((history || []).length ? '<button class="btn ghost sv-clear" type="button" data-stats-clear>Clear history</button>' : '') + '</section>';
    }
    var avg = points.reduce(function (a, b) { return a + b; }, 0) / points.length;
    var latest = points[points.length - 1];
    html += '<span class="sv-source">last ' + points.length + ' replies</span></div>' +
      sparkline(points) +
      '<dl class="sv-trend-stats">' +
        '<div><dt>Latest</dt><dd>' + latest.toFixed(1) + ' tok/s</dd></div>' +
        '<div><dt>Average</dt><dd>' + avg.toFixed(1) + ' tok/s</dd></div>' +
        '<div><dt>Best</dt><dd>' + Math.max.apply(null, points).toFixed(1) + ' tok/s</dd></div>' +
      '</dl>' +
      '<button class="btn ghost sv-clear" type="button" data-stats-clear>Clear history</button>';
    return html + '</section>';
  }

  function html(ctx) {
    var parts = engineParts(ctx.status);
    return modelSection(ctx, parts) + lastReplySection(ctx.last) + contextSection(ctx, parts) + trendSection(ctx.history);
  }

  /* Inline style attributes are allowed by the CSP, but widths are applied
   * through the CSSOM so the markup stays free of them. */
  function paint(root) {
    Array.prototype.forEach.call(root.querySelectorAll('.sv-meter-fill[data-pct]'), function (fill) {
      fill.style.width = fill.getAttribute('data-pct') + '%';
    });
  }

  IM.statsView = { html: html, paint: paint };
})();
