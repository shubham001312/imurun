/* `/skill` autocomplete in the composer.
 *
 *   IMURUN.slash.isOpen()      the suggestion list is showing
 *   IMURUN.slash.refresh()     reload GET /api/skills
 *
 * Typing "/" at the start of the message or after a space lists the stored
 * skills (GET /api/skills) and the `/rename`, `/model` and `/connect` commands (sent,
 * they are run by IMURUN.shell.handleCommand, not the server). Arrow keys move, Enter
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
  var LIMIT = 8;
  var COMMANDS = [
    { id: 'rename', name: 'Rename this session', description: 'Opens the rename dialog.', command: true },
    { id: 'model', name: 'Choose a model', description: 'Opens the model picker. /model <name> switches straight to that model.', command: true },
    { id: 'permission', name: 'Access level', description: 'Manual, Ask for changes or Auto: how much IMURUN may do without asking.', command: true },
    { id: 'connect', name: 'Connect a hosted provider', description: 'Add an API key for NVIDIA, OpenRouter, DeepSeek and more. /connect nvidia opens that one.', command: true }
  ];

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

  function choose(index) {
    var item = items[index];
    if (!item || !token) return;
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
      choose(active);
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

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
