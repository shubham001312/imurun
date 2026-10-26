# IMURUN Rebuild - Decision Log

Every decision of the rebuild lives here: what was decided, who decided it,
and its evidence. Decisions are informed to the user BEFORE they take effect
(rules/process.md). Evidence for behavior claims = a passing test (V7).

Format: ID | date | decision | decided by | evidence / status

## Verdicts and decisions

- V1 | 2026-06-10 | No runnable chat model ships in the bundle. First open =
  welcome banner + model chooser (designed later, element by element). A
  small ~1 GB model for TUI/monitor creative tasks is PARKED (bundled with
  engine or mini engine: undecided). | user |
- V2 | 2026-06-10 | SSD layer-streaming is bounded by bandwidth physics
  (~3-7 GB/s vs RAM ~25-50 GB/s). Design accepted with that limit. | user +
  builder verdict | see docs/02-NAMING.md
- V3 | 2026-06-11 | $0 signing budget. Unsigned builds; documented bypass
  steps (right-click-open / more info -> run anyway) for macOS and Windows.
  | user |
- V4 | 2026-06-11 | "Rules an AI cannot prick" are enforced in code
  (permissions, caps, path guards in the runtime), never in prompt text.
  | user (builder verdict accepted) |
- V5 | 2026-06-11 | Monitor engine = heuristics always on + small model as
  second pair of eyes. | user |
- V6 | 2026-10-25 | Thinking processing is Python code. The model generates
  thinking; it never manages its own thinking. Heuristics carry the weight,
  small model is fallback only. | user |
- V7 | 2026-10-25 | Evidence rule: a passing test, run in CI. Quotes and
  line numbers are context, not proof. | user |
- V8 | 2026-10-25 | Every module is a separate entity, checked end to end,
  each part by itself. | user |
- V9 | 2026-10-25 | Any failure while running is MARKED - never silently
  skipped. A skipped part is a visible, recorded skip with a reason. | user |
- V10 | 2026-06-11 | Two APIs: frontends -> Python agent API; user and third
  parties -> C++ model API. | user |
- V11 | 2026-06-11 | The agent API contract is stable across model swaps;
  the server runs whatever model it wants behind it. | user |
- V12 | 2026-06-11 | No model / server stopped -> API answers with a clear
  status message (what is happening, how to proceed), never a raw error.
  | user |
- V13 | 2026-10-25 | No staging pushes. The user tests on their own device;
  work is developed and demonstrated locally. | user |
- V14 | 2026-10-25 | macOS and Linux are first-class targets with their own
  build and test plans (docs/03-PLATFORMS.md). | user |
- V15 | 2026-10-25 | Instruction routing: out-of-domain instruction = record
  it, finish the current unit first, then do it; live redirect while the
  user monitors thinking = immediate switch (interrupted work saved as
  pending); in-domain = apply now. rules/process.md section 9. | user |
- V16 | 2026-10-25 | Verbatim retention: user statements kept exactly as
  given; every instruction is DONE, QUEUED with exact words, or DECLINED
  with a verdict - never lost. rules/process.md section 10. | user |
- V17 | 2026-10-25 | Completion block on the right side of every presented
  item ([x]/[ ]/[!]); a tick is only allowed after re-reading the changed
  files and re-running the relevant test. rules/process.md section 11.
  | user |
- V18 | 2026-10-26 | Spawned agents: the main session-owner model decides
  the context it passes to a child and what it receives back (bounded
  summary by default, full transcript on request). Runtime caps still
  enforce size limits. docs/05-SPAWN.md. | user |
- V19 | 2026-10-26 | Orphan children continue after the parent stops; their
  results are stored in their own sessions and reported to the parent on
  resume/next turn. Kill switch stops all levels. | user |
- V20 | 2026-10-26 | Nesting allowed: root spawns up to 5 children; each
  child spawns up to 1 grandchild; grandchildren cannot spawn (depth 2,
  confirmation pending). | user |
- V21 | 2026-10-26 | Concurrency caps: 5 children per root session, 1
  grandchild per child session; the global governor still bounds total
  load. | user |
- V22 | 2026-10-26 | Depth confirmed at 2: grandchildren cannot spawn.
  Orphan results are delivered as a notice when the parent resumes or next
  acts, plus the session tree entry. | user |
- V23 | 2026-10-26 | Guardrails live in services/safety/ (inside services,
  no layout change). Build order starts with them. G-1..G-8 approved and
  built: probe, limits, check_start, check_action, governor, killswitch,
  mark/audit. Evidence: 45 passing tests (test/services/safety/). | user |
