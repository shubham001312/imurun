// Models tab: backend-driven model library, downloads, progress modal and guide.
(function () {
  'use strict';

  var GUIDED_KEY = 'imurun.models.guided';
  var MB = 1024 * 1024;
  var GB = 1024 * MB;
  var RING_RADIUS = 52;
  var RING_LENGTH = 2 * Math.PI * RING_RADIUS;

  var STATUS_LABELS = {
    starting: 'Starting…',
    downloading: 'Downloading',
    verifying: 'Verifying…',
    complete: 'Complete',
    error: 'Failed',
  };

  var state = {
    models: [],
    downloaded: new Set(),
    loading: null,
    runSeq: 0,
    active: null,
    overlay: null,
    modal: null,
    pill: null,
    escape: null,
    initialized: false,
    search: '',
    compatibleOnly: true,
  };

  var modelsGrid = null;
  var availableGrid = null;
  var modelsList = null;
  var emptyState = null;
  var searchInput = null;
  var compatibleToggle = null;

  /* ------------------------------------------------------------- utilities */

  function getApi() {
    var api = window.__imurun_api;
    return api && typeof api === 'object' ? api : null;
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    var n = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function clampPercent(value) {
    var n = toNumber(value);
    if (n === null) return 0;
    return Math.min(100, Math.max(0, n));
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatBytes(bytes) {
    var n = toNumber(bytes);
    if (n === null || n < 0) return '0 MB';
    if (n >= GB) return (n / GB).toFixed(n >= 10 * GB ? 0 : 1) + ' GB';
    return (n / MB).toFixed(1) + ' MB';
  }

  function formatSpeed(bps) {
    var n = toNumber(bps);
    if (n === null || n <= 0) return '0 MB/s';
    if (n >= MB) return (n / MB).toFixed(1) + ' MB/s';
    return Math.max(1, Math.round(n / 1024)) + ' KB/s';
  }

  function formatEta(seconds) {
    var n = toNumber(seconds);
    if (n === null || n <= 0) return '—';
    if (n < 60) return Math.round(n) + 's';
    if (n < 3600) return Math.round(n / 60) + 'm ' + Math.round(n % 60) + 's';
    return Math.round(n / 3600) + 'h ' + Math.round((n % 3600) / 60) + 'm';
  }

  function sizeLabel(downloaded, total) {
    var done = toNumber(downloaded);
    var all = toNumber(total);
    var doneText = formatBytes(done === null ? 0 : done);
    if (all === null || all <= 0) return doneText;
    return doneText + ' / ' + formatBytes(all);
  }

  function modelSizeText(model) {
    var gb = toNumber(model && model.size_gb);
    if (gb !== null && gb > 0) return gb.toFixed(1) + ' GB';
    var bytes = toNumber(model && model.size_bytes);
    if (bytes !== null && bytes > 0) return formatBytes(bytes);
    return '—';
  }

  function modelSizeClass(model) {
    var gb = toNumber(model && model.size_gb);
    if (gb === null) {
      var bytes = toNumber(model && model.size_bytes);
      gb = bytes === null ? null : bytes / GB;
    }
    if (gb === null) return 'small';
    if (gb > 10) return 'large';
    if (gb > 4) return 'medium';
    return 'small';
  }

  function setText(root, selector, value) {
    if (!root) return;
    var node = root.querySelector(selector);
    if (node) node.textContent = value;
  }

  function errorMessage(error, fallback) {
    if (error && typeof error.message === 'string' && error.message) return error.message;
    if (typeof error === 'string' && error) return error;
    return fallback || 'Something went wrong.';
  }

  function notify(options) {
    var opts = options || {};
    if (typeof window.showNotification === 'function') {
      window.showNotification(opts);
      return;
    }
    var host = document.getElementById('notifications');
    if (!host) return;

    var tone = opts.tone || 'neutral';
    var node = document.createElement('article');
    node.className = 'notification notification--' + tone;
    node.setAttribute('role', tone === 'error' ? 'alert' : 'status');

    var icon = document.createElement('span');
    icon.className = 'notif-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = tone === 'success' ? '✓' : tone === 'error' ? '!' : 'i';

    var body = document.createElement('div');
    body.className = 'notif-body';
    if (opts.kicker) {
      var kicker = document.createElement('div');
      kicker.className = 'notif-kicker';
      kicker.textContent = opts.kicker;
      body.appendChild(kicker);
    }
    var title = document.createElement('h2');
    title.className = 'notif-title';
    title.textContent = opts.title || '';
    body.appendChild(title);
    if (opts.text) {
      var text = document.createElement('p');
      text.className = 'notif-text';
      text.textContent = opts.text;
      body.appendChild(text);
    }

    node.appendChild(icon);
    node.appendChild(body);
    host.appendChild(node);

    window.setTimeout(function () {
      node.classList.add('is-leaving');
      window.setTimeout(function () { node.remove(); }, 300);
    }, 4500);
  }

  function readGuided() {
    try {
      return window.localStorage.getItem(GUIDED_KEY) === 'true';
    } catch (_) {
      return false;
    }
  }

  function markGuided() {
    try {
      window.localStorage.setItem(GUIDED_KEY, 'true');
    } catch (_) {
      /* storage may be unavailable; the guide simply shows again */
    }
  }

  /* ---------------------------------------------------------------- render */

  function initModelsTab() {
    if (state.initialized) return;
    state.initialized = true;

    modelsGrid = document.getElementById('models-grid');
    availableGrid = document.getElementById('models-available-grid');
    modelsList = document.getElementById('models-list');
    emptyState = document.getElementById('models-empty-state');
    searchInput = document.getElementById('models-search');
    compatibleToggle = document.getElementById('models-compatible-only');

    ensurePillContainer();
    bindEvents();
    bindPicker();

    /* The guide used to open on every first page load, over Home, before the
     * user had asked for models. It now opens the first time the Models
     * section is shown. */
    if (window.IMURUN && window.IMURUN.bus) {
      window.IMURUN.bus.on('settings.section', function (section) {
        if (section !== 'models') return;
        if (!state.models.length && !state.loading) refreshModels();
        if (!readGuided()) showFirstTimeGuide();
      });
      window.IMURUN.bus.on('server:model.switch.queued', function (event) {
        notify({ id: 'model-switch', tone: 'neutral', icon: 'clock', kicker: 'Model',
          title: 'Model switch queued', text: 'Switching to ' + (event.model_name || 'the new model') + ' after the current reply.', timeout: 0 });
      });
      window.IMURUN.bus.on('server:model.switch.done', function (event) {
        notify({ id: 'model-switch', tone: 'success', icon: 'check', kicker: 'Model',
          title: 'Switched to ' + (event.model_name || 'the new model'), text: 'Ready for the next turn.', timeout: 3500 });
        refreshModels();
        if (window.IMURUN.status) window.IMURUN.status.refresh();
      });
    }
    refreshModels();
  }

  /* ------------------------------------------------------ model selection */

  var switching = null; /* model id being switched to, or null */

  /* Switch the engine to a downloaded model. While a turn runs the server
   * queues the switch and holds the request open, so the UI shows a
   * "switching" state until the request settles. */
  function selectModel(modelId) {
    if (!modelId) return Promise.resolve(false);
    var api = getApi();
    if (!api || typeof api.selectModel !== 'function') return Promise.resolve(false);
    if (switching) {
      notify({ tone: 'neutral', icon: 'clock', kicker: 'Model', title: 'A switch is already in progress', text: 'Wait for it to finish.' });
      return Promise.resolve(false);
    }
    switching = modelId;
    renderAll();
    renderPicker();
    var model = state.models.filter(function (item) { return item.id === modelId; })[0];
    var name = (model && model.name) || modelId;
    return api.selectModel(modelId).then(function (result) {
      if (result && result.ok === false) throw new Error(result.error || 'the model could not be selected');
      notify({ id: 'model-switch', tone: 'success', icon: 'check', kicker: 'Model', title: 'Model selected',
        text: (result && result.selected) || name, timeout: 3500 });
      return true;
    }, function (error) {
      notify({ id: 'model-switch', tone: 'error', icon: 'alert', kicker: 'Model', title: 'Could not switch to ' + name, text: errorMessage(error), timeout: 7000 });
      return false;
    }).then(function (ok) {
      switching = null;
      refreshModels();
      if (window.IMURUN && window.IMURUN.status) window.IMURUN.status.refresh();
      return ok;
    });
  }

  /* The composer's model picker lists downloaded, runnable models. */
  function renderPicker() {
    var select = document.getElementById('composer-model');
    if (!select) return;
    var wrap = select.closest('.composer-model');
    var downloaded = state.models.filter(function (model) {
      return state.downloaded.has(model.id) && model.compatible !== false;
    });
    var selected = downloaded.filter(function (model) { return model.selected; })[0] || null;
    select.innerHTML = '';
    if (wrap) wrap.dataset.state = switching ? 'switching' : (downloaded.length ? 'ready' : 'empty');
    if (!downloaded.length) {
      var empty = document.createElement('option');
      empty.value = '';
      empty.textContent = !state.loaded ? 'Loading models…'
        : (state.loadFailed ? 'Models unavailable' : 'No model downloaded');
      select.appendChild(empty);
      select.disabled = true;
      return;
    }
    if (!selected) {
      var none = document.createElement('option');
      none.value = '';
      none.textContent = 'Choose a model';
      select.appendChild(none);
    }
    downloaded.forEach(function (model) {
      var option = document.createElement('option');
      option.value = model.id;
      option.textContent = (switching === model.id ? 'Switching to ' : '') + (model.name || model.id);
      select.appendChild(option);
    });
    select.value = switching || (selected ? selected.id : '');
    select.disabled = Boolean(switching);
  }

  function bindPicker() {
    var select = document.getElementById('composer-model');
    if (!select || select.dataset.bound === '1') return;
    select.dataset.bound = '1';
    select.addEventListener('change', function () {
      if (select.value) selectModel(select.value);
    });
  }

  function bindEvents() {
    var first = document.getElementById('download-first-model');
    var firstBtn = document.getElementById('download-first-model-btn');
    if (first) first.addEventListener('click', startFirstDownload);
    if (firstBtn) firstBtn.addEventListener('click', startFirstDownload);

    var refresh = document.getElementById('refresh-models');
    if (refresh) {
      refresh.addEventListener('click', function () {
        refreshModels({ manual: true });
      });
    }

    if (availableGrid) availableGrid.addEventListener('click', handleGridClick);
    if (modelsGrid) modelsGrid.addEventListener('click', handleGridClick);

    if (searchInput) {
      searchInput.addEventListener('input', function () {
        state.search = searchInput.value || '';
        renderAvailable();
      });
    }
    if (compatibleToggle) {
      compatibleToggle.addEventListener('change', function () {
        state.compatibleOnly = !!compatibleToggle.checked;
        renderAvailable();
      });
    }

  }

  function handleGridClick(event) {
    var target = event.target;
    if (!target || typeof target.closest !== 'function') return;

    var download = target.closest('[data-download-model]');
    if (download) {
      event.preventDefault();
      startDownload(download.getAttribute('data-download-model'));
      return;
    }

    var use = target.closest('[data-select-model]');
    if (use) {
      event.preventDefault();
      /* Before, this called the API without awaiting it: no feedback, no
       * error handling (an unhandled rejection), and the card never showed
       * which model was active. */
      selectModel(use.getAttribute('data-select-model'));
      return;
    }

    var remove = target.closest('[data-delete-model]');
    if (remove) {
      event.preventDefault();
      deleteModel(remove.getAttribute('data-delete-model'));
      return;
    }

    if (target.closest('[data-retry-models]')) {
      event.preventDefault();
      refreshModels({ manual: true });
    }
  }

  function refreshModels(options) {
    if (state.loading) return state.loading;

    var manual = !!(options && options.manual);
    setBusy(true);

    var run = (async function () {
      var api = getApi();
      if (!api || typeof api.listModels !== 'function') {
        renderLoadError('The API bridge is unavailable. Reload the shell and try again.');
        return;
      }
      try {
        var result = await api.listModels();
        var list = Array.isArray(result) ? result : [];
        state.models = list.filter(function (model) {
          return model && typeof model.id === 'string' && model.id;
        });
        state.downloaded = new Set(
          state.models.filter(function (model) { return !!model.downloaded; }).map(function (model) {
            return model.id;
          })
        );
        state.loaded = true;
        state.loadFailed = false;
        renderAll();
        publish();
      } catch (error) {
        state.loaded = true;
        state.loadFailed = !state.models.length;
        renderPicker();
        var message = errorMessage(error, 'Could not reach the models endpoint.');
        if (!state.models.length) renderLoadError(message);
        if (manual || state.models.length) {
          notify({
            tone: 'error',
            kicker: 'Models',
            title: 'Could not load models',
            text: message,
          });
        }
      }
    })();

    state.loading = run.finally(function () {
      state.loading = null;
      setBusy(false);
    });
    return state.loading;
  }

  /* Share the catalogue with the rest of the shell (home status, stats
   * panel, chat's "no model" message). */
  function publish() {
    var IM = window.IMURUN;
    if (!IM) return;
    var runnable = state.models.filter(function (model) {
      return state.downloaded.has(model.id) && model.compatible !== false;
    });
    IM.state.models = state.models.slice();
    IM.state.modelsDownloaded = runnable.length;
    IM.state.selectedModel = runnable.filter(function (model) { return model.selected; })[0] || null;
    if (IM.bus) IM.bus.emit('models', state.models);
  }

  function setBusy(busy) {
    if (availableGrid) {
      availableGrid.setAttribute('aria-busy', busy ? 'true' : 'false');
      if (busy && !state.models.length) {
        availableGrid.innerHTML =
          '<div class="sk sk-card"></div><div class="sk sk-card"></div><div class="sk sk-card"></div>' +
          '<span class="sr-only" role="status">Loading models</span>';
      }
    }
  }

  function renderAll() {
    renderDownloaded();
    renderAvailable();
    renderPicker();
  }

  function renderDownloaded() {
    var downloaded = state.models.filter(function (model) {
      return state.downloaded.has(model.id);
    });

    if (emptyState) emptyState.hidden = downloaded.length > 0;
    if (modelsList) modelsList.hidden = downloaded.length === 0;
    if (!modelsGrid) return;

    modelsGrid.innerHTML = '';
    downloaded.forEach(function (model) {
      modelsGrid.appendChild(createModelCard(model, true));
    });
  }

  function renderAvailable() {
    if (!availableGrid) return;
    availableGrid.innerHTML = '';

    if (!state.models.length) {
      availableGrid.appendChild(
        noteNode('No models were reported by the server. Refresh to try again.')
      );
      return;
    }

    var query = String(state.search || '').trim().toLowerCase();
    var filtered = state.models.filter(function (model) {
      if (state.compatibleOnly && model.compatible === false) return false;
      if (!query) return true;
      var haystack = [
        model.name,
        model.id,
        model.description,
        model.filename,
        (model.tags || []).join(' '),
        modelSizeText(model),
      ].join(' ').toLowerCase();
      return haystack.indexOf(query) !== -1;
    });

    filtered.forEach(function (model) {
      availableGrid.appendChild(createModelCard(model, state.downloaded.has(model.id)));
    });

    if (!filtered.length) {
      availableGrid.appendChild(noteNode('No models match that search.'));
    } else if (filtered.length === state.downloaded.size && state.downloaded.size > 0) {
      availableGrid.appendChild(noteNode('All matching models are downloaded.'));
    }
  }

  function noteNode(text) {
    var note = document.createElement('div');
    note.className = 'models-note';
    note.textContent = text;
    return note;
  }

  function renderLoadError(message) {
    if (!availableGrid) return;
    availableGrid.innerHTML =
      '<div class="models-load-error" role="alert">' +
      '<p class="models-load-error-text">Could not load models. ' + escapeHtml(message) + '</p>' +
      '<button class="btn" type="button" data-retry-models>Retry</button>' +
      '</div>';
    if (emptyState) emptyState.hidden = false;
    if (modelsList) modelsList.hidden = true;
  }

  function createModelCard(model, isDownloaded) {
    var card = document.createElement('article');
    card.className = 'model-card' + (isDownloaded ? ' downloaded' : '');
    card.setAttribute('data-model-id', model.id);

    var name = model.name || model.id;
    var description = model.description || 'No description provided.';
    var filename = model.filename || '';
    var source = model.source || '';
    var sizeClass = modelSizeClass(model);
    var sizeText = modelSizeText(model);

    var tags = Array.isArray(model.tags) ? model.tags : [];
    var isReasoning = tags.indexOf('reasoning') !== -1;
    var compatible = model.compatible !== false;
    var tagChips = tags.slice(0, 3).map(function (tag) {
      return '<span class="model-chip">' + escapeHtml(tag) + '</span>';
    }).join('');

    var isActive = Boolean(model.selected) && isDownloaded;
    var isSwitching = switching === model.id;
    if (isActive) card.classList.add('is-active');
    if (isSwitching) card.classList.add('is-switching');
    var useButton = isActive
      ? '<button class="btn model-use" type="button" disabled aria-label="' + escapeHtml(name) + ' is the active model">In use</button>'
      : '<button class="btn primary model-use" type="button" data-select-model="' + escapeHtml(model.id) + '"' +
        (switching ? ' disabled' : '') + ' aria-label="Use ' + escapeHtml(name) + '">' + (isSwitching ? 'Switching…' : 'Use') + '</button>';

    card.innerHTML =
      '<div class="model-card-head">' +
        '<h4 class="model-name">' + escapeHtml(name) + '</h4>' +
        '<span class="model-size ' + sizeClass + '">' + escapeHtml(sizeText) + '</span>' +
      '</div>' +
      '<p class="model-desc">' + escapeHtml(description) + '</p>' +
      '<div class="model-meta">' +
        (isActive ? '<span class="model-chip model-chip-active">Active</span>' : '') +
        (isDownloaded && !isActive ? '<span class="model-chip">Downloaded</span>' : '') +
        (isReasoning ? '<span class="model-chip model-chip-reasoning">Reasoning</span>' : '') +
        (!compatible ? '<span class="model-chip model-chip-warn">Not runnable yet</span>' : '') +
        tagChips +
        (source ? '<span class="model-chip">' + escapeHtml(source) + '</span>' : '') +
        (filename
          ? '<span class="model-chip model-chip-file" title="' + escapeHtml(filename) + '">' +
            escapeHtml(filename) + '</span>'
          : '') +
      '</div>' +
      '<div class="model-actions">' +
        (isDownloaded
          ? ((compatible ? useButton : '') +
             '<button class="btn model-delete" type="button" data-delete-model="' +
             escapeHtml(model.id) + '" aria-label="Delete ' + escapeHtml(name) + '">Delete</button>')
          : '<button class="btn primary model-download" type="button" data-download-model="' +
            escapeHtml(model.id) + '" aria-label="Download ' + escapeHtml(name) + '">Download</button>') +
      '</div>';

    return card;
  }

  async function startFirstDownload() {
    if (!state.models.length) await (state.loading || refreshModels());

    var preferred = ['llama-3.1-8b-q4', 'llama-3.2-3b-q4', 'llama-3.2-1b-q4'];
    var target = null;
    for (var i = 0; i < preferred.length; i++) {
      var match = state.models.filter(function (model) {
        return model.id === preferred[i] && !model.downloaded;
      })[0];
      if (match) { target = match; break; }
    }
    if (!target) {
      target = state.models.filter(function (model) { return !model.downloaded; })[0] || null;
    }

    if (!target) {
      notify({
        tone: 'neutral',
        kicker: 'Models',
        title: state.models.length ? 'All models are downloaded' : 'No models available',
        text: state.models.length
          ? 'Pick a model from the library to re-download it.'
          : 'Refresh the list once the server is reachable.',
      });
      return;
    }
    startDownload(target.id);
  }

  /* -------------------------------------------------------------- download */

  async function startDownload(modelId) {
    if (!modelId) return;

    var model = state.models.filter(function (item) { return item.id === modelId; })[0];
    if (!model && !state.models.length) {
      await (state.loading || refreshModels());
      model = state.models.filter(function (item) { return item.id === modelId; })[0];
    }
    if (!model) {
      notify({
        tone: 'error',
        kicker: 'Models',
        title: 'Unknown model',
        text: 'The server did not report a model with id “' + modelId + '”.',
      });
      return;
    }

    var api = getApi();
    if (!api || typeof api.downloadModel !== 'function') {
      notify({
        tone: 'error',
        kicker: 'Download',
        title: 'Download unavailable',
        text: 'The API bridge is missing downloadModel(). Reload the shell.',
      });
      return;
    }

    if (state.active && state.active.modelId === modelId && state.active.status === 'running') {
      restoreProgress();
      return;
    }

    disposeProgress(true, true);

    var run = {
      id: ++state.runSeq,
      modelId: modelId,
      model: model,
      status: 'running',
      abort: null,
      failed: false,
    };
    state.active = run;

    buildProgressModal(model);
    updateProgressUI(run, { status: 'starting', percent: 0, message: 'Preparing download…' });

    function onProgress(progress) {
      if (state.active !== run || run.status === 'complete' || run.status === 'error') return;
      updateProgressUI(run, progress || {});
      if (progress && progress.status === 'error') {
        showProgressError(run, progress.error || progress.message);
      }
    }

    function onDone(progress) {
      if (state.active !== run) return;
      finishRun(run, progress || {});
    }

    function onError(error) {
      if (state.active !== run || run.status === 'complete') return;
      failRun(run, error);
    }

    try {
      run.abort = api.downloadModel(modelId, onProgress, onDone, onError);
    } catch (error) {
      failRun(run, error);
    }
  }

  function buildProgressModal(model) {
    var name = (model && (model.name || model.id)) || 'model';

    var overlay = document.createElement('div');
    overlay.className = 'dl-overlay';
    overlay.innerHTML =
      '<div class="dl-modal" role="dialog" aria-modal="true" aria-labelledby="dl-progress-title">' +
        '<header class="dl-header">' +
          '<div class="dl-heading">' +
            '<span class="dl-kicker">Model download</span>' +
            '<h2 class="dl-title" id="dl-progress-title">' + escapeHtml(name) + '</h2>' +
          '</div>' +
          '<div class="dl-header-actions">' +
            '<button class="dl-icon-btn dl-minimize" type="button" title="Minimize to pill" aria-label="Minimize to pill">&#8212;</button>' +
            '<button class="dl-icon-btn dl-close" type="button" title="Close" aria-label="Close">&#215;</button>' +
          '</div>' +
        '</header>' +
        '<div class="dl-body">' +
          '<div class="dl-ring-wrap">' +
            '<svg class="dl-ring" viewBox="0 0 120 120" aria-hidden="true">' +
              '<circle class="dl-ring-bg" cx="60" cy="60" r="' + RING_RADIUS + '"></circle>' +
              '<circle class="dl-ring-fg" cx="60" cy="60" r="' + RING_RADIUS + '"></circle>' +
            '</svg>' +
            '<div class="dl-ring-center">' +
              '<span class="dl-percent">0%</span>' +
              '<span class="dl-state" role="status" aria-live="polite">Starting…</span>' +
            '</div>' +
          '</div>' +
          '<div class="dl-stats">' +
            '<div class="dl-stat"><span class="dl-stat-k">Downloaded</span><span class="dl-stat-v" data-dl="size">0 MB</span></div>' +
            '<div class="dl-stat"><span class="dl-stat-k">Speed</span><span class="dl-stat-v" data-dl="speed">0 MB/s</span></div>' +
            '<div class="dl-stat"><span class="dl-stat-k">ETA</span><span class="dl-stat-v" data-dl="eta">—</span></div>' +
          '</div>' +
          '<div class="dl-bar" role="progressbar" aria-label="Download progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">' +
            '<div class="dl-bar-fill" data-dl="fill"></div>' +
          '</div>' +
          '<p class="dl-note" data-dl="message">Preparing download…</p>' +
          '<div class="dl-error" data-dl="error" hidden>' +
            '<span class="dl-error-icon" aria-hidden="true">!</span>' +
            '<div class="dl-error-body">' +
              '<strong class="dl-error-title">Download failed</strong>' +
              '<p class="dl-error-text" data-dl="error-text"></p>' +
            '</div>' +
            '<button class="btn primary dl-retry" type="button">Retry</button>' +
          '</div>' +
          '<div class="dl-foot">' +
            '<button class="btn dl-cancel" type="button">Cancel download</button>' +
            '<button class="btn primary dl-background" type="button">Keep downloading in the background</button>' +
          '</div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(overlay);
    state.overlay = overlay;
    state.modal = overlay.querySelector('.dl-modal');
    state.returnFocus = document.activeElement;

    var ring = overlay.querySelector('.dl-ring-fg');
    if (ring) {
      ring.style.strokeDasharray = String(RING_LENGTH);
      ring.style.strokeDashoffset = String(RING_LENGTH);
    }

    /* Closing the window used to ABORT the download with no warning.
     * While it runs, closing now minimises to the pill; cancelling is an
     * explicit, confirmed action. */
    overlay.querySelector('.dl-close').addEventListener('click', closeOrMinimize);
    overlay.querySelector('.dl-minimize').addEventListener('click', minimizeProgress);
    overlay.querySelector('.dl-background').addEventListener('click', minimizeProgress);
    overlay.querySelector('.dl-cancel').addEventListener('click', cancelActiveDownload);
    overlay.querySelector('.dl-retry').addEventListener('click', function () {
      var active = state.active;
      startDownload(active ? active.modelId : model.id);
    });
    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) closeOrMinimize();
    });
    state.releaseFocus = window.IMURUN && window.IMURUN.util ? window.IMURUN.util.trapFocus(state.modal) : null;

    attachEscape();
    window.requestAnimationFrame(function () {
      if (state.overlay !== overlay) return;
      overlay.classList.add('is-visible');
      var close = overlay.querySelector('.dl-background') || overlay.querySelector('.dl-close');
      if (close) close.focus();
    });
  }

  function closeOrMinimize() {
    var run = state.active;
    if (run && run.status === 'running') minimizeProgress();
    else disposeProgress(false);
  }

  function cancelActiveDownload() {
    var run = state.active;
    if (!run) return;
    if (run.status === 'running') {
      var name = (run.model && run.model.name) || run.modelId;
      if (!window.confirm('Cancel downloading ' + name + '? The partial file may be kept for resuming.')) return false;
      notify({ tone: 'neutral', icon: 'close', kicker: 'Download', title: 'Download cancelled', text: name });
    }
    disposeProgress(true);
  }

  function updateProgressUI(run, progress) {
    var modal = state.modal;
    if (!modal || state.active !== run) return;

    var data = progress || {};
    var status = typeof data.status === 'string' ? data.status : '';
    var downloaded = toNumber(data.downloaded_bytes);
    var total = toNumber(data.total_bytes);
    var speed = toNumber(data.speed_bps);
    var eta = toNumber(data.eta_seconds);

    var percent = toNumber(data.percent);
    if (percent === null) {
      percent = total !== null && total > 0 ? ((downloaded === null ? 0 : downloaded) / total) * 100 : 0;
    }
    if (status === 'complete') percent = 100;
    percent = clampPercent(percent);

    var statusText = STATUS_LABELS[status] || 'Working…';
    if (status === 'error') statusText = 'Failed';

    setText(modal, '.dl-percent', Math.round(percent) + '%');
    setText(modal, '.dl-state', statusText);
    setText(modal, '[data-dl="size"]', sizeLabel(downloaded, total));
    setText(modal, '[data-dl="speed"]', formatSpeed(speed));
    setText(modal, '[data-dl="eta"]', formatEta(eta));
    setText(modal, '[data-dl="message"]', data.message || statusText);

    var fill = modal.querySelector('[data-dl="fill"]');
    if (fill) fill.style.width = percent + '%';

    var bar = modal.querySelector('.dl-bar');
    if (bar) bar.setAttribute('aria-valuenow', String(Math.round(percent)));

    var ring = modal.querySelector('.dl-ring-fg');
    if (ring) ring.style.strokeDashoffset = String(RING_LENGTH * (1 - percent / 100));

    if (status === 'complete') {
      modal.classList.add('is-complete');
      modal.classList.remove('is-error');
    }

    updateProgressPill(run, {
      percent: percent,
      downloaded: downloaded,
      total: total,
      speed: speed,
      status: status,
    });
  }

  /* A download failure must read like a sentence, not like a stack trace.
   * The first line carries the reason; the rest is noise. */
  function shortError(text) {
    var value = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    if (!value) return 'The download could not be completed.';
    if (value.length > 240) value = value.slice(0, 237) + '...';
    return value;
  }

  function showProgressError(run, message) {
    var modal = state.modal;
    if (!modal || state.active !== run) return;

    var text = shortError(message);
    modal.classList.add('is-error');
    modal.classList.remove('is-complete');

    var box = modal.querySelector('[data-dl="error"]');
    if (box) box.hidden = false;
    setText(modal, '[data-dl="error-text"]', text);
    setText(modal, '.dl-state', 'Failed');
    setText(modal, '[data-dl="message"]', 'The download stopped before it finished.');

    if (state.pill) {
      state.pill.classList.remove('downloading');
      state.pill.classList.add('error');
      setText(state.pill, '.pill-subtitle', 'Download failed');
    }
  }

  function finishRun(run, progress) {
    if (!run || run.status === 'complete' || run.status === 'error') return;
    run.status = 'complete';

    updateProgressUI(run, {
      status: 'complete',
      percent: 100,
      downloaded_bytes: progress.downloaded_bytes != null ? progress.downloaded_bytes : run.model.size_bytes,
      total_bytes: progress.total_bytes != null ? progress.total_bytes : run.model.size_bytes,
      speed_bps: 0,
      eta_seconds: 0,
      message: 'Model downloaded',
    });

    state.downloaded.add(run.modelId);
    state.models = state.models.map(function (model) {
      if (model.id !== run.modelId) return model;
      var copy = {};
      Object.keys(model).forEach(function (key) { copy[key] = model[key]; });
      copy.downloaded = true;
      return copy;
    });
    renderAll();

    notify({
      tone: 'success',
      kicker: 'Model ready',
      title: (run.model.name || run.modelId) + ' downloaded',
      text: 'The model is now available to the FISH engine.',
    });

    refreshModels();

    window.setTimeout(function () {
      if (state.active === run) disposeProgress(false);
    }, 1200);
  }

  function failRun(run, error) {
    if (!run || run.failed) return;
    run.failed = true;
    run.status = 'error';

    var message = errorMessage(error, 'The download failed.');
    showProgressError(run, message);
    notify({
      tone: 'error',
      kicker: 'Download failed',
      title: run.model.name || run.modelId,
      text: message,
    });
  }

  function disposeProgress(abort, immediate) {
    var run = state.active;

    if (abort && run && run.status === 'running' && typeof run.abort === 'function') {
      try {
        run.abort();
      } catch (_) {
        /* aborting an already-finished stream is harmless */
      }
    }
    if (run && run.status === 'running') run.status = abort ? 'aborted' : 'closed';

    detachEscape();
    removeProgressPill();
    if (state.releaseFocus) {
      state.releaseFocus(false);
      state.releaseFocus = null;
    }
    var hadFocus = state.overlay && state.overlay.contains(document.activeElement);
    if (hadFocus && state.returnFocus && document.contains(state.returnFocus)) {
      try { state.returnFocus.focus(); } catch (_) { /* ignore */ }
    }
    state.returnFocus = null;

    var overlay = state.overlay;
    state.overlay = null;
    state.modal = null;
    state.active = null;

    if (overlay) {
      overlay.classList.remove('is-visible');
      if (immediate) {
        overlay.remove();
      } else {
        window.setTimeout(function () { overlay.remove(); }, 220);
      }
    }
  }

  function attachEscape() {
    detachEscape();
    state.escape = function (event) {
      if (event.key !== 'Escape' || !state.overlay || state.overlay.classList.contains('is-hidden')) return;
      event.preventDefault();
      closeOrMinimize();
    };
    document.addEventListener('keydown', state.escape);
  }

  function detachEscape() {
    if (state.escape) {
      document.removeEventListener('keydown', state.escape);
      state.escape = null;
    }
  }

  /* ------------------------------------------------------------------ pill */

  function ensurePillContainer() {
    var container = document.querySelector('.minimized-pills');
    if (container) return container;
    if (!document.body) return null;
    container = document.createElement('div');
    container.className = 'minimized-pills';
    document.body.appendChild(container);
    return container;
  }

  function createPill(options) {
    var opts = options || {};
    var container = ensurePillContainer();
    if (!container) return null;

    var pill = document.createElement('div');
    pill.className = 'pill ' + (opts.type || 'progress');
    pill.setAttribute('role', 'status');
    pill.innerHTML =
      '<div class="pill-icon" aria-hidden="true">' +
        (opts.type === 'guide' ? '?' : '&#8595;') +
      '</div>' +
      '<div class="pill-content">' +
        '<div class="pill-title"></div>' +
        '<div class="pill-subtitle"></div>' +
        '<div class="pill-progress"><div class="pill-fill"></div></div>' +
      '</div>' +
      '<div class="pill-actions">' +
        '<button class="pill-btn" type="button" data-pill-action="restore" aria-label="Restore" title="Restore">&#9634;</button>' +
        '<button class="pill-btn" type="button" data-pill-action="dismiss" aria-label="Dismiss" title="Dismiss">&#10005;</button>' +
      '</div>';

    setText(pill, '.pill-title', opts.title || '');
    setText(pill, '.pill-subtitle', opts.subtitle || '');

    pill.querySelector('[data-pill-action="restore"]').addEventListener('click', function () {
      try {
        if (opts.onRestore) opts.onRestore();
      } finally {
        pill.remove();
      }
    });
    pill.querySelector('[data-pill-action="dismiss"]').addEventListener('click', function () {
      /* onDismiss may decline (returns false), e.g. the user kept the
       * download running; the pill must then stay. */
      var keep = false;
      try {
        if (opts.onDismiss) keep = opts.onDismiss() === false;
      } finally {
        if (!keep) pill.remove();
      }
    });

    container.appendChild(pill);
    return pill;
  }

  function minimizeProgress() {
    if (!state.overlay || !state.active) return;

    var run = state.active;
    var modelName = run.model.name || run.modelId;
    var isError = run.status === 'error';
    var isComplete = run.status === 'complete';

    try {
      var pill = createPill({
        type: 'progress',
        title: modelName,
        subtitle: isError ? 'Download failed' : isComplete ? 'Complete' : 'Downloading…',
        onRestore: restoreProgress,
        onDismiss: cancelActiveDownload,
      });
      if (!pill) return;
      if (isError) pill.classList.add('error');
      else if (isComplete) pill.classList.add('complete');
      else pill.classList.add('downloading');
      var restoreTarget = state.returnFocus;
      state.overlay.classList.add('is-hidden');
      state.pill = pill;
      if (restoreTarget && document.contains(restoreTarget)) {
        try { restoreTarget.focus(); } catch (_) { /* ignore */ }
      }
    } catch (error) {
      if (state.overlay) state.overlay.classList.remove('is-hidden');
    }
  }

  function restoreProgress() {
    if (!state.overlay) return;
    state.overlay.classList.remove('is-hidden');
    removeProgressPill();
    var close = state.overlay.querySelector('.dl-close');
    if (close) close.focus();
  }

  function removeProgressPill() {
    if (state.pill) {
      state.pill.remove();
      state.pill = null;
    }
  }

  function updateProgressPill(run, info) {
    var pill = state.pill;
    if (!pill) return;
    if (!pill.isConnected) {
      state.pill = null;
      return;
    }

    var percent = clampPercent(info.percent);
    pill.classList.remove('error', 'complete', 'downloading');
    if (info.status === 'complete') pill.classList.add('complete');
    else if (info.status === 'error') pill.classList.add('error');
    else pill.classList.add('downloading');
    var bar = pill.querySelector('.pill-progress');
    if (bar) {
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-valuemin', '0');
      bar.setAttribute('aria-valuemax', '100');
      bar.setAttribute('aria-valuenow', String(Math.round(percent)));
    }

    setText(pill, '.pill-subtitle',
      Math.round(percent) + '% · ' + sizeLabel(info.downloaded, info.total) + ' · ' + formatSpeed(info.speed));

    var fill = pill.querySelector('.pill-fill');
    if (fill) fill.style.width = percent + '%';
  }

  /* ----------------------------------------------------------------- guide */

  function showFirstTimeGuide() {
    if (document.querySelector('.first-time-guide')) return;

    var guide = document.createElement('div');
    guide.className = 'first-time-guide';
    guide.innerHTML =
      '<div class="guide-overlay" data-guide-skip></div>' +
      '<div class="guide-panel" role="dialog" aria-modal="true" aria-labelledby="guide-title">' +
        '<header class="guide-head">' +
          '<div class="guide-heading">' +
            '<span class="guide-kicker">Models</span>' +
            '<h2 id="guide-title">Welcome to model management</h2>' +
          '</div>' +
          '<div class="guide-actions">' +
            '<button class="guide-minimize" type="button" aria-label="Minimize guide" title="Minimize">&#8212;</button>' +
            '<button class="guide-close" type="button" aria-label="Skip guide" title="Skip">&#215;</button>' +
          '</div>' +
        '</header>' +
        '<div class="guide-steps">' +
          '<div class="guide-step active" data-step="1">' +
            '<span class="step-number">1</span>' +
            '<div class="step-content">' +
              '<h3>Choose a model</h3>' +
              '<p>Browse the models the server reports. Names, sizes and download state all come from the backend.</p>' +
            '</div>' +
          '</div>' +
          '<div class="guide-step" data-step="2">' +
            '<span class="step-number">2</span>' +
            '<div class="step-content">' +
              '<h3>Download with progress</h3>' +
              '<p>Watch live percent, speed and ETA. If the transfer fails, retry inline without losing your place.</p>' +
            '</div>' +
          '</div>' +
          '<div class="guide-step" data-step="3">' +
            '<span class="step-number">3</span>' +
            '<div class="step-content">' +
              '<h3>Start chatting</h3>' +
              '<p>Downloaded models land in your library, and the FISH engine loads one automatically when you start a chat.</p>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<footer class="guide-nav">' +
          '<button class="btn guide-prev" type="button" disabled>Previous</button>' +
          '<button class="btn primary guide-next" type="button">Next</button>' +
          '<button class="btn primary guide-done" type="button" hidden>Get started</button>' +
        '</footer>' +
      '</div>';

    document.body.appendChild(guide);

    var currentStep = 1;
    var steps = Array.prototype.slice.call(guide.querySelectorAll('.guide-step'));
    var prev = guide.querySelector('.guide-prev');
    var next = guide.querySelector('.guide-next');
    var done = guide.querySelector('.guide-done');

    function updateStep() {
      steps.forEach(function (step, index) {
        step.classList.toggle('active', index + 1 === currentStep);
      });
      prev.disabled = currentStep === 1;
      next.hidden = currentStep === 3;
      done.hidden = currentStep !== 3;
    }

    next.addEventListener('click', function () {
      if (currentStep < 3) {
        currentStep += 1;
        updateStep();
      }
    });

    prev.addEventListener('click', function () {
      if (currentStep > 1) {
        currentStep -= 1;
        updateStep();
      }
    });

    var releaseGuide = window.IMURUN && window.IMURUN.util ? window.IMURUN.util.trapFocus(guide) : null;
    guide.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeGuide();
      }
    });

    function closeGuide() {
      if (releaseGuide) {
        releaseGuide();
        releaseGuide = null;
      }
      guide.classList.remove('is-visible');
      guide.classList.remove('is-minimized');
      if (guide._pill) {
        guide._pill.remove();
        guide._pill = null;
      }
      markGuided();
      window.setTimeout(function () { guide.remove(); }, 260);
    }

    done.addEventListener('click', closeGuide);
    guide.querySelector('.guide-close').addEventListener('click', closeGuide);
    guide.querySelector('.guide-overlay').addEventListener('click', closeGuide);

    guide.querySelector('.guide-minimize').addEventListener('click', function () {
      try {
        var pill = createPill({
          type: 'guide',
          title: 'Model guide',
          subtitle: 'Tap to resume',
          onRestore: function () {
            guide.classList.remove('is-minimized');
            guide._pill = null;
          },
          onDismiss: closeGuide,
        });
        if (!pill) return;
        guide.classList.add('is-minimized');
        guide._pill = pill;
      } catch (error) {
        guide.classList.remove('is-minimized');
      }
    });

    window.requestAnimationFrame(function () {
      guide.classList.add('is-visible');
      next.focus();
    });
  }

  /* ---------------------------------------------------------------- delete */

  async function deleteModel(modelId) {
    if (!modelId) return;

    var api = getApi();
    if (!api || typeof api.deleteModel !== 'function') {
      notify({
        tone: 'error',
        kicker: 'Models',
        title: 'Delete unavailable',
        text: 'The API bridge is missing deleteModel(). Reload the shell.',
      });
      return;
    }

    var model = state.models.filter(function (item) { return item.id === modelId; })[0];
    var name = model && model.name ? model.name : modelId;

    if (!window.confirm('Delete “' + name + '”? The model file will be removed from disk.')) return;

    if (state.active && state.active.modelId === modelId) disposeProgress(true);

    try {
      await api.deleteModel(modelId);
      state.downloaded.delete(modelId);
      notify({
        tone: 'success',
        kicker: 'Deleted',
        title: name + ' removed',
        text: 'The model file is no longer on disk.',
      });
      await refreshModels();
    } catch (error) {
      notify({
        tone: 'error',
        kicker: 'Delete failed',
        title: name,
        text: errorMessage(error, 'The model could not be deleted.'),
      });
    }
  }

  var publicApi = {
    init: initModelsTab,
    refresh: refreshModels,
    select: selectModel,
    renderPicker: renderPicker,
    isSwitching: function () { return Boolean(switching); },
    startDownload: startDownload,
    listModels: function () {
      var api = getApi();
      return api && typeof api.listModels === 'function' ? api.listModels() : Promise.resolve([]);
    },
  };
  window.__imurun_models = publicApi;
  if (window.IMURUN) window.IMURUN.models = publicApi;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initModelsTab);
  } else {
    initModelsTab();
  }
})();
