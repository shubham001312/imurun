/* Settings > Tuning, and the "Use it" card: the local tuning channel
 * (services/tuning, docs/tuning/README.md).
 *
 *   GET  /api/tuning/status          stack, plan for the target model, samples,
 *                                    feedback counts, active/last job, setup
 *   GET/POST /api/tuning/settings    automation and training options, hf_token
 *                                    (write-only: "••••••••" when set)
 *   GET  /api/tuning/plan/{model}    feasibility for another model
 *   POST /api/tuning/setup           install the training stack (~4.9 GB venv)
 *   POST /api/tuning/jobs            Train now: 409 {needs: confirm | allow_slow,
 *                                    plan, message} is answered only after the
 *                                    user clicks; 400 not_enough_samples /
 *                                    not_feasible; 503 training_stack_missing
 *   POST /api/tuning/jobs/{id}/cancel
 *   GET  /api/tuning/jobs, /jobs/{id}  history and per-run metrics
 *   events: tuning.setup (progress), tuning.job (state machine), tuning.ready
 *           ("Use it": POST /api/models/select {id}, never automatic)
 *
 * Loaded when the section is first shown; the "Use it" card works anywhere.
 * ES2017 (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var esc = util.escapeHtml;

  function api() { return window.__imurun_api; }
  function byId(id) { return document.getElementById(id); }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function fail(title, error) {
    toast({ tone: 'error', icon: 'alert', kicker: 'Tuning', title: title, text: (error && error.message) || 'The server did not answer.' });
  }

  var STATES = ['queued', 'preparing', 'training', 'evaluating', 'exporting', 'done'];
  var TERMINAL = { done: 1, failed: 1, rejected: 1, cancelled: 1 };
  var AUTO = [
    ['off', 'Off: only when I press Train now'],
    ['nightly', 'Nightly, at a set hour'],
    ['when-idle', 'When the app has been idle'],
    ['every-N-approved-samples', 'Every N approved samples']
  ];
  var QUANTS = ['Q4_K_M', 'Q5_K_M', 'Q6_K', 'Q8_0', 'F16'];
  var DROP_TEXT = {
    tool_call: 'used a tool (the answer depends on its output)', live_data: 'live data (news, prices, "current" facts)',
    refusal: 'a refusal', disclaimer: 'a knowledge-cutoff disclaimer', too_short: 'too short', pii: 'personal data (PII filter)',
    duplicate: 'duplicate', already_trained: 'already trained'
  };
  var VERDICT = { ok: ['success', 'fits'], slow: ['warn', 'slow'], impossible: ['error', 'refused'] };

  var state = { bound: false, status: null, settings: null, job: null, jobs: [], plan: null, planFor: '', setup: null, pending: null, metrics: {} };

  function root() { return byId('tuning-root'); }

  function gb(v) { var n = Number(v); return isFinite(n) && n > 0 ? (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + ' GB' : ''; }
  function hours(v) {
    var n = Number(v);
    if (!isFinite(n) || n <= 0) return '';
    if (n < 1) return Math.max(1, Math.round(n * 60)) + ' min';
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' h';
  }
  function num(v, digits) { var n = Number(v); return v === null || v === undefined || !isFinite(n) ? '–' : n.toFixed(digits === undefined ? 3 : digits); }
  function when(ts) { var n = Number(ts); return n > 0 ? new Date(n * 1000).toLocaleString() : ''; }

  /* ---------------------------------------------------------------- shell */

  function build(el) {
    el.innerHTML =
      '<div class="panel settings-card tune-honest">' +
        '<h3 class="settings-card-title">What fine-tuning can and cannot do</h3>' +
        '<ul class="tune-list">' +
          '<li><strong>Yes:</strong> your style, format, language and conventions; dropping a bad habit (over-long answers, the "as of my knowledge cutoff" disclaimer) once you rate or teach a few dozen to a few hundred examples.</li>' +
          '<li><strong>No:</strong> facts about people, current events, prices, anything that changes. Weights are a frozen snapshot, so such turns are filtered out; the research tools look those up at question time and cite them.</li>' +
          '<li><strong>Hardware:</strong> 1B to 3B models train on this laptop\'s GPU. An 8B needs a GPU with at least 6 GB; here it would fall back to the CPU and is refused for any real dataset.</li>' +
          '<li><strong>Privacy:</strong> only replies you rated are used; feedback stays next to your transcripts; nothing is uploaded. Downloads: the training stack, the original model weights, and the llama.cpp converter.</li>' +
        '</ul>' +
      '</div>' +
      '<div class="panel settings-card" data-tune="stack"></div>' +
      '<div class="panel settings-card" data-tune="plan"></div>' +
      '<div class="panel settings-card" data-tune="samples"></div>' +
      '<div class="panel settings-card" data-tune="job"></div>' +
      '<form class="panel settings-card tune-form" data-tune="settings" autocomplete="off" novalidate></form>' +
      '<div class="panel settings-card" data-tune="history"></div>';
  }

  function part(name) { var el = root(); return el ? el.querySelector('[data-tune="' + name + '"]') : null; }

  /* ---------------------------------------------------------------- stack */

  function renderStack() {
    var box = part('stack');
    if (!box) return;
    var s = state.status || {};
    var stack = s.stack || {};
    var setup = state.setup || s.setup || {};
    var running = Boolean(setup.running);
    var html = '<h3 class="settings-card-title">Training stack</h3>';
    if (stack.installed) {
      var caps = stack.caps || {};
      var bits = [];
      if (caps.cuda !== undefined) bits.push(caps.cuda ? 'CUDA ✓' : 'no CUDA');
      if (caps.bitsandbytes !== undefined || caps.bnb !== undefined) bits.push((caps.bitsandbytes || caps.bnb) ? '4-bit (bitsandbytes) ✓' : 'no 4-bit');
      html += '<p class="tune-ok">Installed' + (bits.length ? ' · ' + esc(bits.join(' · ')) : '') + '.</p>' +
        (stack.message ? '<p class="settings-note">' + esc(stack.message) + '</p>' : '');
    } else {
      html += '<p class="settings-note">' + esc(stack.message || 'Not installed.') + '</p>' +
        '<p class="settings-note">A separate Python environment for training (PyTorch, transformers, PEFT; measured <strong>≈4.9 GB</strong> on disk, about 9 minutes here) plus the llama.cpp converter (≈66 MB download). The app\'s own environment is not touched.</p>' +
        '<button class="btn primary" type="button" data-tune-install' + (running ? ' disabled' : '') + '>' + (running ? 'Installing…' : 'Install training stack (~4.9 GB)') + '</button>';
    }
    if (running || setup.error || (setup.ok === false)) {
      var pct = Math.max(0, Math.min(100, Number(setup.percent) || 0));
      html += '<div class="tune-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(pct) + '" aria-label="Installing the training stack"><span style="width:' + pct + '%"></span></div>' +
        '<p class="tune-progress-text">' + esc([setup.stage, setup.message].filter(Boolean).join(': ')) + (pct ? ' · ' + Math.round(pct) + ' %' : '') +
        (setup.venv_bytes ? ' · ' + gb(Number(setup.venv_bytes) / 1073741824) + ' so far' : '') + '</p>' +
        (setup.error ? '<p class="tune-error">' + esc(setup.error) + '</p>' : '');
    }
    box.innerHTML = html;
  }

  /* ----------------------------------------------------------------- plan */

  function renderPlan() {
    var box = part('plan');
    if (!box) return;
    var s = state.status || {};
    var plan = state.plan || s.plan || null;
    var target = state.planFor || s.target_model || s.selected_model || '';
    /* The tuning target and the model last looked at are always offered,
     * then the catalogue. */
    var names = {};
    var ids = [];
    function addId(id, name) { if (id && !names[id]) { names[id] = name || id; ids.push(id); } }
    addId(s.target_model, (s.target_model && s.target_model === s.selected_model) ? s.target_model + ' (selected)' : s.target_model);
    addId(state.planFor);
    ((IM.state && IM.state.models) || []).forEach(function (m) { if (m && m.id) addId(m.id, m.name); });
    var options = ids.map(function (id) {
      return '<option value="' + esc(id) + '"' + (id === target ? ' selected' : '') + '>' + esc(names[id]) + '</option>';
    }).join('');
    var html = '<h3 class="settings-card-title">Can this machine tune it?</h3>' +
      '<label class="form-field tune-model"><span class="k">Model</span><select data-tune-planmodel>' + options + '</select></label>';
    if (s.trainable === false && !state.plan) {
      html += '<p class="tune-error">No trainable original is known for ' + esc(target || 'the selected model') + '.</p>';
    } else if (!plan) {
      html += '<p class="settings-note">No plan yet.</p>';
    } else {
      var v = VERDICT[plan.verdict] || ['neutral', plan.verdict || '?'];
      var rows = [];
      if (plan.method) rows.push(['Method', plan.method]);
      if (plan.need_gb) rows.push(['Memory', gb(plan.need_gb) + ' needed / ' + gb(plan.have_gb) + ' available' + (/gpu/.test(plan.method || '') ? ' (GPU)' : ' (RAM)')]);
      if (plan.est_total_hours) rows.push(['Estimated time', hours(plan.est_total_hours) + (plan.tok_s_measured ? ' (from a measured run)' : ' (first guess until a run is measured)')]);
      if (plan.download_gb) rows.push(['First run downloads', gb(plan.download_gb) + ' of original weights']);
      if (plan.disk_need_gb) rows.push(['Disk', gb(plan.disk_need_gb) + ' needed / ' + gb(plan.disk_free_gb) + ' free']);
      html += '<p class="tune-verdict"><span class="chip ' + v[0] + '">' + esc(v[1]) + '</span> ' + esc(plan.reason || '') + '</p>' +
        '<dl class="tune-facts">' + rows.map(function (r) { return '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>'; }).join('') + '</dl>' +
        ((plan.warnings || []).length ? '<ul class="tune-warnings">' + plan.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '');
    }
    box.innerHTML = html;
  }

  /* -------------------------------------------------------------- samples */

  function renderSamples() {
    var box = part('samples');
    if (!box) return;
    var s = state.status || {};
    var samples = s.samples || {};
    var fb = s.feedback || {};
    var usable = Number(samples.usable) || 0;
    var need = Number(samples.min_samples) || Number((state.settings || {}).min_samples) || 200;
    var pct = Math.min(100, Math.round(usable / need * 100));
    var dropped = samples.dropped && typeof samples.dropped === 'object' ? samples.dropped : {};
    var drops = Object.keys(dropped).filter(function (k) { return Number(dropped[k]) > 0; });
    box.innerHTML = '<h3 class="settings-card-title">Approved samples</h3>' +
      '<p class="tune-count"><strong>' + usable + '</strong> of ' + need + ' needed for a run</p>' +
      '<div class="tune-progress" role="progressbar" aria-valuemin="0" aria-valuemax="' + need + '" aria-valuenow="' + usable + '" aria-label="Approved samples"><span style="width:' + pct + '%"></span></div>' +
      '<p class="settings-note">Your ratings: ' + (Number(fb.up) || 0) + ' 👍 · ' + (Number(fb.teach) || 0) + ' taught · ' + (Number(fb.down) || 0) + ' 👎 (never trained on). ' +
        'Rate replies with the buttons under each answer; files in <code>~/.imurun/tuning/datasets/</code> count too.</p>' +
      (drops.length ? '<details class="tune-drops"><summary>Skipped: ' + drops.reduce(function (a, k) { return a + Number(dropped[k]); }, 0) + ' turns</summary><ul>' +
        drops.map(function (k) { return '<li>' + Number(dropped[k]) + ' · ' + esc(DROP_TEXT[k] || k) + '</li>'; }).join('') + '</ul></details>' : '');
  }

  /* ------------------------------------------------------------------ job */

  function stepper(job) {
    var st = job.state;
    var failedAt = TERMINAL[st] && st !== 'done' ? (job.failed_state || '') : '';
    var reached = STATES.indexOf(st);
    return '<ol class="tune-steps">' + STATES.map(function (name, i) {
      var cls = name === st ? 'is-current' : (reached > i || st === 'done' ? 'is-done' : '');
      if (st === 'done' && name === 'done') cls = 'is-done';
      return '<li class="' + cls + '">' + esc(name) + '</li>';
    }).join('') + (TERMINAL[st] && st !== 'done' ? '<li class="is-end is-' + esc(st) + '">' + esc(st) + (failedAt ? ' at ' + esc(failedAt) : '') + '</li>' : '') + '</ol>';
  }

  function renderJob() {
    var box = part('job');
    if (!box) return;
    var s = state.status || {};
    var job = state.job || s.active_job || null;
    var html = '<div class="tune-head"><h3 class="settings-card-title">Current run</h3>' +
      '<button class="btn primary" type="button" data-tune-train' + (job && !TERMINAL[job.state] ? ' disabled' : '') + '>Train now</button></div>';
    if (state.pending) {
      var p = state.pending;
      html += '<div class="tune-needs" role="alert"><p>' + esc(p.message || 'The server needs your confirmation.') + '</p>' +
        (p.plan && p.plan.reason ? '<p class="settings-note">' + esc(p.plan.reason) + '</p>' : '') +
        '<div class="tune-needs-actions">' +
        (p.needs.indexOf('confirm') !== -1 ? '<button class="btn primary" type="button" data-tune-confirm>Download ' + esc(gb(p.plan && p.plan.download_gb) || 'the weights') + ' and train</button>' : '') +
        (p.needs.indexOf('allow_slow') !== -1 ? '<button class="btn" type="button" data-tune-allowslow>Train anyway (' + esc(hours(p.plan && p.plan.est_total_hours) || 'slow') + ')</button>' : '') +
        '<button class="btn ghost" type="button" data-tune-dismiss>Not now</button></div></div>';
    }
    if (!job) {
      html += '<p class="settings-note">No run yet.</p>';
    } else {
      var live = !TERMINAL[job.state];
      var pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
      var facts = [];
      if (job.step || job.max_steps) facts.push('step ' + (job.step || 0) + (job.max_steps ? ' / ' + job.max_steps : ''));
      if (job.loss !== null && job.loss !== undefined) facts.push('loss ' + num(job.loss));
      if (job.base_eval_loss !== null && job.base_eval_loss !== undefined) {
        facts.push(job.eval_loss === null || job.eval_loss === undefined ? 'held-out loss before ' + num(job.base_eval_loss)
          : 'held-out ' + num(job.base_eval_loss) + ' → ' + num(job.eval_loss));
      }
      if (live && job.eta_s) facts.push('about ' + util.formatSeconds(Number(job.eta_s)) + ' left');
      html += '<p class="tune-job-title"><code>' + esc(job.id || '') + '</code> · ' + esc(job.model_id || '') + (job.trigger && job.trigger !== 'manual' ? ' · ' + esc(job.trigger) : '') +
        (job.paused ? ' <span class="chip warn">paused while you chat</span>' : '') + '</p>' +
        stepper(job) +
        '<div class="tune-progress' + (live ? ' is-live' : '') + '" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(pct) + '" aria-label="Run progress"><span style="width:' + pct + '%"></span></div>' +
        '<p class="tune-progress-text">' + esc(job.message || job.state) + (facts.length ? ' · ' + esc(facts.join(' · ')) : '') + '</p>' +
        (job.error ? '<p class="tune-error">' + esc(job.error) + '</p>' : '') +
        (live ? '<button class="btn ghost danger" type="button" data-tune-cancel="' + esc(job.id) + '">Cancel run</button>' : '');
    }
    box.innerHTML = html;
  }

  /* ------------------------------------------------------------- settings */

  function renderSettings() {
    var form = part('settings');
    if (!form || !state.settings) return;
    if (form.contains(document.activeElement)) return;   /* never rebuild under the user */
    var t = state.settings;
    var opt = function (list, value) {
      return list.map(function (o) { var v = Array.isArray(o) ? o[0] : o; var l = Array.isArray(o) ? o[1] : o;
        return '<option value="' + esc(v) + '"' + (String(value) === String(v) ? ' selected' : '') + '>' + esc(l) + '</option>'; }).join('');
    };
    var check = function (key, title, note) {
      return '<label class="toggle-row"><span><strong>' + esc(title) + '</strong>' + (note ? '<small>' + esc(note) + '</small>' : '') + '</span>' +
        '<input type="checkbox" data-tune-set="' + key + '"' + (t[key] ? ' checked' : '') + '></label>';
    };
    var number = function (key, label, min, max, step) {
      return '<label class="form-field"><span class="k">' + esc(label) + '</span><input type="number" data-tune-set="' + key + '" min="' + min + '" max="' + max + '"' +
        (step ? ' step="' + step + '"' : '') + ' value="' + esc(String(t[key] === undefined ? '' : t[key])) + '"></label>';
    };
    form.innerHTML = '<h3 class="settings-card-title">Automation and training</h3>' +
      check('enabled', 'Automatic runs', 'Master switch for the schedule below. "Train now" works either way.') +
      '<label class="form-field"><span class="k">When</span><select data-tune-set="auto">' + opt(AUTO, t.auto) + '</select></label>' +
      '<div class="form-grid">' +
        number('min_samples', 'N: approved samples per run', 1, 100000) +
        number('nightly_hour', 'Nightly at (hour, 0-23)', 0, 23) +
        number('idle_minutes', 'Idle for (minutes)', 1, 1440) +
        number('max_hours', 'Longest run (hours)', 0.1, 168, 'any') +
        number('epochs', 'Epochs (1-3)', 1, 3) +
        number('max_seq_len', 'Tokens per sample', 64, 4096, 64) +
        '<label class="form-field"><span class="k">Result quantisation</span><select data-tune-set="quant">' + opt(QUANTS, t.quant) + '</select></label>' +
      '</div>' +
      check('allow_8b_slow', 'Let automatic runs take slow plans', 'Off by default: a slow plan always waits for your click.') +
      check('auto_download', 'Let automatic runs download base weights', 'Otherwise the first run of each model is started by hand, after the size is shown.') +
      check('include_pii', 'Keep turns with personal data', 'Off by default: e-mail addresses, phone and card numbers, keys and tokens are filtered out.') +
      '<div class="search-key"><label class="form-field"><span class="k">Hugging Face token <span class="chip ' + (state.hfSet ? 'success' : 'neutral') + '">' + (state.hfSet ? 'saved' : 'not set') + '</span></span>' +
        '<input type="password" data-tune-hf autocomplete="new-password" spellcheck="false" placeholder="' + (state.hfSet ? 'Saved (hidden). Type to replace.' : 'Only for licence-gated weights') + '"></label>' +
        '<small class="tool-field-hint">Write-only: stored by the local server, never shown again, never kept in this browser.</small></div>' +
      '<div><button class="btn primary" type="submit">Save</button></div>';
  }

  function collectSettings(form) {
    var out = {};
    var t = state.settings || {};
    Array.prototype.forEach.call(form.querySelectorAll('[data-tune-set]'), function (input) {
      var key = input.getAttribute('data-tune-set');
      var value = input.type === 'checkbox' ? input.checked : (input.type === 'number' ? Number(input.value) : input.value);
      if (input.type === 'number' && (input.value === '' || !isFinite(value))) return;
      if (String(value) !== String(t[key])) out[key] = value;
    });
    var hf = form.querySelector('[data-tune-hf]');
    if (hf && hf.value.trim()) out.hf_token = hf.value.trim();
    return out;
  }

  /* -------------------------------------------------------------- history */

  function renderHistory() {
    var box = part('history');
    if (!box) return;
    var jobs = (state.jobs || []).filter(function (j) { return TERMINAL[j.state]; });
    box.innerHTML = '<h3 class="settings-card-title">Run history</h3>' + (jobs.length ? '<ul class="tune-history">' + jobs.map(function (j) {
      var r = j.result || {};
      var base = r.base_eval_loss !== undefined ? r.base_eval_loss : j.base_eval_loss;
      var after = r.eval_loss !== undefined ? r.eval_loss : j.eval_loss;
      var sanity = r.sanity_total ? r.sanity_passed + '/' + r.sanity_total + ' sanity' : '';
      var metrics = state.metrics[j.id];
      return '<li class="tune-run is-' + esc(j.state) + '"><details data-tune-run="' + esc(j.id) + '"><summary>' +
        '<span class="chip ' + (j.state === 'done' ? 'success' : (j.state === 'cancelled' ? 'neutral' : 'error')) + '">' + esc(j.state) + '</span> ' +
        '<code>' + esc(j.id) + '</code> · ' + esc(j.model_id || '') + ' · ' + esc(when(j.finished_at || j.created_at)) +
        (base !== undefined && base !== null ? ' · held-out ' + num(base) + ' → ' + num(after) : '') + (sanity ? ' · ' + esc(sanity) : '') + '</summary>' +
        (j.error ? '<p class="tune-error">' + esc(j.error) + '</p>' : '') +
        (j.new_model_id ? '<p class="settings-note">Result: <code>' + esc(j.new_model_id) + '</code> <button class="btn ghost" type="button" data-tune-use="' + esc(j.new_model_id) + '">Use it</button></p>' : '') +
        (metrics ? metricsTable(metrics) : '<p class="settings-note">Open to load the metrics.</p>') +
        '</details></li>';
    }).join('') + '</ul>' : '<p class="settings-note">No finished runs yet.</p>');
  }

  /* Loss per eval point and the last training losses, as numbers. */
  function metricsTable(rows) {
    if (!rows.length) return '<p class="settings-note">No metrics were recorded.</p>';
    var evals = rows.filter(function (m) { return m.eval_loss !== undefined && m.eval_loss !== null; });
    var losses = rows.filter(function (m) { return m.loss !== undefined && m.loss !== null; });
    var first = losses[0], last = losses[losses.length - 1];
    return '<dl class="tune-facts">' +
      (first ? '<dt>Training loss</dt><dd>' + num(first.loss) + ' (step ' + (first.step || 0) + ') → ' + num(last.loss) + ' (step ' + (last.step || 0) + ')</dd>' : '') +
      (evals.length ? '<dt>Held-out loss</dt><dd>' + evals.map(function (m) { return num(m.eval_loss) + (m.step !== undefined ? ' @' + m.step : ''); }).join(' · ') + '</dd>' : '') +
      '<dt>Points</dt><dd>' + rows.length + '</dd></dl>';
  }

  function renderAll() {
    renderStack(); renderPlan(); renderSamples(); renderJob(); renderSettings(); renderHistory();
  }

  /* ---------------------------------------------------------------- load */

  function load() {
    var el = root();
    if (!el || !api()) return;
    if (!state.bound) { build(el); bind(el); state.bound = true; }
    var a = api();
    Promise.all([
      a.tuningStatus().then(function (s) { state.status = s || {}; }, function (error) { state.error = error; }),
      a.tuningSettings().then(function (v) { state.settings = (v && v.tuning) || {}; state.hfSet = Boolean(v && v.hf_token); }, function () {}),
      a.tuningJobs().then(function (jobs) { state.jobs = jobs; }, function () {})
    ]).then(function () {
      if (state.error && !state.status) {
        el.innerHTML = '<div class="empty-state"><h3>The tuning channel is not available</h3><p>' + esc(state.error.message || 'The server did not answer.') +
          '</p><button class="btn" type="button" data-tune-refresh>Try again</button></div>';
        state.bound = false;
        state.error = null;
        return;
      }
      if (state.status && state.status.active_job) state.job = state.status.active_job;
      renderAll();
    });
  }

  /* ------------------------------------------------------------- actions */

  function install(button) {
    button.disabled = true;
    button.textContent = 'Starting…';
    api().startTuningSetup({}).then(function (r) {
      state.setup = (r && r.setup) || { running: true, stage: 'starting', percent: 0 };
      renderStack();
    }, function (error) {
      if (error && error.status === 409) { state.setup = (error.body && error.body.setup) || { running: true }; renderStack(); return; }
      fail('Could not start the install', error);
      renderStack();
    });
  }

  function train(extra) {
    var body = Object.assign({}, extra || {});
    var target = state.planFor;
    if (target) body.model_id = target;
    api().startTuningJob(body).then(function (r) {
      state.pending = null;
      state.job = (r && r.job) || null;
      renderJob();
      toast({ tone: 'neutral', icon: 'info', kicker: 'Tuning', title: 'Run queued', text: 'Progress shows here; training pauses while you chat.' });
    }, function (error) {
      var b = (error && error.body) || {};
      if (error && error.status === 409 && Array.isArray(b.needs)) {
        /* the server asks first: answer only after the user clicks */
        state.pending = { needs: b.needs, plan: b.plan || {}, message: b.message, sent: body };
        renderJob();
        return;
      }
      state.pending = null;
      renderJob();
      var why = b.error === 'not_enough_samples' ? 'Not enough approved samples yet. ' + (b.message || '')
        : b.error === 'not_feasible' ? 'This machine cannot tune that model. ' + (b.message || (b.plan && b.plan.reason) || '')
        : b.error === 'training_stack_missing' ? 'Install the training stack first.'
        : (error && error.message);
      toast({ tone: 'error', icon: 'alert', kicker: 'Tuning', title: 'Not started', text: why });
    });
  }

  function cancel(id) {
    if (!window.confirm('Cancel this run? Nothing is exported and the samples stay available for the next run.')) return;
    api().cancelTuningJob(id).then(function (r) {
      if (r && r.job) state.job = r.job;
      renderJob();
    }, function (error) { fail('Could not cancel', error); });
  }

  function saveSettings(form) {
    var changes = collectSettings(form);
    if (!Object.keys(changes).length) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Tuning', title: 'Nothing changed', text: '' });
      return;
    }
    api().saveTuningSettings(changes).then(function (v) {
      state.settings = (v && v.tuning) || Object.assign({}, state.settings, changes);
      state.hfSet = Boolean(v && v.hf_token) || state.hfSet;
      var hf = form.querySelector('[data-tune-hf]');
      if (hf) hf.value = '';
      if (document.activeElement && form.contains(document.activeElement)) document.activeElement.blur();
      renderSettings();
      toast({ tone: 'success', icon: 'check', kicker: 'Tuning', title: 'Saved', text: 'Stored by the local server.' });
      api().tuningStatus().then(function (s) { state.status = s || state.status; renderPlan(); renderSamples(); }, function () {});
    }, function (error) { fail('Not saved', error); });
  }

  function planFor(modelId) {
    state.planFor = modelId;
    api().tuningPlan(modelId).then(function (r) {
      state.plan = (r && r.plan) || null;
      renderPlan();
    }, function (error) {
      state.plan = { verdict: 'impossible', reason: (error && error.message) || 'No plan.' };
      renderPlan();
    });
  }

  function loadMetrics(id) {
    if (state.metrics[id]) return;
    api().tuningJob(id).then(function (r) {
      state.metrics[id] = (r && r.metrics) || [];
      var open = root().querySelector('[data-tune-run="' + id + '"]');
      renderHistory();
      var again = root().querySelector('[data-tune-run="' + id + '"]');
      if (open && again) again.open = true;
    }, function () { state.metrics[id] = []; });
  }

  function bind(el) {
    el.addEventListener('click', function (event) {
      var t = event.target.closest('button');
      if (!t) return;
      if (t.hasAttribute('data-tune-install')) install(t);
      else if (t.hasAttribute('data-tune-train')) train({});
      else if (t.hasAttribute('data-tune-confirm')) train(Object.assign({}, state.pending && state.pending.sent, { confirm: true }));
      else if (t.hasAttribute('data-tune-allowslow')) train(Object.assign({}, state.pending && state.pending.sent, { allow_slow: true }));
      else if (t.hasAttribute('data-tune-dismiss')) { state.pending = null; renderJob(); }
      else if (t.hasAttribute('data-tune-cancel')) cancel(t.getAttribute('data-tune-cancel'));
      else if (t.hasAttribute('data-tune-use')) useModel(t.getAttribute('data-tune-use'), t);
    });
    el.addEventListener('submit', function (event) {
      if (!event.target.matches('[data-tune="settings"]')) return;
      event.preventDefault();
      saveSettings(event.target);
    });
    el.addEventListener('change', function (event) {
      if (event.target.matches('[data-tune-planmodel]')) planFor(event.target.value);
    });
    el.addEventListener('toggle', function (event) {
      var d = event.target;
      if (d.matches && d.matches('[data-tune-run]') && d.open) loadMetrics(d.getAttribute('data-tune-run'));
    }, true);
  }

  /* ------------------------------------------------------- "Use it" card */

  function useModel(id, button) {
    if (!id || !api()) return;
    if (button) { button.disabled = true; button.textContent = 'Switching…'; }
    api().selectModel(id).then(function () {
      toast({ id: 'tuning-ready', tone: 'success', icon: 'check', kicker: 'Model', title: 'Now using the tuned model', text: id + '. Switch back the same way from Models.' });
      if (IM.models && IM.models.refresh) IM.models.refresh();
    }, function (error) {
      if (button) { button.disabled = false; button.textContent = 'Use it'; }
      fail('Could not switch', error);
    });
  }

  function onReady(event) {
    var id = String(event.model_id || (event.action && event.action.body && event.action.body.id) || '');
    if (!id) return;
    var ev = event.eval || {};
    var bits = [];
    if (ev.base_eval_loss !== undefined && ev.eval_loss !== undefined) bits.push('held-out loss ' + num(ev.base_eval_loss) + ' → ' + num(ev.eval_loss));
    if (ev.sanity_total) bits.push(ev.sanity_passed + '/' + ev.sanity_total + ' sanity checks');
    if (ev.samples) bits.push(ev.samples + ' samples');
    var size = event.size_bytes || event.bytes;
    /* Only the documented action is honoured: POST /api/models/select {id}. */
    var action = event.action || {};
    var ok = !action.path || action.path === '/api/models/select';
    toast({ id: 'tuning-ready', tone: 'success', icon: 'check', kicker: 'Tuned model ready',
      title: event.name || id,
      text: [event.quant, size ? gb(Number(size) / 1073741824) : '', bits.join(' · ')].filter(Boolean).join(' · ') + '. It is in Models; nothing switches until you choose.',
      timeout: 0,
      actions: ok ? [{ label: action.label || 'Use it', className: 'primary', onClick: function () { useModel(id); } }] : [] });
    if (part('history')) api().tuningJobs().then(function (jobs) { state.jobs = jobs; renderHistory(); }, function () {});
  }

  function init() {
    IM.bus.on('settings.section', function (section) { if (section === 'tuning') load(); });
    IM.bus.on('server:tuning.setup', function (event) {
      state.setup = event;
      if (event && event.ok && state.status) { state.status.stack = Object.assign({}, state.status.stack, { installed: true }); }
      renderStack();
      if (event && event.ok === true) toast({ tone: 'success', icon: 'check', kicker: 'Tuning', title: 'Training stack installed', text: event.message || '' });
    });
    IM.bus.on('server:tuning.job', function (event) {
      state.job = event;
      if (TERMINAL[event.state]) {
        api().tuningJobs().then(function (jobs) { state.jobs = jobs; renderHistory(); }, function () {});
        api().tuningStatus().then(function (s) { state.status = s || state.status; renderSamples(); }, function () {});
      }
      renderJob();
    });
    IM.bus.on('server:tuning.ready', onReady);
    IM.bus.on('server:tuning.feedback', function () {
      if (!part('samples')) return;
      api().tuningStatus().then(function (s) { state.status = s || state.status; renderSamples(); }, function () {});
    });
    document.addEventListener('click', function (event) {
      if (event.target.closest && event.target.closest('[data-tune-refresh]')) load();
    });
  }

  IM.tuning = { load: load, onReady: onReady, useModel: useModel, _state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
