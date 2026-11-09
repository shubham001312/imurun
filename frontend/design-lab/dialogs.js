/* In-app dialogs that replace window.prompt / window.confirm in Settings.
 *
 *   IMURUN.dialogs.confirm({title, text, confirmLabel, cancelLabel, danger})
 *       -> Promise<boolean>
 *   IMURUN.dialogs.addClient({create(payload) -> Promise<client>, existing})
 *       -> Promise<client | null>     (Settings > Use by API > client tokens)
 *
 * Built on the same pieces as the rename modal (.modal, .dialog,
 * .dialog-actions, util.trapFocus). Each dialog owns its Escape key and stops
 * it there, so the global Escape handler in app.js never sees it. Focus goes
 * back to whatever opened the dialog, or to `returnFocus` when that element
 * was removed meanwhile (a revoked row's button).
 * ES2017, no inline scripts (CSP default-src 'self').
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  function esc(text) {
    if (IM.util && IM.util.escapeHtml) return IM.util.escapeHtml(String(text));
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var counter = 0;
  function uid(prefix) { counter += 1; return prefix + '-' + counter; }

  /* One modal at a time per call. `onEscape` returns false to keep it open. */
  function mount(options) {
    var previous = document.activeElement;
    var modal = document.createElement('div');
    modal.className = 'modal im-dialog-modal';
    modal.setAttribute('data-im-dialog', '');
    var dialog = document.createElement('div');
    dialog.className = 'dialog im-dialog ' + (options.className || '');
    dialog.setAttribute('role', options.role || 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    modal.appendChild(dialog);
    document.body.appendChild(modal);
    if (IM.sound && IM.sound.open) IM.sound.open();

    var release = IM.util && IM.util.trapFocus ? IM.util.trapFocus(modal) : null;
    var closed = false;

    function close() {
      if (closed) return;
      closed = true;
      if (IM.sound && IM.sound.close) IM.sound.close();
      if (release) release(false);
      modal.remove();
      var target = previous && document.contains(previous) ? previous : options.returnFocus;
      if (typeof target === 'function') target = target();
      if (target && document.contains(target) && typeof target.focus === 'function') {
        try { target.focus(); } catch (error) { /* ignore */ }
      }
    }

    modal.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      if (options.onEscape) options.onEscape();
    });
    modal.addEventListener('mousedown', function (event) {
      if (event.target === modal && options.onBackdrop) options.onBackdrop();
    });
    return { modal: modal, dialog: dialog, close: close };
  }

  /* ------------------------------------------------------------ confirm */

  function confirm(options) {
    options = options || {};
    return new Promise(function (resolve) {
      var titleId = uid('im-confirm-title');
      var textId = uid('im-confirm-text');
      var done = false;
      var ui = mount({
        className: 'im-confirm',
        role: 'alertdialog',
        returnFocus: options.returnFocus,
        onEscape: function () { finish(false); },
        onBackdrop: function () { finish(false); }
      });
      function finish(value) {
        if (done) return;
        done = true;
        ui.close();
        resolve(value);
      }
      ui.dialog.setAttribute('aria-labelledby', titleId);
      ui.dialog.setAttribute('aria-describedby', textId);
      ui.dialog.innerHTML =
        '<h2 id="' + titleId + '">' + esc(options.title || 'Are you sure?') + '</h2>' +
        '<p class="sub" id="' + textId + '">' + esc(options.text || '') + '</p>' +
        '<div class="dialog-actions">' +
          '<button class="cancel" type="button" data-im-cancel>' + esc(options.cancelLabel || 'Cancel') + '</button>' +
          '<button class="' + (options.danger ? 'danger' : 'send') + '" type="button" data-im-ok>' +
            esc(options.confirmLabel || 'Continue') + '</button>' +
        '</div>';
      ui.dialog.querySelector('[data-im-cancel]').addEventListener('click', function () { finish(false); });
      ui.dialog.querySelector('[data-im-ok]').addEventListener('click', function () { finish(true); });
      /* Destructive actions start on Cancel so Enter does not destroy. */
      ui.dialog.querySelector(options.danger ? '[data-im-cancel]' : '[data-im-ok]').focus();
    });
  }

  /* ---------------------------------------------------------- add client */

  /* What each scope does, in plain words. The server (services/server/
   * clients.py, openai_api.py) enforces only chat/admin today, on /v1; the
   * rest are stored on the client as labels. Say so instead of implying more. */
  var SCOPES_LIVE = [
    { id: 'chat', label: 'Chat', text: 'Send messages to the loaded model through the OpenAI-compatible API (/v1). Apps like OpenClaw need this.' },
    { id: 'admin', label: 'Admin', text: 'Full control. Today it gives the same /v1 access as Chat; later it will also cover settings.' }
  ];
  var SCOPES_LATER = [
    { id: 'read-only', label: 'Read only', text: 'For looking without changing anything.' },
    { id: 'sessions', label: 'Sessions', text: 'For reading and managing chat sessions.' },
    { id: 'skills', label: 'Skills', text: 'For listing and running skills.' },
    { id: 'models', label: 'Models', text: 'For listing and switching models.' },
    { id: 'connectors', label: 'Connectors', text: 'For using connected apps.' }
  ];
  var NAME_MAX = 80;

  function scopeOption(scope, checked) {
    var id = uid('im-scope');
    return '<label class="im-scope" for="' + id + '">' +
      '<input type="checkbox" id="' + id + '" name="scope" value="' + esc(scope.id) + '"' + (checked ? ' checked' : '') + '>' +
      '<span class="im-scope-text"><span class="im-scope-name">' + esc(scope.label) + '</span>' +
      '<span class="im-scope-help">' + esc(scope.text) + '</span></span></label>';
  }

  function copy(text, button) {
    if (IM.chat && IM.chat.copyText) { IM.chat.copyText(text, button, 'Copy'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = 'Copied';
        setTimeout(function () { button.textContent = 'Copy'; }, 1400);
      }, function () { button.textContent = 'Select and copy'; });
    }
  }

  function addClient(options) {
    options = options || {};
    var existing = (options.existing || []).map(function (c) { return String(c.name || '').trim().toLowerCase(); });
    return new Promise(function (resolve) {
      var created = null;
      var busy = false;
      var step = 'form';
      var ui = mount({
        className: 'im-client-dialog',
        returnFocus: options.returnFocus,
        /* Escape closes; on the token step it counts as Done (a deliberate key). */
        onEscape: function () { if (!busy) finish(); },
        /* A stray click outside must not throw away a token that is shown once. */
        onBackdrop: function () { if (step === 'form' && !busy) finish(); }
      });
      function finish() {
        ui.close();
        resolve(created);
      }

      var titleId = uid('im-client-title');
      var subId = uid('im-client-sub');
      var nameId = uid('im-client-name');
      var nameHelpId = uid('im-client-name-help');
      var nameErrId = uid('im-client-name-err');
      var scopeErrId = uid('im-client-scope-err');
      ui.dialog.setAttribute('aria-labelledby', titleId);
      ui.dialog.setAttribute('aria-describedby', subId);

      function renderForm() {
        ui.dialog.innerHTML =
          '<h2 id="' + titleId + '">Add an API client</h2>' +
          '<p class="sub" id="' + subId + '">Give one app its own token. You can revoke it at any time without touching other apps.</p>' +
          '<form class="im-client-form" novalidate>' +
            '<div class="im-field">' +
              '<label class="im-field-label" for="' + nameId + '">Name</label>' +
              '<input id="' + nameId + '" class="im-input" maxlength="' + (NAME_MAX + 20) + '" autocomplete="off" spellcheck="false" ' +
                'placeholder="For example, OpenClaw on this laptop" aria-describedby="' + nameHelpId + ' ' + nameErrId + '">' +
              '<p class="im-field-help" id="' + nameHelpId + '">Only you see this. It tells your clients apart.</p>' +
              '<p class="im-field-error" id="' + nameErrId + '" hidden></p>' +
            '</div>' +
            '<fieldset class="im-scopes" aria-describedby="' + scopeErrId + '">' +
              '<legend class="im-field-label">What it may do</legend>' +
              '<p class="im-scope-group">Works today</p>' +
              SCOPES_LIVE.map(function (s) { return scopeOption(s, s.id === 'chat'); }).join('') +
              '<p class="im-scope-group">Saved on the token, not enforced yet</p>' +
              SCOPES_LATER.map(function (s) { return scopeOption(s, false); }).join('') +
              '<p class="im-field-error" id="' + scopeErrId + '" hidden></p>' +
              '<p class="im-field-warn" data-im-scope-warn hidden>With only these scopes the token cannot call anything yet. Add Chat for /v1 access.</p>' +
            '</fieldset>' +
            '<p class="im-field-error im-form-error" role="alert" data-im-form-error hidden></p>' +
            '<div class="dialog-actions">' +
              '<button class="cancel" type="button" data-im-cancel>Cancel</button>' +
              '<button class="send" type="submit" data-im-create>Create</button>' +
            '</div>' +
          '</form>';
        var form = ui.dialog.querySelector('form');
        var input = ui.dialog.querySelector('#' + nameId);
        var nameErr = ui.dialog.querySelector('#' + nameErrId);
        var scopeErr = ui.dialog.querySelector('#' + scopeErrId);
        var warn = ui.dialog.querySelector('[data-im-scope-warn]');
        var formErr = ui.dialog.querySelector('[data-im-form-error]');
        var createBtn = ui.dialog.querySelector('[data-im-create]');
        var touched = false;

        function selected() {
          return Array.prototype.slice.call(form.querySelectorAll('input[name="scope"]:checked')).map(function (box) { return box.value; });
        }
        function nameProblem() {
          var value = input.value.trim();
          if (!value) return 'Give the client a name.';
          if (value.length > NAME_MAX) return 'Keep the name to ' + NAME_MAX + ' characters or fewer (' + value.length + ' now).';
          if (existing.indexOf(value.toLowerCase()) !== -1) return 'A client with this name already exists. Pick another so you can tell them apart.';
          return '';
        }
        function validate(showAll) {
          var problem = nameProblem();
          var showName = problem && (touched || showAll);
          nameErr.textContent = showName ? problem : '';
          nameErr.hidden = !showName;
          input.setAttribute('aria-invalid', showName ? 'true' : 'false');
          var scopes = selected();
          var noScope = !scopes.length;
          scopeErr.textContent = noScope ? 'Choose at least one.' : '';
          scopeErr.hidden = !noScope;
          warn.hidden = noScope || scopes.indexOf('chat') !== -1 || scopes.indexOf('admin') !== -1;
          return !problem && !noScope;
        }

        input.addEventListener('input', function () { if (touched) validate(false); });
        input.addEventListener('blur', function () { if (input.value) { touched = true; validate(false); } });
        form.addEventListener('change', function (event) { if (event.target.name === 'scope') validate(false); });
        ui.dialog.querySelector('[data-im-cancel]').addEventListener('click', function () { if (!busy) finish(); });
        form.addEventListener('submit', function (event) {
          event.preventDefault();
          if (busy) return;
          touched = true;
          formErr.hidden = true;
          if (!validate(true)) {
            (nameProblem() ? input : form.querySelector('input[name="scope"]')).focus();
            return;
          }
          busy = true;
          createBtn.disabled = true;
          createBtn.textContent = 'Creating…';
          var payload = { name: input.value.trim(), scopes: selected() };
          Promise.resolve(options.create(payload)).then(function (client) {
            busy = false;
            created = client || {};
            if (!created.name) created.name = payload.name;
            if (!created.scopes) created.scopes = payload.scopes;
            renderToken();
          }, function (error) {
            busy = false;
            createBtn.disabled = false;
            createBtn.textContent = 'Create';
            formErr.textContent = 'Could not create the client: ' + ((error && error.message) || 'the server did not answer.');
            formErr.hidden = false;
          });
        });
        input.focus();
      }

      function renderToken() {
        step = 'token';
        var token = created.token || '';
        var tokenId = uid('im-client-token');
        var scopes = (created.scopes || []).join(', ');
        ui.dialog.innerHTML =
          '<h2 id="' + titleId + '">Client created</h2>' +
          '<p class="sub" id="' + subId + '">' + esc(created.name) + (scopes ? ' · ' + esc(scopes) : '') + '</p>' +
          (token
            ? '<label class="im-field-label" for="' + tokenId + '">Token</label>' +
              '<div class="im-token-box">' +
                '<textarea id="' + tokenId + '" class="im-token" readonly rows="2" spellcheck="false" autocomplete="off">' + esc(token) + '</textarea>' +
                '<button class="btn" type="button" data-im-copy>Copy</button>' +
              '</div>' +
              '<p class="im-token-warn">Copy it now. You will not see this token again: IMURUN keeps only a fingerprint of it. If you lose it, revoke this client and add a new one.</p>'
            : '<p class="im-field-warn">The server did not return a token. Revoke this client and try again.</p>') +
          '<div class="dialog-actions"><button class="send" type="button" data-im-done>Done</button></div>';
        var field = ui.dialog.querySelector('.im-token');
        var copyBtn = ui.dialog.querySelector('[data-im-copy]');
        if (field) {
          field.addEventListener('focus', function () { field.select(); });
          field.addEventListener('click', function () { field.select(); });
        }
        if (copyBtn) copyBtn.addEventListener('click', function () {
          copy(token, copyBtn);
          copyBtn.focus();
        });
        ui.dialog.querySelector('[data-im-done]').addEventListener('click', finish);
        (copyBtn || ui.dialog.querySelector('[data-im-done]')).focus();
      }

      renderForm();
    });
  }

  IM.dialogs = { confirm: confirm, addClient: addClient };
})();
