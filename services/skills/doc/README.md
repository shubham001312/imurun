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
| `SOURCE_FILE` | `".source.json"` | Sidecar of an installed skill: `{provider, owner, repo, ref, commit, path, url, installed_at, files}`. |
| `safe_resource_path` | `(relative) -> str \| None` | A clean POSIX path for a resource next to SKILL.md: `.md`/`.txt` only, ≤ 4 parts, every part `[A-Za-z0-9_][A-Za-z0-9._-]*` (no `..`, `.`, hidden names, backslashes, absolute paths, drive letters, top-level `SKILL.md`). |
| `Skill` | `@dataclass(frozen=True) Skill(id, name, description, body, path, origin="user", source="")` | `path` is the absolute file path; the API deliberately does not expose it. `source` is `owner/repo@<commit>:<path>` for a GitHub skill (frontmatter `source:`). |
| `SkillsStore.__init__` | `(root=None, mark=None)` | No I/O until the first call. |
| `SkillsStore.root` | `-> Path` (property) | |
| `SkillsStore.list` | `() -> list[Skill]` | Scans direct children only, sorted by entry name; skips names starting with `.` (an install in progress `.tmp-*`, a swap backup `.old-*`); an unreadable skill is marked (`G3-skills`) and skipped. |
| `SkillsStore.get` | `(skill_id: str) -> Skill \| None` | Linear scan of `list()`. |
| `SkillsStore.exists` | `(skill_id) -> bool` | Anything (folder, flat file, symlink) already holds the id. |
| `SkillsStore.create` | `(name, description, body, *, skill_id=None, origin="user") -> Skill` | Validates and writes `<root>/<id>/SKILL.md` atomically. |
| `SkillsStore.install` | `(name, description, body, *, skill_id, origin, source="", files=None, manifest=None, replace=False) -> Skill` | Same validation as `create`, plus `files` (resource path → text, each checked by `safe_resource_path` and ≤ `MAX_BODY`). Builds `<root>/.tmp-<id>-<rand>/` (a folder it creates, files opened `O_EXCL\|O_NOFOLLOW`, each parent re-checked to resolve inside it), writes SKILL.md, resources and the sidecar, then renames it to `<root>/<id>`. Existing id → `FileExistsError` unless `replace`; `replace` only swaps a real folder skill (never a symlink or flat file), via a `.old-*` backup that is restored if the rename fails. |
| `SkillsStore.source_manifest` | `(skill_id) -> dict` | The sidecar, or `{}`. |
| `SkillsStore.delete` | `(skill_id) -> bool` | Removes SKILL.md, the sidecar and the resource files the sidecar lists (re-validated, symlinks never followed), prunes empty folders, then the folder if empty. A folder that is a symlink is never touched. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\skills\github.py` — install from GitHub

| Symbol | Notes |
|---|---|
| `parse_link(url) -> Link(owner, repo, kind, rest)` | Accepts `https://github.com/<o>/<r>` (also `.git`, `www.`, a missing scheme), `.../tree/<ref>[/<path>]`, `.../blob/<ref>/<path>/SKILL.md`, `https://raw.githubusercontent.com/<o>/<r>/[refs/heads/]<ref>/<path>/SKILL.md`. Rejects http, other hosts and look-alikes, credentials, ports, `..`/`.` and encoded separators, and blob/raw links to anything but `SKILL.md`. `GitHubError(400, "bad_link")`. |
| `GitHubFetcher(token_provider=None, *, client=None, timeout=12, _bases=None)` | GET through httpx with `follow_redirects=False`, `trust_env=False`; redirects followed by hand (≤ 3) and every hop checked against the allow-list `{https://api.github.com, https://raw.githubusercontent.com, https://github.com}` before connecting. Streams with a byte cap. The token (optional) is sent only to the API host. `_bases` is for tests only (a fake on 127.0.0.1); nothing in production passes it. Rate limit (403/429 with `x-ratelimit-remaining: 0`) → `GitHubError(429, "rate_limited")` with the reset time and, when anonymous, the 60-an-hour hint. |
| `SkillImporter(store, fetcher=None)` | `preview(url)`, `install(url, paths, commit=None)`, `update(skill_id)`. |
| `parse_skill_file(text) -> (meta, body)` | Frontmatter subset: `key: value`, quoted values, `>`/`\|` block scalars folded to one line. |
| `source_view(source) -> {repo, commit, path, url}` | For the API; `{}` for anything that does not parse. |

