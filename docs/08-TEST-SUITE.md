# Test suite design - backend (2026-10-26)

Scope: the built Python backend - `services/`, `response/`, the loopback API,
and the bootstrap. The C++ FISH engine, the C++ model server, and the
frontend are not built, so `test/model/`, `test/server/`, `test/frontend/`,
`test/powers/`, `test/monitor/`, and `test/providers/` stay placeholders.
Blocked work is documented here, not written as skipped tests: the strict
runner treats a skip as a failure unless `--allow-skip` is explicit.

Worktree: `imurun-test-suite`, branch `test/backend-suite`, based on the
backend commit. The suite runs with `python test/run.py`.

## 1. Layers

| Layer | Where | Proves |
|---|---|---|
| L0 runner | `test/runner/` | the gate itself: zero tests, failures, errors and hidden skips fail |
| L1 unit | `test/services/<module>/` | one module in isolation, no network, temp roots |
| L2 contract | `test/services/connectors/` | every adapter and OAuth flow against a fake transport and token store |
| L3 API | `test/services/server/` | every route: auth, Host, body cap, static shell, SSE, errors |
| L4 multi-client | `test/api/` | queue FIFO, per-session order, parallel callers |
| L5 end to end | `test/e2e/` | a real server, real stores, a fake model, SSE and transcript |
| L6 security | across L1-L5 | token, Host, traversal, CSP, keychain, no token in logs |
| L7 performance | `test/api/`, `test/e2e/` | bounded queues, no unbounded memory, turn overhead |
| L8 platform | CI matrix | Windows, macOS, Linux; POSIX modes and paths |

## 2. Fixtures and rules

- Every test uses a fresh `tempfile.mkdtemp` root. No test writes to
  `~/.imurun`; the bootstrap test uses an explicit root.
- The server binds port 0 and is stopped in `tearDown`. The token is read
  from the instance, never a file or a log.
- Models are fakes: `FakeModel` (answer), `BlockingModel` (cancel and queued
  prompts), `FailingModel` (marked error), `QueueingModel` (queued delivery).
- Connector tests use `NoopTransport`, `OAuthTransport`, and
  `MemoryTokenStore`. Live providers need user credentials and are manual.
- Waits are bounded by a deadline; no bare `sleep` as a synchronization
  primitive. Threads are daemon or joined.
- A flaky test is a failure. Fix it or quarantine it with a tracked issue;
  never hide it with a skip.

## 3. Coverage matrix

| Area | Evidence |
|---|---|
| safety: guardrails, audit sink, kill switch, governor, keys, network | `test/services/safety/` |
| prompts: FIFO, per-session drain, cancel, history | `test/services/prompts/` |
| sessions: readable ids, rename, memory, corrupt files | `test/services/sessions/` |
| transcripts: append, order, limit, corrupt line marked | `test/services/sessions/test_transcript.py` |
| skills: create, list, collision, validation | `test/services/skills/` |
| persona: store, composer, tool | `test/services/persona/` |
| connectors: 8 adapters, gate, OAuth PKCE, tokens | `test/services/connectors/` |
| agent loop: steps, queued prompts, kill, persona, max steps | `test/services/response/test_loop.py` |
| API: token, Host, body cap, static shell, CSP, status | `test/services/server/test_api.py`, `test_api_features.py` |
| API: SSE framing, broker bound, drop marking | `test/services/server/test_events.py`, `test_api_features.py` |
| API: turns, cancel, failure, transcript | `test/services/server/test_turns.py`, `test_api_features.py` |
| API: sessions, skills, connect, callback | `test/services/server/test_api_features.py` |
| bootstrap: stores, runtime file, 503 without a model | `test/services/server/test_app.py` |
| multi-client queue | `test/api/test_queue_order.py` |
| full turn flow | `test/e2e/test_turn_lifecycle.py` |

## 4. Multi-client rules (docs/04)

| Rule | Status | Evidence / blocker |
|---|---|---|
| two clients prompt at once: FIFO, per-session order | covered | `test_two_clients_keep_their_own_fifo_order`, `test_ten_parallel_pushes_all_land` |
| prompt during a model swap | blocked | model server not built; swap scheduler has no interface yet |
| client killed mid-stream: stream aborted, session saved, server alive | partial | SSE disconnect is handled; session persistence on abort needs the streaming model |
| request without token / wrong Host | covered | `test_a_request_without_the_token_is_refused`, `test_a_non_loopback_host_is_refused` |
| oversized and malformed bodies | covered | `test_an_oversized_body_is_refused`, `_body` 400 paths |
| parallel calls bounded by the governor | service-level only | `test/services/safety/`; API admission waits for the model server |
| settings changed mid-generation | blocked | settings module not built |
| second OS user: separate instance, no crossover | manual | OS account isolation; documented in docs/04 |

## 5. End-to-end scenarios

1. Happy path: start a session, run a turn, receive `turn.start` -> `queue`
   -> `step.start` -> `step` -> `queue` -> `turn.end` over SSE, read the
   transcript, rename the session, read status.
2. Queued prompt: queue while the model is generating; it reaches the model
   at the next boundary and appears in the transcript in order.
3. Cancel: cancel while generating; state is `stopped`, partial output is
   kept, the server stays alive.
4. Failure: the model raises; `turn.error` is emitted, `G3-turn` is marked,
   only the user message is in the transcript, status still answers.
5. Connect: PKCE URL, one-time callback, token in the store, replay is 400.
6. Restart: session and transcript survive; the token changes per run.

## 6. Performance budgets (provisional)

- `GET /api/status` with 100 sessions: under 50 ms p95 on the dev machine.
- Turn overhead with a fake model: under 100 ms per turn.
- SSE slow reader: bounded at 512 events; a drop is counted and marked.
- Body cap: 1 MB accepted, 10 MB refused without buffering the body.
- Queue: 1,000 pending prompts per session cap; history bounded.

These are budgets, not benchmarks yet. A budget test is added only when the
number can be measured reproducibly; otherwise it is a flaky test.

## 7. Gaps and next suites

- FISH C++ (`test/model/`, `test/server/`): blocked until the function list
  is drafted and approved (docs/00-PLAN.md).
- Frontend (`test/frontend/`): blocked on F1 core.
- `powers/`, `monitor/`, `providers/`: shapes approved, functions not.
- Live connector calls: manual, user credentials, no CI network.
- Clean-room and unsigned-install flows: VM/real-machine checks in docs/04.
