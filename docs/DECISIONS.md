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
