# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\persona\`

## Purpose

The persona is the user's name, email role and a short behaviour note. The model
never reads the persona file. `PersonaComposer` derives a sanitised style
directive and applies the signature by code; `PersonaTool` is the only structured
view the model may see. A missing or corrupt file degrades to
`configured=False`, not an exception.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\persona\store.py`

| Symbol | Signature | Notes |
|---|---|---|
| `MAX_NAME` | `80` | |
| `MAX_ROLE` | `120` | |
| `MAX_DESCRIPTION` | `500` | |
| `MAX_FILE_BYTES` | `64 * 1024` | Refuse to parse a larger file. |
| `Persona` | `@dataclass(frozen=True) Persona(name, email_role, description="")` | |
| `PersonaStore.__init__` | `(path: Path \| str \| None = None)` | Default `~/.imurun/persona.json`. |
| `PersonaStore.save` | `(persona: Persona) -> Persona` | Strips control characters, trims, caps fields; empty name or role → `ValueError`. Atomic write, `0600` file / `0700` folder. |
| `PersonaStore.load` | `() -> Persona \| None` | `None` when the file is absent; `ValueError` when it exceeds `MAX_FILE_BYTES`; JSON errors propagate. |
| `PersonaStore.exists` | `() -> bool` | |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\persona\store.py` (composer)

| Symbol | Signature | Notes |
|---|---|---|
| `PersonaComposer.__init__` | `(store: PersonaStore)` | |
| `PersonaComposer.directive` | `() -> str` | `""` without a persona; otherwise a short style instruction naming the user and role. Never contains the file path. |
| `PersonaComposer.apply_signature` | `(body: str) -> str` | Appends `"\n\n--\n<name>\n<role>"` unless the body already ends with that signature; returns the body unchanged without a persona. |

`directive()` calls `store.load()` on every invocation, so a changed persona is
picked up on the next turn.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\persona\tool.py`

| Symbol | Signature | Notes |
|---|---|---|
| `PersonaView` | `@dataclass(frozen=True) PersonaView(configured: bool, name="", email_role="", description="", error="")` | |
| `PersonaTool.__init__` | `(store: PersonaStore)` | |
| `PersonaTool.read` | `() -> PersonaView` | Never raises: a load failure becomes `configured=False` with `error="<Type>: <message>"`; an absent file becomes `configured=False` with an empty error. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\persona\__init__.py`

Re-exports `Persona`, `PersonaComposer`, `PersonaStore`, `PersonaTool`,
`PersonaView`.

## Connections

| Importer | Uses |
|---|---|
| `services/server/app.py` | `PersonaStore(home/"persona.json")`. |
| `services/server/api.py` | `Persona`, `PersonaStore` (PUT), `PersonaTool` (`GET /api/persona`). |
| `services/server/turns.py` | Passes an optional `persona` into `AgentLoop`. |
| `services/response/loop.py` | Calls `persona.directive()` and prepends it as a `system` message. |

HTTP routes: `GET /api/persona` (returns `configured`, `name`, `email_role`,
`description`, `error`), `PUT /api/persona` (body `{name, email_role,
description}`; `400` on validation failure). UI: the Persona page and the Profile
settings panel in `frontend/design-lab/`.
Tests: `test/services/persona/test_store.py`, `test_tool.py`.

## Invariants

- The persona file is private (`0600` inside a `0700` folder) and is never
  returned raw, and its path never appears in a directive or a tool view.
- The model receives a derived directive, not the file; signature application is
  code, not a prompt instruction.
- `PersonaTool.read()` must not raise for any file state (missing, corrupt,
  oversized).
- Field caps and control-character stripping happen on save and again on read
  paths that build a view.
- Name/role are required to save; clearing the persona means deleting the file,
  not saving empty strings.
