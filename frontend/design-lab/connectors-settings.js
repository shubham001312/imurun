/* Settings > Connectors: one card per service (Gmail, GitHub, Slack, Vercel,
 * Render, Cloudflare, Supabase, Canva), to the contract in
 * docs/connectors/REQUESTS.md F1 (updated 2026-11-01).
 *
 * `auth_mode` from GET /api/connectors/{id}/config decides the card:
 *
 *   OAuth cards (Gmail, GitHub, Slack, Vercel, Supabase, Canva)
 *     One Connect button when a client id exists (`credential_source`
 *     builtin / env / user); "Not available yet" + `registration_todo` when it
 *     is `none`. Connect = POST .../connect {"open": true}: the SERVER opens the
 *     system browser (Google refuses sign-in inside an embedded webview); the
 *     card says "Finish signing in in your browser" and flips on the
 *     `connector` frame (no polling; one status read when the window gets the
 *     focus back, in case the event stream was down). 409 = already connected.
 *     GitHub also: "Connect without a browser" (device code).
 *     Advanced (collapsed): credential source, client id / secret (write-only,
 *     never prefilled), redirect URI + copy, scopes, options, Gmail "Import
 *     client file", the built-in apps' port note.
 *   Token cards (Cloudflare, Render)
 *     Password field, Verify & save = POST .../token {"token"}: 200 shows
 *     account + permissions, 401 shows the error and keeps the field; the
 *     token is never sent back. "Create a token" (token_console) with the
 *     minimum permissions per tool. Forget token = DELETE .../token.
 *   Every card: storage badge (store / store_encrypted / warning), Test,
 *     Disconnect, the tool list with levels (in Advanced), and "How it works"
 *     = GET .../doc (Markdown).
 *
 * Loaded when the section is first shown. One delegated listener per event
 * type on #conn-root; cards are patched in place, never rebuilt while the
 * user is typing in them. ES2017 (see docs/frontend/README.md).
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
    toast({ tone: 'error', icon: 'alert', kicker: 'Connectors', title: title, text: (error && error.message) || 'The server did not answer.' });
  }

  var ORDER = ['gmail', 'github', 'slack', 'vercel', 'supabase', 'canva', 'cloudflare', 'render'];
  var LABELS = { gmail: 'Gmail', github: 'GitHub', slack: 'Slack', vercel: 'Vercel', render: 'Render',
    cloudflare: 'Cloudflare', supabase: 'Supabase', canva: 'Canva' };
  /* The built-in apps of these providers match the redirect exactly. */
  var EXACT_PORT = { slack: true, vercel: true, supabase: true, canva: true };
  /* Fallback levels until /api/tools carries permission_level everywhere. */
  var GMAIL_LEVELS = { gmail_search: 'read', gmail_read: 'read', gmail_labels: 'read', gmail_draft: 'write',
    gmail_send: 'always', gmail_modify: 'always', gmail_trash: 'always' };
  var LEVEL_TEXT = {
    allow: ['runs', 'Runs without asking.'],
    read: ['reads', 'Reads your account. Runs without asking in "Reads run free" and "Allow everything"; asks in "Ask for everything".'],
    write: ['asks', 'Changes something. Asks first unless the mode is "Allow everything".'],
    ask: ['asks', 'Asks before every call; "remember" holds for the session.'],
    always: ['always asks', 'Asks in every mode, and an approval is never remembered.'],
    deny: ['off', 'Refused.']
  };
  var SOURCE_TEXT = {
    builtin: 'IMURUN\'s built-in app', env: 'your app, from the environment (IMURUN_OAUTH_<ID>_CLIENT_ID)',
    user: 'your own app (saved here)', none: 'no app yet'
  };

  var state = {
    bound: false,
    loading: false,
    list: {},        /* id -> GET /api/connectors item, merged with GET /api/connectors/{id} */
    configs: {},     /* id -> config view, or {error} */
    tools: null,     /* GET /api/tools .tools */
    docs: {},        /* id -> {markdown} | {missing: true} */
    tests: {},       /* id -> last test result */
    tokenResult: {}, /* id -> last Verify & save answer */
    pending: {}      /* id -> live sign-in controller */
  };

  function root() { return byId('conn-root'); }
  function label(id) { return (state.configs[id] && state.configs[id].label) || (state.list[id] && state.list[id].label) || LABELS[id] || id; }
  function mode(id) { var c = state.configs[id] || {}; return c.auth_mode === 'token' ? 'token' : 'oauth'; }
  function source(id) {
    var c = state.configs[id] || {};
    if (c.credential_source) return c.credential_source;
    if (c.configured === false) return 'none';
    return c.clientIdSource === 'environment' ? 'env' : (c.clientId ? 'user' : 'none');
  }

  /* ------------------------------------------------------------- derived */

  function toolsFor(id) {
    var cfg = state.configs[id] || {};
    var byName = {};
    (state.tools || []).forEach(function (tool) { if (tool && tool.name) byName[tool.name] = tool; });
    var names = Array.isArray(cfg.availableTools) && cfg.availableTools.length ? cfg.availableTools.slice() : [];
    Object.keys(byName).forEach(function (name) {
      if (name.indexOf(id + '_') === 0 && names.indexOf(name) === -1) names.push(name);
    });
    var enabled = Array.isArray(cfg.tools) ? cfg.tools : null;
    return names.map(function (name) {
      var tool = byName[name] || {};
      var level = tool.permission_level || tool.level ||
        (id === 'gmail' && GMAIL_LEVELS[name]) ||
        (tool.category === 'connector-read' ? 'read' : (tool.category ? 'always' : ''));
      if (tool.permission === 'deny' && tool.available !== false) level = 'deny';
      return {
        name: name,
        description: String(tool.description || '').replace(/^\[[^\]]*\]\s*/, ''),
        level: level,
        on: enabled ? enabled.indexOf(name) !== -1 : true,
        available: tool.available,
        reason: tool.reason || ''
      };
    });
  }

  function connected(id) {
    var item = state.list[id] || {};
    var cfg = state.configs[id] || {};
    return Boolean(item.connected || (mode(id) === 'token' && cfg.has_token));
  }

  function account(id) {
    var item = state.list[id] || {};
    var cfg = state.configs[id] || {};
    var test = state.tests[id] || {};
    return item.account || cfg.account || (test.ok && test.account) || '';
  }

  function status(id) {
    var item = state.list[id] || {};
    var cfg = state.configs[id] || {};
    var acct = account(id);
    if (state.pending[id]) return { cls: 'neutral', text: 'Signing in…', key: 'pending', account: acct };
    if (connected(id)) return { cls: 'success', text: acct ? 'Connected as ' + acct : 'Connected', key: 'connected', account: acct };
    if (item.error) return { cls: 'error', text: 'Needs sign-in again', key: 'error', account: acct };
    if (cfg.error) return { cls: 'neutral', text: 'Status only', key: 'noconfig', account: acct };
    if (mode(id) === 'token') return { cls: 'neutral', text: 'No token yet', key: 'unconfigured', account: acct };
    if (source(id) === 'none') return { cls: 'neutral', text: 'Not available yet', key: 'unavailable', account: acct };
    return { cls: 'neutral', text: 'Not connected', key: 'ready', account: acct };
  }

  /* "Encrypted with Windows DPAPI" & co. (F1 storage badge). */
  function storeBadge(cfg) {
    if (!cfg || cfg.store === undefined) return null;
    var name = String(cfg.store || '');
    var text = /dpapi/i.test(name) ? 'Encrypted with Windows DPAPI'
      : /keychain|macos/i.test(name) ? 'macOS Keychain'
      : /libsecret|secret-tool/i.test(name) ? 'libsecret'
      : /aes/i.test(name) ? 'AES-GCM file'
      : /keyring/i.test(name) ? 'OS keychain'
      : name || 'local store';
    if (cfg.store_encrypted === false) return { cls: 'warn', text: '⚠ not encrypted', title: cfg.warning || name };
    return { cls: 'neutral', text: text, title: name + (cfg.warning ? ' · ' + cfg.warning : '') };
  }

  /* -------------------------------------------------------------- render */

  function icon(id) {
    var icons = IM.shell && IM.shell.connectorIcons;
    return (icons && icons[id]) || '<span class="conn-initial">' + esc((label(id).charAt(0) || '?').toUpperCase()) + '</span>';
  }

  function cardShell(id) {
    return '<article class="panel settings-card conn-card" data-conn="' + esc(id) + '" id="conn-card-' + esc(id) + '">' +
      '<header class="conn-head">' +
        '<span class="connector-mark connector-mark--' + esc(id) + '" aria-hidden="true">' + icon(id) + '</span>' +
        '<div class="conn-title"><h3>' + esc(label(id)) + ' <span class="chip conn-store" data-conn-store hidden></span></h3><p class="conn-blurb" data-conn-blurb></p></div>' +
        '<span class="chip neutral" data-conn-status role="status">…</span>' +
      '</header>' +
      '<p class="conn-statusline" data-conn-statusline hidden></p>' +
      '<div class="conn-unavail" data-conn-unavail hidden></div>' +
      /* OAuth actions */
      '<div class="conn-actions" data-conn-oauth>' +
        '<button class="btn primary" type="button" data-conn-connect>Connect</button>' +
        '<button class="btn" type="button" data-conn-device hidden title="No browser redirect: type a short code on github.com">Connect without a browser</button>' +
        '<button class="btn" type="button" data-conn-disconnect hidden>Disconnect</button>' +
        '<button class="btn" type="button" data-conn-test hidden>Test</button>' +
        '<button class="btn ghost" type="button" data-conn-cancel hidden>Cancel sign-in</button>' +
        '<span class="conn-note" data-conn-note></span>' +
      '</div>' +
      /* Token cards */
      '<form class="conn-token" data-conn-tokenform autocomplete="off" hidden>' +
        '<div class="conn-token-row"><label class="form-field"><span class="k">API token</span>' +
          '<input type="password" data-conn-token autocomplete="new-password" spellcheck="false" placeholder="Paste the token"></label>' +
          '<button class="btn primary" type="submit">Verify &amp; save</button>' +
          '<button class="btn" type="button" data-conn-test hidden>Test</button>' +
          '<button class="btn ghost danger" type="button" data-conn-forget hidden>Forget token</button></div>' +
        '<p class="conn-token-result" data-conn-tokenresult role="status" hidden></p>' +
        '<div class="conn-token-help" data-conn-tokenhelp></div>' +
      '</form>' +
      '<div class="conn-signin" data-conn-signin hidden></div>' +
      '<div class="conn-test" data-conn-testout hidden></div>' +
      '<details class="conn-fold" data-conn-fold="advanced">' +
        '<summary>Advanced <span class="conn-fold-hint" data-conn-advhint></span></summary>' +
        '<div class="conn-adv-oauth" data-conn-advoauth>' +
          '<p class="settings-note" data-conn-source></p>' +
          '<form class="conn-form" data-conn-form autocomplete="off">' +
            '<label class="form-field"><span class="k">Client id</span>' +
              '<input data-conn-field="clientId" spellcheck="false" autocomplete="off" placeholder="Empty = IMURUN\'s built-in app"></label>' +
            '<div class="search-key"><label class="form-field"><span class="k">Client secret <span class="chip neutral" data-conn-secretstate>not set</span></span>' +
              '<input type="password" data-conn-field="clientSecret" autocomplete="new-password" spellcheck="false"></label>' +
              '<button class="btn ghost" type="button" data-conn-clearsecret hidden>Remove</button>' +
              '<small class="tool-field-hint">Write-only: stored by the local server, never shown again, never kept in this browser.</small></div>' +
            '<div class="conn-extra" data-conn-extra></div>' +
            '<div class="settings-line conn-redirect"><span class="k">Redirect URI</span><code class="v mono" data-conn-redirect>…</code>' +
              '<button class="btn ghost" type="button" data-conn-copy>Copy</button></div>' +
            (EXACT_PORT[id] ? '<p class="settings-note conn-port">' + esc(LABELS[id] || id) + ' matches the redirect URI exactly: the built-in app only works while IMURUN runs on port 8732 (the default). Your own app must be registered with the redirect URI above.</p>' : '') +
            '<div class="conn-scopes" data-conn-scopes></div>' +
            (id === 'gmail' ? '<div class="conn-import"><label class="form-field"><span class="k">Import client file…</span>' +
              '<input data-conn-importpath spellcheck="false" autocomplete="off" placeholder="C:\\Users\\you\\Downloads\\client_secret_123.json"></label>' +
              '<button class="btn" type="button" data-conn-pick>Choose file…</button>' +
              '<button class="btn" type="button" data-conn-import>Import</button>' +
              '<small class="tool-field-hint">The local server reads the file at that path and stores the id and secret; this page never reads it.</small>' +
              '<p class="conn-import-result" data-conn-importresult hidden></p></div>' : '') +
            '<div class="conn-form-foot"><button class="btn primary" type="submit">Save</button>' +
              '<a class="btn ghost" data-conn-console target="_blank" rel="noopener noreferrer" hidden>Open developer console</a></div>' +
          '</form>' +
        '</div>' +
        '<div class="conn-adv-tools"><div class="settings-label">Tools <span class="conn-fold-hint" data-conn-toolhint></span></div>' +
          '<p class="settings-note">What the model may use once this account is connected. Turning a tool off also drops the permission it needs from the next sign-in.</p>' +
          '<ul class="conn-tools" data-conn-tools></ul></div>' +
      '</details>' +
      '<details class="conn-fold" data-conn-fold="doc">' +
        '<summary>How it works <span class="conn-fold-hint">docs/connectors</span></summary>' +
        '<div class="conn-doc md" data-conn-doc><p class="settings-note">Loading…</p></div>' +
      '</details>' +
    '</article>';
  }

  function setText(card, sel, text) {
    var node = card.querySelector(sel);
    if (node && node.textContent !== text) node.textContent = text;
    return node;
  }

  function fillCard(id) {
    var card = root() && root().querySelector('[data-conn="' + id + '"]');
    if (!card) return;
    var cfg = state.configs[id] || {};
    var item = state.list[id] || {};
    var st = status(id);
    var isToken = mode(id) === 'token';
    var chip = setText(card, '[data-conn-status]', st.text);
    if (chip) chip.className = 'chip ' + st.cls;
    card.dataset.state = st.key;
    card.dataset.mode = isToken ? 'token' : 'oauth';
    var copy = IM.shell && IM.shell.connectorCopy;
    setText(card, '[data-conn-blurb]', (copy && copy[id]) || item.description || '');

    var badge = storeBadge(cfg);
    var storeEl = card.querySelector('[data-conn-store]');
    storeEl.hidden = !badge;
    if (badge) { storeEl.className = 'chip conn-store ' + badge.cls; storeEl.textContent = badge.text; storeEl.title = badge.title; }

    /* Status line: scopes / permissions / expiry when connected. */
    var line = card.querySelector('[data-conn-statusline]');
    var bits = [];
    var scopes = isToken ? cfg.scopes_or_permissions : (item.scopes || null);
    if (connected(id) && Array.isArray(scopes) && scopes.length) bits.push((isToken ? 'Permissions: ' : 'Granted: ') + scopes.join(', '));
    if (connected(id) && Number(item.expires_in) > 0) bits.push('token renews in ' + util.formatSeconds(Number(item.expires_in)));
    line.hidden = !bits.length;
    line.textContent = bits.join(' · ');

    var pending = Boolean(state.pending[id]);
    var isConnected = connected(id);
    var src = source(id);

    /* OAuth */
    var oauthBox = card.querySelector('[data-conn-oauth]');
    var unavail = card.querySelector('[data-conn-unavail]');
    oauthBox.hidden = isToken;
    var connect = card.querySelector('[data-conn-connect]');
    var canConnect = src !== 'none';
    connect.hidden = isConnected || (!canConnect && !cfg.error);
    connect.disabled = pending;
    connect.textContent = pending ? 'Waiting for the browser…' : (item.error ? 'Connect again' : 'Connect');
    var device = card.querySelector('[data-conn-device]');
    device.hidden = isToken || isConnected || !cfg.deviceFlow || !canConnect;
    device.disabled = pending;
    oauthBox.querySelector('[data-conn-disconnect]').hidden = !isConnected;
    oauthBox.querySelector('[data-conn-test]').hidden = !isConnected;
    card.querySelector('[data-conn-cancel]').hidden = !pending;
    setText(card, '[data-conn-note]', item.error && !isConnected ? String(item.error) : '');
    if (!isToken && src === 'none' && !isConnected && !cfg.error) {
      unavail.hidden = false;
      if (!unavail.dataset.filled || unavail.dataset.filled !== (cfg.registration_todo || '')) {
        unavail.dataset.filled = cfg.registration_todo || '';
        unavail.innerHTML = '<strong>Not available yet.</strong> IMURUN has no registered ' + esc(label(id)) + ' app on this machine.' +
          (cfg.registration_todo ? ' <span class="conn-todo">To do: <code></code></span>' : '') +
          ' <button class="btn ghost" type="button" data-conn-ownapp>Use your own app</button>';
        var code = unavail.querySelector('code');
        if (code) code.textContent = cfg.registration_todo;
      }
    } else {
      unavail.hidden = true;
    }

    /* Token */
    var tokenForm = card.querySelector('[data-conn-tokenform]');
    tokenForm.hidden = !isToken;
    if (isToken) {
      tokenForm.querySelector('[data-conn-forget]').hidden = !cfg.has_token;
      tokenForm.querySelector('[data-conn-test]').hidden = !cfg.has_token;
      var tokenInput = tokenForm.querySelector('[data-conn-token]');
      if (document.activeElement !== tokenInput) tokenInput.placeholder = cfg.has_token ? 'Saved (hidden). Paste a new token to replace it.' : 'Paste the token';
      var help = tokenForm.querySelector('[data-conn-tokenhelp]');
      var perms = cfg.token_permissions && typeof cfg.token_permissions === 'object' ? cfg.token_permissions : {};
      var permRows = Object.keys(perms).map(function (tool) {
        var need = perms[tool];
        return '<li><code>' + esc(tool) + '</code> ' + esc(Array.isArray(need) ? need.join(', ') : String(need)) + '</li>';
      }).join('');
      var consoleUrl = safeHttps(cfg.token_console);
      var html = (consoleUrl ? '<a class="btn ghost" data-conn-tokenconsole target="_blank" rel="noopener noreferrer" href="' + esc(consoleUrl) + '">Create a token</a>' : '') +
        (permRows ? '<p class="settings-note">Give it only what the tools you use need:</p><ul class="conn-perms">' + permRows + '</ul>' : '');
      if (help.dataset.html !== html) { help.dataset.html = html; help.innerHTML = html; }
    }

    /* Advanced */
    var adv = card.querySelector('[data-conn-advoauth]');
    adv.hidden = isToken;
    if (!isToken) fillAdvanced(card, id, cfg, src);

    /* Tools. */
    var tools = toolsFor(id);
    var list = card.querySelector('[data-conn-tools]');
    var always = tools.filter(function (t) { return t.level === 'always'; }).length;
    var toolHint = tools.length
      ? tools.filter(function (t) { return t.on; }).length + ' of ' + tools.length + ' tools on' + (always ? ' · ' + always + ' always ask' : '')
      : 'no tools reported';
    setText(card, '[data-conn-toolhint]', toolHint);
    setText(card, '[data-conn-advhint]', (isToken ? '' : (SOURCE_TEXT[src] || src) + ' · ') + toolHint);
    list.innerHTML = tools.length ? tools.map(function (t) {
      var lv = LEVEL_TEXT[t.level] || ['', ''];
      return '<li class="conn-tool' + (t.on ? '' : ' is-off') + '">' +
        '<label class="conn-tool-toggle"><input type="checkbox" data-conn-tool="' + esc(t.name) + '"' + (t.on ? ' checked' : '') + (cfg.error ? ' disabled' : '') + '>' +
        '<code class="tool-card-name">' + esc(t.name) + '</code></label>' +
        (lv[0] ? '<span class="chip conn-level is-' + esc(t.level) + '" title="' + esc(lv[1]) + '">' + esc(lv[0]) + '</span>' : '') +
        (t.description ? '<span class="conn-tool-desc">' + esc(t.description) + '</span>' : '') +
        (t.on && t.available === false && t.reason ? '<span class="conn-tool-why">' + esc(t.reason) + '</span>' : '') +
      '</li>';
    }).join('') : '<li class="muted">The server lists no tools for ' + esc(label(id)) + ' yet.</li>';

    fillDoc(card, id, cfg);
  }

  function fillAdvanced(card, id, cfg, src) {
    var form = card.querySelector('[data-conn-form]');
    var note = card.querySelector('[data-conn-source]');
    if (cfg.error) {
      form.hidden = true;
      note.innerHTML = 'This server has no settings route for ' + esc(label(id)) + ' (' + esc(cfg.error) + '). Set <code>IMURUN_OAUTH_' +
        esc(id.toUpperCase()) + '_CLIENT_ID</code> (and <code>_CLIENT_SECRET</code>) before starting IMURUN.';
      return;
    }
    form.hidden = false;
    note.textContent = 'Signs in with ' + (SOURCE_TEXT[src] || src) + '. Leave the client id empty to use IMURUN\'s built-in app when there is one.';
    var idInput = form.querySelector('[data-conn-field="clientId"]');
    if (document.activeElement !== idInput && !idInput.dataset.dirty) idInput.value = src === 'user' ? (cfg.clientId || '') : '';
    if (src !== 'user' && cfg.clientId) idInput.placeholder = 'In use: ' + cfg.clientId + ' (' + (SOURCE_TEXT[src] || src) + ')';
    var secretOn = Boolean(cfg.clientSecretSet) && src === 'user';
    var secretChip = card.querySelector('[data-conn-secretstate]');
    secretChip.className = 'chip ' + (secretOn ? 'success' : 'neutral');
    secretChip.textContent = secretOn ? 'saved' : 'not set';
    var secret = form.querySelector('[data-conn-field="clientSecret"]');
    if (document.activeElement !== secret) {
      secret.value = '';
      secret.placeholder = secretOn ? 'Saved (hidden). Type to replace.' : 'Not set';
    }
    card.querySelector('[data-conn-clearsecret]').hidden = !secretOn;
    setText(card, '[data-conn-redirect]', cfg.redirectUri || 'reported by the server once it is running');
    fillExtras(card, id, cfg);
    var scopes = Array.isArray(cfg.scopes) ? cfg.scopes : [];
    card.querySelector('[data-conn-scopes]').innerHTML = scopes.length
      ? '<span class="k">Scopes requested</span>' + scopes.map(function (s) { return '<code class="conn-scope">' + esc(s) + '</code>'; }).join('')
      : '<span class="k">Scopes requested</span><span class="muted">fixed in the app registration</span>';
    var consoleLink = card.querySelector('[data-conn-console]');
    var safe = safeHttps(cfg.console);
    consoleLink.hidden = !safe;
    if (safe) consoleLink.href = safe;
    var pick = card.querySelector('[data-conn-pick]');
    if (pick) pick.hidden = !pickFileAvailable();
  }

  /* Gmail options and Supabase key role. */
  var KNOWN = ['id', 'label', 'clientId', 'clientIdSource', 'credential_source', 'clientSecretSet', 'tools', 'availableTools', 'scopes',
    'redirectUri', 'configured', 'pkce', 'deviceFlow', 'console', 'doc', 'connected', 'oauth', 'account', 'error', 'auth_mode',
    'has_token', 'registration_todo', 'token_console', 'token_permissions', 'scopes_or_permissions', 'store', 'store_encrypted', 'warning'];
  var EXTRA = {
    maxResults: { label: 'Results per search', type: 'number', min: 1, max: 25 },
    bodyChars: { label: 'Characters read per email', type: 'number', min: 500, max: 20000, step: 500 },
    keyRole: { label: 'Database key', type: 'select', note: 'service_role bypasses row-level security: the model could read and write every row.',
      options: [['anon', 'anon (row-level security applies)'], ['service_role', 'service_role (bypasses row-level security)']] }
  };

  function fillExtras(card, id, cfg) {
    var box = card.querySelector('[data-conn-extra]');
    var keys = Object.keys(cfg).filter(function (key) { return KNOWN.indexOf(key) === -1 && EXTRA[key]; });
    if (box.dataset.keys !== keys.join(',')) {
      box.dataset.keys = keys.join(',');
      box.innerHTML = keys.map(function (key) {
        var spec = EXTRA[key];
        if (spec.type === 'select') {
          return '<label class="form-field"><span class="k">' + esc(spec.label) + '</span><select data-conn-field="' + key + '">' +
            spec.options.map(function (o) { return '<option value="' + esc(o[0]) + '">' + esc(o[1]) + '</option>'; }).join('') + '</select>' +
            (spec.note ? '<small class="tool-field-hint conn-warn" data-conn-warn="' + key + '" hidden>' + esc(spec.note) + '</small>' : '') + '</label>';
        }
        return '<label class="form-field"><span class="k">' + esc(spec.label) + '</span><input type="number" data-conn-field="' + key + '"' +
          ' min="' + spec.min + '" max="' + spec.max + '"' + (spec.step ? ' step="' + spec.step + '"' : '') + '></label>';
      }).join('');
    }
    keys.forEach(function (key) {
      var input = box.querySelector('[data-conn-field="' + key + '"]');
      if (input && document.activeElement !== input && !input.dataset.dirty) input.value = cfg[key] === undefined || cfg[key] === null ? '' : String(cfg[key]);
      var warn = box.querySelector('[data-conn-warn="' + key + '"]');
      if (warn && input) warn.hidden = input.value !== 'service_role';
    });
  }

  function safeHttps(value) {
    try {
      var url = new URL(String(value || ''));
      return url.protocol === 'https:' ? url.href : '';
    } catch (error) {
      return '';
    }
  }

  function fillDoc(card, id, cfg) {
    var box = card.querySelector('[data-conn-doc]');
    var doc = state.docs[id];
    var md = window.__imurun_markdown;
    if (doc && doc.markdown && md && md.render) {
      if (box.dataset.src !== 'server') {
        box.dataset.src = 'server';
        box.innerHTML = md.render(String(doc.markdown));
        Array.prototype.forEach.call(box.querySelectorAll('a[href]'), function (a) { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
      }
      return;
    }
    if (doc && doc.missing && box.dataset.src !== 'missing') {
      box.dataset.src = 'missing';
      box.innerHTML = '<p class="settings-note">The set-up steps are in <code>' + esc(cfg.doc || ('docs/connectors/' + id.toUpperCase() + '.md')) +
        '</code> in the IMURUN folder (this server could not serve them).</p>';
    }
  }

  /* --------------------------------------------------------------- load */

  function render() {
    var el = root();
    if (!el) return;
    var ids = ORDER.slice();
    Object.keys(state.list).forEach(function (id) { if (ids.indexOf(id) === -1) ids.push(id); });
    var present = Array.prototype.map.call(el.querySelectorAll('[data-conn]'), function (n) { return n.dataset.conn; });
    if (present.join(',') !== ids.join(',')) {
      el.innerHTML = '<p class="settings-note conn-intro">Press Connect and finish in your browser. Reads follow your permission mode; anything that sends, posts, deploys or deletes asks every time. ' +
        'Cloudflare and Render take an API token instead.</p>' +
        ids.map(cardShell).join('');
    }
    el.setAttribute('aria-busy', 'false');
    ids.forEach(fillCard);
  }

  function load() {
    var el = root();
    if (!el || !api() || state.loading) return;
    state.loading = true;
    bind();
    var a = api();
    Promise.all([
      a.listConnectors().then(function (list) { return list; }, function (error) { return { error: error }; }),
      a.getTools().then(function (view) { return (view && view.tools) || []; }, function () { return null; })
    ]).then(function (results) {
      if (results[0] && results[0].error) {
        state.loading = false;
        el.innerHTML = '<div class="empty-state"><h3>Connectors could not be loaded</h3><p>' + esc(results[0].error.message || 'The server did not answer.') +
          '</p><button class="btn" type="button" data-conn-refresh>Try again</button></div>';
        return null;
      }
      state.list = {};
      (results[0] || []).forEach(function (item) { if (item && item.id) state.list[item.id] = item; });
      if (results[1]) state.tools = results[1];
      render();
      var ids = Object.keys(state.list).length ? Object.keys(state.list) : ORDER;
      return Promise.all(ids.map(function (id) {
        return Promise.all([
          a.getConnectorConfig(id).then(function (cfg) {
            state.configs[id] = cfg || {};
          }, function (error) {
            state.configs[id] = { error: error && error.status ? 'HTTP ' + error.status : ((error && error.message) || 'unreachable') };
          }),
          a.getConnectorStatus(id).then(function (s) { if (s) state.list[id] = Object.assign({}, state.list[id] || {}, s); }, function () {})
        ]).then(function () { fillCard(id); });
      }));
    }).then(function () {
      state.loading = false;
    }, function () { state.loading = false; });
  }

  function loadDoc(id) {
    if (state.docs[id]) return;
    state.docs[id] = { loading: true };
    api().getConnectorDoc(id).then(function (doc) {
      var markdown = typeof doc === 'string' ? doc : (doc && (doc.markdown || doc.text || doc.body));
      state.docs[id] = markdown ? { markdown: markdown } : { missing: true };
      fillCard(id);
    }, function () { state.docs[id] = { missing: true }; fillCard(id); });
  }

  function refreshOne(id) {
    var a = api();
    return Promise.all([
      a.getConnectorStatus(id).then(function (s) { if (s) state.list[id] = Object.assign({}, state.list[id] || {}, s); }, function () {}),
      a.getConnectorConfig(id).then(function (cfg) { state.configs[id] = cfg || {}; }, function () {})
    ]).then(function () { fillCard(id); });
  }

  /* ------------------------------------------------------------ actions */

  function save(id, changes, title) {
    return api().saveConnectorConfig(id, changes).then(function (view) {
      state.configs[id] = Object.assign({}, state.configs[id] || {}, view || {});
      var card = root().querySelector('[data-conn="' + id + '"]');
      if (card) Array.prototype.forEach.call(card.querySelectorAll('[data-dirty]'), function (n) { delete n.dataset.dirty; });
      fillCard(id);
      /* Tool availability follows the config (turned off, not set up). */
      api().getTools().then(function (v) { state.tools = (v && v.tools) || state.tools; fillCard(id); }, function () {});
      toast({ tone: 'success', icon: 'check', kicker: label(id), title: title, text: 'Stored by the local server.' });
    }, function (error) {
      fail(label(id) + ': not saved', error);
      fillCard(id);
    });
  }

  function submitForm(id, form) {
    var changes = {};
    var cfg = state.configs[id] || {};
    var mine = source(id) === 'user' ? (cfg.clientId || '') : '';
    var clientId = form.querySelector('[data-conn-field="clientId"]').value.trim();
    if (clientId !== mine) changes.clientId = clientId;   /* "" returns to the built-in app */
    var secret = form.querySelector('[data-conn-field="clientSecret"]').value.trim();
    if (secret) changes.clientSecret = secret;
    Array.prototype.forEach.call(form.querySelectorAll('[data-conn-extra] [data-conn-field]'), function (input) {
      var key = input.getAttribute('data-conn-field');
      var value = input.type === 'number' ? Number(input.value) : input.value;
      if (String(value) !== String(cfg[key])) changes[key] = value;
    });
    if (!Object.keys(changes).length) {
      toast({ tone: 'neutral', icon: 'info', kicker: label(id), title: 'Nothing changed', text: 'Type a new value to replace the saved one.' });
      return;
    }
    save(id, changes, changes.clientId === '' ? 'Back to the built-in app' : (changes.clientSecret ? 'Your app saved (secret stored)' : 'Saved'));
  }

  function toggleTool(id, name, on) {
    var cfg = state.configs[id] || {};
    var all = toolsFor(id).map(function (t) { return t.name; });
    var current = Array.isArray(cfg.tools) ? cfg.tools.slice() : all.slice();
    var next = current.filter(function (n) { return n !== name; });
    if (on) next.push(name);
    next = all.filter(function (n) { return next.indexOf(n) !== -1; });
    save(id, { tools: next }, name + (on ? ' turned on' : ' turned off'));
  }

  /* Sign-in: the server opens the system browser; the `connector` frame
   * finishes it. The server's OAuth state lives 10 minutes. */
  var SIGNIN_LIMIT_MS = 10 * 60 * 1000;

  function connect(id) {
    if (state.pending[id]) return;
    var card = root().querySelector('[data-conn="' + id + '"]');
    var ctl = { id: id, timer: null, done: false };
    state.pending[id] = ctl;
    fillCard(id);
    api().connectConnector(id, { open: true }).then(function (result) {
      if (ctl.done) return;
      var url = safeHttps(result && result.url);
      showSignin(card, url, Boolean(result && result.opened));
      ctl.timer = setTimeout(function () { end(ctl, false, null, 'The sign-in was not finished within 10 minutes. Press Connect to start again.'); }, SIGNIN_LIMIT_MS);
    }).catch(function (error) {
      if (error && error.status === 409) { end(ctl, true, null); return; }   /* already connected */
      end(ctl, false, null, error && error.status === 404
        ? ((error.body && error.body.reason) || ('There is no ' + label(id) + ' app to sign in with yet (Advanced).'))
        : (error && error.message));
    });
  }

  function showSignin(card, url, opened) {
    var box = card.querySelector('[data-conn-signin]');
    box.hidden = false;
    box.innerHTML = '<span class="search-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>' +
      (opened ? 'Finish signing in in your browser… This card updates by itself.' :
        'The browser could not be opened from here. Open the sign-in page yourself; this card updates by itself.') +
      '</span>' + (url ? '<a class="btn ghost" target="_blank" rel="noopener noreferrer" data-conn-signin-link>Open sign-in page</a>' +
      '<button class="btn ghost" type="button" data-conn-copylink>Copy link</button>' : '');
    var link = box.querySelector('[data-conn-signin-link]');
    if (link) link.href = url;
    box.dataset.url = url || '';
  }

  /* Device flow (GitHub): POST .../device -> {state: "pending", user_code,
   * verification_uri, interval, expires_in}; the server polls GitHub, the
   * page polls GET .../device for connected | denied | expired | error. */
  function connectDevice(id) {
    if (state.pending[id]) return;
    var card = root().querySelector('[data-conn="' + id + '"]');
    var ctl = { id: id, timer: null, done: false, device: true, deadline: Date.now() + SIGNIN_LIMIT_MS };
    state.pending[id] = ctl;
    fillCard(id);
    api().fetchAPI('/api/connectors/' + encodeURIComponent(id) + '/device', { method: 'POST' }).then(function (view) {
      if (ctl.done) return;
      var v = view || {};
      var url = safeHttps(v.verification_uri) || 'https://github.com/login/device';
      if (v.expires_in) ctl.deadline = Date.now() + Number(v.expires_in) * 1000;
      var box = card.querySelector('[data-conn-signin]');
      box.hidden = false;
      box.dataset.url = String(v.user_code || '');
      box.innerHTML = '<span class="search-dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
        '<span>Open the page and type this code:</span><code class="conn-code"></code>' +
        '<a class="btn ghost" target="_blank" rel="noopener noreferrer" data-conn-signin-link>Open ' + esc(url.replace(/^https:\/\//, '')) + '</a>' +
        '<button class="btn ghost" type="button" data-conn-copylink>Copy code</button>';
      box.querySelector('.conn-code').textContent = String(v.user_code || '');
      box.querySelector('[data-conn-signin-link]').href = url;
      ctl.timer = setInterval(function () { pollDevice(ctl); }, Math.max(2000, (Number(v.interval) || 5) * 1000));
    }).catch(function (error) { end(ctl, false, null, error && error.message); });
  }

  function pollDevice(ctl) {
    if (ctl.done) return;
    if (Date.now() > ctl.deadline) { end(ctl, false, null, 'The code expired. Press "Connect without a browser" again.'); return; }
    api().fetchAPI('/api/connectors/' + encodeURIComponent(ctl.id) + '/device').then(function (s) {
      var st = s && s.state;
      if (st === 'connected') end(ctl, true, s.account);
      else if (st === 'denied' || st === 'expired' || st === 'error') end(ctl, false, null, s.message || ('The sign-in was ' + st + '.'));
    }, function () {});
  }

  function end(ctl, ok, acct, why) {
    if (ctl.done) return;
    ctl.done = true;
    if (ctl.timer) { clearTimeout(ctl.timer); clearInterval(ctl.timer); }
    if (state.pending[ctl.id] === ctl) delete state.pending[ctl.id];
    var card = root() && root().querySelector('[data-conn="' + ctl.id + '"]');
    if (card) { var box = card.querySelector('[data-conn-signin]'); box.hidden = true; box.innerHTML = ''; }
    if (ok) {
      state.list[ctl.id] = Object.assign({}, state.list[ctl.id] || {}, { connected: true, error: '' }, acct ? { account: acct } : {});
      if (IM.shell && IM.shell.refreshConnectorCard) IM.shell.refreshConnectorCard(ctl.id, true, acct);
      toast({ id: 'connector-' + ctl.id, tone: 'success', icon: 'check', kicker: 'Connected', title: label(ctl.id) + ' connected' + (acct ? ' as ' + acct : ''), text: 'Kept in the local store on this machine.' });
      util.announce(label(ctl.id) + ' connected.');
      refreshOne(ctl.id);
    } else if (why !== 'cancelled') {
      toast({ id: 'connector-' + ctl.id, tone: 'error', icon: 'alert', kicker: 'Not connected', title: label(ctl.id) + ' was not connected', text: why || 'The authorization was not completed.' });
    }
    fillCard(ctl.id);
  }

  /* The event stream may have been down while the user was in the browser:
   * one status read when the window gets the focus back. */
  function onFocus() {
    Object.keys(state.pending).forEach(function (id) {
      var ctl = state.pending[id];
      if (!ctl || ctl.device) return;
      api().getConnectorStatus(id).then(function (s) { if (s && s.connected && !ctl.done) end(ctl, true, s.account); }, function () {});
    });
  }

  function disconnect(id) {
    if (!window.confirm('Disconnect ' + label(id) + '? The local token is deleted; the app settings stay.')) return;
    var btn = root().querySelector('[data-conn="' + id + '"] [data-conn-disconnect]');
    if (btn) { btn.disabled = true; btn.textContent = 'Disconnecting…'; }
    api().disconnectConnector(id).then(function () {
      state.list[id] = Object.assign({}, state.list[id] || {}, { connected: false, account: '' });
      delete state.tests[id];
      if (IM.shell && IM.shell.refreshConnectorCard) IM.shell.refreshConnectorCard(id, false, null);
      toast({ tone: 'neutral', icon: 'info', kicker: label(id), title: label(id) + ' disconnected', text: 'The local token was removed.' });
    }, function (error) { fail('Could not disconnect ' + label(id), error); }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = 'Disconnect'; }
      fillCard(id);
    });
  }

  /* Token cards. */
  function saveToken(id, form) {
    var input = form.querySelector('[data-conn-token]');
    var token = input.value.trim();
    var out = form.querySelector('[data-conn-tokenresult]');
    if (!token) { input.focus(); return; }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = 'Verifying…';
    out.hidden = false;
    out.className = 'conn-token-result is-running';
    out.textContent = 'Asking ' + label(id) + ' whether the token works…';
    api().fetchAPI('/api/connectors/' + encodeURIComponent(id) + '/token', { method: 'POST', body: JSON.stringify({ token: token }) })
      .then(function (r) {
        var v = r || {};
        input.value = '';   /* the token is never sent back */
        var perms = Array.isArray(v.scopes_or_permissions) ? v.scopes_or_permissions : [];
        state.configs[id] = Object.assign({}, state.configs[id] || {}, { has_token: true, account: v.account || '', scopes_or_permissions: perms });
        state.list[id] = Object.assign({}, state.list[id] || {}, { connected: true, account: v.account || '' });
        out.className = 'conn-token-result is-ok';
        out.textContent = 'Saved. ' + (v.account ? 'Connected as ' + v.account + '. ' : '') +
          (perms.length ? 'Permissions: ' + perms.join(', ') + '.' : '') + (v.expires ? ' Expires ' + v.expires + '.' : '');
        if (IM.shell && IM.shell.refreshConnectorCard) IM.shell.refreshConnectorCard(id, true, v.account);
        refreshOne(id);
      }, function (error) {
        /* 401: the provider refused it and nothing was stored; keep the
         * field so the user can fix a paste. */
        if (!error || error.status !== 401) input.value = '';
        out.className = 'conn-token-result is-failed';
        out.textContent = (error && error.body && error.body.error) || (error && error.message) || 'The token was not saved.';
      }).then(function () {
        button.disabled = false;
        button.textContent = 'Verify & save';
        fillCard(id);
      });
  }

  function forgetToken(id) {
    if (!window.confirm('Forget the ' + label(id) + ' token? It is deleted from this machine; revoke it at ' + label(id) + ' too if you no longer need it.')) return;
    api().fetchAPI('/api/connectors/' + encodeURIComponent(id) + '/token', { method: 'DELETE' }).then(function () {
      state.configs[id] = Object.assign({}, state.configs[id] || {}, { has_token: false, account: '', scopes_or_permissions: [] });
      state.list[id] = Object.assign({}, state.list[id] || {}, { connected: false, account: '' });
      var out = root().querySelector('[data-conn="' + id + '"] [data-conn-tokenresult]');
      if (out) out.hidden = true;
      toast({ tone: 'neutral', icon: 'info', kicker: label(id), title: 'Token forgotten', text: 'Deleted from the local store.' });
      fillCard(id);
    }, function (error) { fail('Could not forget the token', error); });
  }

  function summarizeTest(result) {
    var bits = [];
    if (result.account) bits.push('signed in as ' + result.account);
    if (Array.isArray(result.threads)) bits.push(result.threads.length + (result.threads.length === 1 ? ' recent thread read' : ' recent threads read'));
    if (result.summary || result.message) bits.push(String(result.summary || result.message));
    if (!bits.length && result.tool) bits.push(result.tool + ' answered');
    return bits.join(' · ') || 'the provider answered';
  }

  function test(id) {
    var card = root().querySelector('[data-conn="' + id + '"]');
    var out = card.querySelector('[data-conn-testout]');
    var btns = card.querySelectorAll('[data-conn-test]');
    Array.prototype.forEach.call(btns, function (b) { b.disabled = true; b.textContent = 'Testing…'; });
    out.hidden = false;
    out.className = 'conn-test is-running';
    out.textContent = 'Asking ' + label(id) + '…';
    api().testConnector(id).then(function (result) {
      var r = result || {};
      state.tests[id] = r;
      out.className = 'conn-test ' + (r.ok === false ? 'is-failed' : 'is-ok');
      out.textContent = (r.ok === false ? 'Failed: ' + (r.error || 'the provider refused') : 'Works: ' + summarizeTest(r));
      if (Array.isArray(r.threads) && r.threads.length) {
        var ul = document.createElement('ul');
        ul.className = 'conn-test-list';
        r.threads.slice(0, 3).forEach(function (t) {
          var li = document.createElement('li');
          li.textContent = (t.subject || '(no subject)') + (t.from ? ' · ' + t.from : '') + (t.date ? ' · ' + t.date : '');
          ul.appendChild(li);
        });
        out.appendChild(ul);
      } else if (r.content) {
        var pre = document.createElement('pre');
        pre.className = 'tool-output conn-test-out';
        pre.textContent = String(r.content).slice(0, 1200);
        out.appendChild(pre);
      }
    }, function (error) {
      out.className = 'conn-test is-failed';
      out.textContent = 'Failed: ' + ((error && error.body && error.body.error) || (error && error.message) || 'no answer');
    }).then(function () {
      Array.prototype.forEach.call(btns, function (b) { b.disabled = false; b.textContent = 'Test'; });
      fillCard(id);
    });
  }

  /* Gmail: the server reads a client_secret_*.json by path. "Choose file…"
   * asks the server to open the OS file dialog (POST /api/system/pick-file,
   * a Tk subprocess on the user's own machine, so it works in the window and
   * in a browser) and only fills in the PATH; the page never reads the file.
   * 501 = no Tk on this machine: the button goes away for the session and
   * the typed path stays. */
  var pickUnavailable = false;

  function pickFileAvailable() { return !pickUnavailable; }

  function pickFile(card) {
    if (pickUnavailable) return;
    var button = card.querySelector('[data-conn-pick]');
    var input = card.querySelector('[data-conn-importpath]');
    button.disabled = true;
    button.textContent = 'Waiting for the dialog…';
    api().pickFile({ title: 'Google OAuth client file', patterns: ['client_secret*.json', ['JSON', '*.json']] }).then(function (r) {
      if (r && r.path && input) { input.value = String(r.path); input.focus(); }
    }, function (error) {
      if (error && error.status === 501) {
        pickUnavailable = true;
        Array.prototype.forEach.call(document.querySelectorAll('[data-conn-pick]'), function (b) { b.hidden = true; });
        if (input) input.focus();
        toast({ tone: 'neutral', icon: 'info', kicker: 'Gmail', title: 'No file dialog on this machine', text: 'Type or paste the path of the client_secret_….json file instead.' });
      } else {
        fail('Could not open the file dialog', error);
      }
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Choose file…';
    });
  }

  function importClient(id, card) {
    var input = card.querySelector('[data-conn-importpath]');
    var out = card.querySelector('[data-conn-importresult]');
    var path = input ? input.value.trim() : '';
    if (!path) { input.focus(); return; }
    api().fetchAPI('/api/connectors/' + encodeURIComponent(id) + '/import-client', { method: 'POST', body: JSON.stringify({ path: path }) })
      .then(function (view) {
        var v = view || {};
        input.value = '';
        var warnings = Array.isArray(v.warnings) ? v.warnings : [];
        out.hidden = false;
        out.className = 'conn-import-result ' + (warnings.length ? 'is-warn' : 'is-ok');
        out.textContent = 'Imported client ' + (v.client_id || '') + (v.project_id ? ' (project ' + v.project_id + ')' : '') + '.' +
          (warnings.length ? ' ' + warnings.join(' ') : ' The secret went to the local store.');
        return refreshOne(id);
      }, function (error) {
        out.hidden = false;
        out.className = 'conn-import-result is-failed';
        out.textContent = (error && error.body && error.body.error) || (error && error.message) || 'The file could not be imported.';
      });
  }

  /* ------------------------------------------------------------- wiring */

  function bind() {
    var el = root();
    if (state.bound || !el) return;
    state.bound = true;
    el.addEventListener('submit', function (event) {
      var tokenForm = event.target.closest('[data-conn-tokenform]');
      var form = event.target.closest('[data-conn-form]');
      if (!tokenForm && !form) return;
      event.preventDefault();
      var id = event.target.closest('[data-conn]').dataset.conn;
      if (tokenForm) saveToken(id, tokenForm);
      else submitForm(id, form);
    });
    el.addEventListener('input', function (event) {
      if (event.target.matches && event.target.matches('[data-conn-field]')) event.target.dataset.dirty = '1';
    });
    el.addEventListener('change', function (event) {
      var box = event.target.closest('[data-conn-tool]');
      if (box) { toggleTool(box.closest('[data-conn]').dataset.conn, box.getAttribute('data-conn-tool'), box.checked); return; }
      var field = event.target.closest('[data-conn-field="keyRole"]');
      if (field) {
        var warn = field.closest('[data-conn-extra]').querySelector('[data-conn-warn="keyRole"]');
        if (warn) warn.hidden = field.value !== 'service_role';
      }
    });
    el.addEventListener('toggle', function (event) {
      var fold = event.target;
      if (!fold.matches || !fold.matches('[data-conn-fold="doc"]') || !fold.open) return;
      loadDoc(fold.closest('[data-conn]').dataset.conn);
    }, true);
    el.addEventListener('click', function (event) {
      var t = event.target.closest('button');
      if (!t) return;
      var card = t.closest('[data-conn]');
      if (!card) return;
      var id = card.dataset.conn;
      if (t.hasAttribute('data-conn-connect')) connect(id);
      else if (t.hasAttribute('data-conn-device')) connectDevice(id);
      else if (t.hasAttribute('data-conn-import')) importClient(id, card);
      else if (t.hasAttribute('data-conn-pick')) pickFile(card);
      else if (t.hasAttribute('data-conn-disconnect')) disconnect(id);
      else if (t.hasAttribute('data-conn-forget')) forgetToken(id);
      else if (t.hasAttribute('data-conn-test')) test(id);
      else if (t.hasAttribute('data-conn-cancel')) { if (state.pending[id]) end(state.pending[id], false, null, 'cancelled'); }
      else if (t.hasAttribute('data-conn-ownapp')) {
        var adv = card.querySelector('[data-conn-fold="advanced"]');
        adv.open = true;
        var first = adv.querySelector('[data-conn-field="clientId"]');
        if (first) first.focus();
      } else if (t.hasAttribute('data-conn-clearsecret')) {
        if (window.confirm('Remove the saved ' + label(id) + ' client secret?')) save(id, { clientSecret: '' }, 'Client secret removed');
      } else if (t.hasAttribute('data-conn-copy')) {
        var uri = (state.configs[id] || {}).redirectUri;
        if (uri && IM.chat && IM.chat.copyText) IM.chat.copyText(uri, t, 'Copy');
      } else if (t.hasAttribute('data-conn-copylink')) {
        var url = card.querySelector('[data-conn-signin]').dataset.url;
        if (url && IM.chat && IM.chat.copyText) IM.chat.copyText(url, t, t.textContent);
      }
    });
    window.addEventListener('focus', onFocus);
  }

  /* `connector` frames: {connector, id, connected, account, message, via,
   * configured}. They flip the card; a failed or cancelled sign-in carries
   * `connected: false` and a `message`. */
  function onConnectorEvent(event) {
    var id = String(event.connector || event.id || '');
    if (!id) return;
    var item = state.list[id] = Object.assign({}, state.list[id] || {});
    if (event.connected !== undefined) item.connected = Boolean(event.connected);
    if (event.account) item.account = event.account;
    if (event.connected === false) item.account = '';
    var cfg = state.configs[id];
    if (cfg && !cfg.error) {
      if (event.configured !== undefined) cfg.configured = Boolean(event.configured);
      if (event.via === 'token') cfg.has_token = Boolean(event.connected);
      if (event.connected === false && mode(id) === 'token' && /forgot/i.test(event.message || '')) cfg.has_token = false;
    }
    var ctl = state.pending[id];
    if (ctl && event.connected) end(ctl, true, event.account);
    else if (ctl && event.connected === false && !ctl.device && (event.via === 'oauth' || event.message)) end(ctl, false, null, event.message || event.error);
    else fillCard(id);
  }

  function focusCard(id) {
    var go = function () {
      var card = byId('conn-card-' + id);
      if (!card) return false;
      card.scrollIntoView({ block: 'start', behavior: util.reducedMotion() ? 'auto' : 'smooth' });
      card.classList.add('is-flash');
      setTimeout(function () { card.classList.remove('is-flash'); }, 1600);
      var first = card.querySelector('[data-conn-connect]:not([hidden]):not([disabled]), [data-conn-token]:not([hidden]), button');
      if (first) first.focus({ preventScroll: true });
      return true;
    };
    if (!go()) setTimeout(go, 400);
  }

  function init() {
    IM.bus.on('settings.section', function (section) { if (section === 'connectors') load(); });
    IM.bus.on('server:connector', onConnectorEvent);
    document.addEventListener('click', function (event) {
      if (event.target.closest && event.target.closest('[data-conn-refresh]')) {
        state.docs = {};
        load();
      }
    });
  }

  IM.connectors = { load: load, focus: focusCard, toolsFor: toolsFor, status: status, storeBadge: storeBadge, _state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
