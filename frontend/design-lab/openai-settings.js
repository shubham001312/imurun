/* Settings > Use by API: the OpenAI-compatible API (services/server/openai_api.py).
 *
 *   GET  /api/openai          {baseUrl, apiKey, model, available, contextWindow,
 *                              maxTokens, keyFile, keyCreatedAt, endpoints}
 *   POST /api/openai/rotate   same view with a new key (the old one stops now)
 *
 * Other apps (OpenClaw, the openai SDK, curl) use baseUrl + apiKey + model.
 * The key is persistent (it survives restarts), so it is always shown here,
 * masked until "Show". Copy buttons copy the real value, never the mask.
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

  var state = { view: null, shown: false, error: '', busy: false };

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

  function line(label, shown, copyId, extra) {
    return '<div class="settings-line oai-line"><span class="k">' + esc(label) + '</span>' +
      '<span class="v mono" data-oai-value="' + copyId + '">' + esc(shown) + '</span>' +
      '<span class="oai-actions">' + (extra || '') +
      '<button class="btn" type="button" data-oai-copy="' + copyId + '">Copy</button></span></div>';
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
      : '<span class="chip warn">no model chosen</span>';
    var html =
      '<div class="panel settings-card oai-card">' +
        '<div class="oai-head"><h3 class="settings-card-title">OpenAI-compatible API</h3>' + chip + '</div>' +
        '<p class="settings-note">Point any app that speaks the OpenAI API at these three values. ' +
        'The key stays the same across restarts until you regenerate it.</p>' +
        line('Base URL', view.baseUrl, 'base') +
        line('API key', key, 'key',
          '<button class="btn ghost" type="button" data-oai-toggle aria-pressed="' + (state.shown ? 'true' : 'false') + '">' +
          (state.shown ? 'Hide' : 'Show') + '</button>') +
        line('Model id', view.model || 'choose a model in Models first', 'model') +
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
    if (!window.confirm('Regenerate the API key? Apps using the current key (OpenClaw, scripts) stop working until you paste the new one.')) return;
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
    if (target.closest('[data-oai-toggle]')) { state.shown = !state.shown; render(); return; }
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
