/* API integration layer for the IMURUN shell.
 *
 * The shell is served by the Python API on the same origin, so relative
 * /api/* calls carry the injected bearer token and satisfy connect-src 'self'.
 * When the design lab is opened from a plain static server (dev preview), the
 * layer probes /health once and falls back to the loopback API on 8732.
 *
 * Written to ES2017 (no optional catch binding, no object spread, no `?.`)
 * so the repo's offline syntax check (esprima) can parse it.
 */
(function () {
  'use strict';

  const FALLBACK_PORT = 8732;
  /* Bounded SSE reconnect: 1s, 2s, 5s, then 10s for every further attempt. */
  const RECONNECT_DELAYS = [1000, 2000, 5000, 10000];
  const MAX_RECONNECT_FAILURES = 5;
  /* Every named frame the server publishes on /api/events. A frame whose type
   * is not listed here is silently dropped by EventSource, so this list must
   * track services/server (turns.py, response/loop.py, api.py). */
  const EVENT_TYPES = [
    'queue',
    'step.start',
    'token',
    'step',
    'turn.start',
    'turn.end',
    'turn.error',
    'connector',
    'session',
    'usage',
    'error',
    'compaction.start',
    'compaction.progress',
    'compaction.done',
    'model.switch.queued',
    'model.switch.done',
    'model.download.progress',
    'web_search.start',
    'web_search.progress',
    'web_search.done',
    /* Agentic layer (services/tools, services/response/loop.py). */
    'tools.start',
    'tools.done',
    'tool.call',
    'tool.result',
    'tool.approval_needed',
    'tool.approved',
    'tool.denied',
    'agent.start',
    'agent.end',
    'skill.used',
    'permissions',
    /* MCP servers (services/mcp/registry.py). */
    'mcp.connected',
    'mcp.disconnected',
    'mcp.error',
    /* Research engine (docs/research/): plan, per-source search, page reads. */
    'research.plan',
    'research.search',
    'research.fetch',
    'research.done',
    'verification',
    'sources',
    /* Tuning channel (services/tuning, docs/tuning/). */
    'tuning.job',
    'tuning.ready',
    'tuning.setup',
    'tuning.feedback',
    /* Voice layer (services/voice): model and voice downloads. */
    'voice.model.download',
  ];
  /* Research frames belong to one turn but may be published before the turn
   * id is known to the page; they are kept when they carry no turn_id. */
  const RESEARCH_TYPES = ['research.plan', 'research.search', 'research.fetch', 'research.done',
    'verification', 'sources'];
  /* Tool frames that belong to one turn. They must carry this turn's id: a
   * manual run from Settings > Tools publishes the same frames with an empty
   * turn_id, and those must never land in a live reply. */
  const STRICT_TURN_TYPES = [
    'tool.call', 'tool.result', 'tool.approval_needed', 'tool.approved', 'tool.denied',
    'agent.start', 'agent.end', 'skill.used',
  ];
  /* Frames that belong to one turn; streamTurn listens to these. */
  const TURN_EVENT_TYPES = ['turn.start', 'step.start', 'token', 'step', 'turn.end', 'turn.error']
    .concat(STRICT_TURN_TYPES, RESEARCH_TYPES);
  /* How long streamTurn waits for its EventSource to open before it posts the
   * turn anyway. Posting before the stream is open loses the first tokens. */
  const STREAM_OPEN_TIMEOUT_MS = 2500;
  const TURN_POLL_MS = 1500;

  let resolvedBase = null;
  let authToken = null;

  function readMetaToken() {
    const meta = document.querySelector('meta[name="api-token"]');
    if (!meta) return null;
    const value = (meta.content || '').trim();
    if (!value || value === '__IMURUN_TOKEN__') return null;
    return value;
  }

  function getToken() {
    if (authToken) return authToken;
    authToken = readMetaToken();
    return authToken;
  }

  function setToken(token) {
    authToken = token || null;
  }

  function authHeaders(extra) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    return headers;
  }

  async function probe(url) {
    try {
      /* /health is a liveness probe, but it is also reached through the API
       * base: send the bearer token when one is available so a token-gated
       * deployment answers 200 instead of 401. */
      const headers = {};
      const token = getToken();
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const response = await fetch(`${url}/health`, { cache: 'no-store', headers });
      return response.ok;
    } catch (error) {
      return false;
    }
  }

  async function resolveBase() {
    if (resolvedBase !== null) return resolvedBase;

    if (typeof window.IMURUN_API_BASE === 'string') {
      resolvedBase = window.IMURUN_API_BASE.replace(/\/+$/, '');
      return resolvedBase;
    }

    if (readMetaToken()) {
      // Served by the API itself: same origin is correct.
      resolvedBase = '';
      return resolvedBase;
    }

    if (await probe('')) {
      resolvedBase = '';
      return resolvedBase;
    }

    const protocol = location.protocol === 'https:' ? 'https:' : 'http:';
    const host = location.hostname || '127.0.0.1';
    const fallback = `${protocol}//${host}:${FALLBACK_PORT}`;
    if (await probe(fallback)) {
      resolvedBase = fallback;
      return resolvedBase;
    }

    resolvedBase = '';
    return resolvedBase;
  }

  async function baseUrl() {
    return resolveBase();
  }

  /* The origin API frames come from; used to validate postMessage callbacks. */
  async function apiOrigin() {
    const base = await resolveBase();
    if (!base) return location.origin;
    try {
      return new URL(base).origin;
    } catch (error) {
      return location.origin;
    }
  }

  async function errorFromResponse(response) {
    let errorData;
    try {
      errorData = await response.json();
    } catch (error) {
      errorData = { error: response.statusText };
    }
    const message = (errorData && (errorData.message || errorData.error)) || `API error: ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.code = errorData && errorData.error;
    /* The whole body: POST /api/tools/run answers 403 {decision, reasons}. */
    error.body = errorData && typeof errorData === 'object' ? errorData : null;
    return error;
  }

  async function fetchAPI(path, options) {
    const opts = options || {};
    const base = await resolveBase();
    const init = Object.assign({}, opts, {
      headers: authHeaders(opts.headers),
      credentials: 'omit',
      cache: 'no-store',
    });
    const response = await fetch(`${base}${path}`, init);

    if (!response.ok) throw await errorFromResponse(response);

    if (response.status === 204) return null;
    const text = await response.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (error) {
      return text;
    }
  }

  function eventsUrl(base) {
    const token = getToken();
    const query = token ? `?token=${encodeURIComponent(token)}` : '';
    return `${base}/api/events${query}`;
  }

  /* ---------------------------------------------------------------- Turns */

  /* Start a turn and stream its frames.
   *
   *   streamTurn(input, options, onToken, onDone, onError) -> Promise<turnId>
   *
   * options: { sessionId, mode, onEvent(event), onTurnId(id) }
   *   onEvent receives every frame of this turn ({type, ...data}) in order:
   *   turn.start, step.start, token, step, turn.end / turn.error.
   *
   * Ordering guarantees (each one closes a real race):
   *  1. The EventSource is opened and we wait for `open` (bounded) BEFORE the
   *     POST, so the first tokens are not published to a stream that is not
   *     connected yet.
   *  2. Frames that arrive before the POST reply names the turn are buffered,
   *     then replayed filtered by turn_id, so another turn's frames (another
   *     tab, a finishing earlier turn) never leak into this answer.
   *  3. If the stream drops mid-turn, the turn is not failed: the broker does
   *     not replay, so the final state is recovered by polling
   *     GET /api/turns/{id} until the turn leaves `running`.
   */
  /* POST /api/turns body. `language`: the language the user just spoke
   * (voice-server.js, from the transcription's answer_language), so the
   * model answers in it (docs/agentic/REQUESTS.md #13); sent once. */
  function turnBody(opts, text) {
    const body = { session_id: opts.sessionId, text, mode: opts.mode || 'build' };
    let language = opts.language || '';
    const voice = window.IMURUN && window.IMURUN.voiceServer;
    if (!language && voice && voice.takeAnswerLanguage) language = voice.takeAnswerLanguage() || '';
    if (language) body.language = language;
    return body;
  }

  async function streamTurn(input, options, onToken, onDone, onError) {
    const opts = options || {};
    const text = typeof input === 'string'
      ? input
      : (Array.isArray(input) ? input.map((m) => m.content || '').join('\n\n') : String(input || ''));

    const base = await resolveBase();
    /* Exactly one stream per turn: the POST reply only names the turn, it
     * never reopens the stream, so the token is appended once. */
    const source = new EventSource(eventsUrl(base));

    let turnId = null;
    let finished = false;
    let polling = null;
    const buffered = [];
    let resolveDone;
    let rejectDone;
    const donePromise = new Promise((resolve, reject) => {
      resolveDone = resolve;
      rejectDone = reject;
    });
    /* A stream failure can land before `await donePromise`; keep it from
     * surfacing as an unhandled rejection while the POST is still in flight. */
    donePromise.catch(() => {});

    function stop(error) {
      if (finished) return;
      finished = true;
      source.close();
      if (polling) {
        clearTimeout(polling);
        polling = null;
      }
      if (error) rejectDone(error);
      else resolveDone();
    }

    function dispatch(event) {
      if (finished) return;
      if (event.turn_id && turnId && event.turn_id !== turnId) return;
      if (STRICT_TURN_TYPES.indexOf(event.type) !== -1 && event.turn_id !== turnId) return;
      if (opts.onEvent) {
        try { opts.onEvent(event); } catch (error) { /* a UI bug must not kill the stream */ }
      }
      if (event.type === 'token' && event.token) {
        if (onToken) onToken(event.token, event);
        return;
      }
      if (event.type === 'turn.end') {
        if (onDone) onDone(event);
        stop();
        return;
      }
      if (event.type === 'turn.error') {
        const error = new Error(event.error || 'turn failed');
        error.event = event;
        if (onError) onError(error);
        stop(error);
      }
    }

    function handle(event) {
      if (finished) return;
      if (!turnId) {
        buffered.push(event);
        return;
      }
      dispatch(event);
    }

    TURN_EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (e) => {
        let data;
        try {
          data = JSON.parse(e.data);
        } catch (error) {
          return; /* ignore malformed frame */
        }
        handle(Object.assign({ type }, data));
      });
    });

    /* Recover the outcome of a turn whose stream dropped. */
    function pollTurn() {
      if (finished || !turnId) return;
      fetchAPI(`/api/turns/${encodeURIComponent(turnId)}`).then((turn) => {
        if (finished) return;
        const state = turn && turn.state;
        if (state === 'running' || !state) {
          polling = setTimeout(pollTurn, TURN_POLL_MS);
          return;
        }
        if (state === 'error') {
          const error = new Error(turn.error || 'turn failed');
          if (onError) onError(error);
          stop(error);
          return;
        }
        const event = {
          type: 'turn.end',
          turn_id: turnId,
          status: turn.status || (state === 'stopped' ? 'stopped' : 'done'),
          steps: turn.steps,
          answer: turn.answer || '',
          tool_calls: turn.tool_calls,
          skills: turn.skills,
          verification: turn.verification,
          sources: turn.sources,
          recovered: true,
        };
        if (opts.onEvent) {
          try { opts.onEvent(event); } catch (error) { /* ignore */ }
        }
        if (onDone) onDone(event);
        stop();
      }).catch((error) => {
        if (finished) return;
        if (error && error.status === 404) {
          stop(new Error('the turn is no longer known to the server'));
          return;
        }
        polling = setTimeout(pollTurn, TURN_POLL_MS * 2);
      });
    }

    source.onerror = () => {
      if (finished) return;
      if (turnId) {
        /* The broker does not replay: switch to polling for the outcome. */
        source.close();
        if (!polling) polling = setTimeout(pollTurn, 300);
        return;
      }
      /* Before the turn exists a dropped stream is not fatal yet: the POST
       * decides. EventSource will retry on its own. */
    };

    /* Wait (bounded) for the stream to be connected before posting. */
    await new Promise((resolve) => {
      if (source.readyState === 1) {
        resolve();
        return;
      }
      const timer = setTimeout(resolve, STREAM_OPEN_TIMEOUT_MS);
      source.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });

    try {
      const response = await fetch(`${base}/api/turns`, {
        method: 'POST',
        headers: authHeaders({ Accept: 'application/json' }),
        credentials: 'omit',
        body: JSON.stringify(turnBody(opts, text)),
      });

      if (!response.ok) throw await errorFromResponse(response);

      const payload = await response.json();
      if (!payload || !payload.turn_id) {
        throw new Error('turn id missing from response');
      }
      turnId = payload.turn_id;
      if (opts.onTurnId) {
        try { opts.onTurnId(turnId); } catch (error) { /* ignore */ }
      }
      /* Replay frames that arrived before the POST reply named the turn,
       * keeping only this turn's frames. */
      buffered.splice(0).forEach((event) => {
        if (event.turn_id && event.turn_id !== turnId) return;
        dispatch(event);
      });
      /* The stream may have dropped while the POST was in flight. */
      if (source.readyState === 2 && !finished && !polling) {
        polling = setTimeout(pollTurn, 300);
      }
    } catch (error) {
      stop(error);
      throw error;
    }

    await donePromise;
    return turnId;
  }

  async function cancelTurn(turnId) {
    return fetchAPI(`/api/turns/${encodeURIComponent(turnId)}/cancel`, { method: 'POST' });
  }

  async function getTurn(turnId) {
    return fetchAPI(`/api/turns/${encodeURIComponent(turnId)}`);
  }

  /* Queue a prompt into a running session; it is delivered at the next step
   * of the agent loop (POST /api/prompts). */
  async function queuePrompt(sessionId, text) {
    return fetchAPI('/api/prompts', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, text: text }),
    });
  }

  /* Subscribe to the shared event broker. Named SSE frames are dispatched to
   * one handler as { type, ...data }.
   *
   * Reconnects are bounded: 1s -> 2s -> 5s -> 10s (capped), and the loop gives
   * up after `maxFailures` consecutive attempts. The shell can call
   * reconnect() to start over. `options.onStatus(state)` reports
   * 'connecting' | 'open' | 'reconnecting' | 'offline' so the status line can
   * stay honest. Nothing is written to the console. */
  function subscribeEvents(onEvent, options) {
    const opts = options || {};
    const delays = (Array.isArray(opts.delays) && opts.delays.length)
      ? opts.delays
      : RECONNECT_DELAYS;
    const maxFailures = typeof opts.maxFailures === 'number'
      ? opts.maxFailures
      : MAX_RECONNECT_FAILURES;

    let source = null;
    let closed = false;
    let failures = 0;
    let timer = null;
    let generation = 0;
    let lastStatus = null;

    function report(state) {
      if (state === lastStatus) return;
      lastStatus = state;
      if (opts.onStatus) {
        try { opts.onStatus(state); } catch (error) { /* ignore */ }
      }
    }

    function teardown() {
      if (source) {
        source.close();
        source = null;
      }
    }

    function schedule() {
      if (closed) return;
      failures += 1;
      if (failures >= maxFailures) {
        teardown();
        report('offline');
        return;
      }
      report('reconnecting');
      const delay = delays[Math.min(failures - 1, delays.length - 1)];
      if (timer) clearTimeout(timer);
      timer = setTimeout(connect, delay);
    }

    function connect() {
      if (closed) return;
      timer = null;
      const gen = ++generation;
      if (lastStatus === null) report('connecting');
      baseUrl().then((base) => {
        if (closed || gen !== generation) return;
        teardown();
        const next = new EventSource(eventsUrl(base));
        source = next;
        next.onopen = () => {
          if (source === next) report('open');
        };
        EVENT_TYPES.forEach((type) => {
          next.addEventListener(type, (event) => {
            /* A delivered frame proves the stream is healthy; an open that
             * never yields a frame must keep the backoff escalating. */
            if (source === next) failures = 0;
            let data;
            try {
              data = JSON.parse(event.data);
            } catch (error) {
              return; /* ignore malformed frame */
            }
            try {
              onEvent(Object.assign({ type }, data));
            } catch (error) { /* a handler bug must not drop the stream */ }
          });
        });
        next.onerror = () => {
          if (closed || source !== next) return;
          teardown();
          schedule();
        };
      }).catch(() => {
        if (closed || gen !== generation) return;
        schedule();
      });
    }

    connect();
    return {
      close() {
        closed = true;
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
        teardown();
      },
      reconnect() {
        if (closed) return;
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
        failures = 0;
        teardown();
        report('connecting');
        connect();
      },
      get status() { return lastStatus; },
    };
  }

  /* ------------------------------------------------------------- Sessions */

  async function listSessions() {
    const data = await fetchAPI('/api/sessions');
    return (data && data.sessions) || [];
  }

  async function createSession(name) {
    const first = (name && String(name).trim()) || 'New session';
    return fetchAPI('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ first_command: first }),
    });
  }

  async function getSession(id) {
    return fetchAPI(`/api/sessions/${encodeURIComponent(id)}`);
  }

  async function getMessages(id, limit) {
    const suffix = limit ? `?limit=${encodeURIComponent(limit)}` : '';
    const data = await fetchAPI(`/api/sessions/${encodeURIComponent(id)}/messages${suffix}`);
    return (data && data.messages) || [];
  }

  async function updateSession(id, data) {
    return fetchAPI(`/api/sessions/${encodeURIComponent(id)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name: (data && data.name) || '' }),
    });
  }

  async function deleteSession(id) {
    return fetchAPI(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /* --------------------------------------------------------------- Persona */

  async function getPersona() {
    return fetchAPI('/api/persona');
  }

  async function savePersona(data) {
    return fetchAPI('/api/persona', {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  /* ---------------------------------------------------------------- Skills */

  async function listSkills() {
    const data = await fetchAPI('/api/skills');
    return (data && data.skills) || [];
  }

  async function createSkill(data) {
    return fetchAPI('/api/skills', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /* GET /api/skills/{id} -> {id, name, description, origin, body, invoke}. */
  async function getSkill(id) {
    return fetchAPI(`/api/skills/${encodeURIComponent(id)}`);
  }

  async function deleteSkill(id) {
    return fetchAPI(`/api/skills/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /* ------------------------------------------------------------ Connectors */

  /* options.open: the server opens the sign-in page in the SYSTEM browser
   * (Google refuses OAuth inside embedded webviews) -> {url, state, opened}. */
  async function connectConnector(id, options) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/connect`, {
      method: 'POST',
      body: JSON.stringify({ open: Boolean(options && options.open) }),
    });
  }

  async function disconnectConnector(id) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/disconnect`, { method: 'POST' });
  }

  async function getConnectorStatus(id) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}`);
  }

  async function listConnectors() {
    const data = await fetchAPI('/api/connectors');
    return (data && data.connectors) || [];
  }

  async function runConnectorAction(id, action, params, approved) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/actions/${encodeURIComponent(action)}`, {
      method: 'POST',
      body: JSON.stringify({ params: params || {}, approved: Boolean(approved) }),
    });
  }

  /* Per-service OAuth set-up (services/connectors/setup.py):
   *   GET  /api/connectors/{id}/config -> {clientId, clientSecretSet, tools,
   *        availableTools, scopes, redirectUri, configured, console, doc, ...}
   *   POST /api/connectors/{id}/config {clientId?, clientSecret?, tools?, ...}
   *   GET  /api/connectors/{id}/test   -> {ok, account, ...}
   * The secret is write-only: it is never returned, only `clientSecretSet`. */
  async function getConnectorConfig(id) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/config`);
  }

  async function saveConnectorConfig(id, changes) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/config`, {
      method: 'POST',
      body: JSON.stringify(changes || {}),
    });
  }

  async function testConnector(id) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/test`);
  }

  /* The OS file dialog, opened by the local server (Tk subprocess):
   * {title?, patterns?} -> {path: string|null, cancelled: bool};
   * 501 {error} when the machine has no Tk (keep a typed path then). */
  async function pickFile(options) {
    return fetchAPI('/api/system/pick-file', { method: 'POST', body: JSON.stringify(options || {}) });
  }

  /* Set-up steps as Markdown (API-REQUESTS.md item 24); 404 until it exists. */
  async function getConnectorDoc(id) {
    return fetchAPI(`/api/connectors/${encodeURIComponent(id)}/doc`);
  }

  /* ------------------------------------------------------------------- MCP */

  async function listMcpServers() {
    const data = await fetchAPI('/api/mcp/servers');
    return (data && data.servers) || [];
  }

  async function getMcpTools(id) {
    return fetchAPI(`/api/mcp/servers/${encodeURIComponent(id)}/tools`);
  }

  /* Add or update one server. Env and header values come back as
   * "********"; sending that value back means "unchanged". */
  async function saveMcpServer(config) {
    return fetchAPI('/api/mcp/servers', { method: 'POST', body: JSON.stringify(config || {}) });
  }

  async function connectMcpServer(id) {
    return fetchAPI(`/api/mcp/servers/${encodeURIComponent(id)}/connect`, { method: 'POST' });
  }

  async function disconnectMcpServer(id) {
    return fetchAPI(`/api/mcp/servers/${encodeURIComponent(id)}/disconnect`, { method: 'POST' });
  }

  async function deleteMcpServer(id) {
    return fetchAPI(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /* ---------------------------------------------------------------- Tuning */
  /* docs/tuning/README.md "API". Feedback: {turn_id, rating: up | down |
   * teach | clear, correction?, session_id?} -> {feedback, counts}. */
  async function tuningStatus() { return fetchAPI('/api/tuning/status'); }
  async function tuningSettings() { return fetchAPI('/api/tuning/settings'); }
  async function saveTuningSettings(changes) {
    return fetchAPI('/api/tuning/settings', { method: 'POST', body: JSON.stringify(changes || {}) });
  }
  async function tuningPlan(modelId) { return fetchAPI(`/api/tuning/plan/${encodeURIComponent(modelId)}`); }
  async function tuningJobs() {
    const data = await fetchAPI('/api/tuning/jobs');
    return (data && data.jobs) || [];
  }
  async function tuningJob(id) { return fetchAPI(`/api/tuning/jobs/${encodeURIComponent(id)}`); }
  /* 202 {job}; 409 {needs: ["confirm"] | ["allow_slow"], plan, message} rejects
   * with error.status 409 and error.body = that object. */
  async function startTuningJob(options) {
    return fetchAPI('/api/tuning/jobs', { method: 'POST', body: JSON.stringify(options || {}) });
  }
  async function cancelTuningJob(id) {
    return fetchAPI(`/api/tuning/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  }
  async function tuningFeedback(payload) {
    return fetchAPI('/api/tuning/feedback', { method: 'POST', body: JSON.stringify(payload || {}) });
  }
  /* Ratings already given in a session (docs/tuning/REQUESTS.md, frontend
   * item 4; an older server answers 404).
   * -> {turns: {turn_id: {rating, consented}}} */
  async function tuningFeedbackFor(sessionId) {
    return fetchAPI(`/api/tuning/feedback?session_id=${encodeURIComponent(sessionId)}`);
  }
  async function startTuningSetup(options) {
    return fetchAPI('/api/tuning/setup', { method: 'POST', body: JSON.stringify(options || {}) });
  }

  /* -------------------------------------------------------------- Settings */

  async function getSettings(section) {
    return fetchAPI(`/api/settings/${encodeURIComponent(section)}`);
  }

  /* `options.keepalive` lets a pending save survive page unload. */
  async function updateSettings(section, data, options) {
    const init = {
      method: 'PUT',
      body: JSON.stringify(data),
    };
    if (options && options.keepalive) init.keepalive = true;
    return fetchAPI(`/api/settings/${encodeURIComponent(section)}`, init);
  }

  async function rotateToken() {
    return fetchAPI('/api/auth/rotate', { method: 'POST' });
  }

  /* ---------------------------------------------------------------- Clients */

  async function listClients() {
    const data = await fetchAPI('/api/clients');
    return (data && data.clients) || [];
  }

  async function createClient(data) {
    return fetchAPI('/api/clients', {
      method: 'POST',
      body: JSON.stringify(data || {}),
    });
  }

  async function revokeClient(id) {
    return fetchAPI(`/api/clients/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /* ----------------------------------------------------------------- Usage */

  async function getUsage() {
    return fetchAPI('/api/usage');
  }

  /* ------------------------------------------------------------- Resources */

  async function clearCache() {
    return fetchAPI('/api/resources/clear', { method: 'POST' });
  }

  async function getResources() {
    return fetchAPI('/api/resources');
  }

  /* ---------------------------------------------------------------- Health */

  async function healthCheck() {
    try {
      await fetchAPI('/health');
      return true;
    } catch (error) {
      return false;
    }
  }

  async function getStatus() {
    return fetchAPI('/api/status');
  }

  /* --------------------------------------------------------------- Tools */

  async function getTools() {
    return fetchAPI('/api/tools');
  }

  /* Run one tool by hand. Resolves {result: Execution view}. A call that
   * needs approval rejects with error.status 403 and
   * error.body = {decision: "ask", reasons, tool, args}: show a card, then
   * call again with approved = true. */
  async function runTool(name, args, approved, sessionId) {
    const body = { name: name, args: args || {}, approved: Boolean(approved) };
    if (sessionId) body.session_id = sessionId;
    return fetchAPI('/api/tools/run', { method: 'POST', body: JSON.stringify(body) });
  }

  async function listApprovals() {
    const data = await fetchAPI('/api/tools/approvals');
    return (data && data.approvals) || [];
  }

  /* Answer an approval card: {approved, remember} -> {approval_id, approved, remember}. */
  async function answerApproval(id, approved, remember) {
    return fetchAPI(`/api/tools/approvals/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify({ approved: Boolean(approved), remember: Boolean(remember) }),
    });
  }

  async function getPermissions() {
    return fetchAPI('/api/permissions');
  }

  /* Any of {mode, workspaceRoot, readFiles, writeFiles, runShell, network,
   * approvalTimeout}; resolves the full view, rejects 400 with the reason. */
  async function setPermissions(changes) {
    return fetchAPI('/api/permissions', { method: 'POST', body: JSON.stringify(changes || {}) });
  }

  async function getAgent() {
    return fetchAPI('/api/agent');
  }

  async function setAgent(changes) {
    return fetchAPI('/api/agent', { method: 'POST', body: JSON.stringify(changes || {}) });
  }

  /* Share the browser's position so local questions can be answered without
   * an IP lookup. The user is never asked twice for the same place. */
  async function shareLocation() {
    if (!navigator.geolocation) return null;
    return new Promise(function (resolve) {
      navigator.geolocation.getCurrentPosition(
        function (position) {
          const payload = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude
          };
          fetchAPI('/api/tools/location', {
            method: 'POST',
            body: JSON.stringify(payload)
          }).then(function () {
            resolve(payload);
          }).catch(function () {
            resolve(null);
          });
        },
        function () {
          resolve(null);
        },
        { timeout: 8000, maximumAge: 600000 }
      );
    });
  }

  /* ----------------------------------------------------------- Model files */

  async function listModels() {
    const data = await fetchAPI('/api/models');
    return (data && data.models) || [];
  }

  async function deleteModel(id) {
    return fetchAPI(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /* Pick the model the engine loads from now on. While a turn runs the server
   * queues the switch and this request stays open until it is applied. */
  async function selectModel(id) {
    return fetchAPI('/api/models/select', {
      method: 'POST',
      body: JSON.stringify({ id: id })
    });
  }

  /* Download one model and report progress frames.
   * Returns an abort function so the UI can cancel. */
  function downloadModel(id, onProgress, onDone, onError) {
    const controller = new AbortController();

    (async () => {
      const base = await resolveBase();
      const response = await fetch(`${base}/api/models/download`, {
        method: 'POST',
        headers: authHeaders({ Accept: 'text/event-stream' }),
        credentials: 'omit',
        body: JSON.stringify({ model_id: id }),
        signal: controller.signal,
      });

      if (!response.ok) throw await errorFromResponse(response);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let last = null;

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        /* Normalise CRLF so frames split on a blank line either way. */
        buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n');
        const frames = buffer.split('\n\n');
        buffer = frames.pop() || '';
        for (const frame of frames) {
          /* An SSE frame may carry its JSON over several data: lines. */
          const data = frame.split('\n')
            .filter((item) => item.indexOf('data:') === 0)
            .map((item) => item.slice(5).replace(/^ /, ''))
            .join('\n');
          if (!data) continue;
          let progress;
          try {
            progress = JSON.parse(data);
          } catch (error) {
            continue;
          }
          last = progress;
          if (onProgress) onProgress(progress);
          if (progress.status === 'complete' || progress.status === 'error') {
            if (progress.status === 'complete') {
              if (onDone) onDone(progress);
            } else if (onError) {
              onError(new Error(progress.error || progress.message || 'download failed'));
            }
            return;
          }
        }
      }

      if (last && last.status === 'complete') {
        if (onDone) onDone(last);
      } else if (onError) {
        onError(new Error('download stream ended early'));
      }
    })().catch((error) => {
      if (error && error.name === 'AbortError') return;
      if (onError) onError(error);
    });

    return () => controller.abort();
  }

  /* ----------------------------------------------------------- Stack control */

  async function stackBuild() {
    return fetchAPI('/api/stack/build', { method: 'POST' });
  }

  async function stackStart() {
    return fetchAPI('/api/stack/start', { method: 'POST' });
  }

  async function stackEnsure() {
    return fetchAPI('/api/stack/ensure', { method: 'POST' });
  }

  async function stackStop() {
    return fetchAPI('/api/stack/stop', { method: 'POST' });
  }

  async function stackStatus() {
    return fetchAPI('/api/stack/status');
  }

  window.__imurun_api = {
    get base() { return resolvedBase || ''; },
    EVENT_TYPES: EVENT_TYPES.slice(),
    baseUrl,
    apiOrigin,
    getToken,
    setToken,
    fetchAPI,
    streamTurn,
    cancelTurn,
    getTurn,
    queuePrompt,
    subscribeEvents,
    listSessions,
    createSession,
    getSession,
    getMessages,
    updateSession,
    deleteSession,
    getPersona,
    savePersona,
    listSkills,
    createSkill,
    getSkill,
    deleteSkill,
    connectConnector,
    disconnectConnector,
    getConnectorStatus,
    listConnectors,
    runConnectorAction,
    getConnectorConfig,
    saveConnectorConfig,
    testConnector,
    getConnectorDoc,
    pickFile,
    listMcpServers,
    getMcpTools,
    saveMcpServer,
    connectMcpServer,
    disconnectMcpServer,
    deleteMcpServer,
    tuningStatus,
    tuningSettings,
    saveTuningSettings,
    tuningPlan,
    tuningJobs,
    tuningJob,
    startTuningJob,
    cancelTuningJob,
    tuningFeedback,
    tuningFeedbackFor,
    startTuningSetup,
    getSettings,
    updateSettings,
    rotateToken,
    listClients,
    createClient,
    revokeClient,
    getUsage,
    clearCache,
    getResources,
    healthCheck,
    getStatus,
    getTools,
    runTool,
    listApprovals,
    answerApproval,
    getPermissions,
    setPermissions,
    getAgent,
    setAgent,
    shareLocation,
    listModels,
    deleteModel,
    selectModel,
    downloadModel,
    stackBuild,
    stackStart,
    stackEnsure,
    stackStop,
    stackStatus,
  };
})();
