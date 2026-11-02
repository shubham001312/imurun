/* The research engine in a reply (services/research, docs/research/).
 *
 *   IMURUN.research.handles(type)            research.* frames
 *   IMURUN.research.onEvent(turn, frame)     render into the thinking panel
 *   IMURUN.research.citations(bubble, sources, chipsRoot)
 *                                            "[n]" in the answer -> superscript
 *                                            link to source chip n
 *   IMURUN.research.gallery(demo, label, nodes)  response-gallery states
 *
 * Frames (services/research/engine.py, docs/research/README.md; tolerant:
 * unknown fields are ignored, missing ones shown as nothing, not guessed):
 *   research.plan   {turn_id, needs_web, reason, queries[], freshness, entities[], method, elapsed}
 *   research.search {turn_id, source, query, ok, results (count), error?, blocked?, elapsed}
 *                   one frame per source x query when it FINISHES
 *   research.fetch  {turn_id, url, status (HTTP), ok, chars, error, lang, published}
 *   research.done   {turn_id, ok, queries, sources[], passages, hits, errors[], offline, blocked, elapsed}
 * There is no "start" frame: after a web plan every lane spins until its
 * own frame arrives; lanes that never report go quiet ("not used") once
 * pages are being read or the research ends.
 *
 * Panel layout, top of the thinking panel (above the step blocks):
 *   Looking up: "q1", "q2"            <- the AI's plan, with its reason
 *   [Wikipedia 3] [Public search ...] [News 5] [Direct -]   <- one lane per source
 *   Reading  docs.python.org  python.org  ...                <- pages being read
 *   Read 3 pages in 2.4 s                                    <- done
 * The older "Searching the web..." indicator is hidden while this block
 * exists (`.msg.has-research`). Nothing animates under reduced motion.
 * ES2017 (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;

  var TYPES = ['research.plan', 'research.search', 'research.fetch', 'research.done'];
  /* Provider -> lane. Anything unknown is a public search engine. */
  var LANES = [
    { id: 'wikipedia', label: 'Wikipedia' },
    { id: 'search', label: 'Public search' },
    { id: 'news', label: 'News' },
    { id: 'direct', label: 'Direct' }
  ];
  var LANE_OF = { wikipedia: 'wikipedia', wikidata: 'wikipedia', google_news: 'news', news: 'news', bing_news: 'news',
    gdelt: 'news', rss: 'news', direct: 'direct', url: 'direct', given: 'direct', fetch: 'direct' };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clip(text, max) {
    var value = String(text === undefined || text === null ? '' : text).replace(/\s+/g, ' ').trim();
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  }

  function laneOf(provider) {
    var key = String(provider || '').toLowerCase();
    if (LANE_OF[key]) return LANE_OF[key];
    if (/news/.test(key)) return 'news';
    if (/wiki/.test(key)) return 'wikipedia';
    return 'search';
  }

  /* services/research/engine.py publishes one frame per finished search
   * (`ok`, `results` count, `error`, `blocked`) and per finished page read
   * (`status` = HTTP code, `ok`, `chars`, `error`); there is no "start"
   * frame, so the lanes wait from the plan until their own frame arrives.
   * A `state: "start"` frame is still understood if one is ever added. */
  function phase(event) {
    var s = typeof event.state === 'string' ? event.state.toLowerCase() :
      (typeof event.status === 'string' ? event.status.toLowerCase() : '');
    if (s === 'start' || s === 'started' || s === 'running' || s === 'pending') return 'running';
    if (s === 'error' || s === 'failed' || s === 'timeout' || s === 'blocked' || event.ok === false || event.error || event.blocked) return 'failed';
    if (s === 'done' || s === 'ok' || s === 'finished' || event.ok === true) return 'done';
    if (event.count !== undefined || event.hits !== undefined || event.results !== undefined) return 'done';
    return 'running';
  }

  function countOf(event) {
    if (Array.isArray(event.hits)) return event.hits.length;
    if (Array.isArray(event.results)) return event.results.length;
    var raw = event.results !== undefined ? event.results : (event.count !== undefined ? event.count : event.hits);
    var n = Number(raw);
    return raw !== undefined && raw !== null && isFinite(n) ? n : null;
  }

  function domainOf(event) {
    if (event.domain) return String(event.domain).replace(/^www\./, '');
    try { return new URL(String(event.url || '')).hostname.replace(/^www\./, ''); } catch (error) { return ''; }
  }

  function hue(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  /* ------------------------------------------------------------ panel */

  function Research(turn) {
    this.turn = turn;
    this.lanes = {};
    this.pages = {};
    this.plan = null;
    this.done = false;
    this.build();
  }

  Research.prototype.build = function () {
    var turn = this.turn;
    var box = el('section', 'research-trace');
    box.setAttribute('aria-label', 'Research');
    this.planEl = el('div', 'research-plan');
    this.planEl.hidden = true;
    this.lanesEl = el('ul', 'research-lanes');
    this.lanesEl.setAttribute('aria-label', 'Sources searched');
    this.lanesEl.hidden = true;
    this.readEl = el('div', 'research-read');
    this.readEl.hidden = true;
    this.readEl.appendChild(el('span', 'research-k', 'Reading'));
    this.chipsEl = el('span', 'research-pages');
    this.readEl.appendChild(this.chipsEl);
    this.doneEl = el('p', 'research-done');
    this.doneEl.hidden = true;
    box.appendChild(this.planEl);
    box.appendChild(this.lanesEl);
    box.appendChild(this.readEl);
    box.appendChild(this.doneEl);
    var body = turn.body;
    var empty = body.querySelector('.thinking-empty');
    if (empty) empty.remove();
    var skills = body.querySelector(':scope > .trace-skills');
    body.insertBefore(box, skills ? skills.nextSibling : body.firstChild);
    this.el = box;
    turn.node.classList.add('has-research');
    turn.details.classList.add('has-trace');
  };

  Research.prototype.reveal = function () {
    var turn = this.turn;
    if (turn.activity && turn.activity.reveal) { turn.activity.reveal(); return; }
    turn.details.classList.add('has-trace');
    if (!turn.userToggled && !turn.done && !turn.details.open && !this.opened) {
      this.opened = true;
      turn.details.open = true;
    }
  };

  Research.prototype.label = function (text, preview) {
    var turn = this.turn;
    if (turn.done || turn.firstTokenAt) return;
    turn.setLabel(text);
    if (turn.preview && preview !== undefined) turn.preview.textContent = preview;
  };

  Research.prototype.onPlan = function (event) {
    this.plan = event;
    var queries = (Array.isArray(event.queries) ? event.queries : []).map(function (q) { return clip(q, 80); }).filter(Boolean);
    var planEl = this.planEl;
    planEl.innerHTML = '';
    this.skip = event.needs_web === false;
    if (event.needs_web === false) {
      /* Not a live question: one quiet line, no lanes. */
      planEl.className = 'research-plan is-skip';
      planEl.appendChild(el('span', 'research-k', 'No lookup'));
      planEl.appendChild(el('span', 'research-reason', clip(event.reason || 'answered from the model\'s knowledge', 160)));
      planEl.hidden = false;
      return;
    }
    planEl.className = 'research-plan';
    var line = el('p', 'research-queries');
    line.appendChild(el('span', 'research-k', 'Looking up'));
    queries.forEach(function (q, i) {
      var chip = el('q', 'research-query', q);
      line.appendChild(chip);
      if (i < queries.length - 1) line.appendChild(document.createTextNode(' '));
    });
    if (!queries.length) line.appendChild(el('span', 'research-reason', 'the question as asked'));
    planEl.appendChild(line);
    var bits = [];
    if (event.reason) bits.push(clip(event.reason, 200));
    if (event.freshness && event.freshness !== 'any') bits.push(event.freshness === 'live' ? 'needs live data' : 'prefers recent sources');
    if (bits.length) planEl.appendChild(el('p', 'research-reason', bits.join(' · ')));
    if (event.method) {
      var how = { model: 'planned by the model', heuristic: 'planned by rules', prefilter: 'planned by rules', given: 'query from the model\'s tool call' }[event.method];
      if (how) planEl.appendChild(el('span', 'research-method', how));
    }
    planEl.hidden = false;
    this.ensureLanes();
    this.label(queries.length ? 'Looking up ' + queries[0] : 'Searching the web', queries.join(' · '));
    this.reveal();
  };

  Research.prototype.ensureLanes = function () {
    if (!this.lanesEl.hidden) return;
    var self = this;
    this.lanesEl.hidden = false;
    LANES.forEach(function (lane) {
      var li = el('li', 'research-lane is-idle');
      li.dataset.lane = lane.id;
      li.appendChild(el('span', 'research-spin'));
      li.lastChild.setAttribute('aria-hidden', 'true');
      li.appendChild(el('span', 'research-lane-name', lane.label));
      var count = el('span', 'research-count', '');
      li.appendChild(count);
      self.lanesEl.appendChild(li);
      self.lanes[lane.id] = { el: li, countEl: count, running: 0, done: 0, failed: 0, hits: 0, known: false, errors: [], waiting: true };
      self.paintLane(self.lanes[lane.id]);
    });
  };

  /* Lanes that never reported once the search phase is over were not used
   * for this question (no news for a definition, no direct URL, ...). */
  Research.prototype.settleWaiting = function () {
    var self = this;
    Object.keys(this.lanes).forEach(function (id) {
      var lane = self.lanes[id];
      if (lane.waiting) { lane.waiting = false; self.paintLane(lane); }
    });
  };

  Research.prototype.paintLane = function (lane) {
    var busy = lane.running > 0 || (lane.waiting && !lane.done && !lane.failed);
    var stateCls = busy ? 'is-running' : (lane.done > 0 ? (lane.hits > 0 ? 'is-done' : 'is-empty') : (lane.failed > 0 ? 'is-failed' : 'is-idle'));
    lane.el.className = 'research-lane ' + stateCls;
    var text = busy ? '…' : (lane.done ? String(lane.hits) : '');
    if (stateCls === 'is-failed') text = 'failed';
    lane.countEl.textContent = text;
    var name = lane.el.querySelector('.research-lane-name').textContent;
    lane.el.title = name + ': ' + (stateCls === 'is-running' ? 'searching' : stateCls === 'is-failed' ? (lane.errors[0] || 'failed') :
      stateCls === 'is-idle' ? 'not used for this question' : lane.hits + (lane.hits === 1 ? ' result' : ' results'));
  };

  /* The folded pill when research was the only activity (chat.js foldThinking). */
  Research.prototype.summary = function () {
    var self = this;
    var read = Object.keys(this.pages).filter(function (k) { return self.pages[k].el.classList.contains('is-done'); }).length;
    var hits = 0;
    Object.keys(this.lanes).forEach(function (id) { hits += self.lanes[id].hits; });
    if (read) return 'Researched · read ' + read + (read === 1 ? ' page' : ' pages');
    if (hits) return 'Researched · ' + hits + (hits === 1 ? ' result' : ' results');
    return 'Looked up the web';
  };

  Research.prototype.onSearch = function (event) {
    this.ensureLanes();
    var lane = this.lanes[laneOf(event.source || event.provider)];
    var key = String(event.source || event.provider || '') + '|' + String(event.query || '');
    lane.calls = lane.calls || {};
    var was = lane.calls[key];
    var now = phase(event);
    if (was === 'running') lane.running -= 1;
    if (now === 'running') lane.running += 1;
    else if (now === 'done') {
      lane.done += 1;
      var n = countOf(event);
      if (n !== null) { lane.hits += n; lane.known = true; }
    } else {
      lane.failed += 1;
      if (event.error) lane.errors.push(clip((event.blocked ? 'refused (bot check): ' : '') + event.error, 120));
    }
    lane.waiting = false;
    lane.calls[key] = now;
    this.paintLane(lane);
    if (now === 'running') this.label('Searching ' + lane.el.querySelector('.research-lane-name').textContent.toLowerCase(), event.query ? clip(event.query, 100) : undefined);
    this.reveal();
  };

  Research.prototype.onFetch = function (event) {
    var domain = domainOf(event);
    if (!domain) return;
    var key = String(event.url || domain);
    var page = this.pages[key];
    this.settleWaiting();   /* reading starts after the search phase */
    this.readEl.hidden = false;
    if (!page) {
      var chip = el('span', 'research-page is-running');
      chip.style.setProperty('--h', String(hue(domain)));
      chip.appendChild(el('span', 'research-page-dot'));
      chip.lastChild.setAttribute('aria-hidden', 'true');
      chip.appendChild(el('span', '', domain));
      if (!util.reducedMotion()) chip.classList.add('is-entering');
      this.chipsEl.appendChild(chip);
      page = this.pages[key] = { el: chip, domain: domain };
    }
    var now = phase(event);
    page.el.classList.remove('is-running', 'is-done', 'is-failed');
    page.el.classList.add('is-' + now);
    page.el.title = String(event.url || domain) + (now === 'failed' ? '\n' + clip(event.error || 'could not be read', 160) :
      (now === 'done' && event.chars ? '\n' + util.formatTokens(Number(event.chars)) + ' characters read' : ''));
    this.label('Reading ' + domain, String(event.url || ''));
    this.reveal();
  };

  Research.prototype.onDone = function (event) {
    var self = this;
    this.done = true;
    this.settleWaiting();
    /* A lane still spinning never reported back. */
    Object.keys(this.lanes).forEach(function (id) {
      var lane = self.lanes[id];
      if (lane.running > 0) { lane.running = 0; if (!lane.done) lane.failed += 1; self.paintLane(lane); }
    });
    Object.keys(this.pages).forEach(function (key) {
      var page = self.pages[key];
      if (page.el.classList.contains('is-running')) { page.el.classList.remove('is-running'); page.el.classList.add('is-failed'); }
    });
    var read = Object.keys(this.pages).filter(function (k) { return self.pages[k].el.classList.contains('is-done'); }).length;
    var pages = event.pages !== undefined ? Number(event.pages) : read;
    var bits = [];
    if (event.offline) bits.push('Offline: nothing could be looked up');
    else if (event.blocked) bits.push('The search sites refused the request (bot check)');
    else {
      var sources = Array.isArray(event.sources) ? event.sources.length : null;
      if (pages) bits.push('Read ' + pages + (pages === 1 ? ' page' : ' pages'));
      if (sources !== null) bits.push(sources + (sources === 1 ? ' source kept' : ' sources kept'));
      if (!pages && !sources) bits.push('Nothing useful was found' + (Array.isArray(event.errors) && event.errors.length ? ' (' + clip(event.errors[0], 90) + ')' : ''));
    }
    if (event.elapsed) bits.push(util.formatSeconds(Number(event.elapsed)));
    this.doneEl.textContent = bits.join(' · ');
    this.doneEl.className = 'research-done' + (event.offline || event.blocked || (Array.isArray(event.sources) && !event.sources.length) ? ' is-warn' : '');
    this.doneEl.hidden = !bits.length;
    if (Array.isArray(event.sources) && event.sources.length && IM.activity) IM.activity.noteVerification(this.turn, { sources: event.sources });
    if (typeof event.verification === 'string' && IM.activity) IM.activity.noteVerification(this.turn, { verification: event.verification });
    this.label('Thinking', '');
  };

  function onEvent(turn, event) {
    if (!turn || turn.done) return;
    var r = turn.research;
    if (!r) r = turn.research = new Research(turn);
    if (event.type === 'research.plan') r.onPlan(event);
    else if (event.type === 'research.search') r.onSearch(event);
    else if (event.type === 'research.fetch') r.onFetch(event);
    else if (event.type === 'research.done') r.onDone(event);
  }

  /* --------------------------------------------------------- citations */

  /* "[1]", "[2][3]", "[1, 2]" and "[1-3]" in the finished answer become
   * superscript links to the matching source chip. Text inside code, pre and
   * existing links is left alone, and a number with no source stays text. */
  var CITE_RE = /\[(\d{1,2}(?:\s*[,–-]\s*\d{1,2})*)\](?!\()/g;

  function expand(group, max) {
    var out = [];
    group.split(/\s*,\s*/).forEach(function (part) {
      var range = part.split(/\s*[–-]\s*/);
      var a = Number(range[0]);
      var b = range.length > 1 ? Number(range[1]) : a;
      if (!(a >= 1) || !(b >= a) || b - a > 12) return;
      for (var n = a; n <= b; n += 1) if (n <= max && out.indexOf(n) === -1) out.push(n);
    });
    return out;
  }

  function citations(bubble, sources, chipsRoot) {
    var max = Array.isArray(sources) ? sources.length : 0;
    if (!bubble || !max) return 0;
    var walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!/\[\d/.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
        for (var p = node.parentNode; p && p !== bubble; p = p.parentNode) {
          if (/^(CODE|PRE|A|KBD|SAMP|SCRIPT|STYLE)$/.test(p.nodeName)) return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var made = 0;
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var frag = document.createDocumentFragment();
      var last = 0;
      var any = false;
      CITE_RE.lastIndex = 0;
      var m;
      while ((m = CITE_RE.exec(text))) {
        var nums = expand(m[1], max);
        if (!nums.length) continue;
        any = true;
        /* Drop the space before the marker: "Paris [1]." -> "Paris¹." */
        var before = text.slice(last, m.index).replace(/[ \t]+$/, '');
        if (before) frag.appendChild(document.createTextNode(before));
        var sup = el('sup', 'cite');
        nums.forEach(function (n, i) {
          var source = sources[n - 1] || {};
          var a = el('a', 'cite-link', String(n));
          a.href = source.url || '#';
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.dataset.cite = String(n);
          a.title = (source.title || source.domain || 'Source ' + n) + (source.domain ? ' · ' + source.domain : '');
          a.setAttribute('aria-label', 'Source ' + n + ': ' + a.title);
          sup.appendChild(a);
          if (i < nums.length - 1) sup.appendChild(document.createTextNode(','));
          made += 1;
        });
        frag.appendChild(sup);
        last = m.index + m[0].length;
      }
      if (!any) return;
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
    if (made && chipsRoot) chipsRoot.classList.add('has-citations');
    return made;
  }

  /* A citation click scrolls to its chip and highlights it; Ctrl/Cmd/middle
   * click still opens the source itself. */
  document.addEventListener('click', function (event) {
    var link = event.target.closest ? event.target.closest('a.cite-link') : null;
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    var msg = link.closest('.msg');
    var chip = msg && msg.querySelector('.msg-sources .source-chip[data-n="' + link.dataset.cite + '"]');
    if (!chip) return;
    event.preventDefault();
    chip.scrollIntoView({ block: 'nearest', behavior: util.reducedMotion() ? 'auto' : 'smooth' });
    chip.focus({ preventScroll: true });
    chip.classList.remove('is-cited');
    void chip.offsetWidth;
    chip.classList.add('is-cited');
    setTimeout(function () { chip.classList.remove('is-cited'); }, 1600);
  });

  /* ----------------------------------------------------------- gallery */

  function gallery(demo, label, nodes) {
    var T = 'demo';
    var PLAN = { type: 'research.plan', turn_id: T, needs_web: true, method: 'model', freshness: 'live',
      reason: 'the question is about the present (who holds an office now), so a live source is needed',
      queries: ['current UN Secretary-General', 'UN Secretary-General 2026'] };
    /* The exact frames services/research/engine.py publishes: one per
     * finished search ({source, query, ok, results, error?, blocked?,
     * elapsed}) and per finished page ({url, status, ok, chars, error}). */
    function search(source, query, st, count, extra) {
      return Object.assign({ type: 'research.search', turn_id: T, source: source, query: query,
        ok: st === 'done', results: count || 0, elapsed: 1.2 }, extra || {});
    }
    function fetch(url, st, extra) {
      return Object.assign({ type: 'research.fetch', turn_id: T, url: url, status: st === 'done' ? 200 : 403,
        ok: st === 'done', chars: 0, error: st === 'done' ? '' : 'HTTP 403' }, extra || {});
    }
    var SOURCES = [
      { title: 'Secretary-General of the United Nations', url: 'https://en.wikipedia.org/wiki/Secretary-General_of_the_United_Nations', domain: 'en.wikipedia.org', provider: 'wikipedia',
        snippet: 'The secretary-general is the chief administrative officer of the United Nations.' },
      { title: 'The Secretary-General', url: 'https://www.un.org/sg/en', domain: 'un.org', provider: 'direct', snippet: 'Biography and statements of the Secretary-General.' },
      { title: 'UN chief calls for ceasefire', url: 'https://news.un.org/en/story/2026/10/1', domain: 'news.un.org', provider: 'google_news' }
    ];

    label('Research: the AI plans its lookups ("Looking up" with the reason); every source is asked at once');
    nodes.push(demo.user('Who is the UN Secretary-General right now?'));
    nodes.push(demo.turn({ state: 'thinking', events: [PLAN] }));

    label('Research: searching, source by source (spinner, then the number of results; a refused source says so)');
    nodes.push(demo.turn({ state: 'thinking', events: [PLAN,
      search('wikipedia', PLAN.queries[0], 'done', 3),
      search('searxng_public', PLAN.queries[0], 'error', 0, { error: 'the public instance answered 429 (rate limited)', blocked: true })] }));

    label('Research: reading the best pages (domain chips fill in as each page is read; unused sources go quiet)');
    nodes.push(demo.turn({ state: 'thinking', events: [PLAN,
      search('wikipedia', PLAN.queries[0], 'done', 3), search('searxng_public', PLAN.queries[0], 'done', 8), search('news_rss', PLAN.queries[1], 'done', 5),
      fetch(SOURCES[0].url, 'done', { chars: 18200 }), fetch(SOURCES[1].url, 'done', { chars: 6400 }), fetch('https://www.reuters.com/world/', 'error')] }));

    label('Research done: verified badge, numbered source chips, and [n] in the answer as superscript links to them');
    nodes.push(demo.turn({
      steps: ['The current UN Secretary-General is **António Guterres** [1], in office since 1 January 2017 and re-appointed for a second term [1][2]. ' +
        'He spoke on the latest ceasefire talks this week [3]. Numbers without a source, like [7], stay as text; code such as `a[1]` is untouched.'],
      events: [PLAN,
        search('wikipedia', PLAN.queries[0], 'done', 3), search('searxng_public', PLAN.queries[0], 'done', 8), search('news_rss', PLAN.queries[1], 'done', 5),
        fetch(SOURCES[0].url, 'done', { chars: 18200 }), fetch(SOURCES[1].url, 'done', { chars: 6400 }), fetch('https://www.reuters.com/world/', 'error'),
        fetch(SOURCES[2].url, 'done', { chars: 3100 }),
        { type: 'research.done', turn_id: T, ok: true, sources: SOURCES, passages: 9, hits: 16, errors: [], offline: false, blocked: false, elapsed: 3.4 }],
      end: { verification: 'verified', sources: SOURCES }
    }));

    label('Research: no lookup needed (the plan says why, nothing else is shown)');
    nodes.push(demo.turn({
      steps: ['A prime number has exactly two divisors: 1 and itself. 2, 3, 5 and 7 are the first four.'],
      events: [{ type: 'research.plan', turn_id: T, needs_web: false, method: 'model', reason: 'a definition that does not change over time', queries: [] }]
    }));
  }

  IM.research = {
    handles: function (type) { return TYPES.indexOf(type) !== -1; },
    onEvent: onEvent,
    citations: citations,
    laneOf: laneOf,
    phase: phase,
    expand: expand,
    gallery: gallery
  };
})();
