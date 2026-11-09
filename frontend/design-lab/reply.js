/* The quiet parts of an assistant reply: the per-reply numbers, the Plan
 * tag with its "Switch to Build and do this" button, and the sources
 * disclosure.
 *
 *   IMURUN.reply.meta(stats)          -> compact "8.5 s" button; the full line
 *                                        ("114 tok · 28.5 tok/s · ...") is its
 *                                        tooltip and opens inline on click
 *   IMURUN.reply.finish(turn, event)  live reply finished (chat.js Turn.finish)
 *   IMURUN.reply.history(node)        a reply rendered from a saved transcript
 *   IMURUN.reply.sourcesToggle(...)   the "Searched the web · 4 sources" row
 *                                     (activity.js builds the list inside it)
 *
 * chat.js is at its size limit, so these live here. The action buttons
 * (Copy, Read aloud, thumbs, Teach) keep their markup and handlers; they are
 * drawn as icons by styles/chat.css, which survives the other modules
 * rewriting a button's text ("Copied", "Stop reading", "Taught").
 * ES2017, no build step (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var PLAN_KEY = 'imurun.chat.planTurns';
  var PLAN_KEEP = 200;
  var CARRY_OUT = 'Carry out the plan above.';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function toast(options) { return IM.toast ? IM.toast(options) : null; }

  /* ------------------------------------------------------------ the numbers */

  function compactSeconds(seconds) {
    var n = Number(seconds);
    if (!isFinite(n) || n < 0) return '';
    if (n < 1) return Math.round(n * 1000) + ' ms';
    if (n < 60) return n.toFixed(1) + ' s';
    return Math.floor(n / 60) + 'm ' + Math.round(n % 60) + 's';
  }

  /* One line with every number the reply reported; nothing is dropped, it is
   * only folded behind the total time. The Performance drawer has the rest. */
  function meta(stats) {
    var bits = [];
    if (stats.tokens) bits.push(util.formatTokens(stats.tokens) + ' tok');
    if (stats.tokensPerSecond) bits.push(stats.tokensPerSecond.toFixed(1) + ' tok/s');
    if (stats.prefillSeconds !== null && stats.prefillSeconds !== undefined) bits.push('prefill ' + util.formatSeconds(stats.prefillSeconds));
    else if (stats.firstTokenSeconds !== null && stats.firstTokenSeconds !== undefined) bits.push('first token ' + util.formatSeconds(stats.firstTokenSeconds));
    bits.push(util.formatSeconds(stats.totalSeconds) + ' total');
    if (stats.steps > 1) bits.push(stats.steps + ' steps');
    var source = stats.source === 'engine'
      ? 'reported by the engine'
      : 'measured in the browser from streamed chunks; the engine did not report counters';
    var full = bits.join(' · ');
    var button = el('button', 'msg-meta', compactSeconds(stats.totalSeconds) || full);
    button.type = 'button';
    button.title = full + ' (' + source + ')';
    button.setAttribute('aria-label', 'Reply stats: ' + full + ', ' + source);
    button.setAttribute('aria-expanded', 'false');
    button._short = button.textContent;
    button._full = full + (stats.source === 'browser' && stats.tokensPerSecond ? ' (measured here)' : '');
    return button;
  }

  function toggleMeta(button) {
    var open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.textContent = open ? button._full : button._short;
    button.classList.toggle('is-open', open);
  }

  /* --------------------------------------------------------------- sources */

  var GLOBE = '<svg class="reply-ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M1.8 8h12.4M8 1.8c1.7 1.8 2.5 3.8 2.5 6.2S9.7 12.4 8 14.2C6.3 12.4 5.5 10.4 5.5 8S6.3 3.6 8 1.8z"/></svg>';
  var LINK = '<svg class="reply-ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M6.6 9.4l2.8-2.8M7.2 4.6l1-1a2.6 2.6 0 0 1 3.7 3.7l-1 1M8.8 11.4l-1 1a2.6 2.6 0 0 1-3.7-3.7l1-1"/></svg>';
  var CHECK = '<svg class="reply-ico reply-ok" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4l3 3 6-6.4"/></svg>';
  var seq = 0;

  /* The collapsed row: "Searched the web · 4 sources ⌄", with a check when
   * the server marked the answer as verified. `panel` is the list it opens. */
  function sourcesToggle(count, options) {
    var o = options || {};
    var label = (o.web ? 'Searched the web · ' : '') + count + (count === 1 ? ' source' : ' sources');
    var button = el('button', 'reply-disclosure reply-sources-toggle');
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    button.innerHTML = (o.verified ? CHECK : (o.web ? GLOBE : LINK)) + '<span class="reply-disclosure-label"></span>';
    button.querySelector('.reply-disclosure-label').textContent = label;
    if (o.verified) {
      button.title = 'Verified: this answer used live tool results. Show the sources.';
      button.setAttribute('aria-label', label + ', verified with live results. Show sources');
    } else {
      button.title = 'Show the sources';
    }
    if (o.panel) {
      if (!o.panel.id) o.panel.id = 'reply-sources-' + (++seq);
      button.setAttribute('aria-controls', o.panel.id);
      o.panel.hidden = true;
    }
    return button;
  }

  function setSourcesOpen(section, open) {
    var button = section.querySelector('.reply-sources-toggle');
    var panel = section.querySelector('.reply-source-panel');
    if (!button || !panel) return;
    panel.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    section.classList.toggle('is-open', open);
  }

  /* -------------------------------------------------------------- Plan mode */

  function planTurns() {
    var list = IM.store ? IM.store.get(PLAN_KEY, []) : [];
    return Array.isArray(list) ? list : [];
  }

  function rememberPlan(turnId) {
    if (!turnId || turnId === 'demo' || !IM.store) return;
    var list = planTurns().filter(function (id) { return id !== turnId; });
    list.push(turnId);
    IM.store.set(PLAN_KEY, list.slice(-PLAN_KEEP));
  }

  /* The "Plan" tag above the answer and, under it, the one-click hand-off to
   * Build. The button only shows on the newest reply (styles/chat.css): an
   * older plan is ambiguous once the conversation moved on. */
  function markPlan(node) {
    if (!node || node.querySelector(':scope > .reply-plan-cta')) return;
    node.classList.add('is-plan');
    var bubble = node.querySelector(':scope > .bubble');
    var tag = el('div', 'reply-tags');
    var chip = el('span', 'reply-tag', 'Plan');
    chip.title = 'Written in Plan mode: nothing was changed yet';
    tag.appendChild(chip);
    node.insertBefore(tag, bubble || node.firstChild);

    var cta = el('div', 'reply-plan-cta');
    var button = el('button', 'btn reply-plan-go', 'Switch to Build and do this');
    button.type = 'button';
    button.setAttribute('data-plan-go', '');
    button.title = 'Switch the composer to Build and send "' + CARRY_OUT + '" in this session';
    cta.appendChild(button);
    var actions = node.querySelector(':scope > .msg-actions');
    node.insertBefore(cta, actions || null);
  }

  function setBuildMode() {
    var build = document.querySelector('.mode[data-mode="build"]');
    if (build && !build.classList.contains('active')) build.click();
    else if (IM.store) IM.store.set('imurun.chat.mode', 'build');
  }

  function carryOut(button) {
    var node = button.closest('.msg.assistant');
    if (!node || !IM.chat) return;
    if (IM.chat.isStreaming()) {
      toast({ tone: 'neutral', icon: 'clock', kicker: 'Chat', title: 'A reply is still running', text: 'Wait for it to finish or stop it first.' });
      return;
    }
    var sessionId = node._sessionId || IM.state.activeSessionId;
    if (!sessionId) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Plan', title: 'Preview only', text: 'This reply is not part of a saved session, so nothing was sent.' });
      return;
    }
    if (IM.state.activeSessionId && IM.state.activeSessionId !== sessionId) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Plan', title: 'Open that session first', text: 'The plan belongs to another session.' });
      return;
    }
    setBuildMode();
    var turn = IM.chat.send(CARRY_OUT, sessionId);
    if (turn) {
      var cta = button.closest('.reply-plan-cta');
      if (cta) cta.remove();
    }
  }

  /* ----------------------------------------------------------------- hooks */

  function finish(turn, event) {
    var node = turn && turn.node;
    if (!node) return;
    node._sessionId = turn.sessionId;
    var answered = turn.lastParts && turn.lastParts.answer && turn.lastParts.answer.trim();
    if (turn.mode === 'plan' && turn.status === 'done' && answered) {
      rememberPlan(turn.turnId);
      markPlan(node);
    }
  }

  function history(node) {
    if (!node || !node.dataset || !node.dataset.turnId) return;
    if (planTurns().indexOf(node.dataset.turnId) !== -1) markPlan(node);
  }

  /* ------------------------------------------------------------ delegation */

  document.addEventListener('click', function (event) {
    var target = event.target && event.target.closest ? event.target : null;
    if (!target) return;
    var metaButton = target.closest('.msg-meta');
    if (metaButton && metaButton._full) { toggleMeta(metaButton); return; }
    var go = target.closest('[data-plan-go]');
    if (go) { carryOut(go); return; }
    var toggle = target.closest('.reply-sources-toggle');
    if (toggle) {
      var section = toggle.closest('.msg-sources');
      if (section) setSourcesOpen(section, toggle.getAttribute('aria-expanded') !== 'true');
    }
  });

  /* A citation jumps to its source (research.js); open the list first so the
   * source is there to land on. Capture phase: runs before that handler. */
  document.addEventListener('click', function (event) {
    var link = event.target && event.target.closest ? event.target.closest('a.cite-link') : null;
    if (!link) return;
    var msg = link.closest('.msg');
    var section = msg && msg.querySelector('.msg-sources');
    if (section) setSourcesOpen(section, true);
  }, true);

  IM.reply = {
    meta: meta,
    finish: finish,
    history: history,
    sourcesToggle: sourcesToggle,
    setSourcesOpen: setSourcesOpen,
    markPlan: markPlan,
    carryOutText: CARRY_OUT
  };
})();
