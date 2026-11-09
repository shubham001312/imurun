/* Type-to-focus: start typing anywhere on Home or Chat and it lands in the
 * message box.
 *
 * Before this, a keystroke with nothing focused (after clicking empty space,
 * closing a panel, or arriving on the page) went nowhere and was lost. Now a
 * printable key on Home or Chat, when focus is not already in a field and no
 * dialog is open, moves focus to that page's message box and inserts the
 * character. Shortcuts (Ctrl/Cmd/Alt), "?" for the shortcuts sheet, keys in
 * other fields and keys while a dialog or the palette is open are left alone.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};

  var TARGETS = {
    home: function () { return document.getElementById('home-prompt'); },
    chat: function () { return document.querySelector('#page-chat textarea'); }
  };

  function editable(el) {
    if (!el || el === document.body) return false;
    if (el.isContentEditable) return true;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  }

  /* Only dialogs that are actually on screen count: the rename, palette and
   * shortcut dialogs live in the page all the time, hidden by a parent. */
  function dialogOpen() {
    var nodes = document.querySelectorAll(
      'dialog[open], [role="dialog"], [role="alertdialog"], .modal, #palette');
    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i];
      if (node.getAttribute('aria-hidden') === 'true') continue;
      if (node.offsetParent !== null || (node.tagName === 'DIALOG' && node.open)) return true;
    }
    return false;
  }

  function visible(el) {
    return Boolean(el && !el.disabled && el.offsetParent !== null);
  }

  document.addEventListener('keydown', function (event) {
    if (event.defaultPrevented || event.isComposing) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (!event.key || event.key.length !== 1) return;       /* printable characters only */
    if (event.key === '?' || event.key === ' ') return;      /* "?" opens shortcuts; space scrolls */
    if (editable(document.activeElement) || dialogOpen()) return;
    var page = IM.shell && IM.shell.currentPage ? IM.shell.currentPage() : '';
    var find = TARGETS[page];
    var box = find ? find() : null;
    if (!visible(box)) return;
    event.preventDefault();
    box.focus();
    var start = box.selectionStart != null ? box.selectionStart : box.value.length;
    var end = box.selectionEnd != null ? box.selectionEnd : box.value.length;
    box.value = box.value.slice(0, start) + event.key + box.value.slice(end);
    box.selectionStart = box.selectionEnd = start + 1;
    box.dispatchEvent(new Event('input', { bubbles: true }));
  });
})();
