/* Actions on the user's own messages: Copy and Edit and resend.
 *
 *   - hover (or keyboard focus) shows a small row under the message;
 *   - right-click on the message opens a menu with the same actions;
 *   - Edit turns the bubble into a text box (Enter sends, Shift+Enter is a new
 *     line, Escape cancels). Sending drops that message and everything after
 *     it (POST /api/sessions/{id}/truncate) and sends the edited text, so the
 *     old answer does not stay above the new one.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var menu = null;

  function api() { return window.__imurun_api; }
  function toast(opts) { if (IM.toast) IM.toast(opts); }
  function sessionId() { return IM.state && IM.state.activeSessionId; }
  function textOf(msg) {
    if (!msg) return '';
    if (typeof msg._text === 'string') return msg._text;
    var p = msg.querySelector('.bubble');
    return p ? p.innerText.trim() : '';
  }
  function streaming() { return Boolean(IM.chat && IM.chat.isStreaming && IM.chat.isStreaming()); }

  function copy(msg, button) {
    if (IM.chat && IM.chat.copyText) IM.chat.copyText(textOf(msg), button || null, 'Copy');
    else if (navigator.clipboard) navigator.clipboard.writeText(textOf(msg));
  }

  /* How many user messages, counting this one, from here to the end. */
  function fromEnd(msg) {
    var all = Array.prototype.slice.call(document.querySelectorAll('.transcript > .msg.user'));
    var index = all.indexOf(msg);
    return index === -1 ? 0 : all.length - index;
  }

  function edit(msg) {
    if (!msg || msg.classList.contains('is-editing')) return;
    if (streaming()) {
      toast({ tone: 'neutral', kicker: 'Chat', title: 'A reply is still running', text: 'Stop it or wait for it to finish, then edit.' });
      return;
    }
    var bubble = msg.querySelector('.bubble');
    if (!bubble) return;
    var original = textOf(msg);
    msg.classList.add('is-editing');
    var form = document.createElement('form');
    form.className = 'user-edit';
    form.innerHTML =
      '<label class="sr-only">Edit your message</label>' +
      '<textarea rows="3" spellcheck="true"></textarea>' +
      '<div class="user-edit-foot"><span class="user-edit-note">Sending replaces this message and the replies after it.</span>' +
      '<button class="btn ghost" type="button" data-edit-cancel>Cancel</button>' +
      '<button class="btn primary" type="submit">Send</button></div>';
    var area = form.querySelector('textarea');
    area.value = original;
    bubble.hidden = true;
    msg.insertBefore(form, bubble.nextSibling);
    function grow() { area.style.height = 'auto'; area.style.height = Math.min(area.scrollHeight, 360) + 'px'; }
    area.addEventListener('input', grow);
    grow();
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);

    function close() {
      form.remove();
      bubble.hidden = false;
      msg.classList.remove('is-editing');
    }
    form.querySelector('[data-edit-cancel]').addEventListener('click', close);
    area.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      else if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var value = area.value.trim();
      var sid = sessionId();
      if (!value) { area.focus(); return; }
      if (!sid || !api() || !api().truncateSession) { close(); return; }
      if (streaming()) { toast({ tone: 'neutral', kicker: 'Chat', title: 'A reply is still running' }); return; }
      var count = fromEnd(msg);
      form.querySelectorAll('button, textarea').forEach(function (el) { el.disabled = true; });
      api().truncateSession(sid, count).then(function () {
        // Remove this message and everything after it, then send the new text.
        var node = msg;
        while (node) { var next = node.nextElementSibling; node.remove(); node = next; }
        if (IM.chat && IM.chat.send) IM.chat.send(value, sid);
      }, function (error) {
        form.querySelectorAll('button, textarea').forEach(function (el) { el.disabled = false; });
        toast({ tone: 'error', kicker: 'Chat', title: 'Could not edit the message', text: (error && error.message) || '' });
      });
    });
  }

  /* ------------------------------------------------------- right-click menu */
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }

  function openMenu(msg, x, y) {
    closeMenu();
    menu = document.createElement('div');
    menu.className = 'user-menu';
    menu.setAttribute('role', 'menu');
    menu.innerHTML =
      '<button type="button" role="menuitem" data-menu="copy"><span class="ico ico-copy" aria-hidden="true"></span>Copy</button>' +
      '<button type="button" role="menuitem" data-menu="edit"><span class="ico ico-edit" aria-hidden="true"></span>Edit and resend</button>';
    document.body.appendChild(menu);
    var w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
    menu.addEventListener('click', function (event) {
      var item = event.target.closest('[data-menu]');
      if (!item) return;
      var what = item.dataset.menu;
      closeMenu();
      if (what === 'copy') copy(msg, msg.querySelector('[data-user-copy]'));
      else edit(msg);
    });
    menu.addEventListener('keydown', function (event) {
      var items = Array.prototype.slice.call(menu.querySelectorAll('[data-menu]'));
      var i = items.indexOf(document.activeElement);
      if (event.key === 'Escape') { closeMenu(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        items[(i + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
      }
    });
    menu.querySelector('[data-menu]').focus();
  }

  document.addEventListener('click', function (event) {
    if (menu && !menu.contains(event.target)) closeMenu();
    var target = event.target;
    var copyBtn = target.closest && target.closest('[data-user-copy]');
    if (copyBtn) { copy(copyBtn.closest('.msg'), copyBtn); return; }
    var editBtn = target.closest && target.closest('[data-user-edit]');
    if (editBtn) edit(editBtn.closest('.msg'));
  });
  document.addEventListener('contextmenu', function (event) {
    var msg = event.target.closest && event.target.closest('.transcript > .msg.user');
    if (!msg || msg.classList.contains('is-editing')) { closeMenu(); return; }
    // Keep the browser menu when text is selected, so "copy selection" still works.
    var selection = window.getSelection && String(window.getSelection() || '');
    if (selection) return;
    event.preventDefault();
    openMenu(msg, event.clientX, event.clientY);
  });
  window.addEventListener('blur', closeMenu);
  window.addEventListener('resize', closeMenu);
  document.addEventListener('scroll', closeMenu, true);

  IM.userActions = { edit: edit, copy: copy };
})();
