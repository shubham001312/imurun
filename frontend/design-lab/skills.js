/* Skills page and the Add skill dialog.
 *
 *   IMURUN.skills.render(list)   draw the cards (app.js calls it at boot)
 *   IMURUN.skills.refresh()      reload GET /api/skills
 *   IMURUN.skills.open(tab)      open the dialog on 'write' or 'github'
 *
 * Two ways to add a skill:
 *   Write one     name, description, instructions -> POST /api/skills.
 *                 The /<id> preview and the inline checks mirror the server
 *                 (services/skills/store.py: name <= 80, description <= 200,
 *                 body <= 100 000, id = slugify(name, 8 words, 60 chars)).
 *   From GitHub   paste a link -> POST /api/skills/import/preview (no side
 *                 effects) -> tick skills -> POST /api/skills/import with the
 *                 previewed commit, so what installs is what was shown.
 *
 * Cards show the name, description, /<id> and where the skill came from, with
 * Use in chat, Copy command, Update (GitHub skills) and Delete (confirmed).
 * ES2017, no inline styles or scripts (CSP default-src 'self').
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var escapeHtml = util.escapeHtml;
  var api = window.__imurun_api;

  var MAX_NAME = 80;
  var MAX_DESCRIPTION = 200;
  var MAX_BODY = 100000;
  var SEARCH_FROM = 7;  /* the search box appears with more than six skills */

  var ICONS = {
    skill: '<path d="M12 3.5l2 5.5 5.5 2-5.5 2-2 5.5-2-5.5-5.5-2 5.5-2z"/>',
    pencil: '<path d="M4.5 19.5h4l10-10-4-4-10 10z"/><path d="M13 7l4 4"/>',
    branch: '<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="8" r="2"/><path d="M6.5 7.5v9M17.5 10c0 4-5 3.5-9.5 7"/>',
    chat: '<path d="M5 5.5h14v10H10l-5 4z"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 5.5h-9a1 1 0 0 0-1 1v9"/>',
    trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12.5h8L17 7"/>',
    refresh: '<path d="M19 12a7 7 0 1 1-2.1-5M19 4.5v4h-4"/>',
    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    link: '<path d="M14 5h5v5M19 5l-8 8M17 14v5H5V7h5"/>'
  };

  function icon(name, cls) {
    return '<svg class="' + (cls || 'nav-ico') + '" viewBox="0 0 24 24" aria-hidden="true">' + ICONS[name] + '</svg>';
  }
  function byId(id) { return document.getElementById(id); }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  /* services/sessions/session.py slugify(text, max_words=8, max_len=60). */
  function slugify(text) {
    var folded = String(text || '');
    if (folded.normalize) folded = folded.normalize('NFKD');
    folded = folded.replace(/[^\x00-\x7f]/g, '').toLowerCase();
    var words = (folded.match(/[a-z0-9]+/g) || []).slice(0, 8);
    return words.join('-').slice(0, 60).replace(/^-+|-+$/g, '');
  }

  function singleLine(value) { return String(value || '').replace(/[\r\n]+/g, ' ').trim(); }

  var skills = [];

  /* ------------------------------------------------------------ the page */

  function isGitHub(skill) { return skill.origin === 'github' && skill.source && skill.source.repo; }

  function sourceHtml(skill) {
    if (isGitHub(skill)) {
      var url = String(skill.source.url || '');
      var label = escapeHtml(skill.source.repo) + (skill.source.path ? '<span class="muted"> / ' + escapeHtml(skill.source.path) + '</span>' : '');
      var link = url.indexOf('https://github.com/') === 0
        ? '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>'
        : label;
      return icon('branch', 'nav-ico skill-src-ico') + '<span>GitHub ' + link + '</span>';
    }
    if (skill.origin === 'github') return icon('branch', 'nav-ico skill-src-ico') + '<span>GitHub</span>';
    if (skill.origin === 'agent') return icon('skill', 'nav-ico skill-src-ico') + '<span>Written by Imurun</span>';
    return icon('pencil', 'nav-ico skill-src-ico') + '<span>Written by you</span>';
  }

  function cardHtml(skill) {
    var id = escapeHtml(skill.id || '');
    return '<article class="panel skill skill-card" data-skill="' + id + '">' +
      '<div class="skill-head">' +
        '<span class="skill-mark" aria-hidden="true">' + icon('skill', 'nav-ico') + '</span>' +
        '<div class="skill-name"><strong>' + escapeHtml(skill.name || skill.id || 'Untitled skill') + '</strong>' +
        '<code class="skill-invoke">/' + id + '</code></div>' +
      '</div>' +
      '<p class="skill-desc">' + escapeHtml(skill.description || 'No description') + '</p>' +
      '<div class="skill-source">' + sourceHtml(skill) + '</div>' +
      '<div class="skill-actions">' +
        '<button class="btn skill-act" type="button" data-skill-use="' + id + '">' + icon('chat') + '<span>Use in chat</span></button>' +
        '<button class="btn skill-act" type="button" data-skill-copy="' + id + '">' + icon('copy') + '<span>Copy command</span></button>' +
        (isGitHub(skill) ? '<button class="btn skill-act" type="button" data-skill-update="' + id + '">' + icon('refresh') + '<span>Update</span></button>' : '') +
        '<button class="btn skill-act skill-act-danger" type="button" data-skill-delete="' + id + '" aria-label="Delete ' + escapeHtml(skill.name || skill.id) + '">' + icon('trash') + '<span>Delete</span></button>' +
      '</div>' +
    '</article>';
  }

  function emptyHtml() {
    return '<div class="empty-state skill-empty">' +
      '<span class="empty-icon" aria-hidden="true">' + icon('skill', 'empty-svg') + '</span>' +
      '<h3>No skills yet</h3>' +
      '<p>A skill is a set of instructions Imurun follows when a message names it with <code>/name</code>. Write your own, or install ready-made ones from GitHub.</p>' +
      '<div class="skill-empty-actions">' +
        '<button class="btn primary" type="button" data-skill-add="write">' + icon('pencil') + '<span>Write a skill</span></button>' +
        '<button class="btn" type="button" data-skill-add="github">' + icon('branch') + '<span>Add from GitHub</span></button>' +
      '</div></div>';
  }

  function matches(skill, query) {
    if (!query) return true;
    var hay = [skill.id, skill.name, skill.description, skill.source && skill.source.repo].join(' ').toLowerCase();
    return query.split(/\s+/).every(function (word) { return hay.indexOf(word) !== -1; });
  }

  function draw() {
    var grid = byId('skill-grid');
    if (!grid) return;
    grid.setAttribute('aria-busy', 'false');
    var toolbar = byId('skill-toolbar');
    var search = byId('skill-search');
    var showSearch = skills.length >= SEARCH_FROM;
    if (toolbar) toolbar.hidden = !showSearch;
    if (!showSearch && search) search.value = '';
    if (!skills.length) {
      grid.classList.add('is-empty');
      grid.innerHTML = emptyHtml();
      return;
    }
    grid.classList.remove('is-empty');
    var query = search ? search.value.trim().toLowerCase() : '';
    var shown = skills.filter(function (skill) { return matches(skill, query); });
    var count = byId('skill-count');
    if (count) count.textContent = query ? shown.length + ' of ' + plural(skills.length, 'skill', 'skills') : plural(skills.length, 'skill', 'skills');
    grid.innerHTML = shown.length
      ? shown.map(cardHtml).join('')
      : '<p class="skill-nomatch muted">No skill matches \u201c' + escapeHtml(query) + '\u201d.</p>';
  }

  function render(list) {
    skills = (Array.isArray(list) ? list : []).filter(function (skill) { return skill && skill.id; });
    draw();
  }

  function refresh() {
    return api.listSkills().then(function (list) {
      render(list);
      if (IM.slash && IM.slash.refresh) IM.slash.refresh();
    }, function (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Skills', title: 'Could not load skills', text: error.message });
    });
  }

  function findSkill(id) { return skills.filter(function (skill) { return skill.id === id; })[0]; }

  function useInChat(id) {
    if (IM.shell && IM.shell.setPage) IM.shell.setPage('chat', { force: true });
    var input = byId('composer-input') || document.querySelector('.composer textarea');
    if (!input) return;
    var token = '/' + id;
    var value = input.value || '';
    var present = new RegExp('(^|\\s)' + token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?=\\s|$)').test(value);
    if (!present) input.value = token + ' ' + value.replace(/^\s+/, '');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    var end = input.value.length;
    try { input.setSelectionRange(end, end); } catch (error) { /* not a text field */ }
  }

  function copyCommand(id, button) {
    var text = '/' + id;
    function done(ok) {
      var label = button && button.querySelector('span');
      if (label) {
        label.textContent = ok ? 'Copied' : 'Copy failed';
        clearTimeout(button._skillTimer);
        button._skillTimer = setTimeout(function () { label.textContent = 'Copy command'; }, 1400);
      }
      util.announce(ok ? 'Copied ' + text : 'Copy failed');
    }
    function fallback() {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'sr-only';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      area.remove();
      done(ok);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
    } else {
      fallback();
    }
  }

  function updateSkill(id, button) {
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    api.updateSkill(id).then(function (result) {
      var current = result && result.status === 'current';
      toast({ tone: current ? 'neutral' : 'success', icon: 'check', kicker: 'Skills',
        title: current ? '/' + id + ' is up to date' : '/' + id + ' updated',
        text: 'Commit ' + String((result && result.commit) || '').slice(0, 7) + '.' });
      if (!current) refresh();
    }, function (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Skills', title: 'Could not update /' + id, text: error.message });
    }).then(function () {
      if (button && document.contains(button)) { button.disabled = false; button.removeAttribute('aria-busy'); }
    });
  }

  /* ------------------------------------------------------ shared modal */

  /* One open surface at a time: {el, release, onClose}. */
  var openModal = null;

  function showModal(el, onClose, focusTarget) {
    if (openModal) closeModal();
    el.hidden = false;
    openModal = { el: el, release: util.trapFocus(el), onClose: onClose };
    if (IM.sound && IM.sound.open) IM.sound.open();
    if (focusTarget) setTimeout(function () { focusTarget.focus(); }, 0);
  }

  function closeModal() {
    if (!openModal) return;
    var current = openModal;
    openModal = null;
    current.el.hidden = true;
    if (current.onClose) current.onClose();
    current.release();
    var fallback = byId('new-skill');
    if (!document.activeElement || document.activeElement === document.body) {
      if (fallback) fallback.focus();
    }
  }

  function bindModal(el) {
    el.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();  /* app.js's global Escape skips handled events */
        event.stopPropagation();
        closeModal();
      }
    });
    el.addEventListener('mousedown', function (event) {
      if (event.target === el) closeModal();
    });
  }

  /* ------------------------------------------------------ delete confirm */

  var confirmEl = null;
  var confirmId = '';

  function buildConfirm() {
    confirmEl = document.createElement('div');
    confirmEl.className = 'modal';
    confirmEl.id = 'skill-confirm';
    confirmEl.hidden = true;
    confirmEl.innerHTML =
      '<div class="dialog rename-dialog" role="alertdialog" aria-modal="true" aria-labelledby="skill-confirm-title" aria-describedby="skill-confirm-sub">' +
        '<h2 id="skill-confirm-title">Delete skill</h2>' +
        '<p class="sub" id="skill-confirm-sub"></p>' +
        '<div class="dialog-actions">' +
          '<button class="cancel" type="button" data-confirm-cancel>Cancel</button>' +
          '<button class="danger" type="button" data-confirm-ok>Delete</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(confirmEl);
    bindModal(confirmEl);
    confirmEl.querySelector('[data-confirm-cancel]').addEventListener('click', closeModal);
    confirmEl.querySelector('[data-confirm-ok]').addEventListener('click', function () {
      var id = confirmId;
      var button = this;
      button.disabled = true;
      api.deleteSkill(id).then(function () {
        closeModal();
        toast({ tone: 'neutral', icon: 'close', kicker: 'Skills', title: 'Skill deleted', text: '/' + id });
        refresh().then(function () {
          /* The card that had focus is gone after the re-render. */
          var active = document.activeElement;
          if (!active || active === document.body) {
            var fallback = byId('new-skill');
            if (fallback) fallback.focus();
          }
        });
      }, function (error) {
        toast({ tone: 'error', icon: 'alert', kicker: 'Skills', title: 'Could not delete /' + id, text: error.message });
      }).then(function () { button.disabled = false; });
    });
  }

  function confirmDelete(id) {
    if (!confirmEl) buildConfirm();
    var skill = findSkill(id) || { id: id, name: id };
    confirmId = id;
    confirmEl.querySelector('#skill-confirm-sub').textContent =
      '\u201c' + (skill.name || id) + '\u201d (/' + id + ') and its files are removed from this computer.' +
      (isGitHub(skill) ? ' You can install it again from ' + skill.source.repo + '.' : ' This cannot be undone.');
    showModal(confirmEl, null, confirmEl.querySelector('[data-confirm-cancel]'));
  }

  /* ------------------------------------------------------ Add skill dialog */

  var dialog = null;
  var els = {};
  var preview = null;   /* last import preview */
  var previewUrl = '';
  var installed = false;

  function buildDialog() {
    dialog = document.createElement('div');
    dialog.className = 'modal';
    dialog.id = 'skill-dialog';
    dialog.hidden = true;
    dialog.innerHTML =
      '<div class="dialog skill-dialog" role="dialog" aria-modal="true" aria-labelledby="skill-dialog-title">' +
        '<div class="skill-dialog-head">' +
          '<div><h2 id="skill-dialog-title">Add a skill</h2>' +
          '<p class="sub">Skills are text instructions. Nothing in them is run.</p></div>' +
          '<button class="top-btn top-icon skill-dialog-x" type="button" data-skill-close aria-label="Close">' + icon('close') + '</button>' +
        '</div>' +
        '<div class="skill-tabs" role="tablist" aria-label="How to add a skill">' +
          '<button class="skill-tab" type="button" role="tab" id="skill-tab-write" aria-controls="skill-panel-write" data-skill-tab="write">' + icon('pencil') + '<span>Write one</span></button>' +
          '<button class="skill-tab" type="button" role="tab" id="skill-tab-github" aria-controls="skill-panel-github" data-skill-tab="github">' + icon('branch') + '<span>From GitHub</span></button>' +
        '</div>' +

        '<form class="skill-panel" role="tabpanel" id="skill-panel-write" aria-labelledby="skill-tab-write" novalidate>' +
          '<label class="skill-field"><span class="k">Name</span>' +
            '<input id="skill-name" maxlength="' + MAX_NAME + '" autocomplete="off" spellcheck="false" placeholder="Release notes" aria-describedby="skill-id-preview skill-name-error">' +
          '</label>' +
          '<p class="skill-id-preview" id="skill-id-preview" aria-live="polite"></p>' +
          '<p class="skill-field-error" id="skill-name-error" hidden></p>' +
          '<label class="skill-field"><span class="k">Description <span class="skill-counter" id="skill-desc-count"></span></span>' +
            '<input id="skill-desc" maxlength="' + MAX_DESCRIPTION + '" autocomplete="off" placeholder="One line on when to use it" aria-describedby="skill-desc-error">' +
          '</label>' +
          '<p class="skill-field-error" id="skill-desc-error" hidden></p>' +
          '<label class="skill-field"><span class="k">Instructions <span class="skill-counter" id="skill-body-count"></span></span>' +
            '<textarea id="skill-body" rows="9" spellcheck="true" placeholder="What Imurun should do, step by step, whenever this skill is used." aria-describedby="skill-body-error"></textarea>' +
          '</label>' +
          '<p class="skill-field-error" id="skill-body-error" hidden></p>' +
          '<p class="skill-error" id="skill-write-error" role="alert" hidden></p>' +
          '<div class="dialog-actions">' +
            '<button class="cancel" type="button" data-skill-close>Cancel</button>' +
            '<button class="send" type="submit" id="skill-save">Save skill</button>' +
          '</div>' +
        '</form>' +

        '<div class="skill-panel" role="tabpanel" id="skill-panel-github" aria-labelledby="skill-tab-github" hidden>' +
          '<form class="skill-gh-find" id="skill-gh-form" novalidate>' +
            '<label class="skill-field skill-gh-url"><span class="k">GitHub link</span>' +
              '<input id="skill-gh-url" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://github.com/owner/repo" aria-describedby="skill-gh-help">' +
            '</label>' +
            '<button class="btn primary" type="submit" id="skill-gh-find">Find skills</button>' +
          '</form>' +
          '<p class="skill-gh-help muted" id="skill-gh-help">A repository, a folder (<code>/tree/&hellip;</code>) or one <code>SKILL.md</code> (<code>/blob/&hellip;</code>). Only <code>SKILL.md</code> and <code>.md</code> / <code>.txt</code> files next to it are copied; scripts are never installed.</p>' +
          '<p class="skill-error" id="skill-gh-error" role="alert" hidden></p>' +
          '<div class="skill-gh-results" id="skill-gh-results" hidden>' +
            '<div class="skill-gh-summary">' +
              '<label class="skill-gh-all"><input type="checkbox" id="skill-gh-all"><span>Select all</span></label>' +
              '<span class="muted" id="skill-gh-source"></span>' +
            '</div>' +
            '<ul class="skill-gh-notes" id="skill-gh-notes" hidden></ul>' +
            '<ul class="skill-gh-list" id="skill-gh-list" aria-label="Skills found"></ul>' +
          '</div>' +
          '<div class="dialog-actions">' +
            '<button class="cancel" type="button" data-skill-close>Cancel</button>' +
            '<button class="send" type="button" id="skill-gh-install" disabled>Install</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(dialog);
    bindModal(dialog);

    ['skill-name', 'skill-desc', 'skill-body', 'skill-save', 'skill-id-preview', 'skill-name-error',
      'skill-desc-error', 'skill-body-error', 'skill-desc-count', 'skill-body-count', 'skill-write-error',
      'skill-gh-url', 'skill-gh-find', 'skill-gh-error', 'skill-gh-results', 'skill-gh-all',
      'skill-gh-source', 'skill-gh-notes', 'skill-gh-list', 'skill-gh-install'].forEach(function (id) {
      els[id.replace('skill-', '')] = byId(id);
    });

    Array.prototype.forEach.call(dialog.querySelectorAll('[data-skill-close]'), function (button) {
      button.addEventListener('click', closeModal);
    });
    var tabs = dialog.querySelectorAll('[data-skill-tab]');
    Array.prototype.forEach.call(tabs, function (tab) {
      tab.addEventListener('click', function () { selectTab(tab.dataset.skillTab, true); });
      tab.addEventListener('keydown', function (event) {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return;
        event.preventDefault();
        var next = tab.dataset.skillTab === 'write' ? 'github' : 'write';
        if (event.key === 'Home') next = 'write';
        if (event.key === 'End') next = 'github';
        selectTab(next, false);
        byId('skill-tab-' + next).focus();
      });
    });

    ['name', 'desc', 'body'].forEach(function (key) {
      els[key].addEventListener('input', function () { validateWrite(false); });
      els[key].addEventListener('blur', function () { els[key].dataset.touched = '1'; validateWrite(false); });
    });
    byId('skill-panel-write').addEventListener('submit', function (event) {
      event.preventDefault();
      saveWritten();
    });
    els.body.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveWritten();
      }
    });

    byId('skill-gh-form').addEventListener('submit', function (event) {
      event.preventDefault();
      findSkills();
    });
    els['gh-url'].addEventListener('input', function () {
      showError(els['gh-error'], '');
      if (preview && els['gh-url'].value.trim() !== previewUrl) clearPreview();
    });
    els['gh-all'].addEventListener('change', function () {
      var on = els['gh-all'].checked;
      checkboxes().forEach(function (box) { if (!box.disabled) box.checked = on; });
      syncSelection();
    });
    els['gh-list'].addEventListener('change', function (event) {
      if (event.target.matches('input[type="checkbox"]')) syncSelection();
    });
    els['gh-install'].addEventListener('click', function () {
      if (installed) { closeModal(); return; }
      installChosen();
    });
  }

  function selectTab(name, focusField) {
    Array.prototype.forEach.call(dialog.querySelectorAll('[data-skill-tab]'), function (tab) {
      var on = tab.dataset.skillTab === name;
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
      tab.classList.toggle('active', on);
    });
    byId('skill-panel-write').hidden = name !== 'write';
    byId('skill-panel-github').hidden = name !== 'github';
    if (focusField) (name === 'write' ? els.name : els['gh-url']).focus();
  }

  function open(tab) {
    if (!dialog) buildDialog();
    resetWrite();
    if (!preview || installed) { clearPreview(); els['gh-url'].value = ''; }
    showError(els['gh-error'], '');
    var name = tab === 'github' ? 'github' : 'write';
    selectTab(name, false);
    showModal(dialog, null, name === 'write' ? els.name : els['gh-url']);
  }

  function showError(el, text) {
    el.textContent = text || '';
    el.hidden = !text;
  }

  /* ------------------------------------------------------------ write one */

  function resetWrite() {
    els.name.value = '';
    els.desc.value = '';
    els.body.value = '';
    ['name', 'desc', 'body'].forEach(function (key) { delete els[key].dataset.touched; });
    showError(els['write-error'], '');
    validateWrite(false);
  }

  function setFieldError(input, el, text, show) {
    var visible = Boolean(text) && show;
    showError(el, visible ? text : '');
    input.setAttribute('aria-invalid', visible ? 'true' : 'false');
  }

  function validateWrite(submitting) {
    var name = singleLine(els.name.value);
    var description = singleLine(els.desc.value);
    var body = els.body.value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '').trim();
    var id = slugify(name);
    var nameError = '';
    if (!name) nameError = 'Give the skill a name.';
    else if (!id) nameError = 'Use at least one letter or digit, so it gets a /command.';
    else if (findSkill(id)) nameError = '/' + id + ' already exists. Choose another name.';
    var descError = description ? '' : 'Say in one line what it is for.';
    var bodyError = !body ? 'Write the instructions Imurun should follow.'
      : body.length > MAX_BODY ? 'Keep the instructions under ' + MAX_BODY.toLocaleString() + ' characters.' : '';

    els['id-preview'].innerHTML = id
      ? 'Use it by typing <code>/' + escapeHtml(id) + '</code> in a message.'
      : '<span class="muted">The command is made from the name, like <code>/release-notes</code>.</span>';
    els['desc-count'].textContent = description.length + ' / ' + MAX_DESCRIPTION;
    els['body-count'].textContent = body.length.toLocaleString() + ' / ' + MAX_BODY.toLocaleString();
    els['body-count'].classList.toggle('is-over', body.length > MAX_BODY);

    setFieldError(els.name, els['name-error'], nameError, submitting || els.name.dataset.touched || (findSkill(id) && id));
    setFieldError(els.desc, els['desc-error'], descError, submitting || els.desc.dataset.touched);
    setFieldError(els.body, els['body-error'], bodyError, submitting || els.body.dataset.touched || body.length > MAX_BODY);
    return { ok: !nameError && !descError && !bodyError, name: name, description: description, body: body, id: id,
      first: nameError ? els.name : descError ? els.desc : bodyError ? els.body : null };
  }

  function saveWritten() {
    var check = validateWrite(true);
    if (!check.ok) { if (check.first) check.first.focus(); return; }
    showError(els['write-error'], '');
    els.save.disabled = true;
    els.save.textContent = 'Saving\u2026';
    api.createSkill({ name: check.name, description: check.description, body: check.body }).then(function (skill) {
      closeModal();
      toast({ tone: 'success', icon: 'check', kicker: 'Skills', title: 'Skill saved',
        text: 'Type /' + ((skill && skill.id) || check.id) + ' in a message to use it.' });
      refresh();
    }, function (error) {
      showError(els['write-error'], error.status === 409
        ? '/' + check.id + ' already exists. Choose another name.'
        : error.message || 'The skill was not saved.');
    }).then(function () {
      els.save.disabled = false;
      els.save.textContent = 'Save skill';
    });
  }

  /* ---------------------------------------------------------- from GitHub */

  function looksLikeGitHub(url) {
    return /^(https:\/\/)?(www\.)?(github\.com|raw\.githubusercontent\.com)\/[^/\s]+\/[^/\s]+/i.test(url);
  }

  function clearPreview() {
    preview = null;
    previewUrl = '';
    installed = false;
    els['gh-results'].hidden = true;
    els['gh-list'].innerHTML = '';
    els['gh-notes'].hidden = true;
    els['gh-install'].disabled = true;
    els['gh-install'].textContent = 'Install';
  }

  var STATUS = {
    ok: ['Ready', 'success'],
    exists: ['Already installed', 'neutral'],
    duplicate: ['Duplicate', 'neutral'],
    too_large: ['Too large', 'error'],
    invalid: ['Not valid', 'error'],
    failed: ['Could not read', 'error']
  };

  function rowHtml(skill, index) {
    var status = STATUS[skill.status] || [skill.status, 'neutral'];
    var files = skill.files || [];
    var skipped = skill.skipped || [];
    var skippedCount = skipped.length + (skill.skipped_more || 0);
    var boxId = 'skill-gh-pick-' + index;
    var meta = [skill.path ? escapeHtml(skill.path) : 'repository root', util.formatBytes(skill.size)];
    if (files.length) meta.push(plural(files.length, 'extra file', 'extra files'));
    return '<li class="skill-gh-item' + (skill.installable ? '' : ' is-blocked') + '" data-gh-path="' + escapeHtml(skill.path) + '">' +
      '<input type="checkbox" id="' + boxId + '" data-gh-pick="' + escapeHtml(skill.path) + '"' +
        (skill.installable ? ' checked' : ' disabled') + '>' +
      '<div class="skill-gh-main">' +
        '<label class="skill-gh-title" for="' + boxId + '"><strong>' + escapeHtml(skill.name) + '</strong>' +
          (skill.id ? '<code>/' + escapeHtml(skill.id) + '</code>' : '') + '</label>' +
        '<span class="chip ' + status[1] + ' skill-gh-status">' + escapeHtml(status[0]) + '</span>' +
        (skill.description ? '<p class="skill-gh-desc">' + escapeHtml(skill.description) + '</p>' : '') +
        '<p class="skill-gh-meta">' + meta.join(' \u00b7 ') + '</p>' +
        ((skill.warnings || []).length ? '<ul class="skill-gh-warn">' + skill.warnings.map(function (text) {
          return '<li>' + escapeHtml(text) + '</li>';
        }).join('') + '</ul>' : '') +
        (files.length ? '<details class="skill-gh-files"><summary>' + plural(files.length, 'file', 'files') + ' copied with it</summary><ul>' +
          files.map(function (file) { return '<li><code>' + escapeHtml(file.path) + '</code> <span class="muted">' + util.formatBytes(file.size) + '</span></li>'; }).join('') +
          '</ul></details>' : '') +
        (skippedCount ? '<details class="skill-gh-files"><summary>' + plural(skippedCount, 'file', 'files') + ' skipped</summary><ul>' +
          skipped.map(function (file) { return '<li><code>' + escapeHtml(file.path) + '</code> <span class="muted">' + escapeHtml(file.reason) + '</span></li>'; }).join('') +
          (skill.skipped_more ? '<li class="muted">and ' + skill.skipped_more + ' more</li>' : '') +
          '</ul></details>' : '') +
        '<p class="skill-gh-result" hidden></p>' +
      '</div>' +
    '</li>';
  }

  function checkboxes() {
    return Array.prototype.slice.call(els['gh-list'].querySelectorAll('input[data-gh-pick]'));
  }

  function syncSelection() {
    var boxes = checkboxes().filter(function (box) { return !box.disabled; });
    var chosen = boxes.filter(function (box) { return box.checked; }).length;
    els['gh-all'].checked = boxes.length > 0 && chosen === boxes.length;
    els['gh-all'].indeterminate = chosen > 0 && chosen < boxes.length;
    els['gh-all'].disabled = !boxes.length;
    if (!installed) {
      els['gh-install'].disabled = chosen === 0;
      els['gh-install'].textContent = chosen ? 'Install ' + plural(chosen, 'skill', 'skills') : 'Install';
    }
  }

  function findSkills() {
    var url = els['gh-url'].value.trim();
    if (!url) { showError(els['gh-error'], 'Paste a GitHub link first.'); els['gh-url'].focus(); return; }
    if (/^http:\/\//i.test(url)) { showError(els['gh-error'], 'Use the https:// link.'); return; }
    if (!looksLikeGitHub(url)) {
      showError(els['gh-error'], 'That is not a GitHub link. Use github.com/owner/repo, a /tree/ folder or a SKILL.md.');
      return;
    }
    showError(els['gh-error'], '');
    clearPreview();
    els['gh-find'].disabled = true;
    els['gh-find'].textContent = 'Looking\u2026';
    els['gh-results'].setAttribute('aria-busy', 'true');
    api.previewSkillImport(url).then(function (result) {
      preview = result;
      previewUrl = url;
      var list = result.skills || [];
      var commit = String(result.commit || '').slice(0, 7);
      els['gh-source'].innerHTML = plural(list.length, 'skill', 'skills') + ' in <a href="' + escapeHtml(result.repo_url || '') +
        '" target="_blank" rel="noopener noreferrer">' + escapeHtml(result.owner + '/' + result.repo) + '</a>' +
        ' at ' + escapeHtml(result.ref || '') + ' (' + escapeHtml(commit) + ')';
      var notes = result.notes || [];
      els['gh-notes'].hidden = !notes.length;
      els['gh-notes'].innerHTML = notes.map(function (note) { return '<li>' + escapeHtml(note) + '</li>'; }).join('');
      els['gh-list'].innerHTML = list.map(rowHtml).join('');
      els['gh-results'].hidden = false;
      syncSelection();
      util.announce(plural(list.length, 'skill', 'skills') + ' found');
    }, function (error) {
      showError(els['gh-error'], error.message || 'Could not read that link.');
    }).then(function () {
      els['gh-find'].disabled = false;
      els['gh-find'].textContent = 'Find skills';
      els['gh-results'].removeAttribute('aria-busy');
    });
  }

  var RESULT = {
    installed: ['Installed', 'success'],
    exists: ['Already installed', 'neutral'],
    failed: ['Failed', 'error']
  };

  function installChosen() {
    if (!preview) return;
    var paths = checkboxes().filter(function (box) { return box.checked && !box.disabled; })
      .map(function (box) { return box.dataset.ghPick; });
    if (!paths.length) return;
    var button = els['gh-install'];
    button.disabled = true;
    button.textContent = 'Installing\u2026';
    checkboxes().forEach(function (box) { box.disabled = true; });
    els['gh-all'].disabled = true;
    api.importSkills(previewUrl, paths, preview.commit).then(function (result) {
      var results = (result && result.results) || [];
      var counts = { installed: 0, exists: 0, failed: 0 };
      results.forEach(function (item) {
        counts[item.status] = (counts[item.status] || 0) + 1;
        var row = els['gh-list'].querySelector('[data-gh-path="' + cssEscape(item.path) + '"]');
        if (!row) return;
        var look = RESULT[item.status] || [item.status, 'neutral'];
        var chip = row.querySelector('.skill-gh-status');
        chip.className = 'chip ' + look[1] + ' skill-gh-status';
        chip.textContent = look[0];
        var line = row.querySelector('.skill-gh-result');
        line.hidden = false;
        line.classList.toggle('is-error', item.status === 'failed');
        line.textContent = item.status === 'installed'
          ? 'Type /' + item.id + ' in a message to use it.'
          : (item.error || '');
      });
      installed = true;
      button.disabled = false;
      button.textContent = 'Done';
      button.focus();
      var parts = [];
      if (counts.installed) parts.push(counts.installed + ' installed');
      if (counts.exists) parts.push(counts.exists + ' already there');
      if (counts.failed) parts.push(counts.failed + ' failed');
      toast({ tone: counts.installed ? 'success' : 'neutral', icon: 'check', kicker: 'Skills',
        title: counts.installed ? plural(counts.installed, 'skill', 'skills') + ' installed' : 'Nothing new installed',
        text: parts.join(', ') + '.' });
      util.announce(parts.join(', '));
      refresh();
    }, function (error) {
      showError(els['gh-error'], error.message || 'The install did not finish.');
      button.textContent = 'Try again';
      checkboxes().forEach(function (box) {
        var row = box.closest('.skill-gh-item');
        box.disabled = row && row.classList.contains('is-blocked');
      });
      syncSelection();
      button.disabled = false;
    });
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
  }

  /* ------------------------------------------------------------ binding */

  function bindPage() {
    var grid = byId('skill-grid');
    if (grid) {
      grid.addEventListener('click', function (event) {
        var target = event.target;
        var add = target.closest('[data-skill-add]');
        if (add) { open(add.dataset.skillAdd); return; }
        var use = target.closest('[data-skill-use]');
        if (use) { useInChat(use.dataset.skillUse); return; }
        var copy = target.closest('[data-skill-copy]');
        if (copy) { copyCommand(copy.dataset.skillCopy, copy); return; }
        var update = target.closest('[data-skill-update]');
        if (update) { updateSkill(update.dataset.skillUpdate, update); return; }
        var remove = target.closest('[data-skill-delete]');
        if (remove) confirmDelete(remove.dataset.skillDelete);
      });
    }
    var add = byId('new-skill');
    if (add) add.addEventListener('click', function () { open('write'); });
    var search = byId('skill-search');
    if (search) {
      search.addEventListener('input', util.debounce ? util.debounce(draw, 80) : draw);
      search.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && search.value) {
          event.preventDefault();
          event.stopPropagation();
          search.value = '';
          draw();
        }
      });
    }
  }

  IM.skills = { render: render, refresh: refresh, open: open, slugify: slugify };

  bindPage();
  if (IM.takePendingSkills) {
    var early = IM.takePendingSkills();
    if (early) render(early);
  }
})();
