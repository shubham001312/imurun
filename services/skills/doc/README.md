# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\skills\`

## Purpose

Skills are instruction files the agent can create and later use. A skill is a
directory containing `SKILL.md` with YAML-style frontmatter (`name`,
`description`, `origin`), or a flat `<id>.md` file. Creation is validated,
atomic and refuses to overwrite an existing skill. Nothing here executes code:
a skill is text only.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\skills\store.py`

| Symbol | Signature | Notes |
|---|---|---|
| `SKILL_ID` | `re.Pattern` | `^[a-z0-9]+(-[a-z0-9]+)*$`. |
| `MAX_NAME` | `80` | |
| `MAX_DESCRIPTION` | `200` | |
| `MAX_BODY` | `100_000` | |
| `default_root` | `() -> Path` | `~/.imurun/skills`. |
| `Skill` | `@dataclass(frozen=True) Skill(id, name, description, body, path, origin="user")` | `path` is the absolute file path; the API deliberately does not expose it. |
| `SkillsStore.__init__` | `(root=None, mark=None)` | No I/O until the first call. |
| `SkillsStore.root` | `-> Path` (property) | |
| `SkillsStore.list` | `() -> list[Skill]` | Scans direct children only, sorted by entry name; an unreadable skill is marked (`G3-skills`) and skipped. |
| `SkillsStore.get` | `(skill_id: str) -> Skill \| None` | Linear scan of `list()`. |
| `SkillsStore.create` | `(name, description, body, *, skill_id=None, origin="user") -> Skill` | Validates and writes `<root>/<id>/SKILL.md` atomically. |

`create` validation, in order:

1. `name` and `description` are flattened to one line (newlines → spaces) and
   truncated to their caps; `body` has control characters stripped and is
   trimmed. Empty name/description/body → `ValueError`.
2. Over-cap values → `ValueError` (the truncation above already prevents the
   name/description case; body length is checked explicitly).
3. `skill_id` defaults to `slugify(name, max_words=8, max_len=60)` and must match
   `SKILL_ID`; otherwise `ValueError`.
4. `origin` must match `[a-z0-9_-]+`; otherwise `ValueError`.
5. An existing directory (`<id>/SKILL.md`) or flat file (`<id>.md`) →
   `FileExistsError` (the API maps it to `409`).

Side effects: creates the skill directory and root, chmods them `0700`, writes
through `<file>.tmp` + `os.replace`, then chmods the file `0600`. The returned
`Skill` reflects the cleaned values.

`_parse` (private) reads a file and returns `Skill | None`; a file without
frontmatter is treated as a flat skill whose id is the stem (or the parent
directory name for `SKILL.md`), and `None` is returned for an unreadable file or
unterminated frontmatter.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\skills\__init__.py`

Re-exports `Skill` and `SkillsStore`.

## Connections

| Importer | Uses |
|---|---|
| `services/server/app.py` | `SkillsStore(home/"skills")`. |
| `services/server/api.py` | `GET /api/skills`, `POST /api/skills`, `DELETE /api/skills/{id}`. |
| `services/sessions/session.py` | Provides `slugify`, which this module imports. |

The API creates the store with `mark=self._skill_mark`, which forwards to the
server's `mark` sink as `G3-api`. The Skills UI view is rendered by
`renderSkills()` in `frontend/design-lab/app.js` from `api.js`
(`listSkills`, `createSkill`, `getSkill`, `deleteSkill`). Note: the stdlib API
does not implement `GET /api/skills/{id}` (only list, create, delete), so
`api.getSkill()` returns `404` against the Python server.
Tests: `test/services/skills/test_store.py`.

## Invariants

- A skill id is lowercase kebab-case and is also its directory name; it can never
  contain a path separator.
- Creating an existing skill must fail with `FileExistsError`; nothing overwrites
  a user's skill.
- Frontmatter fields are single-line: a newline in `name`, `description` or
  `origin` must be flattened so it cannot inject YAML keys.
- A skill body is never executed, imported, or passed to a shell.
- `Skill.path` stays out of API responses (the API builds its own dict).
- Files and folders stay `0600`/`0700` on POSIX; a failed chmod is an error.
