# IMURUN Rebuild - Plan and Approval Status

Status of every module of the rebuild. Nothing is built until its status says
APPROVED, and every function inside an approved module still needs its own
approval (see rules/process.md).

The other documents in docs/ (tools.md, models.md, ...) describe the released
0.1.11 product. This file and 01..05 + DECISIONS.md describe the rebuild.

## Stack (APPROVED)

- C++ ......... engine (FISH) + resident model server
- Python ...... backend brain: response, services, powers, monitor, providers
- JavaScript .. both UIs: terminal TUI + browser web UI (Tailwind + GSAP)
- Two APIs .... frontend calls the Python agent API; the user and third
                parties may call the C++ model API; the frontend never sees
                which model runs (V10)

## Module map (v4, APPROVED)

| Module      | Language   | Status   |
|-------------|------------|----------|
| model/      | C++        | APPROVED (shape) - function list not yet drafted |
| server/     | C++        | APPROVED (shape) - function list not yet drafted |
| response/   | Python     | APPROVED (shape) - function list not yet drafted |
| response/   | Python     | IN BUILD - agent loop: boundary prompt delivery, kill switch, persona |
| services/   | Python     | IN BUILD - safety (G-1..G-8, network, keys), prompts, sessions, skills, connectors, persona |
| powers/     | Python     | APPROVED (shape) - function list not yet drafted |
| monitor/    | Python     | APPROVED (shape) - function list not yet drafted |
| providers/  | Python     | APPROVED (shape) - function list not yet drafted |
| server/     | Python     | IN BUILD - loopback API: token gate, body cap, persona/connectors/prompts/sessions |
| frontend/sections/ | JS  | IN BUILD - plan approved (docs/07); F1 core next |
| frontend/input/    | JS  | IN BUILD - input map proposed; F-keys parked |
| rules/      | documents  | APPROVED - drafts pending user review |
| docs/       | documents  | APPROVED - drafts pending user review |

APPROVED (shape) means: the module's place in the layout is approved, its
internals are NOT. Function-by-function definition and approval comes next.

## Process decisions (APPROVED)

- Every function approved by the user before it is written (rules/process.md).
- Every decision informed to the user and logged in docs/DECISIONS.md.
- Evidence for a decision = a passing test (rules/process.md).
- Context is re-read before action; nothing is assumed from memory.
- V8: every module is a separate entity, tested end to end by itself.
- V9: anything that fails while running is MARKED, never silently skipped.
- V7: evidence = a passing test in the CI gate over test/.
- V13: no staging pushes - the user tests on their own device.
- V14: macOS and Linux have first-class build/test plans (04-TESTING.md).

## Open items (parked, by user decision)

1. Model guard-rails system - a separate design, planned later with the user.
   rules/ contains the law for build agents, not this.
2. First-run UI (welcome banner + model chooser) - designed later, element by
   element with the user.
3. Small ~1 GB model riding with the engine for TUI/monitor creative tasks -
   bundled with the engine or given a proper mini engine: undecided (~200-400
   MB RAM while active if kept).
4. Function-key map for frontend/input/ - every key gets one defined ability,
   defined later with the user.
5. No code signing budget ($0, decision V3): users follow documented bypass
   steps on macOS and Windows.

## Delivery rules (APPROVED)

- No runnable chat model ships in the bundle; first open shows the chooser.
- Clean-room test: install with no pre-installed runtimes and no network for
  the base path; banner, chooser, server and API must come up (docs/04).
- Offline first, no telemetry, keys via environment only, never written to
  files.
