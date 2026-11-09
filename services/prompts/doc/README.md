# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\prompts\`

## Purpose

An in-memory FIFO queue for user prompts that arrive while a task is already
running. Nothing the user says is dropped: a queued prompt is delivered by the
agent loop at the next step boundary, in arrival order, marked as queued. The
queue also keeps bounded history of delivered and cancelled prompts, so the UI
can explain what happened. There is no persistence; the queue dies with the
process.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\prompts\queue.py`

| Symbol | Signature | Notes |
|---|---|---|
| `QueuedPrompt` | `@dataclass(frozen=True) QueuedPrompt(id: int, session_id: str, text: str, at: float, delivered=False, cancelled=False)` | Immutable; the queue stores a delivered/cancelled copy rather than mutating. |
| `PromptQueue.__init__` | `(history_limit: int = 1000, max_text: int = 1_000_000)` | Both must be positive (`ValueError`). |
| `PromptQueue.push` | `(session_id: str, text: str) -> QueuedPrompt` | Empty session id or text → `ValueError`; `len(text) > max_text` → `ValueError`. Notifies waiters. |
| `PromptQueue.pending` | `(session_id: str) -> list[QueuedPrompt]` | Does not consume; arrival order. |
| `PromptQueue.has_pending` | `(session_id: str) -> bool` | |
| `PromptQueue.pending_count` | `() -> int` | Across all sessions; used by `GET /api/status` and the `queue` SSE event. |
| `PromptQueue.drain` | `(session_id: str) -> list[QueuedPrompt]` | Removes every pending item for the session, returns them marked `delivered=True`, and appends them to history. |
| `PromptQueue.cancel` | `(prompt_id: int, mark=None) -> bool` | Only undelivered prompts; keeps a `cancelled=True` copy in history and marks `G1-prompt`. `False` when the id is unknown or already delivered. |
| `PromptQueue.wait` | `(session_id: str, timeout: float \| None = None) -> QueuedPrompt \| None` | Blocks until an item for the session exists or the timeout elapses; returns the item without consuming it. |
| `PromptQueue.history` | `(session_id: str) -> list[QueuedPrompt]` | Pending + history for the session, sorted by id. |

Concurrency: one `threading.Condition` over the list; `push`, `drain` and `wait`
use the condition, the read-only accessors use the same lock. `drain` removes
exactly one session's items and leaves the rest untouched.

History is trimmed to the last `history_limit` entries on every `drain` and
`cancel`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\prompts\__init__.py`

Re-exports `PromptQueue` and `QueuedPrompt`.

## Connections

| Importer | Uses |
|---|---|
| `services/server/app.py` | `PromptQueue()` injected into the server. |
| `services/server/api.py` | `POST /api/prompts` pushes and publishes a `queue` event; `pending_count()` feeds `GET /api/status`. |
| `services/server/turns.py` | Passes the queue to `AgentLoop`; publishes `queue` after a turn. |
| `services/response/loop.py` | `has_pending`, `drain` at step boundaries. |

HTTP route: `POST /api/prompts` with `{session_id, text}` returns `{"id": <int>}`
and publishes `queue {session_id, pending}`. `api.js` does not currently call
this route (the chat UI posts a turn directly); it exists for the queued-prompt
flow. Tests: `test/services/prompts/test_queue.py`, and the queue-order API test
`test/api/test_queue_order.py`.

## Invariants

- Delivery is decided by code, never by prompt text, and happens only at a step
  boundary (never mid-token).
- A prompt is delivered at most once: `drain` removes it before returning it.
- Cancellation only applies before delivery; a delivered prompt cannot be
  cancelled.
- Sessions are isolated: `drain`/`pending`/`wait` never return another session's
  prompt.
- Text is stored verbatim (no trimming or normalisation) and bounded by
  `max_text`.
- Memory is bounded by `history_limit`; a long-lived server must not grow the
  history without limit.
