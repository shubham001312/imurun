/* Settings > MCP servers (services/mcp/routes.py, registry.py).
 *
 *   GET    /api/mcp/servers                 list with live status
 *   GET    /api/mcp/servers/{id}/tools      discovered tools (bridged names,
 *                                           schema, permission_level)
 *   POST   /api/mcp/servers                 add or update (body = config)
 *   POST   /api/mcp/servers/{id}/connect    connect now
 *   POST   /api/mcp/servers/{id}/disconnect close the connection
 *   DELETE /api/mcp/servers/{id}            remove
 *   events mcp.connected / mcp.disconnected / mcp.error (server:<type> on the bus)
 *
 * Env and header values never come back from the server: they read
 * "********", and sending that value back means "unchanged". The form keeps
 * them that way unless the user types a new value. The built-in Gmail MCP
 * server (settings default, off) is shown as an example and offered as a
 * template. ES2017 (see docs/frontend/README.md).
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
    toast({ tone: 'error', icon: 'alert', kicker: 'MCP', title: title, text: (error && error.message) || 'The server did not answer.' });
  }

  var REDACTED = '********';
  var ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
  var TEMPLATES = [
    { key: 'gmail', title: 'Gmail (built-in MCP server)', note: 'Ships with IMURUN. Uses the Gmail account connected under Connectors; send, trash and label changes always ask.',
      config: { id: 'gmail', name: 'Gmail (built-in MCP server)', transport: 'stdio', command: '${python}',
        args: ['${repo}/services/mcp/servers/gmail_mcp.py', '--home', '${home}'], env: {}, headers: {}, url: '', enabled: true, auto_approve: [] } },
    { key: 'connectors', title: 'IMURUN connectors (built-in MCP server)', note: 'Ships with IMURUN. Every connected service (GitHub, Slack, Vercel, ...) over MCP; writes are marked destructive, so they always ask.',
      config: { id: 'connectors', name: 'IMURUN connectors (built-in MCP server)', transport: 'stdio', command: '${python}',
        args: ['${repo}/services/mcp/servers/connectors_mcp.py', '--home', '${home}'], env: {}, headers: {}, url: '', enabled: true, auto_approve: [] } },
    { key: 'files', title: 'Filesystem (Node)', note: 'The reference filesystem server. Needs Node.js (npx) on this computer; change the folder at the end of the arguments.',
      config: { id: 'files', name: 'Files', transport: 'stdio', command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:/Users/you/Projects'], env: {}, headers: {}, url: '', enabled: false, auto_approve: [] } },
    { key: 'http', title: 'Remote server over HTTP', note: 'Streamable HTTP. Put a key in an environment variable and reference it as ${env:NAME} so it never sits in the settings file.',
      config: { id: 'remote', name: 'Remote', transport: 'http', command: '', args: [], env: {},
        url: 'https://example.com/mcp', headers: { Authorization: 'Bearer ${env:MCP_TOKEN}' }, enabled: false, auto_approve: [] } }
  ];
  var STATUS = {
    connected: ['success', 'connected'], connecting: ['neutral', 'connecting…'], disconnected: ['neutral', 'not connected'],
    disabled: ['neutral', 'off'], error: ['error', 'error'], unknown: ['neutral', 'starting']
  };
  var LEVELS = {
    allow: ['runs', 'Auto-approved in this server\'s settings: runs without asking.'],
    read: ['read-only', 'The server marks it read-only: asks only in "Ask for everything".'],
    ask: ['asks', 'Asks before every call; "remember" holds for the session.'],
    always: ['always asks', 'The server marks it destructive: asks in every mode, never remembered, auto-approve cannot change that.'],
    deny: ['off', 'Refused.']
  };

  var state = { servers: [], tools: {}, open: {}, editing: null, bound: false, loading: false, busy: {} };

  function root() { return byId('mcp-root'); }
  function find(id) { for (var i = 0; i < state.servers.length; i += 1) if (state.servers[i].id === id) return state.servers[i]; return null; }

  /* ------------------------------------------------------------ render */

  function statusOf(server) {
    var key = state.busy[server.id] || server.status || (server.enabled ? 'unknown' : 'disabled');
    return { key: key, cls: (STATUS[key] || STATUS.unknown)[0], text: (STATUS[key] || [0, key])[1] };
  }

  function where(server) {
    if (server.transport === 'http') return server.url || '';
    return [server.command].concat(server.args || []).filter(Boolean).join(' ');
  }

  function serverCard(server) {
    var st = statusOf(server);
    var info = server.serverInfo || {};
    /* The two shipped entries (settings default `mcp`: gmail, connectors). */
    var builtIn = /\$\{repo\}\/services\/mcp\/servers\/[a-z_]+\.py/.test((server.args || []).join(' '));
    var facts = [];
    facts.push(server.transport === 'http' ? 'HTTP' : 'stdio');
    if (st.key === 'connected') facts.push(server.tools + (Number(server.tools) === 1 ? ' tool' : ' tools'));
    if (info.name) facts.push(info.name + (info.version ? ' ' + info.version : ''));
    if (server.protocolVersion) facts.push('protocol ' + server.protocolVersion);
    if ((server.auto_approve || []).length) facts.push(server.auto_approve.length + ' auto-approved');
    var open = Boolean(state.open[server.id]);
    return '<article class="panel settings-card mcp-card" data-mcp="' + esc(server.id) + '" data-state="' + esc(st.key) + '">' +
      '<header class="mcp-head">' +
        '<span class="mcp-dot is-' + esc(st.key) + '" aria-hidden="true"></span>' +
        '<div class="mcp-title"><h3>' + esc(server.name || server.id) + (builtIn ? ' <span class="chip neutral">built-in example</span>' : '') + '</h3>' +
          '<p class="mcp-where"><code>' + esc(server.id) + '</code> · <span class="mono">' + esc(where(server)) + '</span></p></div>' +
        '<span class="chip ' + st.cls + '" role="status">' + esc(st.text) + '</span>' +
      '</header>' +
      '<p class="mcp-facts">' + facts.map(esc).join(' · ') + '</p>' +
      (builtIn ? '<p class="settings-note">Ships with IMURUN and reuses the accounts connected under Settings &gt; Connectors (no second sign-in). Off until you enable it; IMURUN\'s own model already has these tools, so this is mainly for other MCP hosts or for testing.</p>' : '') +
      (server.error && st.key === 'error' ? '<p class="mcp-error">' + esc(server.error) + '</p>' : '') +
      '<div class="mcp-actions">' +
        '<label class="mcp-enable"><input type="checkbox" data-mcp-enable' + (server.enabled ? ' checked' : '') + '> Enabled</label>' +
        (st.key === 'connected'
          ? '<button class="btn" type="button" data-mcp-disconnect>Disconnect</button>'
          : '<button class="btn primary" type="button" data-mcp-connect' + (server.enabled && !state.busy[server.id] ? '' : ' disabled') +
            ' title="' + (server.enabled ? 'Start the server and list its tools' : 'Enable it first') + '">' + (state.busy[server.id] ? 'Connecting…' : 'Connect') + '</button>') +
        '<button class="btn ghost" type="button" data-mcp-tools aria-expanded="' + open + '">' + (open ? 'Hide tools' : 'Tools') + '</button>' +
        '<button class="btn ghost" type="button" data-mcp-edit>Edit</button>' +
        '<button class="btn ghost danger" type="button" data-mcp-delete>Remove</button>' +
      '</div>' +
      '<div class="mcp-tools" data-mcp-toolbox' + (open ? '' : ' hidden') + '>' + toolsHtml(server) + '</div>' +
    '</article>';
  }

  function toolsHtml(server) {
    var view = state.tools[server.id];
    if (!view) return '<p class="settings-note">Loading tools…</p>';
    if (view.error) return '<p class="settings-note">' + esc(view.error) + '</p>';
    var tools = view.tools || [];
    if (!tools.length) {
      return '<p class="settings-note">' + (statusOf(server).key === 'connected'
        ? 'This server offers no tools.'
        : 'Tools are discovered when the server connects. Enable it and press Connect.') + '</p>';
    }
    var auto = server.auto_approve || [];
    return '<p class="settings-note">Each tool becomes a model tool named <code>mcp_' + esc(server.id) + '_&lt;tool&gt;</code>. ' +
      'Auto-approve lets a tool run without a card; tools the server marks destructive always ask.</p>' +
      '<ul class="conn-tools mcp-tool-list">' + tools.map(function (tool) {
        var remote = tool.remote_name || tool.name;
        var level = tool.permission_level || (auto.indexOf(remote) !== -1 ? 'allow' : 'ask');
        var lv = LEVELS[level] || [level, ''];
        var destructive = level === 'always' || (tool.annotations && tool.annotations.destructiveHint === true);
        var on = auto.indexOf(remote) !== -1;
        return '<li class="conn-tool">' +
          '<label class="conn-tool-toggle" title="' + (destructive ? 'Destructive tools cannot be auto-approved' : 'Run without asking') + '">' +
            '<input type="checkbox" data-mcp-auto="' + esc(remote) + '"' + (on && !destructive ? ' checked' : '') + (destructive ? ' disabled' : '') + '>' +
            '<code class="tool-card-name">' + esc(remote) + '</code></label>' +
          '<span class="chip conn-level is-' + esc(level) + '" title="' + esc(lv[1]) + '">' + esc(lv[0]) + '</span>' +
          '<span class="conn-tool-desc">' + esc(String(tool.description || tool.title || '').replace(/^\[MCP [^\]]*\]\s*/, '')) + '</span>' +
          '<span class="conn-tool-why mono">' + esc(tool.name || '') + (tool.available === false ? ' · not available right now' : '') + '</span>' +
        '</li>';
      }).join('') + '</ul>';
  }

  function render() {
    var el = root();
    if (!el) return;
    el.setAttribute('aria-busy', 'false');
    var editor = state.editing ? editorHtml(state.editing) : '';
    var list = state.servers.length
      ? state.servers.map(serverCard).join('')
      : '<div class="empty-state"><h3>No MCP servers</h3><p>Add one, or start from a template.</p></div>';
    el.innerHTML = editor + list + templatesHtml();
    if (state.editing) {
      var first = el.querySelector('[data-mcp-form] input:not([readonly])');
      if (first && state.editing.focus) { first.focus(); state.editing.focus = false; }
    }
  }

  function patchCard(id) {
    var el = root();
    var server = find(id);
    var card = el && el.querySelector('[data-mcp="' + id + '"]');
    if (!card || !server) { render(); return; }
    var wrap = document.createElement('div');
    wrap.innerHTML = serverCard(server);
    card.parentNode.replaceChild(wrap.firstChild, card);
  }

  function templatesHtml() {
    return '<details class="panel settings-card mcp-templates"><summary>Templates</summary>' +
      '<p class="settings-note">Placeholders: <code>${python}</code> this Python, <code>${repo}</code> the IMURUN folder, <code>${home}</code> the data folder (~/.imurun), <code>${env:NAME}</code> an environment variable.</p>' +
      '<ul class="mcp-template-list">' + TEMPLATES.map(function (t) {
        return '<li><div><strong>' + esc(t.title) + '</strong><p>' + esc(t.note) + '</p><code class="mono">' +
          esc(t.config.transport === 'http' ? t.config.url : [t.config.command].concat(t.config.args).join(' ')) + '</code></div>' +
          '<button class="btn" type="button" data-mcp-template="' + esc(t.key) + '">Use</button></li>';
      }).join('') + '</ul></details>';
  }

  function linesOf(obj, sep) {
    return Object.keys(obj || {}).map(function (k) { return k + sep + obj[k]; }).join('\n');
  }

  function editorHtml(ed) {
    var c = ed.config;
    var http = c.transport === 'http';
    return '<form class="panel settings-card mcp-form" data-mcp-form autocomplete="off" aria-label="' + (ed.isNew ? 'Add an MCP server' : 'Edit ' + esc(c.name || c.id)) + '">' +
      '<h3 class="settings-card-title">' + (ed.isNew ? 'Add an MCP server' : 'Edit ' + esc(c.name || c.id)) + '</h3>' +
      '<div class="form-grid">' +
        '<label class="form-field"><span class="k">Id</span><input name="id" value="' + esc(c.id || '') + '" spellcheck="false" maxlength="32" autocapitalize="off"' + (ed.isNew ? '' : ' readonly') + '>' +
          '<small class="tool-field-hint">a-z, 0-9, - or _. Tools are named mcp_&lt;id&gt;_&lt;tool&gt;.</small></label>' +
        '<label class="form-field"><span class="k">Name</span><input name="name" value="' + esc(c.name || '') + '" maxlength="80"></label>' +
      '</div>' +
      '<fieldset class="mcp-transport"><legend class="k">Transport</legend>' +
        '<label><input type="radio" name="transport" value="stdio"' + (http ? '' : ' checked') + '> stdio (a program on this computer)</label>' +
        '<label><input type="radio" name="transport" value="http"' + (http ? ' checked' : '') + '> HTTP (a URL)</label>' +
      '</fieldset>' +
      '<div class="mcp-stdio"' + (http ? ' hidden' : '') + '>' +
        '<label class="form-field"><span class="k">Command</span><input name="command" value="' + esc(c.command || '') + '" spellcheck="false" placeholder="${python}  or  npx"></label>' +
        '<label class="form-field"><span class="k">Arguments (one per line)</span><textarea name="args" rows="3" spellcheck="false">' + esc((c.args || []).join('\n')) + '</textarea></label>' +
        '<label class="form-field"><span class="k">Environment (NAME=value per line)</span><textarea name="env" rows="2" spellcheck="false" placeholder="API_KEY=${env:MY_KEY}">' + esc(linesOf(c.env, '=')) + '</textarea>' +
          '<small class="tool-field-hint">Saved values are not shown (' + REDACTED + ' means "keep the saved value"). Prefer ${env:NAME} over pasting a key.</small></label>' +
        '<label class="form-field"><span class="k">Working folder (optional)</span><input name="cwd" value="' + esc(c.cwd || '') + '" spellcheck="false"></label>' +
      '</div>' +
      '<div class="mcp-http"' + (http ? '' : ' hidden') + '>' +
        '<label class="form-field"><span class="k">URL</span><input name="url" value="' + esc(c.url || '') + '" spellcheck="false" placeholder="https://example.com/mcp"></label>' +
        '<label class="form-field"><span class="k">Headers (Name: value per line)</span><textarea name="headers" rows="2" spellcheck="false" placeholder="Authorization: Bearer ${env:MCP_TOKEN}">' + esc(linesOf(c.headers, ': ')) + '</textarea>' +
          '<small class="tool-field-hint">Saved values are not shown (' + REDACTED + ' keeps the saved value).</small></label>' +
      '</div>' +
      '<div class="form-grid">' +
        autoApproveField(c) +
        '<label class="form-field"><span class="k">Timeout per call (seconds)</span><input name="timeout" type="number" min="1" max="600" value="' + esc(String(c.timeout || 60)) + '"></label>' +
      '</div>' +
      '<label class="toggle-row"><span><strong>Enabled</strong><small>Connect at start-up and offer its tools to the model.</small></span><input type="checkbox" name="enabled"' + (c.enabled ? ' checked' : '') + '></label>' +
      '<p class="mcp-form-error" data-mcp-formerror role="alert" hidden></p>' +
      '<div class="conn-form-foot"><button class="btn primary" type="submit">' + (ed.isNew ? 'Add server' : 'Save') + '</button>' +
        '<button class="btn ghost" type="button" data-mcp-cancel>Cancel</button></div>' +
    '</form>';
  }

  /* ---------------------------------------------------------- parsing */

  function parsePairs(text, sep) {
    var out = {};
    var bad = '';
    String(text || '').split(/\r?\n/).forEach(function (line) {
      if (!line.trim()) return;
      var at = line.indexOf(sep);
      if (at <= 0) { bad = bad || line.trim(); return; }
      var key = line.slice(0, at).trim();
      var value = line.slice(at + sep.length).trim();
      if (!key) { bad = bad || line.trim(); return; }
      out[key] = value;
    });
    return { values: out, bad: bad };
  }

  /* Form -> server config; returns {config} or {error}. Pure so it can be
   * unit-checked without a DOM (data = plain object of field values). */
  function configFrom(data) {
    var id = String(data.id || '').trim().toLowerCase();
    if (!ID_RE.test(id)) return { error: 'The id must be 1-32 characters: a-z, 0-9, "-" or "_", starting with a letter or digit.' };
    var transport = data.transport === 'http' ? 'http' : 'stdio';
    var config = { id: id, name: String(data.name || '').trim() || id, transport: transport,
      enabled: Boolean(data.enabled), auto_approve: String(data.auto_approve || '').split(/[,\n]/).map(function (s) { return s.trim(); }).filter(Boolean) };
    var timeout = Number(data.timeout);
    if (isFinite(timeout) && timeout > 0) config.timeout = Math.max(1, Math.min(600, timeout));
    if (transport === 'stdio') {
      config.command = String(data.command || '').trim();
      if (!config.command) return { error: 'A stdio server needs a command.' };
      config.args = String(data.args || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
      var env = parsePairs(data.env, '=');
      if (env.bad) return { error: 'Environment line "' + env.bad + '" is not NAME=value.' };
      config.env = env.values;
      config.cwd = String(data.cwd || '').trim();
      config.url = '';
      config.headers = {};
    } else {
      config.url = String(data.url || '').trim();
      if (!config.url) return { error: 'An HTTP server needs a URL.' };
      if (config.url.indexOf('${') === -1 && !/^https?:\/\//i.test(config.url)) return { error: 'The URL must start with https:// (or http:// for a server on this computer).' };
      var headers = parsePairs(data.headers, ':');
      if (headers.bad) return { error: 'Header line "' + headers.bad + '" is not "Name: value".' };
      config.headers = headers.values;
      config.command = '';
      config.args = [];
      config.env = {};
    }
    return { config: config };
  }

  /* Auto-approve (F3): a multi-select of the server's discovered tools when
   * they are known (destructive ones cannot be ticked), plus a free list for
   * names not discovered yet. */
  function autoApproveField(c) {
    var auto = c.auto_approve || [];
    var view = state.tools[c.id];
    var tools = view && Array.isArray(view.tools) ? view.tools : [];
    var known = tools.map(function (t) { return t.remote_name || t.name; });
    var extra = auto.filter(function (n) { return known.indexOf(n) === -1; });
    var list = tools.length ? '<fieldset class="mcp-auto"><legend class="k">Auto-approve (run without a card)</legend>' + tools.map(function (t) {
      var name = t.remote_name || t.name;
      var destructive = t.permission_level === 'always' || (t.annotations && t.annotations.destructiveHint === true);
      return '<label class="mcp-auto-item' + (destructive ? ' is-locked' : '') + '"' + (destructive ? ' title="The server marks it destructive: it always asks"' : '') + '>' +
        '<input type="checkbox" name="auto_pick" value="' + esc(name) + '"' + (auto.indexOf(name) !== -1 && !destructive ? ' checked' : '') + (destructive ? ' disabled' : '') + '> ' +
        '<code>' + esc(name) + '</code></label>';
    }).join('') + '</fieldset>' : '';
    return list + '<label class="form-field"><span class="k">' + (tools.length ? 'Other tool names (comma separated)' : 'Auto-approve (tool names, comma separated; connect the server to pick from its tools)') +
      '</span><input name="auto_approve" value="' + esc((tools.length ? extra : auto).join(', ')) + '" spellcheck="false" placeholder="read_file, list_directory"></label>';
  }

  function formData(form) {
    var get = function (name) { var f = form.elements[name]; return f && f.value !== undefined ? f.value : ''; };
    var transport = form.querySelector('input[name="transport"]:checked');
    var picked = Array.prototype.map.call(form.querySelectorAll('input[name="auto_pick"]:checked'), function (i) { return i.value; });
    var typed = get('auto_approve');
    return { id: get('id'), name: get('name'), transport: transport ? transport.value : 'stdio', command: get('command'),
      args: get('args'), env: get('env'), cwd: get('cwd'), url: get('url'), headers: get('headers'),
      auto_approve: picked.concat(typed ? [typed] : []).join(', '), timeout: get('timeout'), enabled: form.elements.enabled && form.elements.enabled.checked };
  }

  /* ---------------------------------------------------------- actions */

  function load() {
    var el = root();
    if (!el || !api() || state.loading) return;
    bind();
    state.loading = true;
    api().listMcpServers().then(function (servers) {
      state.loading = false;
      state.servers = servers || [];
      render();
      Object.keys(state.open).forEach(function (id) { if (state.open[id]) loadTools(id); });
    }, function (error) {
      state.loading = false;
      el.innerHTML = '<div class="empty-state"><h3>MCP servers could not be loaded</h3><p>' +
        esc(error && error.status === 501 ? 'MCP is not wired on this server.' : ((error && error.message) || 'The server did not answer.')) +
        '</p><button class="btn" type="button" data-mcp-refresh>Try again</button></div>' + templatesHtml();
    });
  }

  function loadTools(id) {
    api().getMcpTools(id).then(function (view) {
      state.tools[id] = { tools: (view && view.tools) || [] };
      if (view && view.status) { var s = find(id); if (s) s.status = view.status; }
    }, function (error) {
      state.tools[id] = { error: (error && error.message) || 'Could not list the tools.' };
    }).then(function () { patchCard(id); });
  }

  function upsert(config, title) {
    return api().saveMcpServer(config).then(function (view) {
      var server = view && view.id ? view : config;
      var at = -1;
      state.servers.forEach(function (s, i) { if (s.id === server.id) at = i; });
      if (at === -1) state.servers.push(server);
      else state.servers[at] = server;
      delete state.tools[server.id];
      toast({ tone: 'success', icon: 'check', kicker: 'MCP', title: title, text: server.enabled ? 'Connecting in the background.' : 'Saved; enable it to connect.' });
      return server;
    });
  }

  function connect(id) {
    state.busy[id] = 'connecting';
    patchCard(id);
    api().connectMcpServer(id).then(function (view) {
      delete state.busy[id];
      var s = find(id);
      if (s && view) Object.assign(s, view);
      loadTools(id);
    }, function (error) {
      delete state.busy[id];
      var s = find(id);
      var body = error && error.body;
      if (s && body && body.server) Object.assign(s, body.server);
      else if (s) { s.status = 'error'; s.error = (error && error.message) || 'could not connect'; }
      patchCard(id);
      fail('Could not connect ' + ((s && s.name) || id), error);
    });
  }

  function disconnect(id) {
    api().disconnectMcpServer(id).then(function (view) {
      var s = find(id);
      if (s && view) Object.assign(s, view);
      patchCard(id);
    }, function (error) { fail('Could not disconnect', error); });
  }

  function remove(id) {
    var s = find(id);
    if (!window.confirm('Remove the MCP server "' + ((s && s.name) || id) + '"? Its tools disappear from the model at once.')) return;
    api().deleteMcpServer(id).then(function () {
      state.servers = state.servers.filter(function (x) { return x.id !== id; });
      delete state.tools[id];
      delete state.open[id];
      render();
      toast({ tone: 'neutral', icon: 'info', kicker: 'MCP', title: 'Server removed', text: id });
    }, function (error) { fail('Could not remove the server', error); });
  }

  function setAuto(id, tool, on) {
    var s = find(id);
    if (!s) return;
    var auto = (s.auto_approve || []).filter(function (n) { return n !== tool; });
    if (on) auto.push(tool);
    var config = Object.assign({}, s, { auto_approve: auto });
    ['status', 'error', 'tools', 'protocolVersion', 'serverInfo', 'connectedAt'].forEach(function (k) { delete config[k]; });
    upsert(config, tool + (on ? ' auto-approved' : ' asks again')).then(function () { state.open[id] = true; loadTools(id); }, function (error) {
      fail('Not saved', error);
      patchCard(id);
    });
  }

  function edit(id) {
    var s = find(id);
    if (!s) return;
    var config = JSON.parse(JSON.stringify(s));
    state.editing = { isNew: false, config: config, focus: true };
    /* Fetch the tools so auto-approve can be picked from them. */
    if (!state.tools[id]) {
      api().getMcpTools(id).then(function (view) {
        state.tools[id] = { tools: (view && view.tools) || [] };
        if (state.editing && state.editing.config.id === id) render();
      }, function () {});
    }
    render();
    root().scrollIntoView({ block: 'start', behavior: util.reducedMotion() ? 'auto' : 'smooth' });
  }

  function startNew(template) {
    var base = template ? JSON.parse(JSON.stringify(template.config)) : { id: '', name: '', transport: 'stdio', command: '', args: [], env: {}, headers: {}, url: '', enabled: true, auto_approve: [] };
    if (template && find(base.id)) {
      if (template.key === 'gmail' || template.key === 'connectors') { edit(base.id); return; }
      var n = 2;
      while (find(base.id + '-' + n)) n += 1;
      base.id = base.id + '-' + n;
    }
    state.editing = { isNew: true, config: base, focus: true };
    render();
    root().scrollIntoView({ block: 'start', behavior: util.reducedMotion() ? 'auto' : 'smooth' });
  }

  function submit(form) {
    var parsed = configFrom(formData(form));
    var err = form.querySelector('[data-mcp-formerror]');
    if (parsed.error) { err.hidden = false; err.textContent = parsed.error; return; }
    if (state.editing.isNew && find(parsed.config.id)) { err.hidden = false; err.textContent = 'A server with the id "' + parsed.config.id + '" already exists.'; return; }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    upsert(parsed.config, state.editing.isNew ? 'MCP server added' : 'MCP server saved').then(function (server) {
      state.editing = null;
      if (server.enabled) state.busy[server.id] = 'connecting';
      render();
      if (server.enabled) setTimeout(function () { delete state.busy[server.id]; load(); }, 2500);
    }, function (error) {
      button.disabled = false;
      err.hidden = false;
      err.textContent = (error && error.message) || 'The server refused the configuration.';
    });
  }

  /* ----------------------------------------------------------- events */

  function onEvent(event) {
    var id = String(event.id || '');
    var s = find(id);
    if (event.type === 'mcp.error') {
      toast({ id: 'mcp-' + id, tone: 'error', icon: 'alert', kicker: 'MCP', title: (event.name || id) + ': error', text: event.error || 'The server failed.' });
    }
    if (!s) { if (root() && state.servers.length) load(); return; }
    delete state.busy[id];
    if (event.type === 'mcp.connected') {
      s.status = 'connected';
      s.error = '';
      if (event.tools !== undefined) s.tools = event.tools;
      if (state.open[id]) loadTools(id);
    } else if (event.type === 'mcp.disconnected') {
      if (s.status === 'connected') s.status = s.enabled ? 'disconnected' : 'disabled';
    } else if (event.type === 'mcp.error') {
      s.status = 'error';
      s.error = event.error || '';
    }
    patchCard(id);
  }

  function bind() {
    var el = root();
    if (state.bound || !el) return;
    state.bound = true;
    el.addEventListener('submit', function (event) {
      var form = event.target.closest('[data-mcp-form]');
      if (!form) return;
      event.preventDefault();
      submit(form);
    });
    el.addEventListener('change', function (event) {
      var t = event.target;
      if (t.name === 'transport') {
        var form = t.closest('[data-mcp-form]');
        form.querySelector('.mcp-stdio').hidden = t.value === 'http';
        form.querySelector('.mcp-http').hidden = t.value !== 'http';
        return;
      }
      var card = t.closest('[data-mcp]');
      if (!card) return;
      var id = card.dataset.mcp;
      if (t.hasAttribute('data-mcp-enable')) {
        var s = find(id);
        var config = Object.assign({}, s, { enabled: t.checked });
        ['status', 'error', 'tools', 'protocolVersion', 'serverInfo', 'connectedAt'].forEach(function (k) { delete config[k]; });
        upsert(config, (s.name || id) + (t.checked ? ' enabled' : ' turned off')).then(function () {
          if (t.checked) state.busy[id] = 'connecting';
          patchCard(id);
          if (t.checked) setTimeout(function () { delete state.busy[id]; load(); }, 2500);
        }, function (error) { fail('Not saved', error); t.checked = !t.checked; });
      } else if (t.hasAttribute('data-mcp-auto')) {
        setAuto(id, t.getAttribute('data-mcp-auto'), t.checked);
      }
    });
    el.addEventListener('click', function (event) {
      var t = event.target.closest('button');
      if (!t) return;
      if (t.hasAttribute('data-mcp-cancel')) { state.editing = null; render(); return; }
      if (t.hasAttribute('data-mcp-template')) {
        var key = t.getAttribute('data-mcp-template');
        startNew(TEMPLATES.filter(function (x) { return x.key === key; })[0]);
        return;
      }
      var card = t.closest('[data-mcp]');
      if (!card) return;
      var id = card.dataset.mcp;
      if (t.hasAttribute('data-mcp-connect')) connect(id);
      else if (t.hasAttribute('data-mcp-disconnect')) disconnect(id);
      else if (t.hasAttribute('data-mcp-edit')) edit(id);
      else if (t.hasAttribute('data-mcp-delete')) remove(id);
      else if (t.hasAttribute('data-mcp-tools')) {
        state.open[id] = !state.open[id];
        if (state.open[id] && !state.tools[id]) loadTools(id);
        patchCard(id);
      }
    });
  }

  function init() {
    IM.bus.on('settings.section', function (section) { if (section === 'mcp') load(); });
    ['mcp.connected', 'mcp.disconnected', 'mcp.error'].forEach(function (type) {
      IM.bus.on('server:' + type, onEvent);
    });
    document.addEventListener('click', function (event) {
      if (!event.target.closest) return;
      if (event.target.closest('[data-mcp-refresh]')) { state.tools = {}; load(); }
      if (event.target.closest('[data-mcp-add]')) { bind(); startNew(null); }
    });
  }

  IM.mcp = { load: load, configFrom: configFrom, parsePairs: parsePairs, onEvent: onEvent, templates: TEMPLATES, _state: state };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
