# Spawned agents (subagents)

A spawned agent is a child session with its own context window, spawned by a
parent agent to do one focused task. It is a first-class session with a
parent link, not a hidden tool call.

Lives in response/ (backend, Python). Talks to the model through the same
server queue as everyone else, so the governor and the one-model-at-a-time
rule still hold (rules/api.md #3).

## Hierarchy and caps (user, 2026-10-26)

- Root session: spawns up to 5 children.
- Each child: spawns up to 1 grandchild.
- Grandchildren: cannot spawn further (depth 2, confirmed 2026-10-26).
- The global resource governor counts every level; caps cannot be bypassed
  by spawning more (G1).

## Communication (user: the main model decides the context)

- The parent model decides what context it passes to the child (the child
  does not inherit the parent transcript automatically).
- The parent model decides what it receives back: bounded summary by
  default; the full child transcript on request via spawn_log(id).
- The runtime enforces hard size caps regardless of what the model asks.
- Messages that cross the boundary, structured:
  - spawn.started  - child id, task title, session id
  - spawn.progress - short status line (no thinking, no raw output)
  - spawn.result   - bounded summary, files changed, status
  - spawn.error    - code + marked reason (V9), never silent

## Sessions across turns

- Every child writes a real session record with parent_id and root_id;
  sessions show as a tree; any child session is resumable on its own.
- Parent resume: finished children appear as results; interrupted children
  are marked interrupted (V9) and can be re-spawned on request - never
  silently re-run.
- Parent stops (turn aborted / session closed): children CONTINUE as
  orphans (user, 2026-10-26). Their results are stored in their own
  sessions and reported to the parent when it resumes or next acts.
- Kill switch (G6) stops parents, children and grandchildren together.
- App/server exit: running children are marked interrupted (V9), never
  silently lost.
- Model swap: a child queues like any client; the swap drains active
  streams first (api rule #5). No child sees a half-swapped model.
- Server down / no model: spawn fails immediately with the designed status
  message (V12), not a hang.

## Budgets and safety (code-enforced, V4)

- Per-child caps: max steps, max tokens, max wall time; on hit the child
  stops and returns spawn.error with the reason.
- Permissions: child gets intersection(parent grants, child tool
  allowlist). Never escalates. Dangerous actions from a child are denied
  with a marked reason (async permission prompts from children would stall
  the queue).
- Monitor (V5/V6): loop caps and stop-caution apply to every level; a
  looping child is stopped by code, not by its own judgment.

## UI

- One spawn block per child in the parent transcript: task title, live
  status, elapsed, result summary collapsed by default, expandable to the
  child session.
- Orphan children stay visible while running (they are not attached to a
  parent turn anymore) and their results appear on the parent's next turn.
- Failures: [!] with the reason (V9). No silent skip anywhere.

## Open items

- Whether a child may be pinned to a different model than the resident one
  (would force a swap; queued).
