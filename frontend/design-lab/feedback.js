/* Thumbs up / thumbs down / Teach on every assistant message: the consent
 * that feeds the local tuning channel (services/tuning, docs/tuning/).
 *
 *   POST /api/tuning/feedback {turn_id, rating, correction?, session_id?}
 *     up     "learn this answer"
 *     down   recorded, never trained on
 *     teach  "the right answer is this": the correction replaces the answer
 *     clear  withdraws consent (a second click on the active rating)
 *   The latest mark wins. Live sync from `tuning.feedback` frames; ratings
 *   already given are read back with GET /api/tuning/feedback?session_id=
 *   -> {session_id, turns: {turn_id: {rating, consented, correction?}}}.
 *
 * The buttons attach themselves to every `.msg.assistant` that carries a
 * turn id (chat.js puts `data-turn-id` on finished and history replies) as
 * soon as its action row appears, so chat.js needs no further changes. The
 * first rating shows the consent wording once.
 * ES2017 (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var CONSENT_KEY = 'imurun.tuning.consent.v1';
  var CONSENT_TEXT = 'Only the replies you rate are ever used, and only to fine-tune the model on this machine. ' +
    'Ratings are stored locally next to your transcripts (~/.imurun/sessions/feedback/) and are never sent anywhere. ' +
    'A thumbs-down alone is never trained on; Teach replaces the answer with yours. Click a rating again to withdraw it.';

  var ratings = {};          /* turn_id -> {rating, correction?} */
  var loadedSessions = {};   /* session_id -> true once read back */
  var routeMissing = false;  /* GET .../feedback?session_id= answered 404 */

  function api() { return window.__imurun_api; }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  var ICON_UP = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.5 9.2v7.3H3.8V9.2h2.7zm0 0l3-5.6c.3-.6 1-.9 1.6-.6.6.3.9 1 .7 1.6l-.8 2.9h4.2c.9 0 1.6.8 1.4 1.7l-1.1 5.6c-.1.7-.8 1.2-1.5 1.2H6.5"/></svg>';
  var ICON_DOWN = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M13.5 10.8V3.5h2.7v7.3h-2.7zm0 0l-3 5.6c-.3.6-1 .9-1.6.6-.6-.3-.9-1-.7-1.6l.8-2.9H4.8c-.9 0-1.6-.8-1.4-1.7l1.1-5.6c.1-.7.8-1.2 1.5-1.2h7.5"/></svg>';

  function sessionId() {
    return (IM.state && IM.state.activeSessionId) || '';
  }

  /* --------------------------------------------------------------- render */

  function decorate(node) {
    if (!node || node._feedback || !node.dataset || !node.dataset.turnId) return;
    var actions = node.querySelector(':scope > .msg-actions');
    if (!actions) return;
    node._feedback = true;
    var group = el('span', 'fb-group');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Rate this reply for local fine-tuning');
    group.innerHTML =
      '<button class="msg-action fb-btn" type="button" data-fb="up" aria-pressed="false" title="Good answer: learn it (local fine-tuning)" aria-label="Good answer">' + ICON_UP + '</button>' +
      '<button class="msg-action fb-btn" type="button" data-fb="down" aria-pressed="false" title="Bad answer (recorded, never trained on)" aria-label="Bad answer">' + ICON_DOWN + '</button>' +
      '<button class="msg-action fb-btn fb-teach" type="button" data-fb="teach" aria-pressed="false" aria-expanded="false" title="Teach: write the answer it should have given">Teach</button>';
    actions.appendChild(group);
    paint(node);
  }

  function paint(node) {
    var current = ratings[node.dataset.turnId] || {};
    Array.prototype.forEach.call(node.querySelectorAll('[data-fb]'), function (b) {
      var on = current.rating === b.getAttribute('data-fb');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.classList.toggle('is-on', on);
      if (b.getAttribute('data-fb') === 'teach') b.textContent = on ? 'Taught' : 'Teach';
    });
    node.classList.toggle('has-feedback', Boolean(current.rating));
  }

  function paintTurn(turnId) {
    Array.prototype.forEach.call(document.querySelectorAll('.msg.assistant[data-turn-id]'), function (node) {
      if (node.dataset.turnId === turnId) paint(node);
    });
  }

  /* ---------------------------------------------------------------- teach */

  function openTeach(node) {
    var existing = node.querySelector(':scope > .fb-teach-box');
    if (existing) { existing.querySelector('textarea').focus(); return; }
    var current = ratings[node.dataset.turnId] || {};
    var box = el('form', 'fb-teach-box');
    box.setAttribute('aria-label', 'Teach the answer it should have given');
    box.innerHTML =
      '<label class="form-field"><span class="k">The answer it should have given</span><textarea rows="6" spellcheck="true"></textarea></label>' +
      '<p class="fb-teach-note">Your text replaces the model\'s answer in training. It teaches style, format and habits; for facts that change (people, news, prices) the research tools are the right fix.</p>' +
      '<div class="fb-teach-actions"><button class="btn primary" type="submit">Save correction</button>' +
      '<button class="btn ghost" type="button" data-fb-cancel>Cancel</button>' +
      (current.rating === 'teach' ? '<button class="btn ghost danger" type="button" data-fb-withdraw>Withdraw</button>' : '') + '</div>';
    var area = box.querySelector('textarea');
    area.value = current.rating === 'teach' && current.correction ? current.correction : (node._answer || '');
    var actions = node.querySelector(':scope > .msg-actions');
    node.insertBefore(box, actions ? actions.nextSibling : null);
    var teachBtn = node.querySelector('[data-fb="teach"]');
    if (teachBtn) teachBtn.setAttribute('aria-expanded', 'true');
    box.addEventListener('submit', function (event) {
      event.preventDefault();
      var text = area.value.trim();
      if (!text) { area.focus(); return; }
      send(node, 'teach', text).then(function (ok) { if (ok) closeTeach(node); });
    });
    box.addEventListener('click', function (event) {
      if (event.target.closest('[data-fb-cancel]')) closeTeach(node);
      else if (event.target.closest('[data-fb-withdraw]')) send(node, 'clear').then(function (ok) { if (ok) closeTeach(node); });
    });
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }

  function closeTeach(node) {
    var box = node.querySelector(':scope > .fb-teach-box');
    if (box) box.remove();
    var teachBtn = node.querySelector('[data-fb="teach"]');
    if (teachBtn) { teachBtn.setAttribute('aria-expanded', 'false'); teachBtn.focus(); }
  }

  /* ----------------------------------------------------------------- send */

  function consentOnce() {
    var seen = false;
    try { seen = window.localStorage.getItem(CONSENT_KEY) === '1'; } catch (error) { seen = false; }
    if (seen) return;
    try { window.localStorage.setItem(CONSENT_KEY, '1'); } catch (error) { /* ignore */ }
    toast({ id: 'tuning-consent', tone: 'neutral', icon: 'info', kicker: 'Local fine-tuning', title: 'How ratings are used',
      text: CONSENT_TEXT, timeout: 0,
      actions: [{ label: 'Tuning settings', onClick: function () { if (IM.shell) IM.shell.openSettings('tuning'); } }] });
  }

  /* -> Promise<boolean> (saved) */
  function send(node, rating, correction) {
    var turnId = node.dataset.turnId;
    var previous = ratings[turnId];
    var next = rating === 'clear' ? null : { rating: rating, correction: correction || '' };
    if (rating !== 'clear') consentOnce();
    /* optimistic, rolled back on failure */
    if (next) ratings[turnId] = next; else delete ratings[turnId];
    paint(node);
    if (turnId === 'demo') return Promise.resolve(true);   /* response gallery */
    var body = { turn_id: turnId, rating: rating };
    if (rating === 'teach') body.correction = correction;
    var session = node.dataset.sessionId || sessionId();
    if (session) body.session_id = session;
    return api().tuningFeedback(body).then(function () {
      if (IM.util) IM.util.announce(rating === 'clear' ? 'Rating withdrawn.' : (rating === 'teach' ? 'Correction saved.' : 'Rating saved.'));
      return true;
    }, function (error) {
      if (previous) ratings[turnId] = previous; else delete ratings[turnId];
      paint(node);
      var status = error && error.status;
      toast({ tone: 'error', icon: 'alert', kicker: 'Rating', title: 'Not saved',
        text: status === 404 && /transcript/.test(String(error.message)) ? 'This reply is not in a saved transcript, so it cannot be rated.'
          : (status === 404 || status === 500 || status === 501) ? 'The tuning channel is not available on this server.'
          : ((error && error.message) || 'The server did not answer.') });
      return false;
    });
  }

  function onClick(event) {
    var b = event.target.closest ? event.target.closest('[data-fb]') : null;
    if (!b) return;
    var node = b.closest('.msg.assistant');
    if (!node) return;
    var kind = b.getAttribute('data-fb');
    var current = ratings[node.dataset.turnId] || {};
    if (kind === 'teach') {
      if (node.querySelector(':scope > .fb-teach-box')) closeTeach(node);
      else openTeach(node);
      return;
    }
    send(node, current.rating === kind ? 'clear' : kind);
  }

  /* Read back the ratings of a session (route requested; skipped on 404). */
  function loadSession(id) {
    if (!id || routeMissing || loadedSessions[id] || !api() || !api().tuningFeedbackFor) return;
    loadedSessions[id] = true;
    api().tuningFeedbackFor(id).then(function (data) {
      var turns = (data && data.turns) || {};
      Object.keys(turns).forEach(function (turnId) {
        var t = turns[turnId] || {};
        if (t.rating && t.rating !== 'clear') ratings[turnId] = { rating: t.rating, correction: t.correction || '' };
        paintTurn(turnId);
      });
    }, function (error) {
      if (error && (error.status === 404 || error.status === 405)) routeMissing = true;
      loadedSessions[id] = false;
    });
  }

  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.matches && root.matches('.msg.assistant')) decorate(root);
    var parent = root.closest ? root.closest('.msg.assistant') : null;
    if (parent) decorate(parent);
    Array.prototype.forEach.call(root.querySelectorAll('.msg.assistant[data-turn-id]'), decorate);
  }

  function init() {
    document.addEventListener('click', onClick);
    scan(document);
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        Array.prototype.forEach.call(r.addedNodes, function (n) { if (n.nodeType === 1) scan(n); });
      });
    }).observe(document.body, { childList: true, subtree: true });
    if (IM.bus) {
      IM.bus.on('server:tuning.feedback', function (event) {
        var id = String(event.turn_id || '');
        if (!id) return;
        /* the latest mark wins, from this window or another */
        if (!event.rating || event.rating === 'clear') delete ratings[id];
        else if (!ratings[id] || ratings[id].rating !== event.rating) {
          ratings[id] = { rating: event.rating, correction: (ratings[id] && ratings[id].correction) || '' };
        }
        paintTurn(id);
      });
      IM.bus.on('session.active', loadSession);
    }
    loadSession(sessionId());
  }

  IM.feedback = { decorate: decorate, ratings: ratings, consentText: CONSENT_TEXT };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
