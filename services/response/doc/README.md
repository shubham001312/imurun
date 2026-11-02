# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\response\`

## Purpose

The agent loop. It is the single place that decides when a turn continues, when
it stops, and when a queued prompt is delivered. It builds the message list
(optional persona system directive, then the user prompt), calls the injected
model client one step at a time, delivers queued prompts at step boundaries, and
honours the kill switch between steps. It knows nothing about HTTP, storage or
which model is running.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\response\loop.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ModelClient` | `Protocol.generate(messages: list[dict]) -> str` | The only model contract the loop needs. |
| `Step` | `@dataclass(frozen=True) Step(index: int, answer: str)` | |
| `LoopResult` | `@dataclass(frozen=True) LoopResult(status: str, steps: tuple[Step, ...], messages: tuple[dict, ...])` | `status` is `done`, `stopped` or `max-steps`. |
| `LoopResult.answer` | `-> str` (property) | Last step's answer, `""` when there are no steps. |
| `AgentLoop.__init__` | `(model, *, queue=None, audit=None, kill=None, persona=None, max_steps=20, session_id="default")` | `max_steps > 0` and a non-empty `session_id`, else `ValueError`. Registers `self.stop` with `kill` when a kill switch is given. |
| `AgentLoop.stop` | `() -> None` | Sets the stop flag; safe from any thread. |
| `AgentLoop.close` | `() -> None` | Unregisters from the kill switch exactly once, so long-lived servers do not leak stops. |
| `AgentLoop.run` | `(prompt: str) -> LoopResult` | Empty prompt → `ValueError`. |

`run` behaviour:

1. `messages` starts with the persona directive as a `system` message (when the
   persona yields one), then `{"role": "user", "content": prompt}`.
2. Before each step: if stopped → mark `G6-killswitch`, return `stopped`.
3. `_deliver` drains the queue and appends each item as a `user` message, marking
   `G1-prompt`.
4. Calls `model.generate` with a shallow copy of the messages, appends the answer
   as `assistant`, records a `Step`.
5. After the step: if stopped → `stopped`; if the queue still has a pending
   prompt → mark `G1-prompt` and continue; else drain once more and continue if
   anything was delivered; else return `done`.
6. Exhausting `max_steps` marks `G1-loop` and returns `max-steps`.

`_deliver` and `_mark` are private helpers. A failing `audit.mark` is swallowed;
a failing model call is not caught and propagates to the caller (the turn runner
turns it into a `turn.error` event).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\response\__init__.py`

Re-exports `AgentLoop`, `LoopResult`, `ModelClient`, `Step`.

## Connections

| Importer | Uses |
|---|---|
| `services/server/turns.py` | `AgentLoop`, `LoopResult`, `ModelClient` — one loop per turn. |
| `model/fish_client.py` | `ModelClient` (the protocol it satisfies structurally). |
| `services/prompts/queue.py` | `PromptQueue` (delivery boundary). |
| `services/persona/store.py` | `PersonaComposer.directive()` through the injected `persona`. |

No HTTP route imports this module directly; `POST /api/turns` reaches it through
`TurnRunner`. SSE events (`step.start`, `token`, `step`, `turn.start`, `turn.end`,
`turn.error`) are emitted by the turn runner around this loop, not by the loop
itself. Tests: `test/services/response/test_loop.py`, plus the end-to-end
`test/e2e/test_turn_lifecycle.py`.

## Invariants

- A queued prompt is delivered at a step boundary, never in the middle of a
  generation, and at most once.
- A prompt that arrives during the last generation must still be delivered before
  the loop returns `done` (the second drain).
- The kill switch is checked between steps and after each generation; `stop()`
  must take effect without waiting for a full step.
- `close()` must unregister the loop from the kill switch; otherwise every turn
  leaks a stop entry.
- `max_steps` is a hard cap; the loop must never exceed it.
- The loop never chooses a model and never inspects the prompt for instructions
  about stopping or continuing.
- A broken audit sink must not change the loop's result.
