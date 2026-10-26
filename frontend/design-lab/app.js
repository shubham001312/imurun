/* Theme + font switcher and popup demo for the design lab. */
(function () {
  var THEMES = [
    { id: 'liquid-paper', file: 'styles/liquid-paper.css', label: 'Liquid Paper', note: 'light glass · warm · serif' },
    { id: 'paper',    file: 'styles/paper.css',    label: 'Paper',    note: 'light · warm · editorial' },
    { id: 'obsidian', file: 'styles/obsidian.css', label: 'Obsidian', note: 'dark · amber · dense' },
    { id: 'neon',     file: 'styles/neon.css',     label: 'Neon',     note: 'dark · cyan/violet · glass' },
    { id: 'slate',    file: 'styles/slate.css',    label: 'Slate',    note: 'light · cool · clean' },
    { id: 'phosphor', file: 'styles/phosphor.css', label: 'Phosphor', note: 'CRT · green · mono' },
    { id: 'aurora',   file: 'styles/aurora.css',   label: 'Aurora',   note: 'dark · soft · rounded' },
    { id: 'liquid',   file: 'styles/liquid.css',   label: 'Liquid',   note: 'glass · blur · pointer light' },
    { id: 'liquid-slate', file: 'styles/liquid-slate.css', label: 'Liquid Slate', note: 'light glass · cool · clean' }
  ];
  var THEME_KEY = 'imurun.design.theme.v3';

  var link = document.getElementById('theme-link');
  var note = document.getElementById('lab-note');
  var switcher = document.getElementById('theme-switcher');
  var buttons = {};

  function select(theme) {
    link.href = theme.file;
    document.body.dataset.theme = theme.id;
    note.textContent = theme.label + ' — ' + theme.note;
    Object.keys(buttons).forEach(function (id) {
      buttons[id].classList.toggle('active', id === theme.id);
    });
    try { localStorage.setItem(THEME_KEY, theme.id); } catch (e) { /* file:// may refuse */ }
  }

  THEMES.forEach(function (theme) {
    var b = document.createElement('button');
    b.className = 'theme-btn';
    b.type = 'button';
    b.textContent = theme.label;
    b.addEventListener('click', function () { select(theme); });
    switcher.appendChild(b);
    buttons[theme.id] = b;
  });

  var saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
  var fromUrl = null;
  try { fromUrl = new URLSearchParams(location.search).get('style'); } catch (e) { /* ignore */ }
  var wanted = fromUrl || saved;
  var start = THEMES.filter(function (t) { return t.id === wanted; })[0] || THEMES[0];
  select(start);

  /* Text font - Manrope is the selected UI font; the others stay for comparison. */
  var FONTS = [
    { id: 'manrope', label: 'Manrope' },
    { id: 'inter',   label: 'Inter' },
    { id: 'grotesk', label: 'Grotesk' },
    { id: 'default', label: 'Style default' }
  ];
  var FONT_KEY = 'imurun.design.font.v2';
  var fontSwitcher = document.getElementById('font-switcher');
  var fontButtons = {};

  function applyFont(font) {
    if (font.id === 'default') document.body.removeAttribute('data-font');
    else document.body.dataset.font = font.id;
    Object.keys(fontButtons).forEach(function (id) {
      fontButtons[id].classList.toggle('active', id === font.id);
    });
    try { localStorage.setItem(FONT_KEY, font.id); } catch (e) { /* ignore */ }
  }

  FONTS.forEach(function (font) {
    var b = document.createElement('button');
    b.className = 'theme-btn';
    b.type = 'button';
    b.textContent = font.label;
    b.addEventListener('click', function () { applyFont(font); });
    fontSwitcher.appendChild(b);
    fontButtons[font.id] = b;
  });

  var savedFont = null;
  try { savedFont = localStorage.getItem(FONT_KEY); } catch (e) { /* ignore */ }
  var urlFont = null;
  try { urlFont = new URLSearchParams(location.search).get('font'); } catch (e) { /* ignore */ }
  var wantedFont = urlFont || savedFont;
  var startFont = FONTS.filter(function (f) { return f.id === wantedFont; })[0] || FONTS[0];
  applyFont(startFont);

  /* Prompt grows to five lines, then scrolls; the scrollbar stays hidden. */
  var prompt = document.querySelector('.input-wrap textarea');
  if (prompt) {
    var maxPromptLines = 5;
    var resizePrompt = function () {
      prompt.style.height = 'auto';
      var style = window.getComputedStyle(prompt);
      var line = parseFloat(style.lineHeight);
      if (!line || isNaN(line)) line = (parseFloat(style.fontSize) || 15) * 1.5;
      var padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      var border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      var max = line * maxPromptLines + padding + border;
      prompt.style.height = Math.min(prompt.scrollHeight, max) + 'px';
      prompt.style.overflowY = prompt.scrollHeight > max ? 'auto' : 'hidden';
    };
    prompt.addEventListener('input', resizePrompt);
    window.addEventListener('resize', resizePrompt);
    prompt.addEventListener('keydown', function (event) {
      var selectAll = (event.ctrlKey || event.metaKey) && !event.altKey &&
        (event.code === 'KeyA' || (event.key && event.key.toLowerCase() === 'a'));
      if (!selectAll) return;
      event.preventDefault();
      prompt.select();
    });
    resizePrompt();
  }

  /* The native caret is one pixel; draw a 3px accent bar in its place. */
  var inputWrap = document.querySelector('.input-wrap');
  var promptCaret = document.getElementById('prompt-caret');
  if (prompt && inputWrap && promptCaret) {
    var mirror = document.createElement('div');
    mirror.className = 'prompt-mirror';
    mirror.setAttribute('aria-hidden', 'true');
    inputWrap.appendChild(mirror);
    var marker = document.createElement('span');
    marker.className = 'caret-marker';
    var updatePromptCaret = function () {
      if (document.activeElement !== prompt || prompt.selectionStart !== prompt.selectionEnd) {
        promptCaret.style.display = 'none';
        return;
      }
      var style = window.getComputedStyle(prompt);
      var line = parseFloat(style.lineHeight);
      if (!line || isNaN(line)) line = (parseFloat(style.fontSize) || 15) * 1.5;
      mirror.style.font = style.font;
      mirror.style.lineHeight = style.lineHeight;
      mirror.style.padding = style.padding;
      mirror.style.border = style.border;
      mirror.style.letterSpacing = style.letterSpacing;
      mirror.style.wordSpacing = style.wordSpacing;
      mirror.style.width = prompt.clientWidth + 'px';
      mirror.style.left = prompt.offsetLeft + 'px';
      mirror.style.top = prompt.offsetTop + 'px';
      mirror.textContent = '';
      mirror.appendChild(document.createTextNode(prompt.value.slice(0, prompt.selectionStart)));
      mirror.appendChild(marker);
      var markerRect = marker.getBoundingClientRect();
      var wrapRect = inputWrap.getBoundingClientRect();
      var wrapStyle = window.getComputedStyle(inputWrap);
      var originX = wrapRect.left + parseFloat(wrapStyle.borderLeftWidth);
      var originY = wrapRect.top + parseFloat(wrapStyle.borderTopWidth);
      var markerHeight = markerRect.height || parseFloat(style.fontSize) || 15;
      var left = markerRect.left - originX;
      var top = markerRect.top - originY - prompt.scrollTop - (line - markerHeight) / 2;
      if (top < prompt.offsetTop - 1 || top + line > prompt.offsetTop + prompt.clientHeight + 1) {
        promptCaret.style.display = 'none';
        return;
      }
      top = Math.max(top, prompt.offsetTop);
      promptCaret.style.display = 'block';
      promptCaret.style.left = left + 'px';
      promptCaret.style.top = top + 'px';
      promptCaret.style.height = line + 'px';
    };
    prompt.addEventListener('input', updatePromptCaret);
    prompt.addEventListener('keyup', updatePromptCaret);
    prompt.addEventListener('click', updatePromptCaret);
    prompt.addEventListener('select', updatePromptCaret);
    prompt.addEventListener('scroll', updatePromptCaret);
    prompt.addEventListener('focus', updatePromptCaret);
    prompt.addEventListener('compositionupdate', updatePromptCaret);
    prompt.addEventListener('blur', function () { promptCaret.style.display = 'none'; });
    window.addEventListener('resize', updatePromptCaret);
    inputWrap.classList.add('custom-caret');
    updatePromptCaret();
  }

  /* Mode and model effort in the composer strip. */
  var modeButtons = document.querySelectorAll('.mode');
  var effort = document.getElementById('effort');
  function setMode(mode) {
    modeButtons.forEach(function (button) {
      var active = button.dataset.mode === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
  modeButtons.forEach(function (button) {
    button.addEventListener('click', function () { setMode(button.dataset.mode); });
  });
  if (modeButtons.length) {
    var activeMode = document.querySelector('.mode.active');
    setMode(activeMode ? activeMode.dataset.mode : 'build');
  }
  var efforts = ['Low', 'Balanced', 'High'];
  var effortIndex = 1;
  if (effort) {
    effort.addEventListener('click', function () {
      effortIndex = (effortIndex + 1) % efforts.length;
      effort.querySelector('strong').textContent = efforts[effortIndex];
    });
  }

  /* Pointer light, used by the Liquid style. */
  var glow = document.getElementById('pointer-glow');
  var reduceMotion = false;
  try { reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { /* ignore */ }
  if (glow && !reduceMotion) {
    var frame = null;
    var px = 0;
    var py = 0;
    document.addEventListener('pointermove', function (event) {
      px = event.clientX;
      py = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(function () {
        frame = null;
        document.documentElement.style.setProperty('--px', px + 'px');
        document.documentElement.style.setProperty('--py', py + 'px');
      });
    });
  }

  /* Notifications: all enter from the top-right. The email approval is a
     persistent cream card; success is green, failure red, everything else
     cream. */
  var notifications = document.getElementById('notifications');
  var NOTIFY_LIMIT = 4;
  var approvalNode = null;
  var reducedMotion = false;
  try { reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { /* ignore */ }

  function dismissNotification(node, immediate) {
    if (!node || node.dataset.leaving) return;
    node.dataset.leaving = '1';
    if (approvalNode === node) approvalNode = null;
    if (immediate || reducedMotion) { node.remove(); return; }
    node.classList.add('is-leaving');
    setTimeout(function () { node.remove(); }, 300);
  }

  function makeButton(label, className, onClick) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', onClick);
    return button;
  }

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

  function makeIcon(name) {
    var span = document.createElement('span');
    span.className = 'notif-icon';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<svg viewBox="0 0 24 24">' + (ICONS[name] || ICONS.info) + '</svg>';
    return span;
  }

  function showNotification(options) {
    if (!notifications) return null;
    var tone = options.tone || 'neutral';
    var node = document.createElement('article');
    node.className = 'notification notification--' + tone;
    node.setAttribute('role', options.role || (tone === 'error' ? 'alert' : 'status'));
    node.setAttribute('aria-live', tone === 'error' ? 'assertive' : 'polite');
    if (options.role === 'alertdialog' && options.title) {
      node.setAttribute('aria-label', options.title);
    }

    var icon = options.icon || (tone === 'success' ? 'check' : tone === 'error' ? 'alert' : 'info');
    node.appendChild(makeIcon(icon));

    var body = document.createElement('div');
    body.className = 'notif-body';
    if (options.kicker) {
      var kicker = document.createElement('div');
      kicker.className = 'notif-kicker';
      kicker.textContent = options.kicker;
      body.appendChild(kicker);
    }
    var title = document.createElement('h2');
    title.className = 'notif-title';
    title.textContent = options.title || '';
    body.appendChild(title);
    if (options.text) {
      var text = document.createElement('p');
      text.className = 'notif-text';
      text.textContent = options.text;
      body.appendChild(text);
    }
    if (options.fields && options.fields.length) {
      var fields = document.createElement('div');
      fields.className = 'notif-fields';
      options.fields.forEach(function (field) {
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
          if (field.input === 'textarea') {
            input.rows = field.rows || 3;
            input.value = field.v || '';
          } else {
            input.type = field.input || 'text';
            input.value = field.v || '';
          }
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
    if (options.note) {
      var note = document.createElement('p');
      note.className = 'notif-note';
      note.textContent = options.note;
      body.appendChild(note);
    }
    if (options.actions && options.actions.length) {
      var actions = document.createElement('div');
      actions.className = 'notif-actions';
      options.actions.forEach(function (action) {
        actions.appendChild(makeButton(action.label, action.className || 'cancel', action.onClick));
      });
      body.appendChild(actions);
    }
    node.appendChild(body);
    if (options.dismissible !== false) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'notif-close';
      close.setAttribute('aria-label', 'Dismiss notification');
      close.textContent = '\u00d7';
      close.addEventListener('click', function () { dismissNotification(node); });
      node.appendChild(close);
    }

    notifications.appendChild(node);
    if (options.approval) approvalNode = node;
    while (notifications.children.length > NOTIFY_LIMIT) {
      var oldest = notifications.firstElementChild;
      if (!oldest) break;
      if (oldest.dataset.leaving) { oldest.remove(); continue; }
      dismissNotification(oldest);
      break;
    }
    if (options.timeout !== 0 && options.timeout !== false) {
      setTimeout(function () { dismissNotification(node); }, options.timeout || 4500);
    }
    if (options.focus) {
      var focusTarget = node.querySelector(options.focusSelector || 'button');
      if (focusTarget) {
        focusTarget.focus();
        if (focusTarget.tagName === 'INPUT' && focusTarget.select) focusTarget.select();
      }
    }
    return node;
  }

  function showApproval() {
    if (approvalNode) {
      var existing = approvalNode.querySelector('[name="to"]');
      if (existing) { existing.focus(); existing.select(); }
      return;
    }
    var card = showNotification({
      tone: 'neutral',
      icon: 'mail',
      kicker: 'Outbound action \u00b7 Gmail',
      title: 'Send this email?',
      text: 'Draft only until you approve.',
      fields: [
        { k: 'To', v: 'dana@acme.dev', editable: true, input: 'email', name: 'to' },
        { k: 'Subject', v: 'Staging redirect fix is ready', editable: true, name: 'subject' },
        { k: 'Body', v: 'Hi Dana \u2014 the staging login redirect is fixed and covered by a regression test. Can you verify on your side?', editable: true, input: 'textarea', name: 'body', rows: 3 }
      ],
      note: 'Cancel is recorded as refused in the audit log.',
      role: 'alertdialog',
      approval: true,
      timeout: 0,
      dismissible: false,
      focus: true,
      focusSelector: '[name="to"]',
      actions: [
        { label: 'Cancel', className: 'cancel', onClick: function () {
          dismissNotification(card, true);
          showNotification({ tone: 'neutral', icon: 'close', kicker: 'Cancelled', title: 'Send cancelled', text: 'Recorded as refused in the audit log.' });
        } },
        { label: 'Send', className: 'danger', onClick: function () {
          var to = card.querySelector('[name="to"]').value.trim();
          var subject = card.querySelector('[name="subject"]').value.trim();
          var body = card.querySelector('[name="body"]').value.trim();
          if (!to || !subject || !body) {
            showNotification({ tone: 'error', icon: 'alert', kicker: 'Check the draft', title: 'Email not sent', text: 'To, subject and body are required.' });
            return;
          }
          dismissNotification(card, true);
          showNotification({ tone: 'success', icon: 'send', kicker: 'Sent', title: 'Email sent', text: 'To ' + to + ' \u2014 ' + subject });
        } }
      ]
    });
  }

  document.getElementById('open-approval').addEventListener('click', showApproval);

  var demoCopy = {
    success: { icon: 'check', kicker: 'Connected', title: 'Gmail connected', text: 'The integration is ready. Tokens stay local.' },
    error: { icon: 'alert', kicker: 'Failed', title: 'GitHub connection failed', text: 'The token was rejected. Reconnect to retry.' },
    neutral: { icon: 'clock', kicker: 'Notice', title: 'Prompt queued', text: 'Delivered at the next step.' }
  };
  document.querySelectorAll('.notify-preview[data-tone]').forEach(function (dot) {
    dot.addEventListener('click', function () {
      var copy = demoCopy[dot.dataset.tone] || demoCopy.neutral;
      showNotification({ tone: dot.dataset.tone || 'neutral', icon: copy.icon, kicker: copy.kicker, title: copy.title, text: copy.text });
    });
  });
  var notifyParam = null;
  try { notifyParam = new URLSearchParams(location.search).get('notify'); } catch (e) { /* ignore */ }
  if (notifyParam && demoCopy[notifyParam]) {
    var demo = demoCopy[notifyParam];
    showNotification({ tone: notifyParam, icon: demo.icon, kicker: demo.kicker, title: demo.title, text: demo.text });
  }

  /* Rename: right-click a session, or type /rename in the composer. */
  var renameModal = document.getElementById('rename');
  var renameInput = document.getElementById('rename-input');
  var renameError = document.getElementById('rename-error');
  var sessionMenu = document.getElementById('session-menu');
  var menuSession = null;
  var renameSession = null;

  function activeSession() {
    return document.querySelector('.session.active');
  }

  function openRename(session) {
    if (!renameModal || !session) return;
    renameSession = session;
    if (sessionMenu) sessionMenu.hidden = true;
    renameInput.value = session.dataset.sessionName || session.querySelector('.s-name').textContent;
    renameError.hidden = true;
    renameModal.hidden = false;
    renameInput.focus();
    renameInput.select();
  }

  function closeRename() {
    if (renameModal) renameModal.hidden = true;
    renameSession = null;
  }

  function saveRename() {
    if (!renameSession) return;
    var value = renameInput.value.trim();
    if (!value) {
      renameError.textContent = 'A session name is required.';
      renameError.hidden = false;
      renameInput.focus();
      return;
    }
    var oldName = renameSession.dataset.sessionName || '';
    var sessionId = renameSession.dataset.sessionId || '';
    document.querySelectorAll('[data-session-id="' + sessionId + '"]').forEach(function (node) {
      node.dataset.sessionName = value;
      var nameNode = node.querySelector('.s-name') || node.querySelector('.session-main strong');
      if (nameNode) nameNode.textContent = value;
    });
    if (renameSession.classList.contains('active')) {
      var crumb = document.querySelector('.topbar .crumb strong');
      if (crumb) crumb.textContent = value + (sessionId ? '-' + sessionId : '');
    }
    var display = value + (sessionId ? '-' + sessionId : '');
    closeRename();
    showNotification({
      tone: 'success',
      icon: 'pencil',
      kicker: 'Renamed',
      title: 'Session renamed',
      text: oldName === value ? 'The name was already up to date.' : 'Now ' + display + '.'
    });
  }

  function showSessionMenu(event, session) {
    if (!sessionMenu) return;
    event.preventDefault();
    menuSession = session;
    sessionMenu.hidden = false;
    var rect = sessionMenu.getBoundingClientRect();
    var left = Math.min(event.clientX, window.innerWidth - rect.width - 12);
    var top = Math.min(event.clientY, window.innerHeight - rect.height - 12);
    sessionMenu.style.left = Math.max(12, left) + 'px';
    sessionMenu.style.top = Math.max(12, top) + 'px';
    var item = sessionMenu.querySelector('[data-action="rename"]');
    if (item) item.focus();
  }

  document.querySelectorAll('.session, .session-row').forEach(function (session) {
    session.addEventListener('contextmenu', function (event) {
      showSessionMenu(event, session);
    });
  });

  if (sessionMenu) {
    sessionMenu.querySelector('[data-action="rename"]').addEventListener('click', function () {
      if (menuSession) openRename(menuSession);
    });
    document.addEventListener('click', function (event) {
      if (!sessionMenu.hidden && !sessionMenu.contains(event.target)) sessionMenu.hidden = true;
    });
    document.addEventListener('scroll', function () { sessionMenu.hidden = true; }, true);
  }

  document.getElementById('rename-cancel').addEventListener('click', closeRename);
  document.getElementById('rename-save').addEventListener('click', saveRename);
  renameInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') { event.preventDefault(); saveRename(); }
  });
  if (renameModal) {
    renameModal.addEventListener('click', function (event) {
      if (event.target === renameModal) closeRename();
    });
  }

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!renameModal.hidden) { closeRename(); return; }
    if (!sessionMenu.hidden) { sessionMenu.hidden = true; return; }
    if (approvalNode) {
      dismissNotification(approvalNode, true);
      showNotification({ tone: 'neutral', icon: 'close', kicker: 'Cancelled', title: 'Send cancelled', text: 'Recorded as refused in the audit log.' });
    }
  });

  /* /rename opens the rename popup for the current session. */
  var composerSend = document.querySelector('.composer .send');
  function handleRenameCommand() {
    if (!prompt) return false;
    var value = prompt.value.trim();
    if (value !== '/rename' && value !== '/rename-session') return false;
    var session = activeSession();
    prompt.value = '';
    prompt.dispatchEvent(new Event('input'));
    if (!session) {
      showNotification({ tone: 'neutral', kicker: 'Rename', title: 'No active session', text: 'Start a session first.' });
      return true;
    }
    openRename(session);
    return true;
  }

  function appendPrompt(text) {
    var transcript = document.querySelector('.transcript');
    if (!transcript) return;
    var message = document.createElement('div');
    message.className = 'msg user';
    var who = document.createElement('div');
    who.className = 'who';
    who.textContent = 'You';
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    message.appendChild(who);
    message.appendChild(bubble);
    var queued = transcript.querySelector('.queued');
    if (queued) transcript.insertBefore(message, queued);
    else transcript.appendChild(message);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function sendPrompt() {
    if (!prompt) return false;
    var value = prompt.value.trim();
    if (!value) return false;
    if (handleRenameCommand()) return true;
    appendPrompt(value);
    prompt.value = '';
    prompt.dispatchEvent(new Event('input'));
    prompt.focus();
    return true;
  }

  if (prompt) {
    prompt.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey) return;
      event.preventDefault();
      sendPrompt();
    });
  }
  if (composerSend) {
    composerSend.addEventListener('click', function (event) {
      event.preventDefault();
      sendPrompt();
    });
  }

  /* The live thinking row derives its status from the thought itself. In
     production the thinking stream supplies this label; it is never a fixed
     UI string. */
  var THINK_MAP = {
    read: 'Scanning', scan: 'Scanning', review: 'Scanning',
    check: 'Hustling', test: 'Hustling', verify: 'Hustling',
    search: 'Hunting', find: 'Hunting', look: 'Hunting',
    add: 'Stitching', write: 'Stitching', build: 'Stitching', fix: 'Stitching',
    debug: 'Untangling', trace: 'Tracing', plan: 'Plotting',
    compare: 'Weighing', run: 'Spinning', wait: 'Holding'
  };

  function thinkingComment(text) {
    var words = String(text || '').toLowerCase()
      .replace(/[^a-z0-9\s-]/g, ' ')
      .split(/\s+/)
      .filter(Boolean);
    var verbPattern = /^(read|scan|review|check|test|verify|search|find|look|add|write|build|fix|debug|trace|plan|compare|run|wait)(s|ed|ing)?$/;
    var verbs = [];
    words.forEach(function (word) {
      var match = word.match(verbPattern);
      if (match && verbs.indexOf(match[1]) < 0) verbs.push(match[1]);
    });
    if (verbs.length >= 3) return 'Juggling';
    return verbs.length ? (THINK_MAP[verbs[0]] || '') : '';
  }

  function updateThinkingComment() {
    var block = document.querySelector('.thinking.is-thinking');
    if (!block) return;
    var thought = block.querySelector('p');
    var comment = block.querySelector('.thinking-comment');
    if (thought && comment) comment.textContent = thinkingComment(thought.textContent);
  }

  updateThinkingComment();
  var liveThought = document.querySelector('.thinking.is-thinking p');
  if (liveThought && window.MutationObserver) {
    new MutationObserver(updateThinkingComment).observe(liveThought, {
      childList: true, characterData: true, subtree: true
    });
  }

  var shell = document.querySelector('.shell');
  var sidebarToggle = document.getElementById('sidebar-toggle');
  var SIDEBAR_KEY = 'imurun.design.sidebar';
  function setSidebar(collapsed) {
    if (!shell) return;
    shell.classList.toggle('sidebar-collapsed', collapsed);
    if (sidebarToggle) {
      sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
    try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch (e) { /* ignore */ }
  }
  if (sidebarToggle && shell) {
    sidebarToggle.addEventListener('click', function () {
      setSidebar(!shell.classList.contains('sidebar-collapsed'));
    });
    try {
      if (localStorage.getItem(SIDEBAR_KEY) === '1') setSidebar(true);
    } catch (e) { /* ignore */ }
  }

  /* Sidebar pages: Chat, Sessions, Connect, Skills, Persona and Settings. */
  var pageNav = document.querySelectorAll('.nav-item[data-page]');
  var pageViews = document.querySelectorAll('.page-view');
  var pageCrumb = document.getElementById('page-crumb');

  /* Settings is a full page with its own section rail. */
  var settingsTabs = document.querySelectorAll('[data-settings-section]');
  var settingsPanels = document.querySelectorAll('[data-settings-panel]');

  function settingsLabel(section) {
    var tab = document.querySelector('[data-settings-section="' + section + '"]');
    return tab ? tab.textContent.trim() : section;
  }

  function setSettingsSection(section) {
    settingsTabs.forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.settingsSection === section);
    });
    settingsPanels.forEach(function (panel) {
      panel.hidden = panel.dataset.settingsPanel !== section;
    });
    if (pageCrumb && !document.getElementById('page-settings').hidden) {
      pageCrumb.textContent = 'Settings / ' + settingsLabel(section);
    }
  }

  settingsTabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      setSettingsSection(tab.dataset.settingsSection);
    });
  });

  function renderChatCrumb() {
    if (!pageCrumb) return;
    var session = activeSession();
    var name = session ? (session.dataset.sessionName || 'session') : 'fix-login-bug';
    var id = session ? (session.dataset.sessionId || '') : '3f2a';
    pageCrumb.textContent = '';
    pageCrumb.appendChild(document.createTextNode('Chat '));
    var sep = document.createElement('span');
    sep.className = 'sep';
    sep.textContent = '/';
    pageCrumb.appendChild(sep);
    pageCrumb.appendChild(document.createTextNode(' '));
    var strong = document.createElement('strong');
    strong.textContent = name + (id ? '-' + id : '');
    pageCrumb.appendChild(strong);
  }

  function setPage(page) {
    pageViews.forEach(function (view) { view.hidden = view.id !== 'page-' + page; });
    pageNav.forEach(function (item) { item.classList.toggle('active', item.dataset.page === page); });
    if (page === 'chat') {
      renderChatCrumb();
    } else if (page === 'settings') {
      setSettingsSection('profile');
    } else if (pageCrumb) {
      var nav = document.querySelector('.nav-item[data-page="' + page + '"]');
      pageCrumb.textContent = nav ? nav.textContent : page;
    }
  }

  pageNav.forEach(function (item) {
    item.addEventListener('click', function () { setPage(item.dataset.page); });
  });

  function activateSession(id) {
    document.querySelectorAll('.session').forEach(function (session) {
      session.classList.toggle('active', session.dataset.sessionId === id);
    });
    document.querySelectorAll('.session-row').forEach(function (row) {
      row.classList.toggle('active', row.dataset.sessionId === id);
    });
    renderChatCrumb();
  }

  document.querySelectorAll('.session').forEach(function (session) {
    session.addEventListener('click', function () {
      activateSession(session.dataset.sessionId);
      setPage('chat');
    });
    session.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      activateSession(session.dataset.sessionId);
      setPage('chat');
    });
  });

  document.querySelectorAll('[data-open-session]').forEach(function (row) {
    row.addEventListener('click', function () {
      activateSession(row.dataset.openSession);
      setPage('chat');
    });
  });

  var newSession = document.getElementById('new-session');
  if (newSession) {
    newSession.addEventListener('click', function () {
      showNotification({
        tone: 'neutral', icon: 'info', kicker: 'Sessions',
        title: 'New session is a mock',
        text: 'The design lab does not create server sessions.'
      });
    });
  }

  function setConnector(card, connected) {
    if (!card) return;
    var chip = card.querySelector('[data-status]');
    var button = card.querySelector('[data-connect], [data-disconnect]');
    if (chip) {
      chip.textContent = connected ? 'Connected' : 'Not connected';
      chip.className = 'chip ' + (connected ? 'success' : 'neutral');
    }
    if (button) {
      button.textContent = connected ? 'Disconnect' : 'Connect';
      if (connected) {
        button.dataset.disconnect = card.dataset.connector;
        delete button.dataset.connect;
        button.className = 'btn';
      } else {
        button.dataset.connect = card.dataset.connector;
        delete button.dataset.disconnect;
        button.className = 'btn primary';
      }
    }
  }

  document.querySelectorAll('[data-connect], [data-disconnect]').forEach(function (button) {
    button.addEventListener('click', function () {
      var card = button.closest('.connector');
      if (!card) return;
      var connecting = button.hasAttribute('data-connect');
      var name = card.querySelector('.connector-name strong');
      setConnector(card, connecting);
      showNotification({
        tone: connecting ? 'success' : 'neutral',
        icon: connecting ? 'check' : 'info',
        kicker: connecting ? 'Connected' : 'Disconnected',
        title: (name ? name.textContent : 'Connector') + (connecting ? ' connected' : ' disconnected'),
        text: connecting ? 'The integration is ready. Tokens stay local.' : 'The local token was removed.'
      });
    });
  });

  document.querySelectorAll('[data-skill]').forEach(function (button) {
    button.addEventListener('click', function () {
      showNotification({
        tone: 'neutral', icon: 'info', kicker: 'Skills',
        title: button.dataset.skill,
        text: 'Opening a skill is a mock in the design lab.'
      });
    });
  });
  var newSkill = document.getElementById('new-skill');
  if (newSkill) {
    newSkill.addEventListener('click', function () {
      showNotification({
        tone: 'neutral', icon: 'info', kicker: 'Skills',
        title: 'New skill is a mock',
        text: 'The design lab does not write a SKILL.md.'
      });
    });
  }

  var savePersona = document.getElementById('save-persona');
  if (savePersona) {
    savePersona.addEventListener('click', function () {
      var nameInput = document.getElementById('persona-name');
      var toneInput = document.getElementById('persona-tone');
      var name = nameInput ? nameInput.value.trim() : 'Imurun';
      var tone = toneInput ? toneInput.value : 'Direct and calm';
      showNotification({
        tone: 'success', icon: 'check', kicker: 'Persona saved',
        title: (name || 'Imurun') + ' updated',
        text: 'New turns will use "' + tone + '".'
      });
    });
  }

  document.querySelectorAll('[data-save-settings]').forEach(function (button) {
    button.addEventListener('click', function () {
      var label = button.dataset.saveSettings || 'Settings';
      showNotification({
        tone: 'success', icon: 'check', kicker: 'Saved',
        title: label + ' saved',
        text: 'The design lab keeps this local; nothing was sent.'
      });
    });
  });

  document.querySelectorAll('[data-mock-action]').forEach(function (button) {
    button.addEventListener('click', function () {
      var label = button.dataset.mockAction || 'Action';
      showNotification({
        tone: 'neutral', icon: 'info', kicker: label,
        title: button.textContent.trim(),
        text: 'This is a design-lab mock; the server was not called.'
      });
    });
  });

  setPage('chat');

  var popup = null;
  try { popup = new URLSearchParams(location.search).get('popup'); } catch (e) { /* ignore */ }
  if (popup === '1') showApproval();

  var renameParam = null;
  try { renameParam = new URLSearchParams(location.search).get('rename'); } catch (e) { /* ignore */ }
  if (renameParam === '1') setTimeout(function () { openRename(activeSession()); }, 0);

  var menuParam = null;
  try { menuParam = new URLSearchParams(location.search).get('menu'); } catch (e) { /* ignore */ }
  if (menuParam === '1') {
    setTimeout(function () {
      var session = document.querySelector('.session');
      if (!session) return;
      var rect = session.getBoundingClientRect();
      showSessionMenu({
        preventDefault: function () {},
        clientX: rect.left + 22,
        clientY: rect.bottom + 6
      }, session);
    }, 0);
  }

  var pageParam = null;
  try { pageParam = new URLSearchParams(location.search).get('page'); } catch (e) { /* ignore */ }
  if (pageParam && document.getElementById('page-' + pageParam)) setPage(pageParam);

  var settingsParam = null;
  try { settingsParam = new URLSearchParams(location.search).get('settings'); } catch (e) { /* ignore */ }
  if (settingsParam) {
    var settingsSection = settingsParam === '1' ? 'customisation' : settingsParam;
    if (document.querySelector('[data-settings-section="' + settingsSection + '"]')) {
      setPage('settings');
      setSettingsSection(settingsSection);
    }
  }
})();
