/* Tool activity, verification and sources for one reply.
 *
 *   IMURUN.activity.handles(type)          true for the frames this module renders
 *   IMURUN.activity.attach(turn)           -> controller for one chat.js Turn
 *   IMURUN.activity.finish(turn, turnEnd)  sources row, "Not verified live",
 *                                          skills / missed tool-call footer
 *   IMURUN.activity.noteVerification(turn, frame)   keep `verification`/`sources`
 *   IMURUN.activity.stripCalls(text)       drop the server's "Using `tool` {...}" lines
 *   IMURUN.activity.stripNotes(text)       drop "[note: unable to verify]" style notes
 *   IMURUN.activity.argSummary(tool, args) one-line argument summary
 *
 * Rows live inside the thinking panel, in the trace block of the step that
 * called the tool (Turn.traceBlock), so a tool appears right after the
 * thought that asked for it. One row per `tool.call`; `tool.result` turns it
 * into ok / failed / denied with latency and a collapsible result preview.
 * Sub-agent runs (`agent.start` / `agent.end`) nest their own calls under
 * the `run_agent` row that started them. Nothing here invents data: a turn
 * whose frames were missed shows only what `turn.end` reports.
 *
 * ES2017, no build step (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;

  var TYPES = ['tool.call', 'tool.result', 'tool.approval_needed', 'tool.approved', 'tool.denied',
    'agent.start', 'agent.end', 'skill.used',
    /* Rendered by research.js; standalone verification/sources frames are kept. */
    'research.plan', 'research.search', 'research.fetch', 'research.done', 'verification', 'sources'];

  /* What the pill says while a tool runs. */
  var VERBS = {
    web_search: 'Searching the web', web_fetch: 'Reading a web page', weather: 'Checking the weather',
    time_now: 'Checking the clock', location: 'Checking the location', file_read: 'Reading a file',
    file_list: 'Listing files', file_write: 'Writing a file', make_dir: 'Creating a folder',
    file_move: 'Moving a file', file_copy: 'Copying a file', file_delete: 'Deleting',
    shell: 'Running a command', run_agent: 'Running a sub-agent'
  };
  var MAIN_ARG = ['query', 'url', 'command', 'path', 'task', 'place', 'timezone', 'src'];

  function clip(text, max) {
    var value = String(text === undefined || text === null ? '' : text).replace(/\s+/g, ' ').trim();
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  }

  function argSummary(tool, args) {
    if (!args || typeof args !== 'object') return '';
    if ((tool === 'file_move' || tool === 'file_copy') && args.src) return clip(args.src, 60) + ' → ' + clip(args.dst || '?', 60);
    for (var i = 0; i < MAIN_ARG.length; i += 1) {
      var value = args[MAIN_ARG[i]];
      if (value !== undefined && value !== null && value !== '') return clip(value, 90);
    }
    if (!Object.keys(args).length) return '';
    try { return clip(JSON.stringify(args), 90); } catch (error) { return ''; }
  }

  function latency(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return '';
    if (n < 1000) return Math.round(n) + ' ms';
    return (n / 1000).toFixed(n < 10000 ? 1 : 0) + ' s';
  }

  /* "Using `web_search` {...}" lines that turns.py puts in a tool step's
   * text; the rows show the same call with its outcome. */
  function stripCalls(text) {
    return String(text || '').replace(/^Using `[^`\n]+`[^\n]*$/gm, '').replace(/\n{3,}/g, '\n\n').trim();
  }

  /* Bracketed verification notes a model sometimes appends. Applied to the
   * final text only (never mid-stream, where a half-written bracket would
   * flicker); the verification badge carries the meaning instead. */
  /* Square brackets only: a parenthesis is too often part of a real sentence. */
  var NOTE_RE = /[ \t]*\[\s*(?:note\s*:\s*)?[^\]\n]{0,60}?\b(?:unverified|not verified|unable to verify|could not (?:be )?verif(?:y|ied)|cannot verify|can't verify|couldn't verify|no live (?:data|source)s?)\b[^\]\n]{0,160}\](?!\()/gi;
  function stripNotes(text) {
    var value = String(text || '');
    if (!/verif|live (?:data|source)/i.test(value)) return value;
    return value.replace(NOTE_RE, '').replace(/[ \t]+$/gm, '').replace(/\n{3,}/g, '\n\n').trim();
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------ controller */

  function Activity(turn) {
    this.turn = turn;
    this.rows = {};      /* call_id -> record */
    this.records = [];   /* every record, in arrival order */
    this.agents = [];    /* open sub-agent records (a stack) */
    this.skills = [];
    this.count = 0;      /* tool calls shown (any depth, plus pre-fetch) */
    this.opened = false;
  }

  Activity.prototype.list = function (step) {
    var turn = this.turn;
    var index = step;
    if (index === undefined || index === null || index === '' || !isFinite(Number(index))) {
      index = turn.order.length ? turn.order[turn.order.length - 1] : 1;
    }
    var block = turn.traceBlock(Number(index));
    var list = block.querySelector(':scope > .trace-tools');
    if (!list) {
      list = el('ul', 'trace-tools');
      list.setAttribute('aria-label', 'Tools used in this step');
      block.appendChild(list);
    }
    return list;
  };

  Activity.prototype.parentList = function (event) {
    var depth = Number(event.depth) || 0;
    if (depth > 0) {
      for (var i = this.agents.length - 1; i >= 0; i -= 1) {
        if (this.agents[i].agentDepth === depth) return this.agents[i].children;
      }
    }
    return this.list(event.source === 'prefetch' ? 0 : event.step);
  };

  Activity.prototype.row = function (event) {
    var record = {
      id: event.call_id || '', tool: event.tool || 'tool', depth: Number(event.depth) || 0,
      args: event.args || {}, state: 'running', source: event.source || 'model'
    };
    var li = el('li', 'tool-row is-running');
    li.dataset.depth = String(record.depth);
    var head = el('div', 'tool-head');
    head.appendChild(el('span', 'tool-dot'));
    head.lastChild.setAttribute('aria-hidden', 'true');
    var what = el('span', 'tool-what');
    what.appendChild(el('code', 'tool-name', record.tool));
    var summary = argSummary(record.tool, record.args);
    record.argsEl = el('span', 'tool-args', summary);
    if (summary) what.appendChild(record.argsEl);
    head.appendChild(what);
    record.statusEl = el('span', 'tool-status', record.source === 'prefetch' ? 'looked up' : 'running');
    head.appendChild(record.statusEl);
    li.appendChild(head);
    record.el = li;
    this.parentList(event).appendChild(li);
    this.records.push(record);
    if (record.id) this.rows[record.id] = record;
    this.count += 1;
    return record;
  };

  Activity.prototype.setState = function (record, state, text) {
    record.state = state;
    record.el.className = 'tool-row is-' + state + (record.agentDepth ? ' has-agent' : '');
    record.statusEl.textContent = text;
  };

  Activity.prototype.preview = function (record, label, text) {
    var body = String(text || '').trim();
    if (!body) return;
    var details = record.resultEl;
    if (!details) {
      details = el('details', 'tool-result');
      details.appendChild(el('summary', '', label));
      details.appendChild(el('pre', 'tool-output'));
      record.el.insertBefore(details, record.children || null);
      record.resultEl = details;
    }
    details.querySelector('summary').textContent = label;
    details.querySelector('pre').textContent = body;
  };

  /* Show the trace while tools run: the pill opens once (unless the reader
   * already toggled it) and folds again when the answer starts. */
  Activity.prototype.reveal = function () {
    var turn = this.turn;
    if (turn.foldedAt && !turn.done) turn.unfold();
    turn.details.classList.add('has-trace');
    if (!this.opened && !turn.userToggled && !turn.done && !turn.details.open) {
      this.opened = true;
      turn.details.open = true;
    }
    var body = turn.body;
    if (turn.details.open) body.scrollTop = body.scrollHeight;
  };

  Activity.prototype.onEvent = function (event) {
    var turn = this.turn;
    var record = event.call_id ? this.rows[event.call_id] : null;
    switch (event.type) {
      case 'tool.call': {
        if (!record) record = this.row(event);
        if (record.depth === 0 && !turn.done) {
          turn.setLabel(VERBS[record.tool] || ('Using ' + record.tool));
          if (turn.preview) turn.preview.textContent = record.tool + (argSummary(record.tool, record.args) ? ' · ' + argSummary(record.tool, record.args) : '');
          if (record.tool === 'web_search' && IM.chat && IM.chat.searchFor) {
            IM.chat.searchFor(turn, { type: 'web_search.start', query: record.args.query || '' });
          }
        }
        this.reveal();
        break;
      }
      case 'tool.approval_needed':
        if (record) this.setState(record, 'waiting', 'waiting for your approval');
        if (!turn.done) turn.setLabel('Waiting for your approval');
        if (IM.approvals && !turn.demo) IM.approvals.add(event);
        break;
      case 'tool.approved':
        if (record) this.setState(record, 'running', event.remember ? 'approved for this session' : 'approved, running');
        if (!turn.done) turn.setLabel(VERBS[event.tool] || 'Thinking');
        if (IM.approvals && !turn.demo) IM.approvals.resolved(event, true);
        break;
      case 'tool.denied':
        if (record) this.setState(record, 'denied', 'denied');
        if (record) this.preview(record, 'Why', event.reason || 'declined');
        if (IM.approvals && !turn.demo) IM.approvals.resolved(event, false);
        break;
      case 'tool.result': {
        if (!record) record = this.row(event);
        var ms = latency(event.latency_ms);
        if (event.ok) {
          this.setState(record, 'ok', (record.source === 'prefetch' ? 'looked up' : 'done') + (ms ? ' · ' + ms : ''));
          this.preview(record, 'Result', event.content);
        } else if (event.decision && event.decision !== 'allow') {
          this.setState(record, 'denied', event.decision === 'ask' ? 'not approved' : 'refused');
          this.preview(record, 'Why', event.error || event.content);
        } else {
          this.setState(record, 'failed', (event.offline ? 'offline' : 'failed') + (ms ? ' · ' + ms : ''));
          this.preview(record, 'Error', event.error || event.content);
        }
        if (record.tool === 'web_search' && record.depth === 0 && record.source !== 'prefetch' && IM.chat && IM.chat.searchFor) {
          IM.chat.searchFor(turn, { type: 'web_search.done', ok: Boolean(event.ok) });
        }
        if (record.depth === 0 && !turn.done && !turn.firstTokenAt) turn.setLabel('Thinking');
        this.reveal();
        break;
      }
      case 'agent.start': {
        var depth = Number(event.depth) || 1;
        var parent = null;
        for (var i = this.records.length - 1; i >= 0; i -= 1) {
          var candidate = this.records[i];
          if (candidate.tool === 'run_agent' && candidate.depth === depth - 1 && !candidate.agentDepth &&
              (candidate.state === 'running' || candidate.state === 'waiting')) {
            parent = candidate;
            break;
          }
        }
        if (!parent) parent = this.row({ tool: 'run_agent', depth: depth - 1, step: event.step, args: { task: event.task } });
        parent.agentDepth = depth;
        parent.el.classList.add('has-agent');
        parent.argsEl.textContent = clip(event.task || '', 140);
        if (!parent.argsEl.parentNode) parent.el.querySelector('.tool-what').appendChild(parent.argsEl);
        var tools = Array.isArray(event.tools) ? event.tools : [];
        parent.statusEl.textContent = 'sub-agent running';
        var note = el('p', 'tool-agent-note', 'Sub-agent' + (tools.length ? ' with ' + tools.join(', ') : '') +
          (event.max_steps ? ' · up to ' + event.max_steps + ' steps' : ''));
        parent.el.appendChild(note);
        parent.children = el('ul', 'tool-children');
        parent.children.setAttribute('aria-label', 'Sub-agent tool calls');
        parent.el.appendChild(parent.children);
        this.agents.push(parent);
        if (!turn.done) turn.setLabel('Running a sub-agent');
        this.reveal();
        break;
      }
      case 'agent.end': {
        var agent = null;
        for (var j = this.agents.length - 1; j >= 0; j -= 1) {
          if (this.agents[j].agentDepth === (Number(event.depth) || 1)) {
            agent = this.agents.splice(j, 1)[0];
            break;
          }
        }
        if (!agent) break;
        var bits = [];
        if (event.steps !== undefined) bits.push(event.steps + (Number(event.steps) === 1 ? ' step' : ' steps'));
        if (event.tool_calls !== undefined) bits.push(event.tool_calls + (Number(event.tool_calls) === 1 ? ' tool call' : ' tool calls'));
        if (event.elapsed) bits.push(util.formatSeconds(Number(event.elapsed)));
        var ok = event.status === 'done' || event.status === 'max-steps';
        var line = el('p', 'tool-agent-end ' + (ok ? 'is-ok' : 'is-failed'),
          'Sub-agent ' + (event.status === 'error' ? 'failed' : (event.status || 'finished')) + (bits.length ? ' · ' + bits.join(' · ') : ''));
        agent.el.appendChild(line);
        if (event.summary || event.error) this.preview(agent, event.error ? 'Error' : 'Summary', event.error || event.summary);
        break;
      }
      case 'skill.used':
        this.addSkills(event.skills);
        break;
      case 'verification':
      case 'sources':
        noteVerification(turn, event);
        break;
      default:
        if (IM.research && IM.research.handles(event.type)) IM.research.onEvent(turn, event);
        break;
    }
  };

  Activity.prototype.addSkills = function (skills) {
    var self = this;
    (Array.isArray(skills) ? skills : []).forEach(function (name) {
      var id = String(name || '').replace(/^\//, '');
      if (id && self.skills.indexOf(id) === -1) self.skills.push(id);
    });
    if (!this.skills.length) return;
    var body = this.turn.body;
    var row = body.querySelector(':scope > .trace-skills');
    if (!row) {
      row = el('div', 'trace-skills');
      var empty = body.querySelector('.thinking-empty');
      if (empty) empty.remove();
      body.insertBefore(row, body.firstChild);
      this.turn.details.classList.add('has-trace');
    }
    row.innerHTML = '<span class="trace-skills-k">Skills</span>' + this.skills.map(function (id) {
      return '<span class="skill-chip">/' + util.escapeHtml(id) + '</span>';
    }).join('');
  };

  /* The turn ended: a row still running never got its result. */
  Activity.prototype.settle = function () {
    var self = this;
    this.records.forEach(function (record) {
      if (record.state === 'running' || record.state === 'waiting') self.setState(record, 'interrupted', 'no result');
    });
    this.agents = [];
  };

  /* Calls grouped by tool for the footer, in first-use order. */
  Activity.prototype.usage = function () {
    var groups = [];
    var byTool = {};
    this.records.forEach(function (record) {
      var group = byTool[record.tool];
      if (!group) {
        group = byTool[record.tool] = { tool: record.tool, n: 0, ok: 0, failed: 0, denied: 0 };
        groups.push(group);
      }
      group.n += 1;
      if (record.state === 'ok') group.ok += 1;
      else if (record.state === 'denied') group.denied += 1;
      else group.failed += 1;
    });
    return groups;
  };

  /* ----------------------------------------------------- verification */

  function noteVerification(turn, event) {
    if (!turn || !event) return;
    if (typeof event.verification === 'string' && event.verification) turn.verification = event.verification;
    if (Array.isArray(event.sources) && event.sources.length) turn.sources = event.sources;
  }

  function safeUrl(value) {
    try {
      var url = new URL(String(value || ''));
      return url.protocol === 'http:' || url.protocol === 'https:' ? url : null;
    } catch (error) {
      return null;
    }
  }

  function hue(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  function normaliseSources(list) {
    var seen = {};
    var out = [];
    (Array.isArray(list) ? list : []).forEach(function (item) {
      if (!item) return;
      var url = safeUrl(item.url || item.link || item.href);
      if (!url || seen[url.href]) return;
      seen[url.href] = true;
      var domain = String(item.domain || url.hostname || '').replace(/^www\./, '');
      out.push({
        url: url.href,
        domain: domain,
        title: String(item.title || item.name || domain || url.href),
        snippet: String(item.snippet || item.description || item.content || ''),
        provider: String(item.provider || '')
      });
    });
    return out;
  }

  var CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4l3 3 6-6.4"/></svg>';
  var INFO = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 7.2v4"/><circle cx="8" cy="4.9" r=".6" fill="currentColor" stroke="none"/></svg>';
  var UNVERIFIED_TIP = "No live source could be reached; this is from the model's knowledge, which may be out of date.";

  function badge(verification, count) {
    if (verification !== 'verified' && verification !== 'unverified') return null;
    var node = el('span', 'verify-badge is-' + verification);
    node.tabIndex = 0;
    if (verification === 'verified') {
      var text = 'Verified' + (count ? ' · ' + count + (count === 1 ? ' source' : ' sources') : '');
      node.innerHTML = CHECK + '<span></span>';
      node.lastChild.textContent = text;
      node.title = 'This answer used live tool results' + (count ? ', listed under Sources.' : '.');
      node.setAttribute('aria-label', text + '. This answer used live tool results.');
    } else {
      node.innerHTML = INFO + '<span>Not verified live</span>';
      node.title = UNVERIFIED_TIP;
      node.setAttribute('aria-label', 'Not verified live. ' + UNVERIFIED_TIP);
    }
    return node;
  }

  /* One collapsed row under the answer: "Searched the web · 4 sources",
   * with a check when the answer is verified (reply.js draws the toggle).
   * Open, it lists each source on one line: initial, title, domain, and the
   * snippets behind "Show snippets". `[n]` citations point at item n. */
  function sourcesRow(sources, options) {
    var o = options || {};
    var wrap = el('section', 'msg-sources reply-sources');
    wrap.setAttribute('aria-label', 'Sources');
    var panel = el('div', 'reply-source-panel');
    var list = el('ol', 'msg-sources-chips reply-source-list');
    var withSnippets = sources.some(function (s) { return s.snippet; });
    sources.forEach(function (source, index) {
      var item = el('li', 'reply-source');
      var chip = el('a', 'source-chip');
      chip.dataset.n = String(index + 1);
      chip.href = source.url;
      chip.target = '_blank';
      chip.rel = 'noopener noreferrer';
      chip.title = source.title + '\n' + source.url;
      /* Remote favicons are blocked by the CSP (img-src 'self'): a
       * domain-initial avatar stands in for them. */
      var avatar = el('span', 'source-avatar', (source.domain.charAt(0) || '?').toUpperCase());
      avatar.setAttribute('aria-hidden', 'true');
      avatar.style.setProperty('--h', String(hue(source.domain)));
      chip.appendChild(el('span', 'source-n', String(index + 1)));
      chip.lastChild.setAttribute('aria-label', 'Source ' + (index + 1));
      chip.appendChild(avatar);
      chip.appendChild(el('span', 'source-title', source.title));
      chip.appendChild(el('span', 'source-domain', source.domain));
      item.appendChild(chip);
      if (source.snippet) item.appendChild(el('p', 'source-snippet', clip(source.snippet, 320)));
      list.appendChild(item);
    });
    panel.appendChild(list);
    if (withSnippets) {
      var toggle = el('button', 'msg-sources-toggle', 'Show snippets');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', 'false');
      toggle.addEventListener('click', function () {
        var open = !wrap.classList.contains('show-snippets');
        wrap.classList.toggle('show-snippets', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.textContent = open ? 'Hide snippets' : 'Show snippets';
      });
      panel.appendChild(toggle);
    }
    var head = IM.reply
      ? IM.reply.sourcesToggle(sources.length, { web: o.web, verified: o.verified, panel: panel })
      : el('span', 'msg-sources-k', 'Sources');
    wrap.appendChild(head);
    wrap.appendChild(panel);
    return wrap;
  }

  /* Did this reply look anything up on the web (model call, pre-fetch or
   * the research engine)? Decides "Searched the web · N sources". */
  function usedWeb(turn) {
    if (turn.search || turn.searchResults || (turn.research && !turn.research.skip)) return true;
    var records = turn.activity ? turn.activity.records : [];
    return records.some(function (r) { return r.tool === 'web_search' || r.tool === 'web_fetch'; });
  }

  /* The folded pill names the tools ("Used web_search"), so the footer does
   * not have to repeat them. */
  Activity.prototype.label = function () {
    var names = [];
    this.records.forEach(function (r) { if (!r.depth && names.indexOf(r.tool) === -1) names.push(r.tool); });
    if (!names.length || this.count > 3 || names.length > 2) return 'Used ' + this.count + (this.count === 1 ? ' tool' : ' tools');
    return 'Used ' + names.join(' and ') + (this.count > names.length ? ' · ' + this.count + ' calls' : '');
  };

  /* ------------------------------------------------------------- finish */

  function finish(turn, evt) {
    var event = evt || {};
    var activity = turn.activity;
    if (Array.isArray(event.skills) && event.skills.length) {
      if (!activity) activity = turn.activity = new Activity(turn);
      activity.addSkills(event.skills);
    }
    if (activity) activity.settle();
    var node = turn.node;
    var actions = node.querySelector(':scope > .msg-actions');

    /* Sources: the server's list, else the pre-fetch search results. */
    var sources = normaliseSources(turn.sources && turn.sources.length ? turn.sources : turn.searchResults);
    var verified = turn.verification === 'verified';
    /* Up to 12 (the server's own cap): "[n]" in the answer refers to the
     * n-th source, so the list is never re-ordered or cut below that. */
    if (sources.length && turn.status !== 'error') {
      var row = sourcesRow(sources.slice(0, 12), { web: usedWeb(turn), verified: verified });
      node.insertBefore(row, actions);
      if (IM.research && IM.research.citations) IM.research.citations(turn.bubble, sources.slice(0, 12), row);
      verified = false; /* the row's check says it */
    }
    /* The live "Searching the web… / Found N sources" status has done its
     * job once the reply is in; a search that found nothing or failed stays. */
    if (turn.search && !turn.search.classList.contains('is-empty')) {
      turn.search.remove();
      turn.search = null;
    }

    /* Tool calls are listed in the folded pill above the answer; the footer
     * only says what the pill cannot: skills when no tool ran (the pill may
     * have folded away), and the bare count when the tool frames were missed. */
    var groups = activity ? activity.usage() : [];
    var reported = Number(event.tool_calls) || 0;
    var skills = activity && !groups.length ? activity.skills : [];
    if ((!groups.length && reported) || skills.length) {
      var foot = el('div', 'msg-agent-foot');
      foot.appendChild(el('span', 'msg-agent-k', 'Used'));
      if (!groups.length && reported) foot.appendChild(el('span', 'tool-chip', reported + (reported === 1 ? ' tool call' : ' tool calls')));
      skills.forEach(function (id) { foot.appendChild(el('span', 'skill-chip', '/' + id)); });
      node.insertBefore(foot, actions);
    }

    /* Verified without a source list, or not verified: a small note where
     * the sources row would be. */
    var mark = badge(verified ? 'verified' : (turn.verification === 'unverified' ? 'unverified' : ''), 0);
    if (mark) {
      var note = el('div', 'reply-evidence');
      note.appendChild(mark);
      node.insertBefore(note, actions);
    }
  }

  IM.activity = {
    handles: function (type) { return TYPES.indexOf(type) !== -1; },
    attach: function (turn) { return new Activity(turn); },
    finish: finish,
    settle: function (turn) { if (turn && turn.activity) turn.activity.settle(); },
    noteVerification: noteVerification,
    stripCalls: stripCalls,
    stripNotes: stripNotes,
    argSummary: argSummary,
    clip: clip,
    verbs: VERBS
  };
})();
