/* Settings > Permissions, Tools, Agent and Search providers.
 *
 *   Permissions  GET/POST /api/permissions (mode, workspace folder, category
 *                toggles, approval timeout, remembered approvals) and the
 *                connector-only `sendEmail` toggle in settings section
 *                `permissions` (PUT merges only that key).
 *   Tools        GET /api/tools: schema, category and permission per tool,
 *                plus a manual run form (POST /api/tools/run, including the
 *                403 "ask" -> confirm -> approved: true round trip).
 *   Agent        GET/POST /api/agent (maxSteps).
 *   Search       settings section `search` (GET/PUT /api/settings/search),
 *                keys named like the environment variables the server
 *                exports them to; write-only here, never cached locally.
 *
 * Each panel loads when its Settings section is first shown and re-renders
 * values in place; listeners are delegated and bound once.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var esc = util.escapeHtml;

  function api() { return window.__imurun_api; }
  function byId(id) { return document.getElementById(id); }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function fail(kicker, title, error) {
    toast({ tone: 'error', icon: 'alert', kicker: kicker, title: title, text: (error && error.message) || 'The server did not answer.' });
  }

  /* ========================================================= Permissions */

  var MODES = [
    { id: 'ask', title: 'Ask for everything',
      text: 'Imurun asks before it reads, writes, deletes or runs anything in the workspace. Web search, web pages, weather and the clock still run without asking.' },
    { id: 'allow-reads', title: 'Reads run free', badge: 'Default',
      text: 'Imurun may read and list files in the workspace without asking. Writing, moving, copying, deleting and shell commands open an approval card first.' },
    { id: 'allow-all', title: 'Allow everything',
      text: 'Every tool runs without asking, inside the workspace. Use it only for a folder you can afford to lose. The limits under "Always refused" still apply.' }
  ];
  var CATEGORIES = [
    ['info', 'Clock and location'], ['network', 'Web search, web pages, weather'], ['agent', 'Sub-agents (their own calls are checked one by one)'],
    ['read', 'Read and list files'], ['write', 'Write, move, copy, make folders'], ['delete', 'Delete files'], ['shell', 'Shell commands']
  ];
  var TOGGLE_OF = { read: 'readFiles', write: 'writeFiles', delete: 'writeFiles', shell: 'runShell', network: 'network' };
  var TOGGLES = [
    ['readFiles', 'Read files', 'Off: file_read and file_list are refused.'],
    ['writeFiles', 'Write files', 'Off: writing, moving, copying, making folders and deleting are all refused.'],
    ['runShell', 'Run shell commands', 'Off: shell is refused. On: commands run in the workspace with credential-like environment variables removed, and they ask unless the mode is Allow everything. Shell is not a sandbox.'],
    ['network', 'Network', 'Off: web search, web pages and weather are refused, and nothing is looked up before answering.']
  ];
  var DENIALS = [
    'Anything outside the workspace folder, in every mode. ".." and links cannot escape it.',
    'Protected paths inside it: .git, secret files (.env, .netrc, .npmrc, .pypirc, .git-credentials, .htpasswd), Imurun\'s own guardrail code, and system locations.',
    'A whole drive (C:\\ or /) can never be the workspace, and the workspace folder itself is never deleted.',
    'Destructive commands, whatever the mode: wiping or formatting disks (rm -rf /, format c:, Format-Volume, Clear-Disk, diskpart), recursive deletes of a drive or your home folder, registry and boot changes, shutdown, piping a download into a shell (curl | sh, iwr | iex), adding users, opening a drive to Everyone, and similar.',
    'A category you switched off below.',
    'With no IMURUN window connected, a call that needs approval is denied at once; an unanswered card is denied when its timer runs out.'
  ];

  var perm = { view: null, built: false, loading: false };

  function permRoot() { return byId('perm-root'); }

  function buildPermissions(root) {
    root.innerHTML =
      '<div class="panel settings-card">' +
        '<h3 class="settings-card-title" id="perm-mode-heading">Mode</h3>' +
        '<div class="perm-modes" role="radiogroup" aria-labelledby="perm-mode-heading">' +
          MODES.map(function (mode) {
            return '<label class="perm-mode" data-mode="' + mode.id + '">' +
              '<input type="radio" name="perm-mode" value="' + mode.id + '">' +
              '<span class="perm-mode-title">' + esc(mode.title) + (mode.badge ? ' <span class="chip neutral">' + esc(mode.badge) + '</span>' : '') + '</span>' +
              '<span class="perm-mode-text">' + esc(mode.text) + '</span>' +
            '</label>';
          }).join('') +
        '</div>' +
        '<div class="perm-table-wrap"><table class="perm-table" aria-label="What runs and what asks, per mode"><thead><tr><th scope="col">Tools</th>' +
          MODES.map(function (mode) { return '<th scope="col" data-col="' + mode.id + '">' + esc(mode.title) + '</th>'; }).join('') +
        '</tr></thead><tbody></tbody></table></div>' +
      '</div>' +
      '<div class="panel settings-card">' +
        '<h3 class="settings-card-title">Workspace folder</h3>' +
        '<p class="settings-note">File tools and shell commands are confined to this one folder. Leave it empty for the default (~/.imurun/workspace); it is created on first use.</p>' +
        '<form class="perm-workspace" id="perm-workspace-form">' +
          '<label class="form-field"><span class="k">Folder (absolute path)</span><input id="perm-workspace" autocomplete="off" spellcheck="false" placeholder="C:\\Users\\you\\Projects\\sandbox"></label>' +
          '<button class="btn primary" type="submit">Save folder</button>' +
        '</form>' +
        '<p class="settings-note" id="perm-workspace-note"></p>' +
      '</div>' +
      '<div class="panel settings-card">' +
        '<h3 class="settings-card-title">Switch categories off</h3>' +
        TOGGLES.map(function (t) {
          return '<label class="toggle-row"><span><strong>' + esc(t[1]) + '</strong><small>' + esc(t[2]) + '</small></span><input type="checkbox" data-perm-toggle="' + t[0] + '"></label>';
        }).join('') +
        '<label class="toggle-row"><span><strong>Send email (Gmail connector)</strong><small>Connector setting. Gmail send always opens the editable approval card.</small></span><input type="checkbox" id="perm-send-email"></label>' +
      '</div>' +
      '<div class="panel settings-card">' +
        '<h3 class="settings-card-title">Approval cards</h3>' +
        '<form class="perm-timeout" id="perm-timeout-form">' +
          '<label class="form-field"><span class="k">Wait for an answer (seconds)</span><input type="number" id="perm-timeout" min="10" max="1800" step="5"></label>' +
          '<button class="btn" type="submit">Save</button>' +
        '</form>' +
        '<p class="settings-note">A reply that needs your approval waits this long, then the call is denied and the reply goes on without it. "Approve and remember" lets that tool run without asking for the rest of the session, until the server restarts. Deletes are never remembered.</p>' +
        '<div class="perm-grants" id="perm-grants"></div>' +
      '</div>' +
      '<div class="panel settings-card">' +
        '<h3 class="settings-card-title">Always refused</h3>' +
        '<p class="settings-note">These are decided in code before the mode is consulted. No setting and no model output can turn them off.</p>' +
        '<ul class="perm-denials">' + DENIALS.map(function (text) { return '<li>' + esc(text) + '</li>'; }).join('') + '</ul>' +
      '</div>';
    perm.built = true;

    root.addEventListener('change', function (event) {
      var target = event.target;
      if (target.name === 'perm-mode') {
        savePermissions({ mode: target.value }, 'Mode: ' + labelOf(target.value));
      } else if (target.dataset && target.dataset.permToggle) {
        var change = {};
        change[target.dataset.permToggle] = target.checked;
        savePermissions(change, (target.checked ? 'On: ' : 'Off: ') + target.closest('.toggle-row').querySelector('strong').textContent);
      } else if (target.id === 'perm-send-email') {
        api().updateSettings('permissions', { sendEmail: target.checked }).then(function () {
          if (window.__imurun_settings) window.__imurun_settings.set({ permissions: { sendEmail: target.checked } }, { silent: true });
          toast({ tone: 'success', icon: 'check', kicker: 'Permissions', title: 'Send email ' + (target.checked ? 'on' : 'off') });
        }, function (error) {
          target.checked = !target.checked;
          fail('Permissions', 'Send email not saved', error);
        });
      }
    });
    root.addEventListener('submit', function (event) {
      event.preventDefault();
      if (event.target.id === 'perm-workspace-form') {
        savePermissions({ workspaceRoot: byId('perm-workspace').value.trim() }, 'Workspace folder saved');
      } else if (event.target.id === 'perm-timeout-form') {
        savePermissions({ approvalTimeout: Number(byId('perm-timeout').value) }, 'Approval timeout saved');
      }
    });
  }

  function labelOf(mode) {
    var match = MODES.filter(function (m) { return m.id === mode; })[0];
    return match ? match.title : mode;
  }

  function fillPermissions(view) {
    perm.view = view;
    var root = permRoot();
    if (!root) return;
    if (!perm.built) buildPermissions(root);
    var state = byId('perm-state');
    if (state) {
      state.className = 'chip ' + (view.mode === 'allow-all' ? 'error' : 'success');
      state.textContent = labelOf(view.mode);
    }
    root.querySelectorAll('input[name="perm-mode"]').forEach(function (input) {
      input.checked = input.value === view.mode;
      input.closest('.perm-mode').classList.toggle('is-active', input.checked);
    });
    var asks = view.table || { ask: ['read', 'write', 'delete', 'shell'], 'allow-reads': ['write', 'delete', 'shell'], 'allow-all': [] };
    root.querySelector('.perm-table tbody').innerHTML = CATEGORIES.map(function (cat) {
      var toggle = TOGGLE_OF[cat[0]];
      var off = toggle && view[toggle] === false;
      return '<tr><th scope="row">' + esc(cat[1]) + '</th>' + MODES.map(function (mode) {
        var ask = (asks[mode.id] || []).indexOf(cat[0]) !== -1;
        var cls = off ? 'is-off' : (ask ? 'is-ask' : 'is-run');
        var text = off ? 'off' : (ask ? 'asks' : 'runs');
        return '<td class="' + cls + (mode.id === view.mode ? ' is-current' : '') + '">' + text + '</td>';
      }).join('') + '</tr>';
    }).join('');
    root.querySelectorAll('.perm-table th[data-col]').forEach(function (th) {
      th.classList.toggle('is-current', th.dataset.col === view.mode);
    });
    var input = byId('perm-workspace');
    if (input && document.activeElement !== input) input.value = view.workspaceRoot || '';
    var note = byId('perm-workspace-note');
    if (note) {
      note.innerHTML = 'In use: <code>' + esc(view.workspace || '') + '</code> ' +
        (view.workspaceExists ? '<span class="chip success">exists</span>' : '<span class="chip neutral">created on first use</span>');
    }
    TOGGLES.forEach(function (t) {
      var box = root.querySelector('[data-perm-toggle="' + t[0] + '"]');
      if (box) box.checked = view[t[0]] !== false;
    });
    var email = byId('perm-send-email');
    var cached = window.__imurun_settings ? (window.__imurun_settings.get().permissions || {}) : {};
    if (email) email.checked = cached.sendEmail !== false;
    var timeout = byId('perm-timeout');
    if (timeout && document.activeElement !== timeout) timeout.value = view.approvalTimeout || 180;
    var grants = byId('perm-grants');
    if (grants) {
      var sessions = Object.keys(view.grants || {}).filter(function (id) { return (view.grants[id] || []).length; });
      grants.innerHTML = sessions.length
        ? '<div class="settings-label">Remembered for this server run</div>' + sessions.map(function (id) {
            var session = (IM.state.sessions || []).filter(function (s) { return s.id === id; })[0];
            return '<div class="settings-line"><span class="k">' + esc(session ? session.name || id : id) + '</span><span class="v mono">' +
              esc(view.grants[id].join(', ')) + '</span></div>';
          }).join('')
        : '<p class="settings-note">No approvals are remembered right now.</p>';
    }
  }

  function loadPermissions() {
    var root = permRoot();
    if (!root || perm.loading || !api()) return;
    perm.loading = true;
    api().getPermissions().then(fillPermissions, function (error) {
      var state = byId('perm-state');
      if (state) { state.className = 'chip error'; state.textContent = 'unavailable'; }
      if (!perm.built) {
        root.innerHTML = '<div class="panel settings-card"><div class="empty-state"><h3>' +
          (error && error.status === 501 ? 'Permissions are not configured on this server' : 'Could not load permissions') +
          '</h3><p>' + esc((error && error.message) || 'The server did not answer.') + '</p>' +
          '<button class="btn primary" type="button" data-perm-retry>Try again</button></div></div>';
      }
    }).then(function () { perm.loading = false; });
  }

  function savePermissions(changes, success) {
    var root = permRoot();
    if (root) root.classList.add('is-saving');
    api().setPermissions(changes).then(function (view) {
      fillPermissions(view);
      toast({ tone: 'success', icon: 'check', kicker: 'Permissions', title: success, text: 'Applies to the next tool call.' });
    }, function (error) {
      if (perm.view) fillPermissions(perm.view);
      fail('Permissions', 'Not saved', error);
    }).then(function () { if (root) root.classList.remove('is-saving'); });
  }

  /* ================================================================ Tools */

  var tools = { view: null, loading: false, bound: false };
  var LONG_TEXT = { content: true, command: true, task: true };

  function permChip(permission) {
    if (permission === 'allow') return '<span class="chip success">runs</span>';
    if (permission === 'ask') return '<span class="chip neutral perm-ask">asks</span>';
    if (permission === 'deny') return '<span class="chip error">off</span>';
    return '';
  }

  function fieldFor(tool, name, schema, required) {
    var id = 'tool-' + tool + '-' + name;
    var type = schema.type || 'string';
    var label = '<span class="k">' + esc(name) + (required ? ' *' : '') + '</span>';
    var hint = schema.description ? '<small class="tool-field-hint">' + esc(schema.description) + '</small>' : '';
    var attrs = ' id="' + esc(id) + '" data-arg="' + esc(name) + '" data-type="' + esc(type) + '"' + (required ? ' required' : '');
    if (Array.isArray(schema.enum)) {
      return '<label class="form-field">' + label + '<select' + attrs + '>' + (required ? '' : '<option value="">(default)</option>') +
        schema.enum.map(function (v) { return '<option value="' + esc(v) + '">' + esc(v) + '</option>'; }).join('') + '</select>' + hint + '</label>';
    }
    if (type === 'boolean') {
      return '<label class="toggle-row"><span><strong>' + esc(name) + '</strong>' + hint + '</span><input type="checkbox"' + attrs.replace(' required', '') + '></label>';
    }
    if (type === 'integer' || type === 'number') {
      var range = (schema.minimum !== undefined ? ' min="' + esc(schema.minimum) + '"' : '') + (schema.maximum !== undefined ? ' max="' + esc(schema.maximum) + '"' : '');
      return '<label class="form-field">' + label + '<input type="number"' + (type === 'integer' ? ' step="1"' : ' step="any"') + range + attrs + '>' + hint + '</label>';
    }
    if (type === 'object') {
      return '<label class="form-field">' + label + '<textarea rows="3" placeholder="{ }"' + attrs + '></textarea>' + hint + '</label>';
    }
    if (type === 'array') {
      return '<label class="form-field">' + label + '<input placeholder="comma-separated"' + attrs + '>' + hint + '</label>';
    }
    if (LONG_TEXT[name]) {
      return '<label class="form-field">' + label + '<textarea rows="3"' + attrs + '></textarea>' + hint + '</label>';
    }
    return '<label class="form-field">' + label + '<input autocomplete="off" spellcheck="false"' + attrs + '>' + hint + '</label>';
  }

  function schemaRows(parameters) {
    var props = (parameters && parameters.properties) || {};
    var required = (parameters && parameters.required) || [];
    var names = Object.keys(props);
    if (!names.length) return '<p class="settings-note">No arguments.</p>';
    return '<table class="tool-schema"><thead><tr><th scope="col">Argument</th><th scope="col">Type</th><th scope="col">Description</th></tr></thead><tbody>' +
      names.map(function (name) {
        var p = props[name] || {};
        var type = p.type || 'any';
        if (Array.isArray(p.enum)) type += ' (' + p.enum.join(' | ') + ')';
        if (p.minimum !== undefined || p.maximum !== undefined) type += ' ' + (p.minimum !== undefined ? p.minimum : '') + '…' + (p.maximum !== undefined ? p.maximum : '');
        return '<tr><td><code>' + esc(name) + '</code>' + (required.indexOf(name) !== -1 ? ' <span class="chip neutral">required</span>' : '') + '</td>' +
          '<td class="mono">' + esc(type) + '</td><td>' + esc(p.description || '') + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function renderTools(view) {
    tools.view = view;
    var root = byId('tools-root');
    if (!root) return;
    var list = (view && view.tools) || [];
    var perms = (view && view.permissions) || {};
    var head = '<div class="panel settings-card tools-summary">' +
      '<div class="settings-line"><span class="k">Network</span><span class="v">' + (view && view.network ? 'reachable' : 'offline or not checked') + '</span>' +
        '<span class="chip ' + (view && view.network ? 'success' : 'neutral') + '">' + (view && view.network ? 'online' : 'offline') + '</span></div>' +
      '<div class="settings-line"><span class="k">Mode</span><span class="v">' + esc(labelOf(perms.mode || '')) + '</span>' +
        '<button class="btn ghost" type="button" data-goto-permissions>Change</button></div>' +
      (perms.workspace ? '<div class="settings-line"><span class="k">Workspace</span><span class="v mono">' + esc(perms.workspace) + '</span></div>' : '') +
      (view && view.format ? '<details class="tools-format"><summary>The call format the model is taught</summary><pre>' + esc(view.format) + '</pre></details>' : '') +
    '</div>';
    if (!list.length) {
      root.innerHTML = head + '<div class="panel settings-card"><div class="empty-state"><h3>No tools on this server</h3><p>The server reported an empty tool list.</p></div></div>';
      return;
    }
    root.innerHTML = head + '<div class="tool-cards">' + list.map(function (tool) {
      var params = tool.parameters || {};
      var props = params.properties || {};
      var required = params.required || [];
      return '<details class="panel tool-card" data-tool="' + esc(tool.name) + '">' +
        '<summary>' +
          '<code class="tool-card-name">' + esc(tool.name) + '</code>' +
          '<span class="chip neutral">' + esc(tool.category || 'tool') + '</span>' +
          permChip(tool.permission) +
          (tool.available === false ? '<span class="chip error">unavailable</span>' : '') +
          '<span class="tool-card-desc">' + esc((tool.description || '').split('\n')[0]) + '</span>' +
        '</summary>' +
        '<div class="tool-card-body">' +
          (tool.description ? '<p class="tool-card-full">' + esc(tool.description) + '</p>' : '') +
          (tool.available === false && tool.reason ? '<p class="settings-note">Unavailable: ' + esc(tool.reason) + '</p>' : '') +
          schemaRows(params) +
          '<form class="tool-run" data-tool-form="' + esc(tool.name) + '" novalidate>' +
            '<div class="tool-run-fields">' + Object.keys(props).map(function (name) {
              return fieldFor(tool.name, name, props[name] || {}, required.indexOf(name) !== -1);
            }).join('') + '</div>' +
            '<div class="tool-run-actions"><button class="btn primary" type="submit">Run ' + esc(tool.name) + '</button>' +
            '<span class="settings-note">Runs now, as you, through the same permission checks as the model.</span></div>' +
          '</form>' +
          '<div class="tool-run-out" aria-live="polite"></div>' +
          '<details class="tool-json"><summary>JSON schema</summary><pre>' + esc(JSON.stringify(params, null, 2)) + '</pre></details>' +
        '</div>' +
      '</details>';
    }).join('') + '</div>';
  }

  function collectArgs(form) {
    var args = {};
    var problem = '';
    form.querySelectorAll('[data-arg]').forEach(function (input) {
      if (problem) return;
      var name = input.dataset.arg;
      var type = input.dataset.type;
      if (input.type === 'checkbox') {
        if (input.checked) args[name] = true;
        return;
      }
      var raw = input.value.trim();
      if (!raw) {
        if (input.required) problem = name + ' is required.';
        return;
      }
      if (type === 'integer' || type === 'number') {
        var n = Number(raw);
        if (!isFinite(n)) problem = name + ' must be a number.';
        else args[name] = type === 'integer' ? Math.round(n) : n;
      } else if (type === 'array') {
        args[name] = raw.split(',').map(function (part) { return part.trim(); }).filter(Boolean);
      } else if (type === 'object') {
        try { args[name] = JSON.parse(raw); } catch (error) { problem = name + ' must be JSON.'; }
      } else {
        args[name] = input.tagName === 'TEXTAREA' ? input.value : raw;
      }
    });
    return { args: args, problem: problem };
  }

  function showRunResult(out, execution) {
    var result = (execution && execution.result) || {};
    var ok = Boolean(result.ok);
    var bits = [ok ? 'Done' : (execution.decision && execution.decision !== 'allow' ? 'Refused' : 'Failed')];
    if (result.latency_ms) bits.push(result.latency_ms < 1000 ? result.latency_ms + ' ms' : (result.latency_ms / 1000).toFixed(1) + ' s');
    if (result.offline) bits.push('offline');
    out.innerHTML = '<div class="tool-run-result ' + (ok ? 'is-ok' : 'is-failed') + '"><strong></strong><pre></pre></div>';
    out.querySelector('strong').textContent = bits.join(' · ');
    out.querySelector('pre').textContent = String(result.content || result.error || '(no output)');
  }

  function runTool(form, approved) {
    var name = form.dataset.toolForm;
    var out = form.parentNode.querySelector('.tool-run-out');
    var collected = collectArgs(form);
    if (collected.problem) {
      out.innerHTML = '<p class="tool-run-msg is-failed"></p>';
      out.firstChild.textContent = collected.problem;
      return;
    }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    out.innerHTML = '<p class="tool-run-msg"><span class="notif-spinner" aria-hidden="true"></span> Running ' + esc(name) + '…</p>';
    api().runTool(name, collected.args, approved).then(function (data) {
      showRunResult(out, (data && data.result) || {});
    }, function (error) {
      var body = (error && error.body) || {};
      if (error && error.status === 403 && body.decision === 'ask') {
        /* Same pattern as connector actions: ask here, then post again. */
        out.innerHTML = '<div class="tool-run-confirm" role="alertdialog" aria-label="Approve this run">' +
          '<strong>' + esc(name) + ' needs your approval</strong><p></p>' +
          '<div class="tool-run-actions"><button class="btn" type="button" data-run-cancel>Cancel</button>' +
          '<button class="btn primary" type="button" data-run-approve>Run it once</button></div></div>';
        out.querySelector('p').textContent = (body.reasons || []).join('; ') || 'This tool asks before it runs in the current mode.';
        out.querySelector('[data-run-approve]').focus();
        out._approve = function () { runTool(form, true); };
        return;
      }
      out.innerHTML = '<p class="tool-run-msg is-failed"></p>';
      out.firstChild.textContent = (error && error.status === 403 ? 'Refused: ' + ((body.reasons || []).join('; ') || error.message)
        : (error && error.status === 503 ? 'Tools are not available on this server.' : (error && error.message) || 'The run failed.'));
    }).then(function () { button.disabled = false; });
  }

  function bindTools(root) {
    if (tools.bound) return;
    tools.bound = true;
    root.addEventListener('submit', function (event) {
      var form = event.target.closest('[data-tool-form]');
      if (!form) return;
      event.preventDefault();
      runTool(form, false);
    });
    root.addEventListener('click', function (event) {
      if (event.target.closest('[data-goto-permissions]')) { IM.shell.openSettings('permissions'); return; }
      var out = event.target.closest('.tool-run-out');
      if (!out) return;
      if (event.target.closest('[data-run-approve]') && out._approve) { var go = out._approve; out._approve = null; go(); }
      else if (event.target.closest('[data-run-cancel]')) { out._approve = null; out.innerHTML = '<p class="tool-run-msg">Cancelled. Nothing ran.</p>'; }
    });
  }

  function loadTools(force) {
    var root = byId('tools-root');
    if (!root || tools.loading || !api()) return;
    if (tools.view && !force) return;
    tools.loading = true;
    bindTools(root);
    api().getTools().then(renderTools, function (error) {
      root.innerHTML = '<div class="panel settings-card"><div class="empty-state"><h3>Could not load the tools</h3><p>' +
        esc((error && error.message) || 'The server did not answer.') + '</p><button class="btn primary" type="button" data-tools-refresh>Try again</button></div></div>';
    }).then(function () { tools.loading = false; });
  }

  /* ================================================================ Agent */

  var agent = { view: null, bound: false };

  function fillAgent(view) {
    agent.view = view;
    var root = byId('agent-root');
    if (!root) return;
    var limit = Number(view.maxStepsLimit) || 20;
    var steps = Number(view.maxSteps) || 6;
    root.innerHTML =
      '<form class="panel settings-card" id="agent-form">' +
        '<label class="range-field" for="agent-steps"><span>Steps per reply</span><output id="agent-steps-out" for="agent-steps">' + steps + '</output></label>' +
        '<input type="range" id="agent-steps" min="1" max="' + limit + '" step="1" value="' + steps + '">' +
        '<p class="settings-note" id="agent-steps-note"></p>' +
        '<div><button class="btn primary" type="submit">Save</button></div>' +
      '</form>' +
      '<div class="panel settings-card">' +
        '<div class="settings-line"><span class="k">Sub-agent steps</span><span class="v">' + esc(view.subAgentSteps) + ' by default, at most ' + esc(view.subAgentStepsLimit) + '</span></div>' +
        '<div class="settings-line"><span class="k">Nesting</span><span class="v">' + esc(view.maxDepth) + ' levels: a sub-agent may start one more, that one may not</span></div>' +
        '<p class="settings-note">A sub-agent works on the same local model with no chat history and only the tools it was given; each of its calls is checked by the same permissions. Stop also stops it.</p>' +
      '</div>';
    stepsNote(steps);
  }

  function stepsNote(steps) {
    var note = byId('agent-steps-note');
    var out = byId('agent-steps-out');
    if (out) out.textContent = String(steps);
    if (!note) return;
    note.textContent = steps === 1
      ? 'One step turns off tool calling by the model. Clock, weather and search are still looked up before answering when the question needs them.'
      : 'Each step is one model generation; a step that calls tools is followed by another. The last step never runs tools, so the reply always ends with an answer. More steps can mean longer replies on a slow machine.';
  }

  function loadAgent() {
    var root = byId('agent-root');
    if (!root || !api()) return;
    if (!agent.bound) {
      agent.bound = true;
      root.addEventListener('input', function (event) {
        if (event.target.id === 'agent-steps') stepsNote(Number(event.target.value));
      });
      root.addEventListener('submit', function (event) {
        event.preventDefault();
        var value = Number(byId('agent-steps').value);
        api().setAgent({ maxSteps: value }).then(function (view) {
          fillAgent(view);
          toast({ tone: 'success', icon: 'check', kicker: 'Agent', title: 'Steps per reply: ' + view.maxSteps, text: 'Applies to the next reply.' });
        }, function (error) { fail('Agent', 'Not saved', error); });
      });
    }
    api().getAgent().then(fillAgent, function (error) {
      root.innerHTML = '<div class="panel settings-card"><div class="empty-state"><h3>Could not load the agent settings</h3><p>' +
        esc((error && error.message) || 'The server did not answer.') + '</p></div></div>';
    });
  }

  /* =============================================================== Search */

  /* The server's `search` section stores the values under the environment
   * variable names (services/server/settings.py SEARCH_KEYS) and returns a
   * set value masked; the UI used to write `braveApiKey` & co., which the
   * server never read (BUGS-FIXED.md 65). The SearXNG URL is masked too, so
   * it is write-only like the keys. */
  var SEARCH_KEYS = [
    ['IMURUN_BRAVE_API_KEY', 'Brave Search API key', 'IMURUN_BRAVE_API_KEY', 'brave'],
    ['IMURUN_TAVILY_API_KEY', 'Tavily API key', 'IMURUN_TAVILY_API_KEY', 'tavily'],
    ['IMURUN_SERPER_API_KEY', 'Serper API key', 'IMURUN_SERPER_API_KEY', 'serper'],
    ['IMURUN_SEARXNG_URL', 'SearXNG URL (your own instance, no key needed)', 'IMURUN_SEARXNG_URL', 'searxng', 'url']
  ];
  var search = { values: {}, bound: false };

  function isSet(value) { return value !== undefined && value !== null && value !== '' && value !== false; }

  function fillSearch(values) {
    search.values = values || {};
    var root = byId('search-root');
    if (!root) return;
    SEARCH_KEYS.forEach(function (key) {
      var state = root.querySelector('[data-search-state="' + key[0] + '"]');
      var input = root.querySelector('[data-search-key="' + key[0] + '"]');
      var on = isSet(search.values[key[0]]);
      if (state) { state.className = 'chip ' + (on ? 'success' : 'neutral'); state.textContent = on ? 'saved' : 'not set'; }
      if (input) { input.value = ''; input.placeholder = on ? 'Saved (hidden). Type to replace.' : 'Not set'; }
      var clear = root.querySelector('[data-search-clear="' + key[0] + '"]');
      if (clear) clear.hidden = !on;
    });
    var provider = byId('search-provider');
    if (provider) provider.value = search.values.provider || '';
  }

  function buildSearch(root) {
    root.innerHTML =
      '<div class="panel settings-card">' +
        '<p class="search-warning"><strong>Why set a key:</strong> without one, search uses DuckDuckGo\'s keyless page, which some networks answer with a bot check. Search then fails (honestly) instead of finding anything.</p>' +
        '<form id="search-form" class="search-form" autocomplete="off">' +
          '<label class="form-field"><span class="k">Provider</span><select id="search-provider">' +
            '<option value="">Automatic (the first key that is set, else DuckDuckGo)</option>' +
            '<option value="brave">Brave</option><option value="tavily">Tavily</option><option value="serper">Serper (Google results)</option>' +
            '<option value="searxng">SearXNG (your own instance)</option><option value="duckduckgo">DuckDuckGo (no key)</option></select></label>' +
          SEARCH_KEYS.map(function (key) {
            return '<div class="search-key"><label class="form-field"><span class="k">' + esc(key[1]) + ' <span class="chip neutral" data-search-state="' + key[0] + '">not set</span></span>' +
              '<input type="' + (key[4] === 'url' ? 'url' : 'password') + '" data-search-key="' + key[0] + '" autocomplete="' + (key[4] === 'url' ? 'off' : 'new-password') + '" spellcheck="false"></label>' +
              '<button class="btn ghost" type="button" data-search-clear="' + key[0] + '" hidden>Remove</button>' +
              '<small class="tool-field-hint">Same as the environment variable <code>' + esc(key[2]) + '</code>.</small></div>';
          }).join('') +
          '<div><button class="btn primary" type="submit">Save search settings</button></div>' +
        '</form>' +
      '</div>' +
      '<p class="settings-note">Keys are sent to the local server only, are never shown again after saving, and are not kept in this browser. ' +
        'They apply to the next search without a restart. A variable already set in the environment that started IMURUN is not cleared by removing a key here.</p>';
  }

  function loadSearch() {
    var root = byId('search-root');
    if (!root || !api()) return;
    if (!search.bound) {
      search.bound = true;
      buildSearch(root);
      root.addEventListener('submit', function (event) {
        event.preventDefault();
        var changes = { provider: byId('search-provider').value };
        SEARCH_KEYS.forEach(function (key) {
          var input = root.querySelector('[data-search-key="' + key[0] + '"]');
          if (input && input.value.trim()) changes[key[0]] = input.value.trim();
        });
        saveSearch(changes, 'Search settings saved');
      });
      root.addEventListener('click', function (event) {
        var clear = event.target.closest('[data-search-clear]');
        if (!clear) return;
        var change = {};
        change[clear.dataset.searchClear] = '';
        saveSearch(change, 'Key removed');
      });
    }
    api().getSettings('search').then(function (payload) {
      fillSearch((payload && payload.values) || {});
    }, function (error) { fail('Search', 'Could not load search settings', error); });
  }

  function saveSearch(changes, title) {
    api().updateSettings('search', changes).then(function (payload) {
      fillSearch((payload && payload.values) || Object.assign({}, search.values, changes));
      toast({ tone: 'success', icon: 'check', kicker: 'Search', title: title, text: 'Stored on the local server.' });
    }, function (error) { fail('Search', 'Not saved', error); });
  }

  /* =============================================================== wiring */

  var LOADERS = { permissions: loadPermissions, tools: function () { loadTools(false); }, agent: loadAgent, search: loadSearch };

  function init() {
    IM.bus.on('settings.section', function (section) {
      if (LOADERS[section]) LOADERS[section]();
    });
    IM.bus.on('server:permissions', function () {
      if (perm.built) loadPermissions();
      if (tools.view) loadTools(true);
    });
    document.addEventListener('click', function (event) {
      if (!event.target.closest) return;
      if (event.target.closest('[data-perm-retry]')) loadPermissions();
      if (event.target.closest('[data-tools-refresh]')) { tools.view = null; loadTools(true); }
    });
  }

  IM.agentSettings = { loadPermissions: loadPermissions, loadTools: loadTools, loadAgent: loadAgent, loadSearch: loadSearch };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
