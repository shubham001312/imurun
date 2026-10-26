# IMURUN Rebuild - Testing

## Test folder (V8, V7)

All tests live in `test/` at the project root, one subfolder per module,
mirroring the module map - each module is a separate entity and is proven
end to end BY ITSELF (V8):

    test/
    ├── model/         C++ unit + benchmark tests (FISH streaming, CPU/GPU choice)
    ├── server/        resident server: swap, health, status messages
    ├── response/      agent loop, thinking split, session handling
    ├── services/      tools, skills, MCP, integrations
    ├── powers/        fallback chains, loop exit points, stop-caution
    ├── monitor/       heuristics rules, hallucination detection cases
    ├── providers/     adapter contract tests (env keys, offline failure)
    ├── api/           the nine multi-client rules as executable tests
    ├── frontend/      input mapping, section rendering (headless)
    └── e2e/           full turn flows across modules

Rules for the folder:

- CI gating runs here: every change is checked end to end before it is
  accepted (V7: evidence = a passing test in this gate).
- Failure marking (V9): a failing or skipped test is MARKED - what failed,
  where, why - in the report. A silent skip is itself a failure. Skips
  print as skipped with a reason.
- No push without a green gate (standing user requirement).

## Staging (V13)

There is NO staging push. Work is developed locally, demonstrated to the
user on their own device, and only released when the full gate is green.
The user tests on their device; nothing is pushed for testing purposes.

## Clean-room protocol (user requirement)

One VM/container with NOTHING pre-installed: no Python, no Node, no C++
toolchain, no model, no cached runtimes.

Pass criteria for the base path:

1. Install the package. It provisions its runtimes in the background while
   the UI is already up (nothing blocks first paint).
2. Welcome banner appears; model chooser appears (no models exist yet -
   V1). No network is used for this path.
3. Server reports status through the API: "no model loaded" as a designed
   message, not a stack trace (V13).
4. Agent API and model API both answer their health checks; token is
   required for the agent API.
5. Nothing wrote outside the user's own directories. Nothing asked for
   admin/root.

Model download is OUTSIDE the clean-room base path: it is a separate,
user-invoked step with resume + checksum.

## Multi-client tests (proof for rules/api.md)

- Two clients prompt at once -> FIFO order, per-client cap, queued status.
- Prompt during model swap -> swap drains first or the caller gets the
  documented cancel answer.
- Client killed mid-stream -> stream aborted, session saved, server alive.
- Request without token / wrong Host header -> refused (drive-by localhost
  protection).
- Oversized and malformed bodies -> proper message, no crash.
- Parallel calls -> total-load governor still bounds the machine.
- Settings changed mid-generation -> applied next request only.
- Second OS user on the machine -> separate instance, no data crossover.

## Evidence rule (APPROVED)

Evidence for any claim about behavior = a passing test, run in CI.
File quotes and line numbers are context, not proof.

## Platform test plans (V14: macOS and Linux are first-class)

Every module's tests must run on all three OSes. Plan per OS:

### Windows (dev machine - runs daily)
- Full suite locally before any push: unit + api + e2e.
- Clean-room: fresh Windows VM, no runtimes, no network for base path.
- Unsigned-install path: SmartScreen "More info -> Run anyway" documented
  and verified by hand (V3).

### macOS (CI runner + one real machine before release)
- CI: universal2 build (Intel + Apple Silicon in one artifact), full test
  suite on a macOS runner.
- Clean-room: fresh macOS user account, no runtimes, no network for base
  path.
- Hand-verified per release: right-click -> Open bypass flow (V3),
  LaunchAgent background server survives logout/sleep, TCC permission
  prompts appear only when a feature needs them, case-insensitive APFS
  path tests.
- Model API + agent API smoke test on Apple Silicon hardware (not only
  under Rosetta).

### Linux (CI runner + one distro matrix)
- CI: full suite on a Linux runner; glibc baseline build checked against
  the oldest supported distro.
- Distro smoke matrix (containers): one glibc distro (e.g. Debian/Ubuntu
  LTS) + musl note: if Alpine/musl is unsupported, it is DOCUMENTED as
  unsupported - never silently broken (V9).
- Clean-room: fresh container with no runtimes, no network for base path.
- No-root and no-systemd paths tested explicitly.

### Cross-platform rules
- Platform-specific code paths are marked in code with the OS they serve
  and have a test or an explicit "untestable here" mark (V9).
- A platform where a test cannot run reports SKIPPED with the reason - a
  missing platform run is never reported as a pass.

## Every other gate

- Each module defines its tests as part of its function approval.
- Full suite runs before every push (user requirement); no push with a
  failing or flaky gate.