- V24 | 2026-10-26 | FISH foundation = HYBRID: llama.cpp is the compute
  core linked into the server process; FISH owns the streaming scheduler
  (resident window, mmap, swap, stats). The from-scratch path was rejected
  in favour of reaching the speed goal sooner. The earlier "oracle only"
  answer applied to the from-scratch path and is superseded. | user |
- V25 | 2026-10-26 | Queued prompts must reach the model before the task
  ends and during the task: at the next step boundary the waiting message
  is injected in order (marked as queued), and before the final answer the
  loop checks again and continues instead of ending unaware. Never mid
  token. Order and wording preserved (V16); cancel before delivery; child
  queues isolated. Code-enforced in services/prompts/ (PromptQueue). | user |
- V26 | 2026-10-26 | Governor: FIFO wait queue with per-client cap, leases
  (300 s default) reclaimed and marked on expiry, slot unit = megabytes of
  RAM, status reports capacity/used/queued. Closes H-12, H-13, M-32. | user |
- V27 | 2026-10-26 | Sessions: readable id (slug + 4 hex), name from the
  first command (60 chars), /rename-session changes the name only (id stays
  stable). Each session has its own memory under ~/.imurun/sessions/;
  cross-session reads need the user's explicit grant and refusals are
  marked. Skills live in ~/.imurun/skills/ as <id>/SKILL.md; the agent can
  create a skill (validated, atomic, origin marked) and any added skill can
  be listed and used. | user |
- V28 | 2026-10-26 | Persona: /persona popup sets Imurun Name, Imurun Role
  in email and an optional Description; stored privately at
  ~/.imurun/persona.json. The model never opens the file: it reads only
  through PersonaTool, which returns a sanitised structured view and turns
  a missing or corrupted file into configured=False with a reason instead of
  an exception (no wasted compute or latency). PersonaComposer derives a
  directive and applies the signature by code. If asked directly whether it
  is an assistant, it says so honestly. | user |
- V29 | 2026-10-26 | /connect: one adapter per provider; capabilities are
  read / write / admin. Reads run freely; writes and admin actions ask and
  run only with per-action approval. Email is draft-first: a popup asks
  Send / Cancel and a Cancel is audited as refused. Tokens live in the OS
  keychain, never files or logs; every action is audited. Provider order:
  GitHub, Gmail, Slack, Vercel, Render, Cloudflare, Supabase, Canva.
  docs/06-CONNECT.md. | user |
- V30 | 2026-10-26 | Audit register: every re-audit publishes its flaws,
  severity, points and proof test in docs/AUDIT.md; a flaw is closed only
  when its test passes. Re-audit of the built tree found 13 flaws (144
  points), all fixed. The original 80-flaw sheet is not in the repository,
  so its 50 open ids cannot be closed one by one until the sheet is
  provided; the remaining criticals are C-03, C-04, C-08. | user |
- V31 | 2026-10-26 | Response and API: AgentLoop delivers queued prompts only
  at step boundaries and again before the final answer; the kill switch stops
  it between steps. ApiServer binds loopback only, requires a per-run bearer
  token, caps bodies at 1 MB, and exposes persona, connectors, prompts and
  sessions. OAuth uses PKCE with one-time state and a loopback redirect.
  | user |
- V32 | 2026-10-26 | Frontend delivery: the web UI is a static shell served
  by the loopback agent API and opened in the system browser; no Electron.
  docs/07-FRONTEND.md. | user |
- V33 | 2026-10-26 | TUI runtime: JS on a bundled Node runtime provisioned
  in the background; zero npm packages at runtime. | user |
- V34 | 2026-10-26 | Turn events stream over SSE on the loopback API
  (tokens, thinking, tool, queue, turn-end, status). | user |
- V35 | 2026-10-26 | Backend API: the static shell injects the per-run token;
  `Host` must be loopback; SSE is an authenticated fetch stream because
  `EventSource` cannot send a bearer token; transcripts are per-session
  JSONL; turns run in worker threads and cancel at a loop boundary; the
  model is injected, so without FISH a turn is an honest 503. | assistant |
- V36 | 2026-10-27 | UI text font: Manrope. JetBrains Mono stays the code and
  id font; Inter and Space Grotesk remain vendored for comparison in the
  design lab. | user |

## Process decisions (standing law)

- Every module and every function is approved by the user before it is
  built. | user
- Every decision is informed to the user and logged here. | user
- Context is re-read before action; decisions from other agents/models are
  logged here too. | user
- rules/ is the law for BUILD agents. Model guard-rails = separate system,
  planned later with the user. | user

## Rejected / superseded

- Staging push workflow (first phrasing, 2026-10-25) -> superseded by V13
  in the same message.
- Layout v2 (model/server/response/service four folders) -> superseded by
  layout v4, approved 2026-06-11: model, server, response, services, powers,
  monitor, providers + frontend/{sections,input}.
