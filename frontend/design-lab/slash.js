/* `/skill` autocomplete in the composer.
 *
 *   IMURUN.slash.isOpen()      the suggestion list is showing
 *   IMURUN.slash.refresh()     reload GET /api/skills
 *
 * Typing "/" at the start of the message or after a space lists the stored
 * skills (GET /api/skills) and the built-in commands (/help lists them). Arrow keys move, Enter
 * or Tab inserts "/<id> ", Esc closes. The server injects a skill when the
 * prompt contains `/<id>` (docs/agentic/README.md section 5), so inserting
 * the token is all the UI has to do. Skills the message already names are
 * shown as chips under the composer, so you can see what will be applied.
 *
 * Keys are caught in the capture phase on `.input-wrap`, which runs before
 * chat.js's Enter-to-send handler on the textarea.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var STALE_MS = 30000;
  var LIMIT = 14;
  var COMMANDS = [];
  var RUN = {};
  function cmd(id, name, description, run) {
    COMMANDS.push({ id: id, name: name, description: description, command: true });
    RUN[id] = run;
  }
  function shell() { return IM.shell || {}; }
  function esc(v) { return util.escapeHtml(String(v == null ? '' : v)); }

  /* Command output: an inline note in the open chat, else a toast. */
  function note(title, lines, actions) {
    var box = document.getElementById('transcript');
    var onChat = box && shell().currentPage && shell().currentPage() === 'chat';
    if (!onChat) {
      if (IM.toast) IM.toast({ tone: 'neutral', icon: 'info', kicker: 'Command', title: title, text: lines.join(' · '), actions: actions, timeout: 9000 });
      return;
    }
    var empty = box.querySelector('.chat-empty');
    if (empty) empty.remove();
    var el = document.createElement('div');
    el.className = 'cmd-note';
    el.setAttribute('role', 'status');
    el.innerHTML = '<strong>' + esc(title) + '</strong>' + lines.map(function (l) { return '<div>' + esc(l) + '</div>'; }).join('');
    if (actions && actions.length) {
      var row = document.createElement('div');
      row.className = 'cmd-note-actions';
      actions.forEach(function (a) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn' + (a.primary ? ' primary' : '');
        b.textContent = a.label;
        b.addEventListener('click', a.onClick);
        row.appendChild(b);
      });
      el.appendChild(row);
    }
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  function scalars(obj, limit) {
    var out = [];
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k];
      if (out.length < limit && (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')) out.push(k + ': ' + v);
    });
    return out;
  }

  cmd('rename', 'Rename this session', 'Opens the rename dialog.', function () { shell().handleCommand('/rename'); });
  cmd('new', 'New session', 'Start a fresh conversation.', function () { if (shell().createSession) shell().createSession(); });
  cmd('clear', 'Clear the view', 'Empty the visible transcript (server history is kept).', function () { if (IM.chat) IM.chat.clear(); });
  cmd('model', 'Model', 'Show the loaded model and speed, and open the Models tab.', function () {
    api().getStatus().then(function (st) {
      var eng = (st && st.engine) || {};
      var tps = Number(eng.tokens_per_second);
      note('Model', [
        'Loaded: ' + ((st && st.selected_model) || 'none'),
        'Speed: ' + (tps ? tps.toFixed(1) + ' tok/s (last reply)' : 'not reported yet')
      ]);
    }, function (e) { note('Model', ['Status unavailable: ' + e.message]); });
    if (shell().openSettings) shell().openSettings('models');
  });
  cmd('hardware', 'Hardware', 'Open the Resource manager.', function () { if (shell().openSettings) shell().openSettings('resources'); });
  cmd('settings', 'Settings', 'Open Settings.', function () { if (shell().openSettings) shell().openSettings(); });
  cmd('voice', 'Voice dictation', 'Start or stop dictation.', function () {
    var mic = document.getElementById('composer-mic');
    if (mic && !mic.disabled) mic.click();
    else note('Voice', ['Dictation is not available here.']);
  });
  cmd('export', 'Export chat', 'Download this chat as Markdown.', function () {
    var id = IM.state && IM.state.activeSessionId;
    if (!id) { note('Export', ['Open a session first.']); return; }
    api().getMessages(id, 2000).then(function (messages) {
      var sess = ((IM.state && IM.state.sessions) || []).filter(function (x) { return x.id === id; })[0];
      var md = '# ' + ((sess && sess.name) || id) + '\n\n' +
        (messages || []).filter(function (m) { return m && (m.role === 'user' || m.role === 'assistant'); }).map(function (m) {
          return '**' + (m.role === 'user' ? 'You' : 'Imurun') + '**\n\n' + (m.content || '');
        }).join('\n\n---\n\n') + '\n';
      var a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
      a.download = 'imurun-' + id + '.md';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    }, function (e) { note('Export', ['Could not read the chat: ' + e.message]); });
  });
  cmd('doctor', 'Doctor', 'Show server status and resources.', function () {
    Promise.all([
      api().getStatus().catch(function (e) { return { error: e.message }; }),
      api().getResources().catch(function (e) { return { error: e.message }; })
    ]).then(function (r) {
      var st = r[0] || {};
      var res = r[1] || {};
      var lines = ['Server: ' + (st.error ? 'unreachable (' + st.error + ')' : 'ok'), 'Model: ' + (st.selected_model || 'none')];
      lines = lines.concat(scalars(st.engine, 5));
      lines.push('Resources: ' + (res.error ? res.error : (scalars(res, 6).join(', ') || 'no summary')));
      note('Doctor', lines);
    });
  });
  cmd('update', 'Check for updates', 'Check now and offer to install.', function () {
    if (!IM.update) { note('Update', ['Updates are not available.']); return; }
    IM.update.check().then(function (i) {
      note('Update', [IM.update.summary(i)], i && i.available ? [
        { label: 'Update now', primary: true, onClick: function () { IM.update.apply(); } },
        { label: 'Not now', onClick: function () { IM.update.later(); } }
      ] : null);
    }, function (e) { note('Update', ['Check failed: ' + e.message]); });
  });
  cmd('help', 'Help', 'List all commands.', function () {
    var lines = COMMANDS.map(function (c) { return '/' + c.id + ' - ' + c.description; });
    lines.push('Skills: type / and pick one; it is applied to the message.');
    note('Commands', lines);
  });

  /* Run "/name" typed in full. Returns true when it was a command. */
  function run(text) {
    var m = /^\/([A-Za-z0-9_-]+)\s*$/.exec(String(text || '').trim());
    if (!m) return false;
    var id = m[1] === 'rename-session' ? 'rename' : m[1].toLowerCase();
    if (!RUN[id]) return false;
    RUN[id]();
    return true;
  }

  var skills = [];
  var loadedAt = 0;
  var loading = null;
  var prompt = null;
  var wrap = null;
  var menu = null;
  var chips = null;
  var items = [];
  var active = 0;
  var token = null; /* {start, end, query} of the "/..." being typed */

  function api() { return window.__imurun_api; }

  function refresh() {
    if (!api() || !api().listSkills) return Promise.resolve(skills);
    if (loading) return loading;
    loading = api().listSkills().then(function (list) {
      skills = (list || []).filter(function (s) { return s && s.id; });
      loadedAt = Date.now();
      return skills;
    }, function () { return skills; }).then(function (value) {
      loading = null;
      renderChips();
      return value;
    });
    return loading;
  }

  function isOpen() { return Boolean(menu && !menu.hidden); }

  function currentToken() {
    var caret = prompt.selectionStart;
    if (caret !== prompt.selectionEnd) return null;
    var before = prompt.value.slice(0, caret);
    var match = /(^|\s)\/([A-Za-z0-9_-]*)$/.exec(before);
    if (!match) return null;
    return { start: caret - match[2].length - 1, end: caret, query: match[2].toLowerCase() };
  }

  function score(item, query) {
    if (!query) return 1;
    var id = String(item.id).toLowerCase();
    var name = String(item.name || '').toLowerCase();
    if (id.indexOf(query) === 0) return 3;
    if (name.indexOf(query) === 0) return 2;
    return id.indexOf(query) !== -1 || name.indexOf(query) !== -1 ? 1 : 0;
  }

  function close() {
    if (!menu) return;
    menu.hidden = true;
    token = null;
    prompt.setAttribute('aria-expanded', 'false');
    prompt.removeAttribute('aria-activedescendant');
  }

  function render() {
    if (!items.length) {
      menu.innerHTML = '<li class="slash-empty" role="presentation">' +
        (skills.length ? 'No skill matches.' : 'No skills yet. Create one on the Skills page.') + '</li>';
      prompt.removeAttribute('aria-activedescendant');
      return;
    }
    menu.innerHTML = items.map(function (item, index) {
      return '<li class="slash-item' + (index === active ? ' is-active' : '') + '" role="option" id="slash-opt-' + index + '"' +
        ' aria-selected="' + (index === active ? 'true' : 'false') + '" data-slash-index="' + index + '">' +
        '<code>/' + util.escapeHtml(item.id) + '</code>' +
        '<span class="slash-name">' + util.escapeHtml(item.name || item.id) + '</span>' +
        '<span class="slash-desc">' + util.escapeHtml(item.description || (item.command ? '' : 'Skill')) + '</span>' +
        (item.command ? '<span class="chip neutral">command</span>' : '') +
      '</li>';
    }).join('');
    prompt.setAttribute('aria-activedescendant', 'slash-opt-' + active);
    var current = menu.querySelector('.is-active');
    if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest' });
  }

  function open() {
    token = currentToken();
    if (!token) {
      close();
      return;
    }
    var pool = skills.concat(COMMANDS);
    items = pool.map(function (item) { return { item: item, s: score(item, token.query) }; })
      .filter(function (pair) { return pair.s > 0; })
      .sort(function (a, b) { return b.s - a.s; })
      .slice(0, LIMIT)
      .map(function (pair) { return pair.item; });
    active = Math.min(active, Math.max(0, items.length - 1));
    menu.hidden = false;
    prompt.setAttribute('aria-expanded', 'true');
    render();
    if (Date.now() - loadedAt > STALE_MS) refresh().then(function () { if (isOpen()) open(); });
  }

  function choose(index, fromEnter) {
    var item = items[index];
    if (!item || !token) return;
    if (item.command && fromEnter && token.start === 0 && !prompt.value.slice(token.end).trim()) {
      prompt.value = '';
      close();
      prompt.dispatchEvent(new Event('input'));
      run('/' + item.id);
      return;
    }
    var value = prompt.value;
    var insert = '/' + item.id + ' ';
    prompt.value = value.slice(0, token.start) + insert + value.slice(token.end).replace(/^ /, '');
    var caret = token.start + insert.length;
    prompt.setSelectionRange(caret, caret);
    close();
    prompt.dispatchEvent(new Event('input'));
    prompt.focus();
  }

  /* Chips for the skills this message names. */
  function renderChips() {
    if (!chips || !prompt) return;
    var text = prompt.value;
    var named = skills.filter(function (skill) {
      var re = new RegExp('(^|\\s)/' + String(skill.id).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?=\\s|$)');
      return re.test(text);
    });
    chips.hidden = !named.length;
    chips.innerHTML = named.length
      ? '<span class="composer-skills-k">Skill</span>' + named.map(function (skill) {
          return '<span class="skill-chip" title="' + util.escapeHtml(skill.description || skill.name || '') + '">/' + util.escapeHtml(skill.id) + '</span>';
        }).join('')
      : '';
  }

  function onKey(event) {
    if (!isOpen() || event.target !== prompt) return;
    var key = event.key;
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (!items.length) return;
      event.preventDefault();
      event.stopPropagation();
      active = (active + (key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      render();
    } else if ((key === 'Enter' && !event.shiftKey && !event.isComposing) || key === 'Tab') {
      if (!items.length) {
        close();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      choose(active, key === 'Enter');
    } else if (key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  }

  function init() {
    prompt = document.querySelector('.input-wrap textarea');
    wrap = document.querySelector('.input-wrap');
    if (!prompt || !wrap) return;
    menu = document.createElement('ul');
    menu.className = 'slash-menu';
    menu.id = 'slash-menu';
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', 'Skills and commands');
    menu.hidden = true;
    wrap.appendChild(menu);
    prompt.setAttribute('aria-autocomplete', 'list');
    prompt.setAttribute('aria-controls', 'slash-menu');
    prompt.setAttribute('aria-expanded', 'false');

    var foot = document.querySelector('.composer-foot');
    chips = document.createElement('div');
    chips.className = 'composer-skills';
    chips.hidden = true;
    if (foot) foot.insertBefore(chips, document.getElementById('composer-usage'));

    wrap.addEventListener('keydown', onKey, true);
    prompt.addEventListener('input', function () {
      renderChips();
      if (currentToken()) {
        if (!isOpen()) active = 0;
        open();
      } else close();
    });
    prompt.addEventListener('click', function () { if (currentToken()) open(); else close(); });
    prompt.addEventListener('blur', function () { setTimeout(close, 120); });
    prompt.addEventListener('focus', function () { if (Date.now() - loadedAt > STALE_MS) refresh(); });
    /* mousedown, not click: the textarea must keep focus. */
    menu.addEventListener('mousedown', function (event) {
      var row = event.target.closest ? event.target.closest('[data-slash-index]') : null;
      if (!row) return;
      event.preventDefault();
      choose(Number(row.dataset.slashIndex));
    });
    refresh();
  }

  IM.slash = { isOpen: isOpen, refresh: refresh };
  IM.commands = { run: run, list: function () { return COMMANDS.slice(); } };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
