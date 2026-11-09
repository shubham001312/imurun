/* Approval cards for tool calls that need the user's yes.
 *
 *   IMURUN.approvals.add(frame)            a `tool.approval_needed` frame (idempotent)
 *   IMURUN.approvals.resolved(frame, ok)   a `tool.approved` / `tool.denied` frame
 *   IMURUN.approvals.sync()                re-place cards (page, session or turn changed)
 *   IMURUN.approvals.restore()             GET /api/tools/approvals (start-up, reconnect)
 *   IMURUN.approvals.demo(turn, data)      a non-functional card for the gallery
 *   IMURUN.approvals.pendingCount()
 *
 * A running turn blocks on the card until POST /api/tools/approvals/{id}
 * answers it, the server's timeout passes (permissions.approvalTimeout,
 * default 180 s, counts as denied) or the turn is stopped. So the card must
 * be impossible to miss:
 *   - inline in the live reply when that reply is on screen, focused (unless
 *     you are typing), with a countdown bar;
 *   - otherwise as a toast that cannot be pushed out or dismissed without an
 *     answer, with an "Open chat" button;
 *   - the tab title reads "(1) Approval needed" while anything waits.
 * Keys: Enter approves (ignored for the first 700 ms, so an Enter meant for
 * the composer cannot approve), Esc denies.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var ENTER_GUARD_MS = 700;
  var RESOLVED_LINGER_MS = 1600;

  var pending = {};      /* approval_id -> record */
  var finished = {};     /* approval_id -> true, so a late restore cannot resurrect it */
  var ticker = null;
  var baseTitle = document.title || 'IMURUN';
  var categories = null; /* tool -> category, from GET /api/tools */
  var workspace = '';

  var CATEGORY = {
    time_now: 'info', location: 'info', weather: 'network', web_search: 'network', web_fetch: 'network',
    file_read: 'read', file_list: 'read', file_write: 'write', make_dir: 'write', file_move: 'write',
    file_copy: 'write', file_delete: 'delete', shell: 'shell', run_agent: 'agent'
  };
  var ACTION = {
    shell: 'run a command', file_write: 'write a file', make_dir: 'create a folder', file_move: 'move a file',
    file_copy: 'copy a file', file_delete: 'delete from the workspace', file_read: 'read a file',
    file_list: 'list a folder', web_fetch: 'open a web page', web_search: 'search the web',
    run_agent: 'start a sub-agent', weather: 'check the weather'
  };
  var RISK = {
    low: { label: 'Low risk', text: 'Reads only. Nothing is changed.' },
    medium: { label: 'Changes files', text: 'Changes files inside the workspace folder.' },
    high: { label: 'High risk', text: '' }
  };

  function api() { return window.__imurun_api; }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function clip(text, max) { return IM.activity ? IM.activity.clip(text, max) : String(text || '').slice(0, max); }

  function category(tool) {
    return (categories && categories[tool]) || CATEGORY[tool] || 'other';
  }

  function risk(tool, reasons) {
    var cat = category(tool);
    var why = (reasons || []).join(' ').toLowerCase();
    if (cat === 'shell') return { level: 'high', text: 'Shell is not a sandbox: a command can reach any file you can.' };
    if (cat === 'delete') return { level: 'high', text: 'Removes files from the workspace. There is no undo.' };
    if (cat === 'write' && /overwrit|replace/.test(why)) return { level: 'high', text: "Replaces an existing file's contents." };
    if (cat === 'write') return { level: 'medium', text: RISK.medium.text };
    if (cat === 'connector-write' || cat === 'mcp' || serviceOf(tool)) {
      if (/_send$|_post$|_comment$/.test(tool)) return { level: 'high', text: 'Acts in your account where other people will see it; it cannot be taken back.' };
      if (/delete|trash|purge|deploy|restart/.test(tool) || /destructive/.test(why)) return { level: 'high', text: 'Changes something in your account; there may be no undo.' };
      if (cat === 'connector-read') return { level: 'low', text: 'Reads your account. Nothing is changed.' };
      return { level: 'medium', text: 'Changes something in your account.' };
    }
    if (cat === 'read' || cat === 'info' || cat === 'network' || cat === 'agent') return { level: 'low', text: RISK.low.text };
    return { level: 'medium', text: 'Runs a tool that needs your approval.' };
  }

  function sentence(text) {
    var value = String(text || '').trim();
    if (!value) return '';
    value = value.charAt(0).toUpperCase() + value.slice(1);
    return /[.!?]$/.test(value) ? value : value + '.';
  }

  /* [label, value, kind] rows saying exactly what the call will touch. */
  function touches(tool, args) {
    var a = args || {};
    var out = [];
    function where(value) { return value ? String(value) : 'the workspace folder'; }
    function stringify(value) {
      if (typeof value === 'string') return value;
      try { return JSON.stringify(value); } catch (error) { return String(value); }
    }
    switch (tool) {
      case 'shell':
        out.push(['Command', String(a.command || ''), 'code']);
        out.push(['Runs in', where(a.cwd), 'mono']);
        if (a.timeout) out.push(['Time limit', a.timeout + ' s']);
        break;
      case 'file_write':
        out.push(['File', where(a.path), 'mono']);
        out.push(['How', a.mode === 'append' ? 'Add to the end of the file' : (a.mode === 'create' ? 'Create it (fails if it exists)' : 'Replace the whole file')]);
        if (a.content !== undefined) out.push(['Content', String(a.content), 'preview']);
        break;
      case 'make_dir':
        out.push(['Folder', where(a.path), 'mono']);
        break;
      case 'file_move':
      case 'file_copy':
        out.push([tool === 'file_move' ? 'Moves' : 'Copies', where(a.src), 'mono']);
        out.push(['To', where(a.dst), 'mono']);
        if (a.overwrite) out.push(['Overwrite', 'Yes, replaces what is at the destination']);
        break;
      case 'file_delete':
        out.push(['Deletes', where(a.path), 'mono']);
        if (a.recursive) out.push(['Scope', 'The folder and everything inside it']);
        break;
      case 'file_read':
      case 'file_list':
        out.push([tool === 'file_read' ? 'Reads' : 'Lists', where(a.path), 'mono']);
        break;
      case 'gmail_send':
      case 'gmail_draft':
        /* An email preview, not raw JSON. */
        if (a.to) out.push(['To', stringify(a.to)]);
        if (a.cc) out.push(['Cc', stringify(a.cc)]);
        if (a.bcc) out.push(['Bcc', stringify(a.bcc)]);
        out.push(['Subject', a.subject ? String(a.subject) : (a.reply_to || a.thread_id ? '(reply in the same thread)' : '(no subject)')]);
        if (a.body !== undefined) out.push(['Body', String(a.body), 'preview']);
        break;
      default:
        Object.keys(a).forEach(function (key) { out.push([key, stringify(a[key]), 'mono']); });
    }
    var cat = category(tool);
    if (workspace && (cat === 'read' || cat === 'write' || cat === 'delete' || cat === 'shell')) {
      out.push(['Workspace', workspace, 'mono']);
    }
    return out;
  }

  /* Connector and MCP tools (docs/connectors/REQUESTS.md F2). */
  var SERVICES = { gmail: 'Gmail', github: 'GitHub', slack: 'Slack', vercel: 'Vercel', render: 'Render',
    cloudflare: 'Cloudflare', supabase: 'Supabase', canva: 'Canva' };
  var CONNECTOR_ACTION = {
    gmail_send: 'send an email', gmail_draft: 'save a draft', gmail_trash: 'move an email to the trash',
    gmail_modify: 'change the labels of an email', github_create_issue: 'open an issue', github_comment: 'post a comment',
    slack_post: 'post a message', vercel_redeploy: 'redeploy a project', render_deploy: 'deploy a service',
    render_restart: 'restart a service', cloudflare_dns_create: 'add a DNS record', cloudflare_dns_update: 'change a DNS record',
    cloudflare_dns_delete: 'delete a DNS record', cloudflare_purge_cache: 'purge the cache', supabase_insert: 'insert rows'
  };

  function serviceOf(tool) {
    var prefix = String(tool || '').split('_')[0];
    return SERVICES[prefix] || (prefix === 'mcp' ? 'An MCP server' : '');
  }

  /* "Always ask" tools: the server never honours "remember" for them
   * (connectors/tool_policy.py level `always`), so the button is hidden.
   * Until the frame says so (`rememberable`, REQUESTS L2), decide from the
   * tool: every connector write except a Gmail draft or a label add, and
   * MCP tools the server calls destructive. */
  function rememberable(d) {
    /* The server now sends `rememberable` and `level` with every approval
     * (frame and GET /api/tools/approvals); the guess below is for older
     * servers only. */
    if (d.rememberable === false || d.tool === 'file_delete') return false;
    if (d.rememberable === true) return true;
    if (d.level === 'always' || d.level === 'deny') return false;
    if (d.level === 'read' || d.level === 'write' || d.level === 'ask' || d.level === 'allow') return true;
    var why = (d.reasons || []).join(' ').toLowerCase();
    if (d.tool === 'gmail_draft') return true;
    if (d.tool === 'gmail_modify') return /^adds label/.test(why);
    if (category(d.tool) === 'connector-write' || CONNECTOR_ACTION[d.tool]) return false;
    if (/^mcp_/.test(d.tool) && /destructive/.test(why)) return false;
    return true;
  }

  function verbFor(tool) {
    var t = String(tool || '');
    if (/_send$/.test(t)) return 'Send';
    if (/_post$|_comment$/.test(t)) return 'Post';
    if (/deploy/.test(t)) return 'Deploy';
    if (/_restart$/.test(t)) return 'Restart';
    if (/_delete$|_trash$|delete/.test(t)) return 'Delete';
    return 'Approve';
  }

  function titleFor(tool) {
    var service = serviceOf(tool);
    if (service && (CONNECTOR_ACTION[tool] || category(tool) === 'connector-write' || /^mcp_/.test(tool))) {
      return service + ' wants to ' + (CONNECTOR_ACTION[tool] || 'run ' + tool) + '. Allow it?';
    }
    return 'Allow ' + tool + ' to ' + (ACTION[tool] || 'run') + '?';
  }

  /* ------------------------------------------------------------ the card */

  function buildCard(record) {
    var d = record.data;
    var level = risk(d.tool, d.reasons);
    var id = 'approval-' + String(d.approval_id || 'demo').replace(/[^a-z0-9_-]/gi, '');
    var card = document.createElement('section');
    card.className = 'approval-card is-' + level.level;
    card.tabIndex = -1;
    card.setAttribute('role', 'alertdialog');
    card.setAttribute('aria-labelledby', id + '-title');
    card.setAttribute('aria-describedby', id + '-why');
    card.innerHTML =
      '<header class="approval-head">' +
        '<span class="approval-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3.5l8.5 15H3.5z"/><path d="M12 9.5v4.2"/><circle cx="12" cy="16.6" r=".9" fill="currentColor" stroke="none"/></svg></span>' +
        '<div class="approval-titles"><div class="approval-kicker">Approval needed</div><h3 class="approval-title"></h3></div>' +
        '<span class="approval-risk"></span>' +
      '</header>' +
      '<p class="approval-why"></p>' +
      '<dl class="approval-touch"></dl>' +
      '<div class="approval-timer" aria-hidden="true"><span></span></div>' +
      '<p class="approval-countdown" aria-live="off"></p>' +
      '<div class="approval-actions">' +
        '<button class="btn" type="button" data-approval-deny>Deny <kbd>Esc</kbd></button>' +
        '<button class="btn" type="button" data-approval-remember>Approve and remember for this session</button>' +
        '<button class="btn primary" type="button" data-approval-approve>Approve <kbd>Enter</kbd></button>' +
      '</div>' +
      '<p class="approval-note"></p>';
    var title = card.querySelector('.approval-title');
    title.id = id + '-title';
    title.textContent = titleFor(d.tool);
    card.querySelector('.approval-risk').textContent = RISK[level.level].label;
    var why = card.querySelector('.approval-why');
    why.id = id + '-why';
    why.textContent = [sentence((d.reasons || []).join('; ')), level.text].filter(Boolean).join(' ');
    var list = card.querySelector('.approval-touch');
    touches(d.tool, d.args).forEach(function (row) {
      var dt = document.createElement('dt');
      dt.textContent = row[0];
      var dd = document.createElement('dd');
      if (row[2] === 'code' || row[2] === 'preview') {
        var pre = document.createElement('pre');
        pre.className = 'approval-code' + (row[2] === 'preview' ? ' is-preview' : '');
        var value = row[1];
        if (row[2] === 'preview' && value.length > 600) value = value.slice(0, 600) + '\n… (' + (row[1].length - 600) + ' more characters)';
        pre.textContent = value;
        dd.appendChild(pre);
      } else {
        dd.textContent = row[1];
        if (row[2] === 'mono') dd.className = 'mono';
      }
      list.appendChild(dt);
      list.appendChild(dd);
    });
    var remember = card.querySelector('[data-approval-remember]');
    var note = card.querySelector('.approval-note');
    var verb = verbFor(d.tool);
    if (verb !== 'Approve') card.querySelector('[data-approval-approve]').innerHTML = verb + ' <kbd>Enter</kbd>';
    if (d.tool === 'file_delete') {
      remember.hidden = true;
      note.textContent = 'Deletes are never remembered: each one asks.';
    } else if (!rememberable(d)) {
      remember.hidden = true;
      note.textContent = (serviceOf(d.tool) || 'This tool') + ' actions like this always ask, in every permission mode, and are never remembered.';
    } else {
      remember.title = 'Runs ' + d.tool + ' without asking again in this session, until the server restarts.';
      note.textContent = 'Remembering lasts until the server restarts. Change what asks in Settings → Permissions.';
    }
    card.addEventListener('click', function (event) {
      var target = event.target.closest ? event.target.closest('button') : null;
      if (!target) return;
      if (target.hasAttribute('data-approval-approve')) answer(record, true, false);
      else if (target.hasAttribute('data-approval-remember')) answer(record, true, true);
      else if (target.hasAttribute('data-approval-deny')) answer(record, false, false);
    });
    card.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      if (event.target.closest && event.target.closest('button, a, input, textarea, select, summary')) return;
      event.preventDefault();
      if (Date.now() - (record.mountedAt || 0) < ENTER_GUARD_MS) return;
      answer(record, true, false);
    });
    return card;
  }

  function countdownText(record) {
    if (record.demo) return 'Denied automatically in 2:22 if nobody answers.';
    var left = Math.max(0, Math.round((record.deadline - Date.now()) / 1000));
    if (!left) return 'Time is up: the server denies the call.';
    var m = Math.floor(left / 60);
    var s = left % 60;
    return 'Denied automatically in ' + m + ':' + (s < 10 ? '0' : '') + s + ' if nobody answers.';
  }

  function paintTimer(record) {
    var left = record.demo ? 142000 : Math.max(0, record.deadline - Date.now());
    var share = record.total > 0 ? Math.max(0, Math.min(1, left / record.total)) : 0;
    var urgent = left < 20000;
    [record.inline, record.toastNode].forEach(function (node) {
      if (!node) return;
      var text = node.querySelector('.approval-countdown');
      if (text) text.textContent = countdownText(record);
      var bar = node.querySelector('.approval-timer > span');
      if (bar) bar.style.transform = 'scaleX(' + share.toFixed(3) + ')';
      node.classList.toggle('is-urgent', urgent);
    });
  }

  function tick() {
    var any = false;
    Object.keys(pending).forEach(function (id) {
      var record = pending[id];
      if (record.state !== 'pending') return;
      any = true;
      paintTimer(record);
    });
    if (!any && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  function ensureTicker() {
    if (!ticker) ticker = setInterval(tick, 1000);
  }

  function updateTitle() {
    var n = pendingCount();
    document.title = n ? '(' + n + ') Approval needed · ' + baseTitle : baseTitle;
    document.body.classList.toggle('has-approval', n > 0);
  }

  function pendingCount() {
    return Object.keys(pending).filter(function (id) { return pending[id].state === 'pending'; }).length;
  }

  /* ------------------------------------------------------------ answering */

  function setBusy(record, text) {
    [record.inline, record.toastNode].forEach(function (node) {
      if (!node) return;
      node.querySelectorAll('button').forEach(function (button) {
        if (button.classList.contains('notif-close')) return;
        button.disabled = Boolean(text);
      });
      var line = node.querySelector('.approval-countdown');
      if (line && text) line.textContent = text;
    });
  }

  function answer(record, approved, remember) {
    if (record.busy || record.state !== 'pending') return;
    if (record.demo) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Preview', title: 'Nothing was sent', text: 'This card is part of the response gallery.' });
      return;
    }
    record.busy = true;
    setBusy(record, approved ? 'Approving…' : 'Denying…');
    api().answerApproval(record.data.approval_id, approved, remember).then(function () {
      record.busy = false;
      finish(record.data.approval_id, approved, approved ? (remember ? 'Approved for this session' : 'Approved') : 'Denied');
    }, function (error) {
      record.busy = false;
      if (error && error.status === 404) {
        finish(record.data.approval_id, false, 'Already answered or expired');
        return;
      }
      setBusy(record, null);
      paintTimer(record);
      toast({ tone: 'error', icon: 'alert', kicker: 'Approval', title: 'Your answer did not reach the server',
        text: (error && error.message ? error.message : 'No answer') + '. The card is still open; try again.' });
    });
  }

  /* The card is answered (here, elsewhere, by the timeout or by Stop). */
  function finish(id, approved, text) {
    var record = pending[id];
    finished[id] = true;
    if (!record || record.state !== 'pending') return;
    record.state = 'done';
    [record.inline, record.toastNode].forEach(function (node) {
      if (!node) return;
      node.classList.add('is-resolved', approved ? 'is-approved' : 'is-denied');
      var actions = node.querySelector('.approval-actions, .notif-actions');
      if (actions) actions.hidden = true;
      var line = node.querySelector('.approval-countdown');
      if (line) line.textContent = text;
    });
    util.announce(text + ': ' + record.data.tool);
    updateTitle();
    setTimeout(function () {
      if (record.inline) {
        var inline = record.inline;
        if (util.reducedMotion() || !inline.animate) inline.remove();
        else {
          var anim = inline.animate([{ opacity: 1, height: inline.offsetHeight + 'px' }, { opacity: 0, height: '0px', marginTop: '0px', marginBottom: '0px', paddingTop: '0px', paddingBottom: '0px' }],
            { duration: 240, easing: 'cubic-bezier(.2, .8, .2, 1)' });
          anim.onfinish = function () { inline.remove(); };
        }
      }
      if (record.toastNode && IM.toast) IM.toast.dismiss(record.toastNode);
      delete pending[id];
    }, RESOLVED_LINGER_MS);
  }

  function resolved(event, approved) {
    if (!event || !event.approval_id) return;
    var reason = event.reason || '';
    var text = approved
      ? (event.remember ? 'Approved for this session' : 'Approved')
      : 'Denied' + (reason && reason !== 'declined by the user' ? ': ' + reason : '');
    finish(event.approval_id, approved, text);
  }

  /* ------------------------------------------------------------ placement */

  function liveTurn() {
    var turn = IM.state.turn;
    return turn && !turn.done && turn.node ? turn : null;
  }

  function chatShowing(turn) {
    return Boolean(turn && IM.shell && IM.shell.currentPage() === 'chat' &&
      IM.state.activeSessionId === turn.sessionId && turn.node.isConnected &&
      document.visibilityState !== 'hidden');
  }

  function mountInline(record, turn) {
    var card = buildCard(record);
    record.inline = card;
    record.mountedAt = Date.now();
    var before = turn.bubble && turn.bubble.parentNode === turn.node ? turn.bubble : null;
    turn.node.insertBefore(card, before);
    if (!util.reducedMotion()) card.classList.add('is-entering');
    paintTimer(record);
    if (chatShowing(turn)) {
      if (card.scrollIntoView) card.scrollIntoView({ block: 'nearest', behavior: util.reducedMotion() ? 'auto' : 'smooth' });
      var active = document.activeElement;
      var typing = util.isTypingTarget(active) && !(active && active.tagName === 'TEXTAREA' && !active.value.trim() && active.closest('.composer'));
      if (!typing) {
        try { card.focus({ preventScroll: true }); } catch (error) { card.focus(); }
      }
    }
  }

  function mountToast(record) {
    var d = record.data;
    var level = risk(d.tool, d.reasons);
    var fields = touches(d.tool, d.args).slice(0, 3).map(function (row) {
      return { k: row[0], v: clip(row[1], 160) };
    });
    var actions = [
      { label: 'Deny', className: 'cancel', onClick: function () { answer(record, false, false); } }
    ];
    if (d.session_id && IM.shell) {
      actions.push({ label: 'Open chat', className: 'cancel', onClick: function () { IM.shell.activateSession(d.session_id); } });
    }
    actions.push({ label: verbFor(d.tool), className: 'danger', onClick: function () { answer(record, true, false); } });
    var node = toast({
      id: 'approval-' + d.approval_id, tone: 'neutral', icon: 'alert',
      kicker: 'Approval needed · ' + RISK[level.level].label,
      title: titleFor(d.tool),
      text: [sentence((d.reasons || []).join('; ')), level.text].filter(Boolean).join(' '),
      fields: fields, actions: actions, role: 'alertdialog', keep: true, timeout: 0, dismissible: false
    });
    if (!node) return;
    node.classList.add('approval-toast', 'is-' + level.level);
    var body = node.querySelector('.notif-body');
    var bar = document.createElement('div');
    bar.className = 'approval-timer';
    bar.setAttribute('aria-hidden', 'true');
    bar.appendChild(document.createElement('span'));
    var line = document.createElement('p');
    line.className = 'approval-countdown';
    var actionsRow = body ? body.querySelector('.notif-actions') : null;
    if (body) {
      body.insertBefore(bar, actionsRow);
      body.insertBefore(line, actionsRow);
    }
    record.toastNode = node;
    paintTimer(record);
  }

  /* Put each pending card where it will be seen. */
  function sync() {
    var turn = liveTurn();
    Object.keys(pending).forEach(function (id) {
      var record = pending[id];
      if (record.state !== 'pending' || record.demo) return;
      var mine = turn && turn.turnId && turn.turnId === record.data.turn_id;
      if (mine && !record.inline) mountInline(record, turn);
      var seen = mine && chatShowing(turn);
      if (!seen && !record.toastNode) mountToast(record);
      else if (seen && record.toastNode) {
        if (IM.toast) IM.toast.dismiss(record.toastNode, true);
        record.toastNode = null;
      }
    });
  }

  function add(data) {
    if (!data || !data.approval_id || finished[data.approval_id] || pending[data.approval_id]) return;
    var seconds = Number(data.expires_in);
    if (!isFinite(seconds) || seconds <= 0) seconds = 180;
    pending[data.approval_id] = {
      data: data, state: 'pending',
      total: seconds * 1000, deadline: Date.now() + seconds * 1000
    };
    learn();
    ensureTicker();
    updateTitle();
    sync();
    util.announce('Approval needed: ' + titleFor(data.tool));
  }

  function restore() {
    if (!api() || !api().listApprovals) return;
    api().listApprovals().then(function (list) {
      (list || []).forEach(add);
    }, function () { /* an older server: nothing to restore */ });
  }

  /* Categories and the workspace path, once, for nicer cards. */
  var learning = false;
  function learn() {
    if (learning || !api()) return;
    learning = true;
    if (api().getTools) {
      api().getTools().then(function (view) {
        categories = {};
        ((view && view.tools) || []).forEach(function (tool) { if (tool && tool.name) categories[tool.name] = tool.category; });
        if (view && view.permissions && view.permissions.workspace) workspace = view.permissions.workspace;
      }, function () {});
    }
  }

  /* Gallery: the real card markup, buttons answer with a "preview" notice. */
  function demo(turn, data) {
    var record = { data: Object.assign({ approval_id: 'demo' }, data || {}), state: 'pending', demo: true, total: 180000, deadline: 0 };
    var card = buildCard(record);
    record.inline = card;
    record.mountedAt = Date.now();
    turn.node.insertBefore(card, turn.bubble);
    paintTimer(record);
    return card;
  }

  /* Esc denies the focused card, or the card in the reply on screen when
   * nothing else has focus (capture phase, so it runs before Esc = Stop). */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (IM.slash && IM.slash.isOpen && IM.slash.isOpen()) return;
    var active = document.activeElement;
    var card = active && active.closest ? active.closest('.approval-card') : null;
    var record = null;
    Object.keys(pending).forEach(function (id) {
      var r = pending[id];
      if (r.state !== 'pending') return;
      if (card && r.inline === card) record = r;
    });
    if (!record && !card) {
      var free = !active || active === document.body || (active.tagName === 'TEXTAREA' && active.closest && active.closest('.composer'));
      var turn = liveTurn();
      if (free && chatShowing(turn)) {
        Object.keys(pending).forEach(function (id) {
          var r = pending[id];
          if (r.state === 'pending' && r.inline && r.inline.isConnected && !r.demo) record = r;
        });
      }
    }
    if (!record && card) {
      /* A gallery card: still swallow the key so Esc does not stop anything. */
      event.preventDefault();
      return;
    }
    if (!record) return;
    event.preventDefault();
    event.stopPropagation();
    answer(record, false, false);
  }, true);

  function init() {
    baseTitle = document.title || 'IMURUN';
    IM.bus.on('server:tool.approval_needed', add);
    IM.bus.on('server:tool.approved', function (event) { resolved(event, true); });
    IM.bus.on('server:tool.denied', function (event) { resolved(event, false); });
    IM.bus.on('server:permissions', function (event) { if (event && event.workspace) workspace = event.workspace; });
    IM.bus.on('page', sync);
    IM.bus.on('session.active', sync);
    IM.bus.on('turn.settled', sync);
    IM.bus.on('events.status', function (status) { if (status === 'open') restore(); });
    document.addEventListener('visibilitychange', sync);
    restore();
  }

  IM.approvals = {
    add: add,
    resolved: resolved,
    sync: sync,
    restore: restore,
    demo: demo,
    pendingCount: pendingCount,
    risk: risk,
    touches: touches,
    rememberable: rememberable,
    titleFor: titleFor,
    verbFor: verbFor
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
