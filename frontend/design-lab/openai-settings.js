/* Settings > Use by API: the OpenAI-compatible API (services/server/openai_api.py).
 *
 *   GET  /api/openai          {baseUrl, apiKey, model, available, contextWindow,
 *                              maxTokens, keyFile, keyCreatedAt, endpoints}
 *   POST /api/openai/rotate   same view with a new key (the old one stops now)
 *
 * Other apps (OpenClaw, the openai SDK, curl) use baseUrl + apiKey + model.
 * The key is persistent (it survives restarts) and is shown in full by
 * default, as asked; "Hide" masks it and the choice is remembered in
 * localStorage (HIDE_KEY). Copy buttons copy the real value, never the mask.
 * ES2017, no inline scripts (CSP default-src 'self').
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  function api() { return window.__imurun_api; }
  function byId(id) { return document.getElementById(id); }
  function esc(text) {
    if (IM.util && IM.util.escapeHtml) return IM.util.escapeHtml(String(text));
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }

  var HIDE_KEY = 'imurun.openai.keyHidden';
  function readHidden() {
    try { return window.localStorage.getItem(HIDE_KEY) === '1'; } catch (error) { return false; }
  }
  function writeHidden(hidden) {
    try {
      if (hidden) window.localStorage.setItem(HIDE_KEY, '1');
      else window.localStorage.removeItem(HIDE_KEY);
    } catch (error) { /* private window or blocked storage: keep it for this page only */ }
  }

  var state = { view: null, shown: !readHidden(), error: '', busy: false };

  function root() { return byId('openai-api-root'); }

  function mask(key) {
    if (!key) return '';
    return key.slice(0, 10) + '•'.repeat(16) + key.slice(-4);
  }

  function modelId(view) { return (view && view.model) || 'MODEL_ID'; }

  /* Examples are built from the real values; `key` is the real key or the
   * mask, so what is on screen matches what "Show" says. */
  function examples(view, key) {
    var base = view.baseUrl;
    var model = modelId(view);
    var curl =
      'curl ' + base + '/chat/completions \\\n' +
      '  -H "Authorization: Bearer ' + key + '" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -d \'{"model": "' + model + '", "messages": [{"role": "user", "content": "Hello"}]}\'';
    var python =
      '# pip install openai\n' +
      'from openai import OpenAI\n\n' +
      'client = OpenAI(base_url="' + base + '", api_key="' + key + '")\n\n' +
      'reply = client.chat.completions.create(\n' +
      '    model="' + model + '",\n' +
      '    messages=[{"role": "user", "content": "Hello"}],\n' +
      ')\n' +
      'print(reply.choices[0].message.content)\n\n' +
      '# streaming\n' +
      'for chunk in client.chat.completions.create(model="' + model + '", stream=True,\n' +
      '        messages=[{"role": "user", "content": "Tell me a joke"}]):\n' +
      '    print(chunk.choices[0].delta.content or "", end="", flush=True)';
    var openclaw =
      '// ~/.openclaw/openclaw.json (JSON5): merge into your existing config\n' +
      '{\n' +
      '  models: {\n' +
      '    mode: "merge",\n' +
      '    providers: {\n' +
      '      imurun: {\n' +
      '        baseUrl: "' + base + '",\n' +
      '        apiKey: "' + key + '",\n' +
      '        api: "openai-completions",\n' +
      '        models: [\n' +
      '          {\n' +
      '            id: "' + model + '",\n' +
      '            name: "IMURUN ' + model + '",\n' +
      '            reasoning: false,\n' +
      '            input: ["text"],\n' +
      '            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },\n' +
      '            contextWindow: ' + (Number(view.contextWindow) || 4096) + ',\n' +
      '            maxTokens: ' + (Number(view.maxTokens) || 2048) + ',\n' +
      '          },\n' +
      '        ],\n' +
      '      },\n' +
      '    },\n' +
      '  },\n' +
      '  agents: { defaults: { model: { primary: "imurun/' + model + '" } } },\n' +
      '}';
    return [
      { id: 'curl', label: 'curl', text: curl },
      { id: 'python', label: 'Python (openai)', text: python },
      { id: 'openclaw', label: 'OpenClaw', text: openclaw }
    ];
  }

  /* One value per row: label above, the value full width in monospace and
   * wrapping (never cut off), Copy (and Hide for the key) beside it. */
  function line(label, shown, copyId, extra, placeholder) {
    var id = 'oai-value-' + copyId;
    return '<div class="oai-line">' +
      '<span class="oai-k" id="' + id + '-label">' + esc(label) + '</span>' +
      '<div class="oai-field">' +
        '<code class="oai-v' + (placeholder ? ' is-placeholder' : '') + '" id="' + id + '" data-oai-value="' + copyId + '" ' +
          'tabindex="0" aria-labelledby="' + id + '-label">' + esc(shown) + '</code>' +
        '<span class="oai-actions">' + (extra || '') +
        (placeholder ? '' : '<button class="btn" type="button" data-oai-copy="' + copyId + '" aria-label="Copy the ' + esc(label.toLowerCase()) + '">Copy</button>') +
        '</span>' +
      '</div></div>';
  }

  function render() {
    var el = root();
    if (!el) return;
    var view = state.view;
    if (!view) {
      el.innerHTML = state.error
        ? '<div class="panel settings-card"><p class="tune-error">' + esc(state.error) + '</p>' +
          '<button class="btn" type="button" data-oai-reload>Retry</button></div>'
        : '<div class="sk sk-card"></div>';
      return;
    }
    var key = state.shown ? view.apiKey : mask(view.apiKey);
    var chip = view.available
      ? '<span class="chip success">model ready</span>'
      : '<span class="chip neutral">no model loaded</span>';
    /* Without a model the base URL and key still work for setup; say why
     * replies will fail instead of hiding the values. */
    var noModel = view.available ? '' :
      '<p class="oai-callout" role="note">The base URL and key below are ready to paste now. ' +
      'Apps get an answer only once a model is loaded: choose one in ' +
      '<button class="oai-link" type="button" data-oai-models>Settings → Models</button>.</p>';
    var html =
      '<div class="panel settings-card oai-card">' +
        '<div class="oai-head"><h3 class="settings-card-title">OpenAI-compatible API</h3>' + chip + '</div>' +
        '<p class="settings-note">Point any app that speaks the OpenAI API at these three values. ' +
        'The key stays the same across restarts until you regenerate it.</p>' +
        noModel +
        line('Base URL', view.baseUrl, 'base') +
        line('API key', key, 'key',
          '<button class="btn ghost" type="button" data-oai-toggle aria-label="' + (state.shown ? 'Hide' : 'Show') + ' the API key">' +
          (state.shown ? 'Hide' : 'Show') + '</button>') +
        line('Model id', view.model || 'No model loaded yet', 'model', '', !view.model) +
        '<div class="settings-actions oai-rotate">' +
          '<button class="btn" type="button" data-oai-rotate' + (state.busy ? ' disabled' : '') + '>' +
          (state.busy ? 'Regenerating…' : 'Regenerate key') + '</button>' +
        '</div>' +
        '<p class="settings-note">Endpoints: <span class="mono">GET /v1/models</span>, ' +
        '<span class="mono">POST /v1/chat/completions</span> (stream, max_tokens, temperature, top_p, stop). ' +
        'Loopback only: apps on this computer can connect, other machines cannot. ' +
        'Text only; tool calling is not offered on this API. The key is stored in ' +
        '<span class="mono">' + esc(view.keyFile || '') + '</span>.</p>' +
      '</div>' +
      '<div class="panel settings-card oai-card"><h3 class="settings-card-title">Ready to paste</h3>' +
      examples(view, key).map(function (ex) {
        return '<div class="md-codeblock"><div class="md-codehead"><span class="md-lang">' + esc(ex.label) + '</span>' +
          '<button class="md-copy" type="button" data-oai-copy="ex-' + ex.id + '" aria-label="Copy the ' + esc(ex.label) + ' example">Copy</button></div>' +
          '<pre class="md-code" tabindex="0"><code>' + esc(ex.text) + '</code></pre></div>';
      }).join('') +
      (view.model ? '' : '<p class="settings-note">MODEL_ID is a placeholder until a model is chosen in Models.</p>') +
      '<p class="settings-note">Copy always copies the real key, even while it is hidden.</p>' +
      '</div>';
    el.innerHTML = html;
  }

  function valueFor(id) {
    var view = state.view || {};
    if (id === 'base') return view.baseUrl || '';
    if (id === 'key') return view.apiKey || '';
    if (id === 'model') return view.model || '';
    if (id.indexOf('ex-') === 0) {
      var wanted = id.slice(3);
      var found = examples(view, view.apiKey || '').filter(function (ex) { return ex.id === wanted; })[0];
      return found ? found.text : '';
    }
    return '';
  }

  function copy(text, button) {
    if (!text) return;
    var label = button.textContent;
    if (IM.chat && IM.chat.copyText) { IM.chat.copyText(text, button, label); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = 'Copied';
        setTimeout(function () { button.textContent = label; }, 1400);
      }, function () {});
    }
  }

  function load() {
    var client = api();
    if (!client || !client.fetchAPI) return;
    client.fetchAPI('/api/openai').then(function (view) {
      state.view = view;
      state.error = '';
      render();
    }, function (error) {
      state.error = 'Could not read the API settings: ' + ((error && error.message) || 'the server did not answer.');
      render();
    });
  }

  function rotate() {
    var ask = IM.dialogs && IM.dialogs.confirm
      ? IM.dialogs.confirm({ title: 'Regenerate the API key?', text: 'Apps using the current key (OpenClaw, scripts) stop working until you paste the new one.', confirmLabel: 'Regenerate key', danger: true })
      : Promise.resolve(window.confirm('Regenerate the API key? Apps using the current key stop working until you paste the new one.'));
    ask.then(function (ok) { if (ok) doRotate(); });
  }

  function doRotate() {
    state.busy = true;
    render();
    api().fetchAPI('/api/openai/rotate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function (view) {
      state.view = view;
      state.busy = false;
      render();
      toast({ tone: 'success', icon: 'check', kicker: 'API', title: 'New API key', text: 'The old key no longer works. Paste the new one into your apps.' });
    }, function (error) {
      state.busy = false;
      render();
      toast({ tone: 'error', icon: 'alert', kicker: 'API', title: 'Could not regenerate the key', text: (error && error.message) || '' });
    });
  }

  function onClick(event) {
    var target = event.target;
    if (!target || !target.closest || !root() || !root().contains(target)) return;
    var button = target.closest('[data-oai-copy]');
    if (button) { copy(valueFor(button.getAttribute('data-oai-copy')), button); return; }
    if (target.closest('[data-oai-toggle]')) {
      state.shown = !state.shown;
      writeHidden(!state.shown);
      render();
      var again = root().querySelector('[data-oai-toggle]');
      if (again) again.focus();
      return;
    }
    if (target.closest('[data-oai-models]')) {
      if (IM.shell && IM.shell.openSettings) IM.shell.openSettings('models');
      return;
    }
    if (target.closest('[data-oai-rotate]')) { rotate(); return; }
    if (target.closest('[data-oai-reload]')) load();
  }

  function init() {
    document.addEventListener('click', onClick);
    if (IM.bus && IM.bus.on) {
      IM.bus.on('settings.section', function (section) { if (section === 'use-by-api') load(); });
      /* The model id in the examples follows the Models tab. */
      IM.bus.on('server:model.switch.done', function () { if (state.view) load(); });
    }
  }

  IM.openaiApi = { load: load, _state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
