/* Chat: transcript rendering, the composer, and the streaming turn.
 *
 *   IMURUN.chat.send(text, sessionId)     append the prompt and start a turn
 *   IMURUN.chat.loadTranscript(id)        fetch and render a session
 *   IMURUN.chat.clear()                   empty transcript (shows empty state)
 *   IMURUN.chat.stop()                    cancel the live turn
 *   IMURUN.chat.isStreaming()
 *   IMURUN.chat.onServerEvent(event)      web_search.* and queue frames
 *
 * One Turn object owns one assistant message from the moment the prompt is
 * sent. The node exists before the POST, so a token can never arrive before
 * its UI. Frames are applied in arrival order per step: `token` appends to
 * its step, `step` replaces that step with the authoritative text, and the
 * last step is the answer (earlier steps are shown inside the thinking
 * panel). Rendering is batched to one frame.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var STICK_THRESHOLD = 96;
  var SLOW_MS = 6000;

  var transcript = null;
  var jumpButton = null;
  var prompt = null;
  var sendButton = null;
  var stopButton = null;
  var current = null;          /* the live Turn, or null */
  var engineWarm = false;      /* the model has answered once this page load */
  var stick = true;            /* keep the view pinned to the newest text */

  function md() { return window.__imurun_markdown; }
  function api() { return window.__imurun_api; }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }

  function renderMarkdown(text) {
    var renderer = md();
    if (renderer) return renderer.render(text || '');
    return '<p class="md-p">' + util.escapeHtml(text || '') + '</p>';
  }

  function split(raw, streaming) {
    var renderer = md();
    if (renderer && renderer.splitReasoning) return renderer.splitReasoning(raw, { streaming: streaming });
    return { reasoning: '', answer: String(raw || ''), thinking: false, hasReasoning: false };
  }

  /* ------------------------------------------------- smooth DOM updates */

  /* Patch `container` to match `html` without rebuilding what did not
   * change. Re-assigning innerHTML on every frame made fast replies flicker:
   * earlier paragraphs were recreated, code blocks lost their scroll
   * position and copy-button state, and selection was lost. Here, unchanged
   * leading blocks are kept, the block being written is updated in place
   * (code blocks only get their text updated), and genuinely new blocks
   * fade in. */
  function patchHtml(container, html, animateNew) {
    var template = document.createElement('template');
    template.innerHTML = html;
    var next = Array.prototype.slice.call(template.content.childNodes);
    var index = 0;
    while (index < next.length) {
      var cur = container.childNodes[index];
      var incoming = next[index];
      if (!cur) {
        container.appendChild(incoming);
        if (animateNew && incoming.nodeType === 1) incoming.classList.add('md-enter');
      } else if (cur.nodeType === 1 && incoming.nodeType === 1 &&
          cur.tagName === incoming.tagName && baseClass(cur.className) === baseClass(incoming.className)) {
        if (cur.outerHTML !== incoming.outerHTML && !sameBlock(cur, incoming)) updateBlock(cur, incoming);
      } else if (!(cur.nodeType === 3 && incoming.nodeType === 3 && cur.data === incoming.data)) {
        container.replaceChild(incoming, cur);
        if (animateNew && incoming.nodeType === 1) incoming.classList.add('md-enter');
      }
      index += 1;
    }
    while (container.childNodes.length > next.length) container.removeChild(container.lastChild);
  }

  /* Class list without the transient state classes (entering, open fence). */
  function baseClass(name) {
    return String(name || '').replace(/\b(md-enter|is-open)\b/g, '').replace(/\s+/g, ' ').trim();
  }

  /* Blocks equal apart from the transient classes we add are left alone. */
  function sameBlock(cur, incoming) {
    var clone = cur.cloneNode(true);
    clone.classList.remove('md-enter');
    var copy = clone.querySelector('.md-copy.is-done');
    if (copy) {
      copy.classList.remove('is-done');
      copy.textContent = 'Copy';
    }
    return clone.outerHTML === incoming.outerHTML;
  }

  function updateBlock(cur, incoming) {
    if (cur.classList.contains('md-codeblock')) {
      /* Only the code text grows while streaming; keep the header, the copy
       * button and the horizontal scroll position. */
      var code = cur.querySelector('pre code');
      var nextCode = incoming.querySelector('pre code');
      if (code && nextCode && code.textContent !== nextCode.textContent) code.textContent = nextCode.textContent;
      cur.classList.toggle('is-open', incoming.classList.contains('is-open'));
      var lang = cur.querySelector('.md-lang');
      var nextLang = incoming.querySelector('.md-lang');
      if (lang && nextLang && lang.textContent !== nextLang.textContent) lang.textContent = nextLang.textContent;
      return;
    }
    if (cur.classList.contains('md-table-wrap')) {
      var body = cur.querySelector('tbody');
      var nextBody = incoming.querySelector('tbody');
      var head = cur.querySelector('thead');
      var nextHead = incoming.querySelector('thead');
      if (body && nextBody && head && nextHead && head.innerHTML === nextHead.innerHTML) {
        if (body.innerHTML !== nextBody.innerHTML) body.innerHTML = nextBody.innerHTML;
        return;
      }
    }
    if (cur.innerHTML !== incoming.innerHTML) cur.innerHTML = incoming.innerHTML;
  }

  var EASE = 'cubic-bezier(.2, .8, .2, 1)';

  /* Open or close a reasoning panel with a height animation instead of a
   * jump. Falls back to an instant toggle under reduced motion. */
  function animateDetails(details, open) {
    var body = details.querySelector('.thinking-body');
    if (details._anim) {
      details._anim.cancel();
      details._anim = null;
    }
    if (util.reducedMotion() || !body || !body.animate) {
      details.open = open;
      return;
    }
    if (open) {
      details.open = true;
      var height = body.offsetHeight;
      details._anim = body.animate(
        [{ height: '0px', opacity: 0, transform: 'translateY(-4px)' }, { height: height + 'px', opacity: 1, transform: 'none' }],
        { duration: 260, easing: EASE });
      details._anim.onfinish = function () { details._anim = null; };
    } else {
      if (!details.open) return;
      var from = body.offsetHeight;
      details._anim = body.animate(
        [{ height: from + 'px', opacity: 1 }, { height: '0px', opacity: 0 }],
        { duration: 200, easing: EASE });
      details._anim.onfinish = function () {
        details.open = false;
        details._anim = null;
      };
    }
  }

  /* Fold an element away (height and opacity) and then hide it. */
  function collapseAway(el) {
    if (!el || el.hidden) return;
    if (util.reducedMotion() || !el.animate) {
      el.hidden = true;
      return;
    }
    var height = el.offsetHeight;
    el.style.overflow = 'hidden';
    var anim = el.animate(
      [{ height: height + 'px', opacity: 1 }, { height: '0px', opacity: 0, marginBottom: '0px' }],
      { duration: 220, easing: EASE });
    anim.onfinish = function () {
      el.hidden = true;
      el.style.overflow = '';
    };
  }

  /* ----------------------------------------------------------- scrolling */

  function nearBottom() {
    if (!transcript) return true;
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < STICK_THRESHOLD;
  }

  function scrollToEnd(force) {
    if (!transcript) return;
    if (!force && !stick) {
      if (jumpButton) jumpButton.hidden = false;
      return;
    }
    transcript.scrollTop = transcript.scrollHeight;
    if (jumpButton) jumpButton.hidden = true;
  }

  /* ------------------------------------------------------------ messages */

  function messageNode(role) {
    var node = document.createElement('div');
    node.className = 'msg ' + role;
    if (!util.reducedMotion()) node.classList.add('is-entering');
    var who = document.createElement('div');
    who.className = 'who';
    who.textContent = role === 'user' ? 'You' : 'Imurun';
    node.appendChild(who);
    return node;
  }

  function userMessage(text, options) {
    var node = messageNode('user');
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    var paragraph = document.createElement('p');
    paragraph.className = 'md-p';
    paragraph.textContent = text;
    bubble.appendChild(paragraph);
    node.appendChild(bubble);
    if (options && options.queued) {
      var chip = document.createElement('span');
      chip.className = 'msg-chip';
      chip.textContent = 'Queued for the next step';
      node.appendChild(chip);
      node.classList.add('is-queued');
    }
    return node;
  }

  function thinkingPanel() {
    var details = document.createElement('details');
    details.className = 'thinking';
    /* The brand mark doubles as the live indicator: it spins while the
     * model works (styles/brand.css). The old orb is the fallback. */
    var orb = IM.brand
      ? '<span class="thinking-orb is-mark" aria-hidden="true">' + IM.brand.logo({ size: 18, label: false, live: 'thinking' }) + '</span>'
      : '<span class="thinking-orb" aria-hidden="true"><i></i><i></i><i></i></span>';
    details.innerHTML =
      '<summary>' + orb +
        '<span class="thinking-label">Sending</span>' +
        '<span class="thinking-time"></span>' +
        '<span class="thinking-preview" aria-hidden="true"></span>' +
      '</summary>' +
      '<div class="thinking-body"><p class="thinking-empty">Waiting for the first token…</p></div>';
    return details;
  }

  /* Static (history) rendering of one assistant message, reasoning folded. */
  function assistantMessage(raw) {
    var node = messageNode('assistant');
    var parts = split(raw, false);
    if (parts.hasReasoning && parts.reasoning) {
      var details = thinkingPanel();
      details.classList.add('has-trace');
      details.querySelector('.thinking-label').textContent = 'Thought process';
      details.querySelector('.thinking-time').textContent = '';
      var body = details.querySelector('.thinking-body');
      body.innerHTML = '';
      var text = document.createElement('p');
      text.className = 'thinking-text';
      text.textContent = parts.reasoning;
      body.appendChild(text);
      node.appendChild(details);
    }
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    var answer = cleanFinal(parts.answer || (parts.hasReasoning ? '' : raw));
    bubble.innerHTML = renderMarkdown(answer);
    node.appendChild(bubble);
    node._answer = answer || '';
    node.appendChild(messageActions());
    return node;
  }

  function messageActions() {
    var row = document.createElement('div');
    row.className = 'msg-actions';
    row.innerHTML = '<button class="msg-action" type="button" data-msg-copy aria-label="Copy reply">Copy</button>' +
      '<button class="msg-action msg-speak" type="button" data-msg-speak aria-label="Read reply aloud" aria-pressed="false">Read aloud</button>';
    return row;
  }

  function emptyState() {
    var node = document.createElement('div');
    node.className = 'chat-empty';
    node.innerHTML =
      '<span class="chat-empty-mark" aria-hidden="true">&#9672;</span>' +
      '<h2>Start the conversation</h2>' +
      '<p>Ask a question, paste code to review, or describe what you want built. Replies stream from the model on this machine.</p>' +
      '<div class="chat-empty-chips">' +
        '<button class="home-chip" type="button" data-chat-suggest="Explain what this workspace does and where to start.">Explain this workspace</button>' +
        '<button class="home-chip" type="button" data-chat-suggest="Write a small Python function that parses an ISO date and test it.">Write and test a function</button>' +
        '<button class="home-chip" type="button" data-chat-suggest="Summarise the trade-offs of q4 versus q8 quantisation.">Compare quantisations</button>' +
      '</div>';
    return node;
  }

  function clear() {
    if (!transcript) return;
    transcript.innerHTML = '';
    transcript.appendChild(emptyState());
    stick = true;
  }

  function removeEmptyState() {
    if (!transcript) return;
    var empty = transcript.querySelector('.chat-empty');
    if (empty) empty.remove();
  }

  function renderTranscript(messages) {
    if (!transcript) return;
    transcript.innerHTML = '';
    var list = (messages || []).filter(function (message) {
      return message && (message.role === 'user' || message.role === 'assistant');
    });
    if (!list.length) {
      transcript.appendChild(emptyState());
      return;
    }
    var fragment = document.createDocumentFragment();
    list.forEach(function (message) {
      var node = message.role === 'user'
        ? userMessage(message.content || '')
        : assistantMessage(message.content || '');
      node.classList.remove('is-entering'); if (message.turn_id) node.dataset.turnId = message.turn_id; /* feedback.js */
      fragment.appendChild(node);
    });
    transcript.appendChild(fragment);
    stick = true;
    scrollToEnd(true);
  }

  var loadSeq = 0;
  async function loadTranscript(id) {
    if (!transcript) return;
    var seq = ++loadSeq;
    transcript.setAttribute('aria-busy', 'true');
    transcript.innerHTML =
      '<div class="transcript-loading" role="status">' +
        '<span class="sk sk-bubble"></span><span class="sk sk-bubble is-right"></span><span class="sk sk-bubble"></span>' +
        '<span class="sr-only">Loading session</span>' +
      '</div>';
    try {
      var messages = await api().getMessages(id, 400);
      if (seq !== loadSeq || IM.state.activeSessionId !== id) return;
      renderTranscript(messages);
      /* A reply still streaming into this session is re-attached at the end. */
      if (current && current.sessionId === id && !current.done && current.node) {
        transcript.appendChild(current.node);
        scrollToEnd(true);
      }
    } catch (error) {
      if (seq !== loadSeq) return;
      transcript.innerHTML = '';
      var failed = document.createElement('div');
      failed.className = 'chat-empty is-error';
      failed.innerHTML = '<h2>Could not load this session</h2><p></p>' +
        '<button class="btn" type="button" data-chat-retry>Try again</button>';
      failed.querySelector('p').textContent = error.message;
      transcript.appendChild(failed);
    } finally {
      if (seq === loadSeq) transcript.setAttribute('aria-busy', 'false');
    }
  }

  /* --------------------------------------------------------------- turns */

  function Turn(sessionId, text) {
    this.sessionId = sessionId;
    this.text = text;
    this.steps = {};
    this.order = [];
    this.turnId = null;
    this.done = false;
    this.stopRequested = false;
    this.status = 'running';
    this.startedAt = performance.now();
    this.postedAt = null;
    this.firstTokenAt = null;
    this.endedAt = null;
    this.tokens = 0;
    this.stepCount = 0;
    this.renderQueued = false;
    this.userToggled = false;
    this.kinds = {};           /* step -> 'tool' | 'answer' (from the `step` frame) */
    this.blocks = {};          /* step -> trace block in the thinking panel */
    this.activity = null;      /* activity.js controller, created on the first tool frame */
    this.build();
  }

  /* Bracketed "[note: unable to verify]" style notes are dropped from the
   * final text: the verification badge carries that meaning (activity.js). */
  function cleanFinal(text) {
    return IM.activity && IM.activity.stripNotes ? IM.activity.stripNotes(text) : text;
  }

  Turn.prototype.build = function () {
    var self = this;
    var node = messageNode('assistant');
    node.classList.add('is-thinking');
    node.setAttribute('aria-busy', 'true');
    this.details = thinkingPanel();
    this.details.classList.add('is-live');
    node.appendChild(this.details);
    this.search = null;
    this.bubble = document.createElement('div');
    this.bubble.className = 'bubble';
    node.appendChild(this.bubble);
    this.node = node;
    this.label = this.details.querySelector('.thinking-label');
    this.time = this.details.querySelector('.thinking-time');
    this.body = this.details.querySelector('.thinking-body');
    this.preview = this.details.querySelector('.thinking-preview');
    /* User clicks on the summary are animated (and remembered) by the
     * delegated handler in bindTranscript. */
    this.details._turn = this;

    this.clock = setInterval(function () { self.tick(); }, 100);
    this.slowTimer = setTimeout(function () {
      if (self.firstTokenAt || self.done) return;
      self.setLabel(engineWarm ? 'Still working' : 'Loading the model');
    }, SLOW_MS);
  };

  Turn.prototype.elapsed = function () {
    var end = this.endedAt || performance.now();
    return (end - this.startedAt) / 1000;
  };

  Turn.prototype.tick = function () {
    if (this.done) return;
    var parts = this.lastParts;
    if (!this.firstTokenAt || (parts && parts.thinking)) {
      this.time.textContent = this.elapsed().toFixed(1) + 's';
    }
  };

  /* Change the status label only when it really changes, with a short
   * cross-fade, so fast frames do not restart animations or jitter. */
  Turn.prototype.setLabel = function (text) {
    var label = this.label;
    if (!label || label.textContent === text) return;
    label.textContent = text;
    if (util.reducedMotion()) return;
    label.classList.remove('is-swapping');
    void label.offsetWidth;
    label.classList.add('is-swapping');
  };

  Turn.prototype.onTurnId = function (id) {
    this.turnId = id;
    this.postedAt = performance.now();
    if (!this.firstTokenAt) this.setLabel(engineWarm ? 'Thinking' : 'Waiting for the engine');
    if (this.stopRequested) this.cancel();
    /* An approval that arrived before the POST named the turn was shown as a
     * toast; now it can move into this reply. */
    if (IM.approvals && !this.demo) IM.approvals.sync();
  };

  Turn.prototype.onEvent = function (event) {
    if (this.done) return;
    if (event.type === 'step.start') {
      var index = Number(event.step) || this.order.length + 1;
      this.ensureStep(index);
      this.stepCount = Math.max(this.stepCount, index);
      if (index > 1) this.setLabel('Step ' + index);
      this.schedule();
    } else if (event.type === 'step') {
      var step = Number(event.step) || this.order.length || 1;
      this.ensureStep(step);
      if (typeof event.text === 'string') this.steps[step] = event.text;
      if (event.kind) this.kinds[step] = event.kind;
      if (IM.activity) IM.activity.noteVerification(this, event);
      this.schedule();
    } else if (IM.activity && IM.activity.handles(event.type)) {
      if (!this.activity) this.activity = IM.activity.attach(this);
      this.activity.onEvent(event);
      this.schedule();
    }
  };

  /* The trace block for one step inside the thinking panel, created in step
   * order. Each block holds that step's reasoning text and (activity.js) its
   * tool rows, so tools appear right after the thought that called them. */
  Turn.prototype.traceBlock = function (step) {
    var key = String(step);
    if (this.blocks[key]) return this.blocks[key];
    var empty = this.body.querySelector('.thinking-empty');
    if (empty) empty.remove();
    var block = document.createElement('div');
    block.className = 'trace-step';
    block.dataset.step = key;
    block.dataset.label = Number(step) <= 0 ? 'Looked up first' : 'Step ' + step;
    var text = document.createElement('p');
    text.className = 'thinking-text';
    text.hidden = true;
    block.appendChild(text);
    block._text = text;
    var next = null;
    Array.prototype.forEach.call(this.body.children, function (child) {
      if (!next && child.dataset && child.dataset.step !== undefined && Number(child.dataset.step) > Number(step)) next = child;
    });
    this.body.insertBefore(block, next);
    this.body.classList.toggle('is-multi', this.body.querySelectorAll('.trace-step').length > 1);
    this.details.classList.add('has-trace');
    this.blocks[key] = block;
    return block;
  };

  /* A tool step started after the pill had folded for an answer: bring the
   * live status back, the "answer" so far was the text before a tool call. */
  Turn.prototype.unfold = function () {
    if (this.done || !this.foldedAt) return;
    this.foldedAt = null;
    /* A fold-away still running would hide the pill again when it ends. */
    if (this.details.getAnimations) this.details.getAnimations().forEach(function (anim) { anim.cancel(); });
    this.details.hidden = false;
    this.details.style.overflow = '';
    this.details.classList.add('is-live');
    this.node.classList.remove('is-streaming');
    this.node.classList.add('is-thinking');
    this.bubble.classList.remove('is-revealing');
  };

  Turn.prototype.ensureStep = function (index) {
    if (this.steps[index] === undefined) {
      this.steps[index] = '';
      this.order.push(index);
      this.order.sort(function (a, b) { return a - b; });
    }
  };

  Turn.prototype.onToken = function (token, event) {
    if (this.done) return;
    var index = Number(event && event.step) || (this.order.length ? this.order[this.order.length - 1] : 1);
    this.ensureStep(index);
    this.steps[index] += token;
    this.tokens += 1;
    if (!this.firstTokenAt) {
      this.firstTokenAt = performance.now();
      engineWarm = true;
      clearTimeout(this.slowTimer);
    }
    this.schedule();
  };

  /* Batch frames into at most one render per animation frame, and back off
   * (to ~30 or ~16 renders a second) when renders get expensive on long
   * answers, so a fast model never makes the page stutter. */
  Turn.prototype.schedule = function () {
    if (this.renderQueued) return;
    this.renderQueued = true;
    var self = this;
    var raf = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
    var wait = (this.lastRenderAt || 0) + (this.minGap || 0) - performance.now();
    function go() {
      raf(function () {
        self.renderQueued = false;
        var t0 = performance.now();
        self.render();
        var cost = performance.now() - t0;
        self.lastRenderAt = performance.now();
        self.minGap = cost > 12 ? 60 : (cost > 5 ? 33 : 0);
      });
    }
    if (wait > 0) setTimeout(go, wait);
    else go();
  };

  /* Text of the answer step and of the earlier (tool) steps. */
  /* A step whose `step` frame said kind "tool" is never the answer, even
   * while it is the newest step: its text belongs in the thinking panel. */
  Turn.prototype.parts = function () {
    var last = this.order.length ? this.order[this.order.length - 1] : null;
    var lastIsTool = last !== null && this.kinds[last] === 'tool';
    var answerRaw = (last === null || lastIsTool) ? '' : this.steps[last];
    var strip = this.activity && IM.activity ? IM.activity.stripCalls : null;
    var trace = (lastIsTool ? this.order : this.order.slice(0, -1)).map(function (index) {
      var piece = split(this.steps[index], false);
      /* With live tool rows, the server's "Using `tool` {...}" lines are
       * shown as rows instead of text. */
      var answer = strip ? strip(piece.answer) : piece.answer;
      return { step: index, text: [piece.reasoning, answer].filter(Boolean).join('\n').trim() };
    }, this);
    var parts = split(answerRaw, !this.done);
    if (this.done) parts.answer = cleanFinal(parts.answer);
    parts.trace = trace;
    parts.lastStep = last;
    parts.lastIsTool = lastIsTool;
    parts.earlier = trace.filter(function (t) { return t.text; }).map(function (t) { return 'Step ' + t.step + '\n' + t.text; });
    return parts;
  };

  Turn.prototype.render = function () {
    if (!this.node) return;
    var self = this;
    var parts = this.parts();
    this.lastParts = parts;
    this.hasThought = this.hasThought || Boolean(parts.reasoning);
    this.toolSteps = parts.trace.length;
    var trace = parts.trace.slice();
    if (parts.reasoning) trace.push({ step: parts.lastStep || 1, text: parts.reasoning });
    var reasoning = trace.map(function (t) { return t.text; }).filter(Boolean).join('\n\n');

    if (parts.lastIsTool && this.foldedAt) this.unfold();
    if (reasoning && reasoning !== this.lastReasoning) {
      this.lastReasoning = reasoning;
      /* Keep the trace pinned to its newest line only if the reader has not
       * scrolled up inside it. */
      var pinned = this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 28;
      trace.forEach(function (t) {
        if (!t.text && !self.blocks[String(t.step)]) return;
        var text = self.traceBlock(t.step)._text;
        if (text.textContent !== t.text) text.textContent = t.text;
        text.hidden = !t.text;
      });
      if (this.details.open && pinned) this.body.scrollTop = this.body.scrollHeight;
      if (parts.thinking) {
        this.setLabel('Thinking');
        /* A one-line live preview, so the panel says something while closed. */
        var lastLine = parts.reasoning.split('\n').filter(Boolean).pop() || '';
        if (this.preview) this.preview.textContent = lastLine.slice(-120);
      }
    }

    var answer = parts.answer;
    if (answer.trim()) {
      if (this.node.classList.contains('is-thinking')) {
        this.node.classList.remove('is-thinking');
        this.node.classList.add('is-streaming');
        if (!util.reducedMotion()) this.bubble.classList.add('is-revealing');
        this.foldThinking(reasoning);
      }
      if (answer !== this.lastAnswer) {
        this.lastAnswer = answer;
        patchHtml(this.bubble, renderMarkdown(answer), !this.done && !util.reducedMotion());
      }
    } else if (this.lastAnswer) {
      /* The text streamed so far turned out to be a tool step's preamble. */
      this.lastAnswer = '';
      patchHtml(this.bubble, '', false);
    }
    scrollToEnd(false);
  };

  /* Close the reasoning panel once the answer starts (unless the user opened
   * it on purpose) and label it with how long the model thought. */
  Turn.prototype.foldThinking = function (reasoning) {
    this.foldedAt = performance.now();
    this.details.classList.remove('is-live');
    var tools = this.activity ? this.activity.count : 0;
    if (!reasoning && !tools && !(this.research && !this.research.skip)) {
      /* Nothing to show: fold the status row away instead of letting the
       * answer jump up by its height. */
      collapseAway(this.details);
      return;
    }
    if (!this.userToggled) animateDetails(this.details, false);
    if (this.preview) this.preview.textContent = '';
    var steps = this.toolSteps || 0;
    var toolText = tools ? 'used ' + tools + (tools === 1 ? ' tool' : ' tools') : '';
    if (this.hasThought) {
      this.setLabel('Thought for ' + this.elapsed().toFixed(1) + 's' + (toolText ? ' · ' + toolText : (steps ? ' · ' + steps + (steps === 1 ? ' step' : ' steps') : '')));
    } else if (tools) {
      this.setLabel(toolText.charAt(0).toUpperCase() + toolText.slice(1) + (steps > 1 ? ' · ' + steps + ' steps' : ''));
    } else {
      this.setLabel(this.research && this.research.summary ? this.research.summary() : 'Worked through ' + steps + (steps === 1 ? ' step' : ' steps'));
    }
    this.time.textContent = '';
  };

  Turn.prototype.cleanup = function () {
    clearInterval(this.clock);
    clearTimeout(this.slowTimer);
    clearTimeout(this.cancelTimer);
  };

  Turn.prototype.finish = function (event) {
    if (this.done) return;
    var evt = event || {};
    var parts = this.parts();
    /* Engines without token frames (or a recovered turn) deliver the text in
     * turn.end only. */
    if (!parts.answer.trim() && evt.answer) {
      var last = this.order.length ? this.order[this.order.length - 1] : 1;
      /* Never overwrite a tool step's trace with the answer. */
      if (parts.lastIsTool) last += 1;
      this.ensureStep(last);
      this.steps[last] = String(evt.answer);
      this.kinds[last] = 'answer';
    }
    this.done = true;
    this.endedAt = performance.now();
    this.status = evt.status || 'done';
    this.cleanup();
    if (IM.activity) IM.activity.noteVerification(this, evt);
    this.render();
    parts = this.lastParts;
    var reasoning = parts.earlier.concat(parts.reasoning ? [parts.reasoning] : []).join('\n\n');
    this.node.classList.remove('is-thinking', 'is-streaming');
    this.node.removeAttribute('aria-busy');
    if (!parts.answer.trim()) {
      this.bubble.innerHTML = '<p class="md-p muted">' +
        (this.status === 'stopped' ? 'Stopped before the model wrote an answer.' : 'The model returned an empty reply.') + '</p>';
    }
    if (!this.foldedAt) this.foldThinking(reasoning);
    this.node._answer = parts.answer; if (this.turnId) this.node.dataset.turnId = this.turnId; /* feedback.js: thumbs/Teach */
    if (this.status === 'stopped') this.addChip('Stopped', 'is-warn');
    if (evt.recovered) this.addChip('Recovered after the stream dropped', '');
    this.node.appendChild(messageActions());
    this.addStats(evt);
    /* "Used: ..." footer, skill chips, verification badge and sources. */
    if (IM.activity) IM.activity.finish(this, evt);
    if (!this.demo) util.announce(this.status === 'stopped' ? 'Generation stopped.' : 'Imurun replied.');
  };

  Turn.prototype.addChip = function (text, cls) {
    var chip = document.createElement('span');
    chip.className = 'msg-chip ' + (cls || '');
    chip.textContent = text;
    this.node.appendChild(chip);
  };

  /* Per-turn numbers. Prefer engine-reported stats (turn.end `stats`, when the
   * server provides them); otherwise measure in the browser and say so. */
  Turn.prototype.addStats = function (event) {
    var engine = (event && event.stats && typeof event.stats === 'object') ? event.stats : null;
    var ttft = this.firstTokenAt ? (this.firstTokenAt - (this.postedAt || this.startedAt)) / 1000 : null;
    var decode = this.firstTokenAt && this.tokens > 1 ? (this.endedAt - this.firstTokenAt) / 1000 : null;
    /* Under a quarter second the rate is noise (buffered frames arrive in
     * one burst); report nothing rather than "15000 tok/s". */
    if (decode !== null && decode < 0.25) decode = null;
    var stats = {
      sessionId: this.sessionId,
      turnId: this.turnId,
      status: this.status,
      at: Date.now(),
      source: engine ? 'engine' : 'browser',
      tokens: engine && engine.generated_tokens ? Number(engine.generated_tokens) : this.tokens,
      promptTokens: engine && engine.prompt_tokens ? Number(engine.prompt_tokens) : null,
      tokensPerSecond: engine && engine.tokens_per_second
        ? Number(engine.tokens_per_second)
        : (decode && decode > 0 ? (this.tokens - 1) / decode : null),
      prefillSeconds: engine && engine.prefill_seconds !== undefined ? Number(engine.prefill_seconds) : null,
      firstTokenSeconds: ttft,
      totalSeconds: (this.endedAt - this.startedAt) / 1000,
      contextUsed: engine && engine.context_used ? Number(engine.context_used) : null,
      contextSize: engine && engine.context_size ? Number(engine.context_size) : null,
      steps: Number(event && event.steps) || this.stepCount || this.order.length
    };
    this.stats = stats;

    var bits = [];
    if (stats.tokens) bits.push(util.formatTokens(stats.tokens) + ' tok');
    if (stats.tokensPerSecond) bits.push(stats.tokensPerSecond.toFixed(1) + ' tok/s');
    if (stats.prefillSeconds !== null) bits.push('prefill ' + util.formatSeconds(stats.prefillSeconds));
    else if (stats.firstTokenSeconds !== null) bits.push('first token ' + util.formatSeconds(stats.firstTokenSeconds));
    bits.push(util.formatSeconds(stats.totalSeconds));
    if (stats.steps > 1) bits.push(stats.steps + ' steps');
    var meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.title = stats.source === 'engine'
      ? 'Reported by the engine'
      : 'Measured in the browser from streamed chunks; the engine did not report counters';
    meta.textContent = bits.join('  •  ') + (stats.source === 'browser' && stats.tokensPerSecond ? '  (measured here)' : '');
    var actions = this.node.querySelector('.msg-actions');
    if (actions) actions.insertBefore(meta, actions.firstChild);
    else this.node.appendChild(meta);
    if (!this.demo) IM.bus.emit('turn.stats', stats);
  };

  Turn.prototype.fail = function (error, options) {
    if (this.done) return;
    this.done = true;
    this.endedAt = performance.now();
    this.status = 'error';
    this.cleanup();
    if (this.activity) this.activity.settle();
    var parts = this.parts();
    this.node.classList.remove('is-thinking', 'is-streaming');
    this.node.removeAttribute('aria-busy');
    this.foldThinking(parts.reasoning);
    var html = parts.answer.trim() ? renderMarkdown(parts.answer) : '';
    var note = document.createElement('div');
    note.className = 'msg-error' + (options && options.kind === 'choose-model' ? ' is-setup' : '');
    note.setAttribute('role', 'alert');
    var strong = document.createElement('strong');
    strong.textContent = (options && options.title) || 'The reply failed';
    var text = document.createElement('p');
    text.textContent = (options && options.text) || error.message;
    note.appendChild(strong);
    note.appendChild(text);
    if (options && options.actions) {
      var row = document.createElement('div');
      row.className = 'msg-error-actions';
      options.actions.forEach(function (action) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = action.primary ? 'btn primary' : 'btn';
        button.textContent = action.label;
        button.addEventListener('click', action.onClick);
        row.appendChild(button);
      });
      note.appendChild(row);
    }
    this.bubble.innerHTML = html;
    if (!html) this.bubble.hidden = true;
    this.node.appendChild(note);
    this.node._answer = parts.answer;
    if (!options || options.toast !== false) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Chat', title: (options && options.title) || 'Reply failed', text: (options && options.text) || error.message });
    }
  };

  Turn.prototype.cancel = function () {
    var self = this;
    if (this.done) return;
    this.stopRequested = true;
    this.setLabel('Stopping');
    if (!this.turnId) return; /* cancelled as soon as the POST names the turn */
    api().cancelTurn(this.turnId).then(function () {
      /* The server ends the turn with status "stopped"; if that frame never
       * arrives (a hung engine), stop waiting after a grace period. */
      self.cancelTimer = setTimeout(function () {
        if (!self.done) self.finish({ status: 'stopped' });
        if (current === self) settle();
      }, 10000);
    }, function (error) {
      self.stopRequested = false;
      if (!self.firstTokenAt) self.setLabel('Thinking');
      toast({ tone: 'error', icon: 'alert', kicker: 'Stop', title: 'Could not stop the reply', text: error.message });
      setComposerState();
    });
  };

  /* ------------------------------------------------------------- sending */

  function setComposerState() {
    var streaming = Boolean(current && !current.done);
    if (sendButton) {
      sendButton.disabled = false;
      sendButton.textContent = streaming ? 'Queue' : 'Send';
      sendButton.title = streaming ? 'Queue this prompt for the next step of the running reply' : 'Send (Enter)';
    }
    if (stopButton) {
      stopButton.disabled = !streaming || Boolean(current && current.stopRequested);
      stopButton.classList.toggle('is-live', streaming);
    }
    document.body.classList.toggle('is-generating', streaming);
    IM.state.turn = streaming ? current : null;
  }

  function settle() {
    current = null;
    setComposerState();
    IM.bus.emit('turn.settled');
    if (IM.status) IM.status.refresh();
  }

  function waitForEngine(timeoutMs, onTick) {
    var deadline = Date.now() + timeoutMs;
    return new Promise(function (resolve) {
      function check() {
        IM.status.refresh().then(function (status) {
          if (status && status.runtime === 'ready') {
            resolve(true);
            return;
          }
          if (Date.now() > deadline) {
            resolve(false);
            return;
          }
          if (onTick) onTick(Math.round((timeoutMs - (deadline - Date.now())) / 1000));
          setTimeout(check, 2000);
        });
      }
      check();
    });
  }

  function isUnavailable(error) {
    return Boolean(error) && (
      error.code === 'model_unavailable' ||
      error.status === 503 ||
      /model_unavailable|provisioning|not ready/i.test(error.message || '')
    );
  }

  function currentMode() {
    var active = document.querySelector('.mode.active');
    return active && active.dataset.mode ? active.dataset.mode : 'build';
  }

  function stream(turn) {
    return api().streamTurn(turn.text, {
      sessionId: turn.sessionId,
      mode: currentMode(),
      onTurnId: function (id) { turn.onTurnId(id); },
      onEvent: function (event) { turn.onEvent(event); }
    }, function (token, event) {
      turn.onToken(token, event);
    }, function (event) {
      turn.finish(event);
    }, function () { /* failure is handled once, in run() */ });
  }

  async function run(turn) {
    try {
      await stream(turn);
    } catch (error) {
      if (turn.done) return;
      if (isUnavailable(error) && !turn.retried) {
        await provisionAndRetry(turn, error);
        return;
      }
      if (turn.stopRequested) {
        turn.finish({ status: 'stopped' });
        return;
      }
      turn.fail(error);
    }
  }

  /* The server answered 503: be honest about why and try to start the
   * engine once, showing progress inline instead of blocking the shell. */
  async function provisionAndRetry(turn, error) {
    turn.retried = true;
    var noModel = IM.state.modelsDownloaded === 0;
    if (noModel) {
      turn.fail(error, {
        toast: false,
        title: 'No model downloaded',
        text: 'The engine needs a model before it can answer. Download one, then send your message again.',
        actions: [{ label: 'Open Models', primary: true, onClick: function () { if (IM.shell) IM.shell.openSettings('models'); } }]
      });
      return;
    }
    /* POST /api/turns answers 503 when no model is configured (or the engine
     * reports it cannot generate). With no model chosen, waiting for the
     * engine cannot help: say so and point at Models, no error toast. */
    var status = IM.status ? await IM.status.refresh() : null;
    if (turn.done) return;
    if (error.status === 503 && !(status && status.selected_model)) {
      turn.fail(error, chooseModelState(function () { retry(turn); }));
      return;
    }
    turn.setLabel('Starting the engine');
    try {
      await api().stackEnsure();
    } catch (startError) {
      /* No Docker stack (native engine): still wait, the engine may be
       * loading in-process. */
    }
    var ready = await waitForEngine(180000, function (seconds) {
      if (!turn.done) turn.setLabel('Starting the engine (' + seconds + 's)');
    });
    if (turn.done) return;
    if (!ready) {
      turn.fail(error, {
        toast: false,
        title: 'The engine is still provisioning',
        text: 'It did not become ready within three minutes. Check Settings → Server, or retry once it reports online.',
        actions: [
          { label: 'Retry', primary: true, onClick: function () { retry(turn); } },
          { label: 'Open Server settings', onClick: function () { if (IM.shell) IM.shell.openSettings('server'); } }
        ]
      });
      return;
    }
    turn.setLabel('Waiting for the engine');
    await run(turn);
  }

  function chooseModelState(onRetry) {
    return {
      toast: false,
      kind: 'choose-model',
      title: 'Choose a model',
      text: 'No model is selected for the engine, so nothing can answer yet. Pick one of your downloaded models (or download one) in Models, then send again.',
      actions: [
        { label: 'Open Models', primary: true, onClick: function () { if (IM.shell) IM.shell.openSettings('models'); } },
        { label: 'Try again', onClick: onRetry }
      ]
    };
  }

  function retry(turn) {
    if (current && !current.done) {
      toast({ tone: 'neutral', icon: 'clock', kicker: 'Chat', title: 'A reply is still running', text: 'Wait for it to finish or stop it first.' });
      return;
    }
    if (turn.node && turn.node.parentNode) turn.node.remove();
    start(turn.text, turn.sessionId);
  }

  function start(text, sessionId) {
    removeEmptyState();
    var turn = new Turn(sessionId, text);
    current = turn;
    if (transcript && IM.state.activeSessionId === sessionId) transcript.appendChild(turn.node);
    stick = true;
    scrollToEnd(true);
    setComposerState();
    run(turn).then(function () {
      if (current === turn) settle();
    });
    return turn;
  }

  /* Public entry: append the prompt and start a turn. */
  function send(text, sessionId) {
    var value = String(text || '').trim();
    if (!value || !sessionId) return null;
    if (current && !current.done) {
      queue(value, sessionId);
      return null;
    }
    removeEmptyState();
    if (transcript) transcript.appendChild(userMessage(value));
    return start(value, sessionId);
  }

  /* While a reply runs, a new prompt is queued into the agent loop
   * (POST /api/prompts) instead of racing a second turn. */
  function queue(value, sessionId) {
    if (current && current.sessionId !== sessionId) {
      toast({ tone: 'neutral', icon: 'clock', kicker: 'Chat', title: 'A reply is running in another session', text: 'Wait for it to finish or stop it, then send again.' });
      return false;
    }
    var node = userMessage(value, { queued: true });
    if (transcript && current && current.node && current.node.parentNode === transcript) {
      transcript.insertBefore(node, current.node.nextSibling);
    } else if (transcript) {
      transcript.appendChild(node);
    }
    scrollToEnd(true);
    api().queuePrompt(sessionId, value).catch(function (error) {
      node.classList.add('is-failed');
      var chip = node.querySelector('.msg-chip');
      if (chip) chip.textContent = 'Not queued: ' + error.message;
      toast({ tone: 'error', icon: 'alert', kicker: 'Queue', title: 'Prompt not queued', text: error.message });
    });
    return true;
  }

  function stop() {
    if (!current || current.done) return false;
    current.cancel();
    setComposerState();
    return true;
  }

  /* ------------------------------------------------------- server frames */

  function onServerEvent(event) {
    if (!current || current.done) return;
    if (event.type === 'web_search.start') {
      var box = current.search;
      if (!box) {
        box = document.createElement('div');
        box.className = 'web-search-indicator';
        box.setAttribute('role', 'status');
        current.node.insertBefore(box, current.bubble);
        current.search = box;
      }
      /* Three soft pulsing dots (static under reduced motion) and the query;
       * replaced by "Found N sources" when the search finishes. The sources
       * themselves are shown once, as chips under the reply (activity.js). */
      box.classList.remove('is-done', 'is-empty');
      box.innerHTML = '<span class="search-dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
        '<span class="web-search-label">Searching the web…</span><span class="web-search-query"></span>';
      box.querySelector('.web-search-query').textContent = event.query || '';
      current.setLabel('Searching the web');
    } else if (event.type === 'web_search.progress' && current.search) {
      var label = current.search.querySelector('.web-search-label');
      if (label && event.stage) label.textContent = 'Searching the web… ' + event.stage;
    } else if (event.type === 'web_search.done' && current.search) {
      /* `results` comes from the pre-fetch frame; a model-called search
       * (activity.js) only knows ok / failed. */
      var results = Array.isArray(event.results) ? event.results : null;
      if (results) current.searchResults = results;
      var found = results ? results.length : (event.ok ? -1 : 0);
      current.search.classList.add('is-done');
      current.search.classList.toggle('is-empty', found === 0);
      current.search.innerHTML = '<span class="search-check" aria-hidden="true"></span><span class="web-search-label"></span>';
      current.search.querySelector('.web-search-label').textContent = found > 0
        ? 'Found ' + found + (found === 1 ? ' source' : ' sources')
        : (found < 0 ? 'Search finished' : (event.ok === false ? 'The search failed' : 'The search found no sources'));
      if (!current.firstTokenAt) current.setLabel('Thinking');
    }
  }

  /* The same indicator for a search the model asked for (tool.call
   * web_search), applied to a given turn (activity.js, the gallery). */
  function searchFor(turn, event) {
    var saved = current;
    current = turn;
    try { onServerEvent(event); } finally { current = saved; }
  }

  /* ------------------------------------------------------------ composer */

  function bindComposer() {
    prompt = document.querySelector('.input-wrap textarea');
    sendButton = document.querySelector('.composer .send');
    stopButton = document.querySelector('.composer .stop');
    if (!prompt) return;
    prompt.setAttribute('aria-label', 'Message Imurun');
    prompt.id = prompt.id || 'composer-input';

    var maxPromptLines = 5;
    function resizePrompt() {
      prompt.style.height = 'auto';
      var style = window.getComputedStyle(prompt);
      var line = parseFloat(style.lineHeight);
      if (!line || isNaN(line)) line = (parseFloat(style.fontSize) || 15) * 1.5;
      var padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      var border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      var max = line * maxPromptLines + padding + border;
      prompt.style.height = Math.min(prompt.scrollHeight, max) + 'px';
      prompt.style.overflowY = prompt.scrollHeight > max ? 'auto' : 'hidden';
    }
    prompt.addEventListener('input', resizePrompt);
    window.addEventListener('resize', resizePrompt);
    resizePrompt();

    prompt.addEventListener('keydown', function (event) {
      var selectAll = (event.ctrlKey || event.metaKey) && !event.altKey &&
        (event.code === 'KeyA' || (event.key && event.key.toLowerCase() === 'a'));
      if (selectAll) {
        event.preventDefault();
        prompt.select();
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submit();
      }
    });

    bindCaret();

    if (sendButton) {
      sendButton.type = 'button';
      sendButton.addEventListener('click', function (event) {
        event.preventDefault();
        submit();
      });
    }
    if (stopButton) {
      stopButton.type = 'button';
      stopButton.setAttribute('aria-label', 'Stop generating');
      stopButton.title = 'Stop generating (Esc)';
      stopButton.addEventListener('click', stop);
    }
    var attach = document.querySelector('.composer-buttons .icon');
    if (attach) {
      attach.type = 'button';
      attach.disabled = true;
      attach.setAttribute('aria-label', 'Attach files (not available yet)');
      attach.title = 'Attachments are not supported by the local API yet';
    }

    /* Mode and effort */
    var modeButtons = document.querySelectorAll('.mode');
    function setMode(mode) {
      modeButtons.forEach(function (button) {
        var active = button.dataset.mode === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      IM.store.set('imurun.chat.mode', mode);
    }
    modeButtons.forEach(function (button) {
      button.addEventListener('click', function () { setMode(button.dataset.mode); });
    });
    setMode(IM.store.get('imurun.chat.mode', 'build') === 'plan' ? 'plan' : 'build');

    /* Effort maps onto the server's thinkingLevel setting. */
    var effort = document.getElementById('effort');
    var LEVELS = [['low', 'Low'], ['balanced', 'Balanced'], ['high', 'High']];
    function showEffort() {
      if (!effort) return;
      var settings = window.__imurun_settings ? window.__imurun_settings.get() : null;
      var level = settings && settings.server ? settings.server.thinkingLevel : 'balanced';
      var match = LEVELS.filter(function (pair) { return pair[0] === level; })[0] || LEVELS[1];
      effort.querySelector('strong').textContent = match[1];
      effort.setAttribute('aria-label', 'Thinking effort: ' + match[1] + '. Click to change.');
    }
    if (effort) {
      effort.title = 'Thinking effort (Settings → Server → Thinking level)';
      effort.addEventListener('click', function () {
        var settings = window.__imurun_settings;
        if (!settings) return;
        var level = (settings.get().server || {}).thinkingLevel || 'balanced';
        var index = LEVELS.map(function (pair) { return pair[0]; }).indexOf(level);
        var next = LEVELS[(index + 1) % LEVELS.length][0];
        settings.set({ server: { thinkingLevel: next } });
        settings.save('server');
        showEffort();
      });
      document.addEventListener('imurun:settings', showEffort);
      showEffort();
    }
    setComposerState();
  }

  /* The native caret is one pixel; draw a 3px accent bar in its place. */
  function bindCaret() {
    var inputWrap = document.querySelector('.input-wrap');
    var caret = document.getElementById('prompt-caret');
    if (!inputWrap || !caret) return;
    var mirror = document.createElement('div');
    mirror.className = 'prompt-mirror';
    mirror.setAttribute('aria-hidden', 'true');
    inputWrap.appendChild(mirror);
    var marker = document.createElement('span');
    marker.className = 'caret-marker';
    function update() {
      if (document.activeElement !== prompt || prompt.selectionStart !== prompt.selectionEnd) {
        caret.style.display = 'none';
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
        caret.style.display = 'none';
        return;
      }
      top = Math.max(top, prompt.offsetTop);
      caret.style.display = 'block';
      caret.style.left = left + 'px';
      caret.style.top = top + 'px';
      caret.style.height = line + 'px';
    }
    ['input', 'keyup', 'click', 'select', 'scroll', 'focus', 'compositionupdate'].forEach(function (type) {
      prompt.addEventListener(type, update);
    });
    prompt.addEventListener('blur', function () { caret.style.display = 'none'; });
    window.addEventListener('resize', update);
    inputWrap.classList.add('custom-caret');
    update();
  }

  function submit() {
    if (!prompt) return;
    var value = prompt.value.trim();
    if (!value) return;
    if (IM.shell && IM.shell.handleCommand(value)) {
      prompt.value = '';
      prompt.dispatchEvent(new Event('input'));
      return;
    }
    var sessionId = IM.state.activeSessionId;
    function go(id) {
      var sent = (current && !current.done) ? queue(value, id) : Boolean(send(value, id));
      if (sent || (current && !current.done)) {
        prompt.value = '';
        prompt.dispatchEvent(new Event('input'));
        prompt.focus();
      }
    }
    if (sessionId) {
      go(sessionId);
      return;
    }
    if (!IM.shell) return;
    IM.shell.createSession(value).then(function (session) {
      if (session) go(session.id);
    });
  }

  /* ------------------------------------------------------------ delegation */

  function bindTranscript() {
    transcript = document.getElementById('transcript');
    if (!transcript) return;
    transcript.setAttribute('role', 'log');
    transcript.setAttribute('aria-label', 'Conversation');
    /* Streaming text is announced once on completion, not token by token. */
    transcript.setAttribute('aria-live', 'off');

    jumpButton = document.createElement('button');
    jumpButton.type = 'button';
    jumpButton.className = 'jump-latest';
    jumpButton.hidden = true;
    jumpButton.textContent = 'Jump to latest';
    jumpButton.addEventListener('click', function () {
      stick = true;
      scrollToEnd(true);
    });
    var page = document.getElementById('page-chat');
    if (page) page.appendChild(jumpButton);

    transcript.addEventListener('scroll', function () {
      stick = nearBottom();
      if (stick) jumpButton.hidden = true;
    }, { passive: true });

    transcript.addEventListener('animationend', function (event) {
      var target = event.target;
      if (target && target.classList && target.classList.contains('is-entering')) target.classList.remove('is-entering');
    });

    transcript.addEventListener('click', function (event) {
      var target = event.target && event.target.closest ? event.target : null;
      if (!target) return;
      var summary = target.closest('.thinking > summary');
      if (summary) {
        /* Animate the reasoning panel open/closed instead of snapping. */
        event.preventDefault();
        var details = summary.parentNode;
        if (!details.classList.contains('has-trace')) return;
        if (details._turn) details._turn.userToggled = true;
        animateDetails(details, !details.open);
        return;
      }
      var suggest = target.closest('[data-chat-suggest]');
      if (suggest && prompt) {
        prompt.value = suggest.dataset.chatSuggest;
        prompt.dispatchEvent(new Event('input'));
        prompt.focus();
        return;
      }
      if (target.closest('[data-chat-retry]') && IM.state.activeSessionId) {
        loadTranscript(IM.state.activeSessionId);
        return;
      }
      var copy = target.closest('[data-msg-copy]');
      if (copy) {
        var msg = copy.closest('.msg');
        copyText((msg && msg._answer) || '', copy, 'Copy');
      }
    });
  }

  /* Copy with a visible confirmation; falls back to execCommand where the
   * async clipboard is unavailable (plain http on a LAN address). */
  function copyText(text, button, idleLabel) {
    function flash(label) {
      if (!button) return;
      button.textContent = label;
      button.classList.add('is-done');
      clearTimeout(button._flashTimer);
      button._flashTimer = setTimeout(function () {
        button.textContent = idleLabel;
        button.classList.remove('is-done');
      }, 1400);
      util.announce(label === 'Copied' ? 'Copied to clipboard' : 'Copy failed');
    }
    function fallback() {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', 'readonly');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var copied = false;
      try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
      document.body.removeChild(area);
      return copied;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash('Copied'); }, function () {
        flash(fallback() ? 'Copied' : 'Failed');
      });
    } else {
      flash(fallback() ? 'Copied' : 'Failed');
    }
  }

  /* One delegated listener for every rendered code block, including blocks
   * re-rendered while the answer streams. */
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var button = target.closest('[data-md-copy]');
    if (!button) return;
    var block = button.closest('.md-codeblock');
    var code = block ? block.querySelector('pre code') : null;
    if (code) copyText(code.textContent || '', button, 'Copy');
  });

  /* ---------------------------------------------------------------- demo */

  /* Build a finished, failed or still-streaming reply from canned frames,
   * with no network and no stats recorded. Used by the response gallery
   * (panels.js, ?demo=responses) so every response design can be reviewed
   * with the real rendering code. spec:
   *   { steps: ['raw text', ...], state: 'done'|'streaming'|'thinking'|'error',
   *     status, recovered, error: {title, text, actions}, search: {query, results},
   *     kinds: ['tool', 'answer'], events: [server frames], approval: {...},
   *     end: {extra turn.end fields: tool_calls, skills, verification, sources},
   *     tokensPerSecond } */
  function demoTurn(spec) {
    var s = spec || {};
    var turn = new Turn(null, '');
    turn.demo = true;
    turn.startedAt = performance.now() - 2600;
    turn.onTurnId('demo');
    turn.postedAt = turn.startedAt + 60;
    if (s.search) {
      searchFor(turn, { type: 'web_search.start', query: s.search.query });
      if (s.search.results) searchFor(turn, { type: 'web_search.done', query: s.search.query, results: s.search.results });
    }
    (s.steps || []).forEach(function (text, i) {
      turn.onEvent({ type: 'step.start', step: i + 1 });
      turn.onToken(text, { step: i + 1 });
      if (s.kinds && s.kinds[i]) turn.kinds[i + 1] = s.kinds[i];
      turn.tokens = Math.max(1, Math.round(text.length / 4));
    });
    (s.events || []).forEach(function (event) { turn.onEvent(event); });
    if (s.approval && IM.approvals) IM.approvals.demo(turn, s.approval);
    turn.firstTokenAt = s.state === 'searching' ? null : turn.startedAt + 480;
    turn.render();
    if (s.state === 'searching') {
      turn.cleanup();
      turn.setLabel('Searching the web');
      turn.time.textContent = '1.8s';
      return turn.node;
    }
    if (s.state === 'streaming' || s.state === 'thinking') {
      turn.cleanup();
      turn.time.textContent = s.state === 'thinking' ? '3.4s' : '';
      return turn.node;
    }
    if (s.state === 'error') {
      var err = s.error || {};
      turn.fail(new Error(err.text || 'failed'), { toast: false, title: err.title, text: err.text, actions: err.actions });
      return turn.node;
    }
    turn.finish(Object.assign({ status: s.status || 'done', recovered: s.recovered }, s.end || {}));
    return turn.node;
  }

  function init() {
    bindTranscript();
    bindComposer();
  }

  IM.chat = {
    send: send,
    stop: stop,
    clear: clear,
    loadTranscript: loadTranscript,
    renderTranscript: renderTranscript,
    isStreaming: function () { return Boolean(current && !current.done); },
    streamingSessionId: function () { return current && !current.done ? current.sessionId : null; },
    onServerEvent: onServerEvent,
    searchFor: searchFor,
    chooseModelState: chooseModelState,
    current: function () { return current; },
    focusComposer: function () { if (prompt) prompt.focus(); },
    copyText: copyText,
    demo: {
      user: function (text, options) { return userMessage(text, options); },
      assistant: function (raw) { return assistantMessage(raw); },
      turn: demoTurn,
      mount: function (nodes) {
        if (!transcript) return;
        transcript.innerHTML = '';
        nodes.forEach(function (node) {
          node.classList.remove('is-entering');
          transcript.appendChild(node);
        });
        transcript.scrollTop = 0;
      }
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
