# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\sessions\`

## Purpose

Session identity and per-session memory, plus the append-only chat transcript.
A session id is readable (slug of the first command + four hex characters) and
stable; renaming changes only the name. Memory is a JSON object owned by one
session and persisted under `<data_root>/sessions/<id>.json`. Reading another
session's memory requires an explicit grant and every refusal is marked. The
transcript is a separate JSONL file per session, which is what the chat screen
renders; it is not the same thing as session memory.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\sessions\session.py`

| Symbol | Signature | Notes |
|---|---|---|
| `MAX_NAME` | `80` | Rename cap. |
| `default_root` | `() -> Path` | `~/.imurun/sessions`. |
| `slugify` | `(text: str, max_words: int = 6, max_len: int = 40) -> str` | NFKD → ASCII, lowercase `[a-z0-9]+` words, joined by `-`; `"session"` when empty. |
| `new_session_id` | `(first_command: str) -> str` | `f"{slugify(first_command)}-{secrets.token_hex(2)}"`. |
| `derive_name` | `(first_command: str) -> str` | Control characters replaced by spaces, trimmed to 60 chars; `"Untitled session"` when empty. |
| `SessionIdentity` | `@dataclass(frozen=True) SessionIdentity(id, name, created_at, renamed_at=None)` | Serialized with `dataclasses.asdict` by the API. |
| `Session.__init__` | `(identity, memory: dict, path: Path, mark=None)` | Copies `memory`; no I/O. |
| `Session.identity` / `.id` / `.name` | properties | |
| `Session.start` | `@classmethod (first_command, root=None, mark=None) -> Session` | Creates the file with `O_CREAT\|O_EXCL`, retries up to 10 ids, then `save()`. Raises `ValueError` for an empty command, `RuntimeError` after 10 collisions. |
| `Session.load` | `@classmethod (session_id, root=None, mark=None) -> Session` | `OSError`/`ValueError`/`KeyError`/`TypeError` propagate; raises `ValueError` if the file's `id` does not match. |
| `Session.rename` | `(new_name: str) -> SessionIdentity` | Trims; empty or `> 80` chars → `ValueError`. Persists immediately. |
| `Session.remember` | `(key: str, value) -> None` | Empty key → `ValueError`; persists. |
| `Session.recall` | `(key: str, default=None)` | Read-only. |
| `Session.notes` | `() -> dict` | Shallow copy. |
| `Session.forget` | `(key: str) -> bool` | `False` when the key is absent. |
| `Session.clear` | `() -> None` | Empties memory and persists. |
| `Session.save` | `() -> None` | Atomic: `.tmp` + `os.replace`, chmod `0600`, parent chmod `0700` (POSIX failures re-raise). |
| `Session.read_other` | `@staticmethod (target_session_id, *, root=None, allowed=False, requester=None, mark=None) -> dict` | Without `allowed` marks `G3-cross-session` and raises `PermissionRequired`; with it, marks the allowed read and returns a copy of the memory. |
| `Session.delete` | `@classmethod (session_id, root=None) -> None` | Unlinks; a missing file becomes `ValueError("unknown session ...")`. Authorization is the caller's job. |
| `Session.list_sessions` | `@staticmethod (root=None, mark=None) -> list[SessionIdentity]` | Sorted by `created_at`; unreadable files are marked (`G3-session`) and skipped. `[]` when the directory is absent. |

Filenames are sanitized with `[^A-Za-z0-9._-] → "_"`, so a crafted id cannot
escape the sessions directory. The id is embedded in the payload and checked on
load.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\sessions\transcript.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ROLES` | `frozenset({"user", "assistant", "system"})` | |
| `MAX_CONTENT` | `200_000` | Characters per message. |
| `MAX_READ` | `1_000` | Hard cap on `read`. |
| `Message` | `@dataclass(frozen=True) Message(role, content, at, turn_id="")` | |
| `TranscriptStore.__init__` | `(root=None, *, mark=None)` | Default root `~/.imurun/sessions`. |
| `TranscriptStore.append` | `(session_id, role, content, turn_id="") -> Message` | Validates session id, role, non-empty content and the size cap; appends one JSON line under a per-session lock. |
| `TranscriptStore.read` | `(session_id, limit=200) -> list[Message]` | Last `limit` messages, oldest first; `[]` for a missing file; corrupt lines and unknown roles are marked (`G3-transcript`) and skipped. |

`limit` must be positive (`ValueError`); it is clamped to `MAX_READ`. Appends are
serialized per session id (`self._locks`), and the file is chmod `0600` after each
write.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\sessions\__init__.py`

Re-exports `Session`, `SessionIdentity`, `derive_name`, `new_session_id`,
`slugify`. `TranscriptStore` and `Message` are imported from
`services.sessions.transcript` directly.

## Connections

| Importer | Uses |
|---|---|
| `services/server/api.py` | `Session` (status count, start, load, rename, delete, list), `TranscriptStore` (messages). |
| `services/server/app.py` | `TranscriptStore(sessions)` where `sessions = home/"sessions"`. |
| `services/server/turns.py` | `TranscriptStore.append` for the user prompt, queued prompts and step answers. |
| `services/skills/store.py` | `slugify` for skill ids. |
| `services/response/loop.py` | Not directly; the runner owns transcript writes. |

HTTP routes: `GET /api/sessions`, `POST /api/sessions`,
`GET /api/sessions/{id}`, `GET /api/sessions/{id}/messages?limit=`,
`POST /api/sessions/{id}/rename`, `DELETE /api/sessions/{id}`. UI: the Sessions
list, chat transcript and rename dialog in `frontend/design-lab/app.js`.
Tests: `test/services/sessions/test_session.py`, `test_transcript.py`.

## Invariants

- A session id is never reused and never changes on rename; `SessionIdentity.id`
  is the only stable key.
- The session file's `id` field must equal the requested id; a mismatch is a
  `ValueError`, not a silent load.
- One session never reads another's memory without `allowed=True`; the refusal
  and the grant are both marked.
- All writes are atomic (`.tmp` + `os.replace`) and private (`0600` file,
  `0700` folder). On POSIX a failed chmod is an error, not a warning.
- Transcripts are append-only; a corrupt line is skipped with a mark, never
  rewritten or silently accepted.
- `MAX_READ`/`limit` bounds every transcript read so one request cannot load an
  unbounded history.