Resolution: `GET /repos/{o}/{r}` (default branch, repo links only), then
`GET /repos/{o}/{r}/commits/{ref}` with `Accept: application/vnd.github.sha`
(a branch with `/` is found by trying the shortest prefix first), then one
`GET /repos/{o}/{r}/git/trees/{sha}?recursive=1`. Three API calls for a repo
link, two for a folder; file contents come from `raw.githubusercontent.com/{o}/{r}/{sha}/...`
(no API quota) or, with a token, from the contents API (`application/vnd.github.raw`), so private repos work.

Discovery: every `SKILL.md` blob (mode 100644/100755) under the linked folder is a
skill; its folder's other files are its resources unless they belong to a nested
skill. A resource is kept only if it is `.md`/`.txt`, not a symlink (120000), not
executable (100755), not a submodule, passes `safe_resource_path`, is ≤ `MAX_BODY`
bytes, and is within `MAX_RESOURCES` (20). Everything else is listed as skipped
with its reason (up to 40, then a count). Nothing is ever executed.

Id: the frontmatter `name` when it is already a valid id (≤ 60 chars), else the
slug of the folder name. Description is truncated to 200 (warned); a missing one
becomes "Imported from owner/repo" (warned).

Caps: `MAX_SKILLS` 50 per link/import (preview lists the first 50 and says so;
install refuses more than 50 paths), `MAX_TOTAL_BYTES` 5 000 000 per import,
`MAX_BODY` per file (the stream itself is capped at 4× bytes; the body at 100 000
characters → `too_large`), `MAX_TREE_BYTES` 12 MB, 12 s per request.

Preview entry: `{path, id, invoke, name, description, size, status, installable,
files:[{path,size}], skipped:[{path,reason}], skipped_more, warnings}` with
`status` one of `ok`, `exists` (the id is already installed), `duplicate` (two
skills in the link share an id), `too_large`, `invalid`, `failed`. Warnings also
say when the body is longer than `invoke.MAX_SKILL_CHARS` (4 000), because a turn
injects only that much, and that resource files are stored but not read by the model.

Install re-resolves the link, uses the previewed commit (`ref`, 40 hex), and
returns one result per path: `installed` (with `source` and `files`), `exists`
(never overwritten) or `failed` (with the reason). Each skill is written with
`origin: github`, `source: owner/repo@<commit>:<path>` in its frontmatter, and the
sidecar (which keeps the branch name for Update).

Update: reads the sidecar, resolves the recorded branch again; same commit →
`{status: "current"}`, otherwise re-installs that folder with `replace=True`
under the same id → `{status: "updated", commit}`.

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
| `services/server/api.py` | `GET /api/skills`, `GET /api/skills/{id}`, `POST /api/skills`, `DELETE /api/skills/{id}`, `POST /api/skills/import/preview`, `POST /api/skills/import`, `POST /api/skills/{id}/update`; `ApiServer._skill_importer` (a `SkillImporter` whose token comes from the GitHub connector only when it is connected). |
| `services/sessions/session.py` | Provides `slugify`, which this module imports. |
| `services/skills/invoke.py` | `MAX_SKILL_CHARS`, used by the importer's warning. |

The API creates the store with `mark=self._skill_mark`, which forwards to the
server's `mark` sink as `G3-api`. The Skills page and the Add skill dialog are
`frontend/design-lab/skills.js` (styles in `styles/skills.css`), using `api.js`
(`listSkills`, `createSkill`, `getSkill`, `deleteSkill`, `previewSkillImport`,
`importSkills`, `updateSkill`); `app.js` only forwards the boot-time list.
Tests: `tests/test_skills_github.py` (link parsing, host allow-list and redirects,
caps, traversal, clashes, install/update/delete round trips, the HTTP routes)
against a fake GitHub on 127.0.0.1; `tests/test_agentic_api.py` covers create/get/delete.

## Invariants

- A skill id is lowercase kebab-case and is also its directory name; it can never
  contain a path separator.
- Creating an existing skill must fail with `FileExistsError`; nothing overwrites
  a user's skill.
- Frontmatter fields are single-line: a newline in `name`, `description` or
  `origin` must be flattened so it cannot inject YAML keys.
- A skill body is never executed, imported, or passed to a shell.
- An installed skill folder holds only SKILL.md, `.md`/`.txt` resources and the
  sidecar; the importer contacts only the three GitHub hosts over https.
- `Skill.path` stays out of API responses (the API builds its own dict).
- Files and folders stay `0600`/`0700` on POSIX; a failed chmod is an error.
