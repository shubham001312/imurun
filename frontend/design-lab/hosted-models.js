/* Model picker and hosted model providers.
 *
 *   IMURUN.modelPicker.open(query)       the "Choose a model" dialog
 *   IMURUN.modelPicker.command(arg)      `/model` and `/model <name>`
 *   IMURUN.modelPicker.connect(id)       the "Connect a provider" dialog
 *   IMURUN.modelPicker.chooseModels(id)  the "Choose models" dialog
 *
 * The picker opens from the `/model` slash command, from the composer's
 * model button (which stands in for models.js's <select>; the select stays
 * in the DOM, hidden, so models.js keeps rendering into it) and from
 * Ctrl/Cmd+M. It lists the downloaded local models and every connected
 * provider's switched-on models (GET /api/models `hosted`), grouped, with a
 * search box. Selecting calls POST /api/models/select through
 * IMURUN.models.select, which shows the success or failure toast.
 *
 * Settings > Models gets a "Hosted providers" card under the local models:
 * one row per provider (GET /api/remote) with Connect / Change key,
 * Choose models and Remove. Keys are sent once to POST /api/remote/<id>/key
 * and never come back: the server only ever returns "****abcd".
 *
 * No inline scripts or styles (CSP): every node is built here and styled by
 * styles/hosted-models.css with the theme's variables.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util || {};
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  var MOD = isMac ? '⌘' : 'Ctrl';
  var RENDER_LIMIT = 300;

  function api() { return window.__imurun_api; }
  function byId(id) { return document.getElementById(id); }
  function esc(text) {
    if (util.escapeHtml) return util.escapeHtml(String(text === null || text === undefined ? '' : text));
    return String(text === null || text === undefined ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function errorText(error, fallback) {
    return (error && error.message) ? String(error.message) : (fallback || 'Something went wrong.');
  }

  /* Line icons in the rail's style (.nav-ico): 24 px box, 1.6 stroke. */
  var ICONS = {
    chip: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 3.5v3.5M14 3.5v3.5M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
    cloud: '<path d="M7.5 18.5h9.25a4 4 0 0 0 .55-7.96A5.5 5.5 0 0 0 6.7 9.6 4.5 4.5 0 0 0 7.5 18.5Z"/>',
    check: '<path d="M5 12.5l4.2 4.2L19 7"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    chevron: '<path d="M7 10l5 5 5-5"/>',
    sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    key: '<circle cx="8" cy="15" r="3.5"/><path d="M10.5 12.5L19 4M16 7l2.5 2.5M14 9l2 2"/>',
    external: '<path d="M14 5h5v5M19 5l-8 8M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4"/>'
  };
  function icon(name, extra) {
    return '<svg class="hm-ico' + (extra ? ' ' + extra : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      (ICONS[name] || '') + '</svg>';
  }

  function contextText(tokens) {
    var n = Number(tokens);
    if (!Number.isFinite(n) || n <= 0) return '';
    if (n >= 1000000) return (Math.round(n / 100000) / 10) + 'M context';
    return Math.round(n / 1024) + 'K context';
  }
  function hostOf(url) {
    try { return new URL(url).host; } catch (error) { return ''; }
  }

  /* ------------------------------------------------------------- modals */

  /* One overlay per dialog, built once, in the .modal / .dialog style the
   * rename dialog uses. Esc and a click on the backdrop close it; Tab is
   * kept inside while it is open; focus goes back where it came from. */
  function makeModal(id, title, extraClass) {
    var root = document.createElement('div');
    root.className = 'modal hm-modal';
    root.id = id;
    root.hidden = true;
    root.innerHTML =
      '<div class="dialog hm-dialog ' + (extraClass || '') + '" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
        '<div class="hm-head"><h2 id="' + id + '-title">' + esc(title) + '</h2>' +
          '<button class="hm-icon-btn" type="button" data-hm-close aria-label="Close">' + icon('close') + '</button></div>' +
        '<div class="hm-body"></div>' +
      '</div>';
    document.body.appendChild(root);
    var modal = { root: root, dialog: root.firstChild, body: root.querySelector('.hm-body'),
      title: root.querySelector('h2'), release: null, returnTo: null, onClose: null };
    root.addEventListener('click', function (event) {
      if (event.target === root || (event.target.closest && event.target.closest('[data-hm-close]'))) closeModal(modal);
    });
    root.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeModal(modal);
      }
    });
    return modal;
  }

  function openModal(modal, focusTarget, returnTo) {
    modal.returnTo = returnTo || modal.returnTo || document.activeElement;
    if (modal.root.hidden && IM.sound && IM.sound.open) IM.sound.open();
    modal.root.hidden = false;
    if (modal.release) modal.release(false);
    modal.release = util.trapFocus ? util.trapFocus(modal.root) : null;
    if (focusTarget && focusTarget.focus) focusTarget.focus();
  }

  function closeModal(modal, keepFocus) {
    if (!modal || modal.root.hidden) return;
    modal.root.hidden = true;
    if (modal.release) {
      modal.release(false);
      modal.release = null;
    }
    var back = modal.returnTo;
    modal.returnTo = null;
    if (!keepFocus && back && document.contains(back) && back.focus) {
      try { back.focus(); } catch (error) { /* ignore */ }
    }
    if (modal.onClose) modal.onClose();
  }

  function isOpen(modal) { return Boolean(modal && !modal.root.hidden); }

  /* ------------------------------------------------------------ data */

  var data = { models: [], hosted: [], providers: [], loaded: false, loading: null };

  function loadModels() {
    var bridge = api();
    if (!bridge || !bridge.listModels) return Promise.resolve();
    return bridge.listModels().then(function (models) {
      var last = bridge.lastModels || {};
      data.models = Array.isArray(models) ? models : [];
      data.hosted = Array.isArray(last.hosted) ? last.hosted : [];
      data.loaded = true;
      updateTrigger();
    });
  }

  function loadProviders() {
    var bridge = api();
    if (!bridge || !bridge.listProviders) return Promise.resolve([]);
    return bridge.listProviders().then(function (view) {
      data.providers = (view && view.providers) || [];
      renderProvidersCard();
      return data.providers;
    });
  }

  function loadAll() {
    if (data.loading) return data.loading;
    data.loading = Promise.all([
      loadModels().catch(function (error) { data.loadError = errorText(error); }),
      loadProviders().catch(function (error) { data.providersError = errorText(error); })
    ]).then(function () { data.loading = null; }, function () { data.loading = null; });
    return data.loading;
  }

  function providerById(id) {
    return data.providers.filter(function (p) { return p.id === id; })[0] || null;
  }

  /* Every selectable row, in display order: local first, then providers. */
  function allRows() {
    var rows = [];
    data.models.forEach(function (model) {
      if (!model || !model.downloaded || model.compatible === false || model.remote) return;
      rows.push({
        id: model.id, name: model.name || model.id, model: model.id, group: 'local',
        groupLabel: 'On this computer', provider: 'On this computer',
        detail: model.size_gb ? Number(model.size_gb).toFixed(1) + ' GB' : '',
        current: Boolean(model.selected)
      });
    });
    var order = data.providers.map(function (p) { return p.id; });
    var hosted = data.hosted.slice().sort(function (a, b) {
      return order.indexOf(a.provider) - order.indexOf(b.provider);
    });
    hosted.forEach(function (model) {
      rows.push({
        id: model.id, name: model.name || model.model, model: model.model, group: model.provider,
        groupLabel: model.provider_label || model.provider, provider: model.provider_label || model.provider,
        detail: contextText(model.context), current: Boolean(model.selected), remote: true
      });
    });
    return rows;
  }

  function currentRow() {
    return allRows().filter(function (row) { return row.current; })[0] || null;
  }

  function matches(row, query) {
    if (!query) return true;
    var hay = (row.name + ' ' + row.model + ' ' + row.provider + ' ' + row.id).toLowerCase();
    return query.toLowerCase().split(/\s+/).every(function (word) { return !word || hay.indexOf(word) !== -1; });
  }

  /* ---------------------------------------------------- composer button */

  var trigger = null;

  function ensureTrigger() {
    if (trigger && document.contains(trigger)) return trigger;
    var select = byId('composer-model');
    if (!select) return null;
    var label = select.closest('.composer-model');
    trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.id = 'composer-model-button';
    trigger.className = 'mp-trigger';
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.title = 'Choose the model that answers (' + MOD + ' M)';
    trigger.innerHTML = icon('chip', 'mp-trigger-ico') + '<span class="mp-trigger-text">Loading models…</span>' + icon('chevron', 'mp-trigger-chev');
    trigger.addEventListener('click', function () { open(''); });
    /* The select stays (models.js renders into it) but the button replaces it. */
    if (label) {
      label.hidden = true;
      label.parentNode.insertBefore(trigger, label.nextSibling);
    } else {
      select.hidden = true;
      select.parentNode.insertBefore(trigger, select.nextSibling);
    }
    return trigger;
  }

  function updateTrigger() {
    var button = ensureTrigger();
    if (!button) return;
    var text = button.querySelector('.mp-trigger-text');
    var row = currentRow();
    var switching = IM.models && IM.models.isSwitching && IM.models.isSwitching();
    var label;
    if (switching) label = 'Switching model…';
    else if (row) label = row.remote ? row.name + ' · ' + row.provider : row.name;
    else if (!data.loaded) label = 'Loading models…';
    else label = allRows().length ? 'Choose a model' : 'No model yet';
    text.textContent = label;
    button.dataset.state = switching ? 'switching' : (row ? (row.remote ? 'hosted' : 'local') : 'empty');
    button.innerHTML = icon(row && row.remote ? 'cloud' : 'chip', 'mp-trigger-ico') + text.outerHTML + icon('chevron', 'mp-trigger-chev');
    button.setAttribute('aria-label', 'Model: ' + label + '. Choose a model');
  }

  /* ------------------------------------------------------------ picker */

  var picker = null;
  var pickerInput = null;
  var pickerList = null;
  var pickerRows = [];
  var pickerIndex = 0;
  var pickerMoved = false; /* the user moved or typed since the picker opened */

  function buildPicker() {
    if (picker) return picker;
    picker = makeModal('model-picker', 'Choose a model', 'mp-dialog');
    picker.root.classList.add('mp-modal');
    picker.body.innerHTML =
      '<div class="mp-field">' + icon('search') +
        '<input id="model-picker-input" type="text" role="combobox" aria-expanded="true" aria-controls="model-picker-list" ' +
        'aria-autocomplete="list" placeholder="Search models and providers" autocomplete="off" spellcheck="false" ' +
        'aria-label="Search models and providers"></div>' +
      '<div class="mp-list" id="model-picker-list" role="listbox" aria-label="Models"></div>' +
      '<div class="mp-foot">' +
        '<span class="mp-keys"><kbd>&uarr;</kbd><kbd>&darr;</kbd> move <kbd>Enter</kbd> use <kbd>Esc</kbd> close</span>' +
        '<button class="btn" type="button" data-mp-manage>' + icon('sliders') + 'Manage models in Settings</button>' +
      '</div>';
    pickerInput = picker.body.querySelector('input');
    pickerList = picker.body.querySelector('.mp-list');
    pickerInput.addEventListener('input', function () {
      pickerIndex = 0;
      pickerMoved = true;
      renderPicker();
    });
    pickerInput.addEventListener('keydown', function (event) {
      if (/^(Arrow|Home$|End$)/.test(event.key)) pickerMoved = true;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        if (!pickerRows.length) return;
        event.preventDefault();
        pickerIndex = (pickerIndex + (event.key === 'ArrowDown' ? 1 : -1) + pickerRows.length) % pickerRows.length;
        renderPicker();
      } else if (event.key === 'Home' || event.key === 'End') {
        if (!pickerRows.length || pickerInput.value) return;
        event.preventDefault();
        pickerIndex = event.key === 'Home' ? 0 : pickerRows.length - 1;
        renderPicker();
      } else if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        choose(pickerRows[pickerIndex]);
      }
    });
    /* mousedown keeps focus in the search box; click does the choosing. */
    pickerList.addEventListener('mousedown', function (event) {
      if (event.target.closest && event.target.closest('[data-mp-index]')) event.preventDefault();
    });
    pickerList.addEventListener('click', function (event) {
      var row = event.target.closest ? event.target.closest('[data-mp-index]') : null;
      if (row) choose(pickerRows[Number(row.dataset.mpIndex)]);
    });
    pickerList.addEventListener('mousemove', function (event) {
      var row = event.target.closest ? event.target.closest('[data-mp-index]') : null;
      if (!row) return;
      var index = Number(row.dataset.mpIndex);
      if (index !== pickerIndex) {
        pickerMoved = true;
        pickerIndex = index;
        markActive();
      }
    });
    picker.body.querySelector('[data-mp-manage]').addEventListener('click', function () {
      closeModal(picker, true);
      openSettingsCard();
    });
    return picker;
  }

  function renderPicker() {
    var query = pickerInput.value.trim();
    var rows = allRows().filter(function (row) { return matches(row, query); });
    var addRow = { action: 'add', id: '__add', name: 'Add a provider', provider: 'NVIDIA, OpenRouter, DeepSeek, OpenAI, Groq, Together, Mistral or your own server' };
    pickerRows = rows.concat([addRow]);
    pickerIndex = Math.min(pickerIndex, pickerRows.length - 1);
    var html = '';
    var group = null;
    var n = 0;
    if (!rows.length) {
      html += '<p class="mp-empty" role="presentation">' + (query
        ? 'No model matches “' + esc(query) + '”.'
        : (data.loaded ? 'No model yet. Download one in Settings, Models, or add a provider below.' : 'Loading models…')) + '</p>';
    }
    rows.forEach(function (row, index) {
      if (row.group !== group) {
        if (group !== null) html += '</div>';
        group = row.group;
        n += 1;
        html += '<div class="mp-group" role="group" aria-labelledby="mp-group-' + n + '">' +
          '<div class="mp-group-title" id="mp-group-' + n + '" role="presentation">' +
          icon(row.remote ? 'cloud' : 'chip') + esc(row.groupLabel) + '</div>';
      }
      html += optionHtml(row, index);
    });
    if (group !== null) html += '</div>';
    html += '<div class="mp-group mp-group-add" role="group" aria-label="Providers">' + optionHtml(addRow, pickerRows.length - 1) + '</div>';
    pickerList.innerHTML = html;
    markActive();
  }

  function optionHtml(row, index) {
    var meta = row.action ? esc(row.provider)
      : esc(row.remote ? row.provider : 'Local') + (row.detail ? ' · ' + esc(row.detail) : '');
    return '<div class="mp-option' + (row.current ? ' is-current' : '') + (row.action ? ' is-action' : '') + '" role="option" ' +
      'id="mp-opt-' + index + '" data-mp-index="' + index + '" aria-selected="false">' +
      (row.action ? icon('plus', 'mp-option-ico') : '') +
      '<span class="mp-option-main"><span class="mp-option-name">' + esc(row.name) +
        (row.current ? '<span class="sr-only"> (current model)</span>' : '') + '</span>' +
      '<span class="mp-option-meta">' + meta + '</span></span>' +
      (row.current ? '<span class="mp-current" aria-hidden="true">' + icon('check') + 'Current</span>' : '') +
    '</div>';
  }

  function markActive() {
    var nodes = pickerList.querySelectorAll('[data-mp-index]');
    Array.prototype.forEach.call(nodes, function (node) {
      var on = Number(node.dataset.mpIndex) === pickerIndex;
      node.classList.toggle('is-active', on);
      node.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
    });
    if (pickerRows.length) pickerInput.setAttribute('aria-activedescendant', 'mp-opt-' + pickerIndex);
    else pickerInput.removeAttribute('aria-activedescendant');
  }

  function open(query, returnTo) {
    buildPicker();
    pickerInput.value = query || '';
    pickerIndex = 0;
    renderPicker();
    /* Start on the current model so Enter keeps it and arrows move from it. */
    if (!query) seekCurrent();
    pickerMoved = false;
    openModal(picker, pickerInput, returnTo);
    return loadAll().then(function () {
      if (!isOpen(picker)) return;
      renderPicker();
      if (!pickerMoved && !pickerInput.value) seekCurrent();
    });
  }

  function seekCurrent() {
    var current = pickerRows.map(function (row) { return row.current; }).indexOf(true);
    if (current >= 0 && current !== pickerIndex) {
      pickerIndex = current;
      markActive();
    }
  }

  function choose(row) {
    if (!row) return;
    if (row.action === 'add') {
      var back = picker.returnTo;
      closeModal(picker, true);
      connect('', { returnTo: back });
      return;
    }
    closeModal(picker);
    if (row.current) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Model', title: 'Already using ' + row.name, text: row.provider, timeout: 2500 });
      return;
    }
    selectRow(row);
  }

  function selectRow(row) {
    var done;
    if (IM.models && IM.models.select) done = IM.models.select(row.id); /* toasts success or failure */
    else {
      var bridge = api();
      done = bridge.selectModel(row.id).then(function (result) {
        toast({ tone: 'success', icon: 'check', kicker: 'Model', title: 'Model selected', text: (result && result.selected) || row.name });
        return true;
      }, function (error) {
        toast({ tone: 'error', icon: 'alert', kicker: 'Model', title: 'Could not switch to ' + row.name, text: errorText(error) });
        return false;
      });
    }
    updateTrigger();
    return Promise.resolve(done).then(function (ok) {
      return loadModels().then(function () { return ok; }, function () { return ok; });
    });
  }

  /* `/model` opens the picker; `/model <name>` switches when the name
   * matches exactly one model (by name, model id or provider:id), else
   * opens the picker with the name as the search. */
  function command(arg) {
    var query = String(arg || '').trim();
    if (!query) return open('');
    return loadAll().then(function () {
      var lowered = query.toLowerCase();
      var rows = allRows();
      var exact = rows.filter(function (row) {
        return [row.name, row.model, row.id, row.name + ' (' + row.provider + ')'].some(function (value) {
          return String(value || '').toLowerCase() === lowered;
        });
      });
      var pool = exact.length ? exact : rows.filter(function (row) { return matches(row, query); });
      if (pool.length === 1) {
        if (pool[0].current) {
          toast({ tone: 'neutral', icon: 'info', kicker: 'Model', title: 'Already using ' + pool[0].name, text: pool[0].provider, timeout: 2500 });
          return;
        }
        selectRow(pool[0]);
        return;
      }
      if (!pool.length) toast({ tone: 'neutral', icon: 'info', kicker: 'Model', title: 'No model matches “' + query + '”', text: 'Pick one from the list.', timeout: 3500 });
      return open(query);
    });
  }

  /* --------------------------------------------------- connect dialog */

  var connectModal = null;
  var connectState = { id: '', options: {} };

  function buildConnect() {
    if (connectModal) return connectModal;
    connectModal = makeModal('provider-connect', 'Connect a provider', 'hp-connect');
    connectModal.body.innerHTML =
      '<p class="sub" id="provider-connect-sub"></p>' +
      '<label class="hm-field" data-hp-pick><span class="hm-k">Provider</span><select id="hp-provider"></select></label>' +
      '<label class="hm-field" data-hp-custom><span class="hm-k">Name shown in the model list</span>' +
        '<input id="hp-name" maxlength="60" autocomplete="off" spellcheck="false" placeholder="My server"></label>' +
      '<label class="hm-field" data-hp-custom><span class="hm-k">Base URL</span>' +
        '<input id="hp-url" type="url" autocomplete="off" spellcheck="false" placeholder="https://llm.example.com/v1">' +
        '<span class="hm-hint">The address that ends before /chat/completions. Use https; plain http only works for this computer (localhost).</span></label>' +
      '<label class="hm-field"><span class="hm-k">API key</span>' +
        '<span class="hm-key-row"><input id="hp-key" type="password" autocomplete="off" spellcheck="false">' +
        '<button class="btn" type="button" id="hp-key-show" aria-controls="hp-key" aria-pressed="false">Show</button></span>' +
        '<span class="hm-hint" id="hp-key-hint"></span></label>' +
      '<label class="hm-check" data-hp-custom><input type="checkbox" id="hp-nokey"> This server needs no key</label>' +
      '<p class="hm-link-row" id="hp-get-key"></p>' +
      '<p class="hm-note" id="hp-note"></p>' +
      '<p class="hm-error" id="hp-error" role="alert" hidden></p>' +
      '<div class="dialog-actions"><button class="cancel" type="button" data-hm-close>Cancel</button>' +
        '<button class="send" type="button" id="hp-save">Verify and save</button></div>';
    var select = byId('hp-provider');
    select.addEventListener('change', function () { fillConnect(select.value); });
    byId('hp-key-show').addEventListener('click', function () {
      var input = byId('hp-key');
      var shown = input.type === 'text';
      input.type = shown ? 'password' : 'text';
      this.textContent = shown ? 'Show' : 'Hide';
      this.setAttribute('aria-pressed', shown ? 'false' : 'true');
    });
    byId('hp-nokey').addEventListener('change', function () {
      byId('hp-key').disabled = this.checked;
    });
    byId('hp-save').addEventListener('click', saveConnect);
    connectModal.body.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && event.target.tagName === 'INPUT' && event.target.type !== 'checkbox') {
        event.preventDefault();
        saveConnect();
      }
    });
    connectModal.onClose = function () {
      var input = byId('hp-key');
      if (input) input.value = ''; /* the key never lingers in the page */
    };
    return connectModal;
  }

  function fillConnect(id) {
    var provider = providerById(id);
    connectState.id = id;
    var custom = Boolean(provider && provider.custom);
    Array.prototype.forEach.call(connectModal.body.querySelectorAll('[data-hp-custom]'), function (node) { node.hidden = !custom; });
    connectModal.title.textContent = provider
      ? (provider.configured ? 'Change the ' + provider.label + ' key' : 'Connect ' + provider.label)
      : 'Connect a provider';
    byId('provider-connect-sub').textContent = provider ? (provider.description || '') : '';
    var keyInput = byId('hp-key');
    keyInput.value = '';
    keyInput.type = 'password';
    keyInput.disabled = false;
    byId('hp-key-show').textContent = 'Show';
    byId('hp-nokey').checked = Boolean(provider && provider.keyless);
    keyInput.disabled = Boolean(provider && provider.keyless);
    var keepable = custom && provider.configured && provider.key_hint;
    keyInput.placeholder = keepable ? 'Leave empty to keep the saved key (' + provider.key_hint + ')'
      : (provider ? provider.key_format : '');
    byId('hp-key-hint').textContent = provider ? provider.key_format + '.' : '';
    byId('hp-name').value = custom ? (provider.name || '') : '';
    byId('hp-url').value = custom ? (provider.base_url || '') : '';
    var link = byId('hp-get-key');
    if (provider && provider.key_url) {
      link.hidden = false;
      link.innerHTML = '<a href="' + esc(provider.key_url) + '" target="_blank" rel="noopener noreferrer">' +
        'Get a key at ' + esc(hostOf(provider.key_url)) + icon('external') + '</a>';
    } else {
      link.hidden = true;
      link.innerHTML = '';
    }
    var host = provider && !custom ? (provider.hosts && provider.hosts[0]) || hostOf(provider.base_url) : '';
    byId('hp-note').textContent = provider
      ? (custom
          ? 'The key is checked with one request to the address above and saved on this computer. It is only ever sent to that host, and it is never shown again.'
          : 'The key is checked with one request to ' + host + ' and saved on this computer. It is only ever sent to ' + host + ', and it is never shown again.')
      : '';
    setConnectError('');
  }

  function setConnectError(message, field) {
    var node = byId('hp-error');
    node.textContent = message || '';
    node.hidden = !message;
    ['hp-key', 'hp-url', 'hp-name'].forEach(function (id) { byId(id).removeAttribute('aria-invalid'); });
    var target = field === 'base_url' ? byId('hp-url') : field === 'name' ? byId('hp-name') : (message ? byId('hp-key') : null);
    if (target && message) {
      target.setAttribute('aria-invalid', 'true');
      if (!target.disabled && !target.closest('[hidden]')) target.focus();
    }
  }

  function connect(id, options) {
    buildConnect();
    connectState.options = options || {};
    connectState.saved = false;
    return loadProviders().catch(function () { return data.providers; }).then(function () {
      var select = byId('hp-provider');
      var pick = connectModal.body.querySelector('[data-hp-pick]');
      select.innerHTML = data.providers.map(function (p) {
        return '<option value="' + esc(p.id) + '">' + esc(p.label) + (p.configured ? ' (connected)' : '') + '</option>';
      }).join('');
      var target = id || (data.providers.filter(function (p) { return !p.configured; })[0] || data.providers[0] || {}).id || '';
      select.value = target;
      pick.hidden = Boolean(id);
      fillConnect(target);
      var provider = providerById(target);
      var first = id ? (provider && provider.custom ? byId('hp-url') : byId('hp-key')) : select;
      openModal(connectModal, first, connectState.options.returnTo);
    });
  }

  function saveConnect() {
    var provider = providerById(connectState.id);
    if (!provider) return;
    var button = byId('hp-save');
    if (button.disabled) return;
    var body = { key: byId('hp-key').value.trim() };
    if (provider.custom) {
      body.base_url = byId('hp-url').value.trim();
      body.name = byId('hp-name').value.trim();
      if (byId('hp-nokey').checked) {
        body.no_key = true;
        body.key = '';
      }
      if (!body.base_url) {
        setConnectError('Enter the server’s base URL.', 'base_url');
        return;
      }
    } else if (!body.key) {
      setConnectError('Paste the ' + provider.label + ' key.', 'key');
      return;
    }
    setConnectError('');
    button.disabled = true;
    button.textContent = 'Checking…';
    api().saveProviderKey(provider.id, body).then(function (view) {
      connectState.saved = true;
      var back = connectModal.returnTo;
      closeModal(connectModal, true);
      var count = (view.enabled || []).length;
      toast({ tone: view.verified === false ? 'neutral' : 'success', icon: view.verified === false ? 'info' : 'check',
        kicker: 'Hosted models', title: 'Connected to ' + (view.label || provider.label),
        text: view.warning || (count ? count + (count === 1 ? ' model is' : ' models are') + ' switched on. Check the list, then pick one to use.'
          : 'Choose which of its models to use.'), timeout: 6000 });
      /* Next step: choose its models, then pick one to switch to. */
      return refreshEverything().then(function () {
        return chooseModels(provider.id, { returnTo: back, thenPick: true });
      });
    }, function (error) {
      setConnectError(errorText(error, 'The key could not be saved.'), error && error.body && error.body.field);
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Verify and save';
    });
  }

  /* ------------------------------------------------ choose-models dialog */

  var modelsModal = null;
  var modelsState = { id: '', rows: [], enabled: [], query: '' };

  function buildModelsDialog() {
    if (modelsModal) return modelsModal;
    modelsModal = makeModal('provider-models', 'Choose models', 'hp-models');
    modelsModal.body.innerHTML =
      '<p class="sub" id="hp-models-sub">Switched-on models appear in the model picker.</p>' +
      '<div class="hp-models-tools">' +
        '<div class="mp-field hp-models-search">' + icon('search') +
          '<input id="hp-models-search" type="search" placeholder="Search this provider’s models" aria-label="Search this provider’s models" autocomplete="off" spellcheck="false"></div>' +
        '<button class="btn" type="button" id="hp-models-refresh">Reload the list</button>' +
      '</div>' +
      '<p class="hp-models-count" id="hp-models-count" aria-live="polite"></p>' +
      '<div class="hp-models-list" id="hp-models-list" role="group" aria-label="Models"></div>' +
      '<div class="hp-models-add"><label class="hm-field"><span class="hm-k">Add a model by its id</span>' +
        '<span class="hm-key-row"><input id="hp-models-manual" autocomplete="off" spellcheck="false" placeholder="for example vendor/model-name">' +
        '<button class="btn" type="button" id="hp-models-manual-add">Add</button></span></label></div>' +
      '<p class="hm-error" id="hp-models-error" role="alert" hidden></p>' +
      '<div class="dialog-actions"><button class="cancel" type="button" data-hm-close>Cancel</button>' +
        '<button class="send" type="button" id="hp-models-save">Save</button></div>';
    byId('hp-models-search').addEventListener('input', function () {
      modelsState.query = this.value;
      renderModelsList();
    });
    byId('hp-models-refresh').addEventListener('click', function () { fetchModels(true); });
    byId('hp-models-list').addEventListener('change', function (event) {
      var box = event.target;
      if (!box || box.type !== 'checkbox') return;
      var id = box.value;
      modelsState.touched = true;
      var at = modelsState.enabled.indexOf(id);
      if (box.checked && at === -1) modelsState.enabled.push(id);
      if (!box.checked && at !== -1) modelsState.enabled.splice(at, 1);
      renderCount();
    });
    function addManual() {
      var input = byId('hp-models-manual');
      var id = input.value.trim();
      if (!id) return;
      if (!/^[\w.\-/:@+]{1,200}$/.test(id)) {
        setModelsError('A model id has no spaces, for example vendor/model-name.');
        return;
      }
      setModelsError('');
      if (!modelsState.rows.some(function (row) { return row.id === id; })) {
        modelsState.rows.unshift({ id: id, name: id, owner: '', context: null, listed: false });
      }
      if (modelsState.enabled.indexOf(id) === -1) modelsState.enabled.push(id);
      modelsState.touched = true;
      input.value = '';
      renderModelsList();
    }
    byId('hp-models-manual-add').addEventListener('click', addManual);
    byId('hp-models-manual').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        addManual();
      }
    });
    byId('hp-models-save').addEventListener('click', saveModels);
    return modelsModal;
  }

  function setModelsError(message) {
    var node = byId('hp-models-error');
    node.textContent = message || '';
    node.hidden = !message;
  }

  function renderCount() {
    var n = modelsState.enabled.length;
    byId('hp-models-count').textContent = n + (n === 1 ? ' model' : ' models') + ' switched on' +
      (modelsState.rows.length ? ' · ' + modelsState.rows.length + ' available' : '');
  }

  function renderModelsList() {
    var list = byId('hp-models-list');
    var query = modelsState.query.trim();
    var rows = modelsState.rows.filter(function (row) {
      return matches({ name: row.name || '', model: row.id, provider: row.owner || '', id: row.id }, query);
    });
    /* Switched-on models first, so the choice is visible without scrolling. */
    rows.sort(function (a, b) {
      return (modelsState.enabled.indexOf(a.id) === -1) - (modelsState.enabled.indexOf(b.id) === -1);
    });
    var shown = rows.slice(0, RENDER_LIMIT);
    if (modelsState.loading) list.innerHTML = '<p class="mp-empty">Loading the list…</p>';
    else if (!shown.length) {
      list.innerHTML = '<p class="mp-empty">' + (query ? 'No model matches “' + esc(query) + '”.'
        : (modelsState.hasModels === false ? 'This server has no model list. Add the ids it serves below.' : 'No models to show.')) + '</p>';
    } else {
      list.innerHTML = shown.map(function (row, index) {
        var on = modelsState.enabled.indexOf(row.id) !== -1;
        var bits = [];
        if (row.context) bits.push(contextText(row.context));
        if (row.listed === false) bits.push('added by hand');
        return '<label class="hp-model-row' + (on ? ' is-on' : '') + '" for="hp-model-' + index + '">' +
          '<input type="checkbox" id="hp-model-' + index + '" value="' + esc(row.id) + '"' + (on ? ' checked' : '') + '>' +
          '<span class="hp-model-main"><span class="hp-model-name">' + esc(row.name || row.id) + '</span>' +
          '<code class="hp-model-id">' + esc(row.id) + '</code></span>' +
          (bits.length ? '<span class="hp-model-meta">' + esc(bits.join(' · ')) + '</span>' : '') +
          (row.recommended ? '<span class="hp-tag">Suggested</span>' : '') +
        '</label>';
      }).join('') + (rows.length > shown.length
        ? '<p class="mp-empty">Showing the first ' + RENDER_LIMIT + ' of ' + rows.length + '. Search to narrow the list.</p>' : '');
    }
    renderCount();
  }

  function fetchModels(refresh) {
    modelsState.loading = true;
    setModelsError('');
    renderModelsList();
    return api().providerModels(modelsState.id, refresh).then(function (view) {
      modelsState.rows = (view && view.models) || [];
      modelsState.hasModels = view ? view.has_models !== false : true;
      if (!modelsState.touched) modelsState.enabled = ((view && view.enabled) || []).slice();
    }, function (error) {
      setModelsError(errorText(error, 'The list could not be loaded.') + ' You can still add model ids by hand.');
    }).then(function () {
      modelsState.loading = false;
      renderModelsList();
    });
  }

  function chooseModels(id, options) {
    buildModelsDialog();
    var provider = providerById(id);
    modelsState = { id: id, rows: [], enabled: provider ? (provider.enabled || []).slice() : [], query: '',
      loading: true, touched: false, thenPick: Boolean(options && options.thenPick) };
    modelsModal.title.textContent = 'Choose ' + (provider ? provider.label : '') + ' models';
    byId('hp-models-save').textContent = modelsState.thenPick ? 'Save and pick a model' : 'Save';
    byId('hp-models-search').value = '';
    byId('hp-models-manual').value = '';
    openModal(modelsModal, byId('hp-models-search'), options && options.returnTo);
    return fetchModels(false);
  }

  function saveModels() {
    var button = byId('hp-models-save');
    if (button.disabled) return;
    button.disabled = true;
    var provider = providerById(modelsState.id);
    api().saveProviderModels(modelsState.id, modelsState.enabled).then(function (view) {
      var thenPick = modelsState.thenPick && (view.enabled || []).length > 0;
      var savedId = modelsState.id;
      var back = modelsModal.returnTo;
      closeModal(modelsModal, thenPick);
      var n = (view.enabled || []).length;
      var label = provider ? provider.label : 'Provider';
      toast({ tone: 'success', icon: 'check', kicker: 'Hosted models', title: label + ' models saved',
        text: n + (n === 1 ? ' model is' : ' models are') + ' in the model picker' + (thenPick ? '. Pick one to switch to it.' : '.'), timeout: 3500 });
      return refreshEverything().then(function () {
        /* After connecting: the picker, narrowed to this provider, to switch. */
        if (!thenPick) return;
        var fresh = providerById(savedId); /* a custom server's name may be new */
        return open(fresh ? fresh.label : label, back);
      });
    }, function (error) {
      setModelsError(errorText(error, 'The list could not be saved.'));
    }).then(function () { button.disabled = false; });
  }

  /* ------------------------------------------- Settings > Models card */

  var card = null;
  var intro = null;
  var pendingRemove = { id: '', timer: null };

  function ensureCard() {
    if (card && document.contains(card)) return card;
    var section = document.querySelector('[data-settings-panel="models"]');
    if (!section) return null;
    card = document.createElement('div');
    card.className = 'panel settings-card hp-card';
    card.id = 'hosted-providers';
    card.innerHTML =
      '<div class="hp-card-head"><div><h3 class="settings-label">Hosted providers</h3>' +
        '<p class="hp-card-sub">Use models that run on a provider’s servers. Replies then need an internet connection. ' +
        'Each key is stored on this computer and is only ever sent to its own provider.</p></div>' +
        '<button class="btn" type="button" data-hp-add>' + icon('plus') + 'Add a provider</button></div>' +
      '<div class="hp-list" id="hosted-providers-list" aria-live="polite"><p class="mp-empty">Loading providers…</p></div>';
    section.appendChild(card); /* after the local models: they stay the first part */
    card.addEventListener('click', onCardClick);
    /* ...but the section opens with a one-line intro so hosted models are
     * visible at first look, not only after scrolling past the local list. */
    intro = document.createElement('div');
    intro.className = 'hp-intro';
    intro.id = 'hosted-intro';
    intro.innerHTML =
      '<span class="hp-row-ico" aria-hidden="true">' + icon('cloud') + '</span>' +
      '<div class="hp-intro-main"><strong>Hosted providers</strong>' +
        '<span class="hp-intro-text" id="hosted-intro-text">Use NVIDIA, OpenRouter, DeepSeek, OpenAI, Groq, Together, Mistral or your own server with an API key.</span></div>' +
      '<div class="hp-row-actions">' +
        '<button class="btn" type="button" data-hp-jump>See hosted providers</button>' +
        '<button class="btn primary" type="button" data-hp-add>' + icon('plus') + 'Connect a provider</button></div>';
    var head = section.querySelector('.settings-section-head');
    if (head && head.nextSibling) section.insertBefore(intro, head.nextSibling);
    else section.insertBefore(intro, section.firstChild);
    intro.addEventListener('click', function (event) {
      var target = event.target.closest ? event.target.closest('button') : null;
      if (!target) return;
      if (target.hasAttribute('data-hp-jump')) {
        if (card.scrollIntoView) card.scrollIntoView({ block: 'start', behavior: 'smooth' });
        var first = card.querySelector('.hp-row button');
        if (first) first.focus({ preventScroll: true });
      } else onCardClick(event);
    });
    return card;
  }

  function renderIntro() {
    var text = byId('hosted-intro-text');
    if (!text || !data.providers.length) return;
    var on = data.providers.filter(function (p) { return p.configured; });
    var active = on.filter(function (p) { return p.active; })[0];
    text.textContent = on.length
      ? on.length + ' connected: ' + on.map(function (p) { return p.label; }).join(', ') + '.' +
        (active ? ' Replies now come from ' + active.label + '.' : ' Pick one of their models with ' + MOD + ' M or /model.')
      : 'Not set up yet. Use NVIDIA, OpenRouter, DeepSeek, OpenAI, Groq, Together, Mistral or your own server with an API key.';
  }

  function renderProvidersCard() {
    var root = ensureCard();
    if (!root) return;
    var list = root.querySelector('.hp-list');
    renderIntro();
    if (!data.providers.length) {
      list.innerHTML = '<p class="mp-empty">' + esc(data.providersError ? 'Could not load providers: ' + data.providersError : 'Loading providers…') + '</p>';
      return;
    }
    list.innerHTML = data.providers.map(function (p) {
      var state;
      if (!p.configured) state = '<span class="hp-status">Not set up</span>';
      else {
        var bits = [];
        if (p.keyless) bits.push('no key needed');
        else if (p.key_hint) bits.push('key ' + p.key_hint);
        if (p.from_env) bits.push('from the ' + esc(p.provider === 'nvidia' ? 'NVIDIA_API_KEY' : 'environment') + ' variable');
        var n = (p.enabled || []).length;
        bits.push(n + (n === 1 ? ' model' : ' models') + ' on');
        if (p.custom && p.base_url) bits.push(hostOf(p.base_url));
        state = '<span class="hp-status is-on">' + (p.active ? 'In use' : 'Connected') + '</span>' +
          '<span class="hp-status-meta">' + esc(bits.join(' · ')) + '</span>';
      }
      var removing = pendingRemove.id === p.id;
      var actions = p.configured
        ? '<button class="btn" type="button" data-hp-models="' + esc(p.id) + '">Choose models</button>' +
          '<button class="btn" type="button" data-hp-connect="' + esc(p.id) + '">' + (p.custom ? 'Edit' : 'Change key') + '</button>' +
          (p.from_env ? '' : '<button class="btn danger" type="button" data-hp-remove="' + esc(p.id) + '">' +
            (removing ? 'Click again to remove' : 'Remove') + '</button>')
        : '<button class="btn primary" type="button" data-hp-connect="' + esc(p.id) + '">Connect</button>';
      return '<div class="hp-row' + (p.configured ? ' is-on' : '') + '" data-provider="' + esc(p.id) + '">' +
        '<span class="hp-row-ico" aria-hidden="true">' + icon(p.custom ? 'sliders' : 'cloud') + '</span>' +
        '<div class="hp-row-main"><strong>' + esc(p.label) + '</strong>' +
          '<span class="hp-row-desc">' + esc(p.description || '') + '</span>' +
          '<span class="hp-row-state">' + state + '</span></div>' +
        '<div class="hp-row-actions">' + actions + '</div>' +
      '</div>';
    }).join('');
  }

  function onCardClick(event) {
    var target = event.target.closest ? event.target.closest('button') : null;
    if (!target) return;
    if (target.hasAttribute('data-hp-add')) connect('', { returnTo: target });
    else if (target.dataset.hpConnect) connect(target.dataset.hpConnect, { returnTo: target });
    else if (target.dataset.hpModels) chooseModels(target.dataset.hpModels, { returnTo: target });
    else if (target.dataset.hpRemove) removeProvider(target.dataset.hpRemove);
  }

  function removeProvider(id) {
    var provider = providerById(id);
    if (!provider) return;
    if (pendingRemove.id !== id) {
      /* Two clicks: the first arms the button for a few seconds. */
      clearTimeout(pendingRemove.timer);
      pendingRemove = { id: id, timer: setTimeout(function () {
        pendingRemove = { id: '', timer: null };
        renderProvidersCard();
      }, 4000) };
      renderProvidersCard();
      var again = card.querySelector('[data-hp-remove="' + id + '"]');
      if (again) again.focus();
      return;
    }
    clearTimeout(pendingRemove.timer);
    pendingRemove = { id: '', timer: null };
    api().removeProvider(id).then(function () {
      toast({ tone: 'success', icon: 'check', kicker: 'Hosted models', title: provider.label + ' removed',
        text: provider.active ? 'Replies use the local model again.' : 'Its key was deleted from this computer.', timeout: 4000 });
      return refreshEverything();
    }, function (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Hosted models', title: 'Could not remove ' + provider.label, text: errorText(error) });
      renderProvidersCard();
    });
  }

  function openSettingsCard() {
    if (IM.shell && IM.shell.openSettings) IM.shell.openSettings('models');
    loadProviders().catch(function () {});
    var root = ensureCard();
    if (root && root.scrollIntoView) setTimeout(function () { root.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, 60);
  }

  function refreshEverything() {
    var jobs = [loadProviders().catch(function () {})];
    if (IM.models && IM.models.refresh) jobs.push(Promise.resolve(IM.models.refresh()).catch(function () {}));
    else jobs.push(loadModels().catch(function () {}));
    if (IM.status && IM.status.refresh) IM.status.refresh();
    return Promise.all(jobs).then(updateTrigger);
  }

  /* -------------------------------------------------------------- wiring */

  function init() {
    ensureTrigger();
    ensureCard();
    updateTrigger();
    if (IM.bus) {
      IM.bus.on('models', function (models) {
        data.models = Array.isArray(models) ? models : data.models;
        data.loaded = true;
        updateTrigger();
        if (isOpen(picker)) renderPicker();
      });
      IM.bus.on('models.hosted', function (hosted) {
        data.hosted = Array.isArray(hosted) ? hosted : [];
        updateTrigger();
        if (isOpen(picker)) renderPicker();
      });
      IM.bus.on('settings.section', function (section) {
        if (section === 'models') {
          ensureCard();
          loadProviders().catch(function (error) {
            data.providersError = errorText(error);
            renderProvidersCard();
          });
        }
      });
      IM.bus.on('server:model.switch.done', function () { setTimeout(updateTrigger, 0); });
    }
    /* Ctrl/Cmd+M: no browser or app shortcut uses it here (panels.js has
     * K . B / and Alt N). macOS may keep Cmd+M for "minimise window". */
    document.addEventListener('keydown', function (event) {
      var mod = event.ctrlKey || event.metaKey;
      if (!mod || event.altKey || event.shiftKey) return;
      if (event.key !== 'm' && event.key !== 'M' && event.code !== 'KeyM') return;
      event.preventDefault();
      if (isOpen(picker)) closeModal(picker);
      else if (!isOpen(connectModal) && !isOpen(modelsModal)) open('');
    });
  }

  /* `/connect` opens the provider chooser; `/connect <provider>` (id or
   * name, e.g. nvidia, openrouter, deepseek, custom) opens that one. */
  function connectCommand(arg) {
    var query = String(arg || '').trim().toLowerCase();
    if (!query) return connect('', {});
    return loadProviders().catch(function () { return data.providers; }).then(function () {
      var hits = data.providers.filter(function (p) {
        return p.id === query || String(p.label || '').toLowerCase() === query;
      });
      if (!hits.length) {
        hits = data.providers.filter(function (p) {
          return p.id.indexOf(query) === 0 || String(p.label || '').toLowerCase().indexOf(query) === 0;
        });
      }
      if (hits.length === 1) return connect(hits[0].id, {});
      toast({ tone: 'neutral', icon: 'info', kicker: 'Connect', title: 'No provider called “' + String(arg).trim() + '”',
        text: 'Pick one from the list: ' + data.providers.map(function (p) { return p.id; }).join(', ') + '.', timeout: 5000 });
      return connect('', {});
    });
  }

  IM.modelPicker = {
    connectCommand: connectCommand,
    open: open,
    command: command,
    connect: function (id) { return connect(id || '', {}); },
    chooseModels: function (id) { return chooseModels(id, {}); },
    isOpen: function () { return isOpen(picker) || isOpen(connectModal) || isOpen(modelsModal); },
    refresh: refreshEverything
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
