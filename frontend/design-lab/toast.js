/* Notifications (toasts): every card enters from the top-right.
 *
 *   IMURUN.toast(options) -> node          (also window.showNotification)
 *   IMURUN.toast.update(id, options)       patch a card in place
 *   IMURUN.toast.dismiss(idOrNode)
 *
 * options: tone ('success' | 'error' | 'neutral'), icon (check, alert, info,
 * clock, mail, send, pencil, close, spinner), kicker, title, text, fields,
 * note, actions [{label, className, onClick}], timeout (ms; 0 = sticky),
 * persistent (alias for timeout 0), id (a second call with the same id
 * updates the existing card instead of stacking a duplicate), role, focus,
 * focusSelector, dismissible, approval.
 *
 * Timers pause while the pointer or keyboard focus is on a card, and are
 * always cleared when the card leaves, so nothing fires on a removed node.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var NOTIFY_LIMIT = 4;
  var approvalNode = null;

  var ICONS = {
    check: '<path d="M5 12.5l4.2 4.2L19 7.5"/>',
    alert: '<circle cx="12" cy="12" r="8"/><path d="M12 7.8v4.8"/><circle cx="12" cy="16.2" r=".9" fill="currentColor" stroke="none"/>',
    info: '<circle cx="12" cy="12" r="8"/><path d="M12 11.2v4.8"/><circle cx="12" cy="8" r=".9" fill="currentColor" stroke="none"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5v4.8l3.2 1.9"/>',
    mail: '<rect x="3.8" y="6" width="16.4" height="12" rx="2.4"/><path d="M4.8 7.4l7.2 5.2 7.2-5.2"/>',
    send: '<path d="M4.2 11.6l15.4-6.8-5.1 15.4-3.5-6.1-6.8-2.5z"/><path d="M11 14.1l3.6-3.6"/>',
    pencil: '<path d="M4.5 19.5l.9-4.1L15.9 4.9a2.1 2.1 0 0 1 3 3L8.6 18.6l-4.1.9z"/><path d="M14.4 6.4l3.2 3.2"/>',
    close: '<path d="M7 7l10 10M17 7L7 17"/>'
  };

  function host() {
    return document.getElementById('notifications');
  }

  function iconMarkup(name) {
    if (name === 'spinner') return '<span class="notif-spinner"></span>';
    return '<svg viewBox="0 0 24 24">' + (ICONS[name] || ICONS.info) + '</svg>';
  }

  function makeIcon(name) {
    var span = document.createElement('span');
    span.className = 'notif-icon';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = iconMarkup(name);
    return span;
  }

  function defaultIcon(tone) {
    return tone === 'success' ? 'check' : tone === 'error' ? 'alert' : 'info';
  }

  function clearTimer(node) {
    if (node && node._toastTimer) {
      clearTimeout(node._toastTimer);
      node._toastTimer = null;
    }
  }

  function armTimer(node, timeout) {
    clearTimer(node);
    node._toastTimeout = timeout;
    if (!timeout) return;
    node._toastTimer = setTimeout(function () { dismiss(node); }, timeout);
  }

  function resolveTimeout(options) {
    if (options.persistent || options.timeout === 0 || options.timeout === false) return 0;
    return options.timeout || window.__imurun_notify_timeout || 4500;
  }

  function dismiss(target, immediate) {
    var node = typeof target === 'string' ? findById(target) : target;
    if (!node || node.dataset.leaving) return;
    node.dataset.leaving = '1';
    clearTimer(node);
    if (approvalNode === node) approvalNode = null;
    if (immediate || util.reducedMotion()) {
      node.remove();
      return;
    }
    node.classList.add('is-leaving');
    setTimeout(function () { node.remove(); }, 300);
  }

  function findById(id) {
    var root = host();
    if (!root || !id) return null;
    var nodes = root.querySelectorAll('[data-notification-id]');
    for (var i = 0; i < nodes.length; i += 1) {
      if (nodes[i].dataset.notificationId === id && !nodes[i].dataset.leaving) return nodes[i];
    }
    return null;
  }

  function makeButton(label, className, onClick) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', onClick);
    return button;
  }

  function setTone(node, tone) {
    node.className = node.className.replace(/\bnotification--\w+\b/g, '').trim() + ' notification--' + tone;
    node.setAttribute('role', node.dataset.role || (tone === 'error' ? 'alert' : 'status'));
  }

  /* Patch an existing card: title/text/kicker/tone/icon/timeout. */
  function update(id, options) {
    var node = typeof id === 'string' ? findById(id) : id;
    if (!node) return null;
    var opts = options || {};
    if (opts.tone) setTone(node, opts.tone);
    if (opts.icon || opts.tone) {
      var icon = node.querySelector('.notif-icon');
      if (icon) icon.innerHTML = iconMarkup(opts.icon || defaultIcon(opts.tone));
    }
    var pairs = [['kicker', '.notif-kicker'], ['title', '.notif-title'], ['text', '.notif-text']];
    pairs.forEach(function (pair) {
      if (opts[pair[0]] === undefined) return;
      var el = node.querySelector(pair[1]);
      if (!el && pair[0] === 'text') {
        el = document.createElement('p');
        el.className = 'notif-text';
        var title = node.querySelector('.notif-title');
        if (title && title.parentNode) title.parentNode.insertBefore(el, title.nextSibling);
      }
      if (el) el.textContent = opts[pair[0]] || '';
    });
    if (opts.timeout !== undefined || opts.persistent !== undefined) {
      armTimer(node, resolveTimeout(opts));
    }
    node.classList.remove('is-updated');
    void node.offsetWidth; /* restart the pulse animation */
    node.classList.add('is-updated');
    return node;
  }

  function showNotification(options) {
    var opts = options || {};
    var root = host();
    if (!root) return null;

    if (opts.id) {
      var existing = findById(opts.id);
      if (existing) {
        update(existing, Object.assign({ timeout: resolveTimeout(opts) }, opts));
        return existing;
      }
    }

    var tone = opts.tone || 'neutral';
    /* the website's palette: a confirm for success, a low tone for errors */
    if (window.IMURUN && IMURUN.sound) { if (tone === 'success') IMURUN.sound.success(); else if (tone === 'error') IMURUN.sound.error(); }
    var node = document.createElement('article');
    node.className = 'notification notification--' + tone;
    if (opts.role) node.dataset.role = opts.role;
    node.setAttribute('role', opts.role || (tone === 'error' ? 'alert' : 'status'));
    if (opts.id) node.dataset.notificationId = opts.id;
    if (opts.role === 'alertdialog' && opts.title) node.setAttribute('aria-label', opts.title);

    node.appendChild(makeIcon(opts.icon || defaultIcon(tone)));

    var body = document.createElement('div');
    body.className = 'notif-body';
    if (opts.kicker) {
      var kicker = document.createElement('div');
      kicker.className = 'notif-kicker';
      kicker.textContent = opts.kicker;
      body.appendChild(kicker);
    }
    var title = document.createElement('h2');
    title.className = 'notif-title';
    title.textContent = opts.title || '';
    body.appendChild(title);
    if (opts.text) {
      var text = document.createElement('p');
      text.className = 'notif-text';
      text.textContent = opts.text;
      body.appendChild(text);
    }
    if (opts.fields && opts.fields.length) {
      var fields = document.createElement('div');
      fields.className = 'notif-fields';
      opts.fields.forEach(function (field) {
        var wrap;
        if (field.editable) {
          wrap = document.createElement('label');
          wrap.className = 'notif-field';
          var label = document.createElement('span');
          label.className = 'k';
          label.textContent = field.k;
          wrap.appendChild(label);
          var input = document.createElement(field.input === 'textarea' ? 'textarea' : 'input');
          input.className = 'notif-input';
          input.name = field.name || '';
          if (field.input === 'textarea') input.rows = field.rows || 3;
          else input.type = field.input || 'text';
          input.value = field.v || '';
          wrap.appendChild(input);
        } else {
          wrap = document.createElement('div');
          wrap.className = 'notif-field';
          var key = document.createElement('span');
          key.className = 'k';
          key.textContent = field.k;
          var value = document.createElement('span');
          value.className = 'v';
          value.textContent = field.v;
          wrap.appendChild(key);
          wrap.appendChild(value);
        }
        fields.appendChild(wrap);
      });
      body.appendChild(fields);
    }
    if (opts.note) {
      var note = document.createElement('p');
      note.className = 'notif-note';
      note.textContent = opts.note;
      body.appendChild(note);
    }
    if (opts.actions && opts.actions.length) {
      var actions = document.createElement('div');
      actions.className = 'notif-actions';
      opts.actions.forEach(function (action) {
        actions.appendChild(makeButton(action.label, action.className || 'cancel', action.onClick));
      });
      body.appendChild(actions);
    }
    node.appendChild(body);
    if (opts.dismissible !== false) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'notif-close';
      close.setAttribute('aria-label', 'Dismiss notification');
      close.textContent = '×';
      close.addEventListener('click', function () { dismiss(node); });
      node.appendChild(close);
    }

    /* Pause auto-dismiss while the user reads or tabs through the card. */
    function pause() { clearTimer(node); }
    function resume() {
      if (node.dataset.leaving) return;
      if (node.contains(document.activeElement)) return;
      if (node._toastTimeout) armTimer(node, Math.max(1500, node._toastTimeout / 2));
    }
    node.addEventListener('mouseenter', pause);
    node.addEventListener('focusin', pause);
    node.addEventListener('mouseleave', resume);
    node.addEventListener('focusout', function () { setTimeout(resume, 0); });

    root.appendChild(node);
    if (opts.approval) approvalNode = node;
    /* `keep` cards (tool approvals, approvals.js) are never pushed out by
     * newer notices, however many arrive. */
    if (opts.keep) node.dataset.keep = '1';
    while (root.children.length > NOTIFY_LIMIT) {
      var victim = null;
      var removed = false;
      for (var i = 0; i < root.children.length; i += 1) {
        var child = root.children[i];
        if (child.dataset.leaving) {
          child.remove();
          removed = true;
          break;
        }
        if (child === approvalNode || child.dataset.keep || child === node) continue; /* never drop a pending approval */
        victim = child;
        break;
      }
      if (removed) continue;
      if (!victim) break;
      dismiss(victim);
      break;
    }
    armTimer(node, resolveTimeout(opts));
    if (opts.focus) {
      var focusTarget = node.querySelector(opts.focusSelector || 'button');
      if (focusTarget) {
        focusTarget.focus();
        if (focusTarget.tagName === 'INPUT' && focusTarget.select) focusTarget.select();
      }
    }
    return node;
  }

  /* ------------------------------------------------- approval and previews */

  function showApproval() {
    if (approvalNode) {
      var existing = approvalNode.querySelector('[name="to"]');
      if (existing) {
        existing.focus();
        existing.select();
      }
      return;
    }
    var card = showNotification({
      tone: 'neutral',
      icon: 'mail',
      kicker: 'Outbound action · Gmail',
      title: 'Send this email?',
      text: 'Draft only until you approve.',
      fields: [
        { k: 'To', v: 'dana@acme.dev', editable: true, input: 'email', name: 'to' },
        { k: 'Subject', v: 'Staging redirect fix is ready', editable: true, name: 'subject' },
        { k: 'Body', v: 'Hi Dana — the staging login redirect is fixed and covered by a regression test. Can you verify on your side?', editable: true, input: 'textarea', name: 'body', rows: 3 }
      ],
      note: 'Cancel is recorded as refused in the audit log.',
      role: 'alertdialog',
      approval: true,
      timeout: 0,
      dismissible: false,
      focus: true,
      focusSelector: '[name="to"]',
      actions: [
        { label: 'Cancel', className: 'cancel', onClick: function () { cancelApproval(); } },
        { label: 'Send', className: 'danger', onClick: function () {
          var to = card.querySelector('[name="to"]').value.trim();
          var subject = card.querySelector('[name="subject"]').value.trim();
          var body = card.querySelector('[name="body"]').value.trim();
          if (!to || !subject || !body) {
            showNotification({ tone: 'error', icon: 'alert', kicker: 'Check the draft', title: 'Email not sent', text: 'To, subject and body are required.' });
            return;
          }
          dismiss(card, true);
          showNotification({ tone: 'success', icon: 'send', kicker: 'Sent', title: 'Email sent', text: 'To ' + to + ' — ' + subject });
        } }
      ]
    });
  }

  function cancelApproval() {
    if (!approvalNode) return false;
    dismiss(approvalNode, true);
    showNotification({ tone: 'neutral', icon: 'close', kicker: 'Cancelled', title: 'Send cancelled', text: 'Recorded as refused in the audit log.' });
    return true;
  }

  var demoCopy = {
    success: { icon: 'check', kicker: 'Connected', title: 'Gmail connected', text: 'The integration is ready. Tokens stay local.' },
    error: { icon: 'alert', kicker: 'Failed', title: 'GitHub connection failed', text: 'The token was rejected. Reconnect to retry.' },
    neutral: { icon: 'clock', kicker: 'Notice', title: 'Prompt queued', text: 'Delivered at the next step.' }
  };

  function bindPreviews() {
    document.addEventListener('click', function (event) {
      var target = event.target && event.target.closest ? event.target : null;
      if (!target) return;
      if (target.closest('#open-approval')) {
        showApproval();
        return;
      }
      var dot = target.closest('.notify-preview[data-tone]');
      if (!dot) return;
      var copy = demoCopy[dot.dataset.tone] || demoCopy.neutral;
      showNotification({ tone: dot.dataset.tone || 'neutral', icon: copy.icon, kicker: copy.kicker, title: copy.title, text: copy.text });
    });

    var params = null;
    try { params = new URLSearchParams(location.search); } catch (error) { params = null; }
    if (!params) return;
    var notifyParam = params.get('notify');
    if (notifyParam && demoCopy[notifyParam]) {
      var demo = demoCopy[notifyParam];
      showNotification({ tone: notifyParam, icon: demo.icon, kicker: demo.kicker, title: demo.title, text: demo.text });
    }
    if (params.get('popup') === '1') showApproval();
  }

  /* The container is a labelled region; each card carries its own
   * status/alert role, so the container must not also be a live region (that
   * made screen readers announce every card twice). */
  var root = host();
  if (root) root.removeAttribute('aria-live');

  bindPreviews();

  showNotification.update = update;
  showNotification.dismiss = dismiss;
  showNotification.approval = showApproval;
  showNotification.cancelApproval = cancelApproval;
  showNotification.hasApproval = function () { return Boolean(approvalNode); };

  IM.toast = showNotification;
  window.showNotification = showNotification;
})();
