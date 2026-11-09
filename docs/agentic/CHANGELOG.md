# Agentic layer — changelog

## 2026-11-09 — install skills from a GitHub link

Owner request: "adding new skill should be designed properly, like the user can
add a GitHub link to add the skills."

- `services/skills/github.py`: `POST /api/skills/import/preview {url}` resolves a
  repo, `/tree/` folder, `/blob/.../SKILL.md` or raw `SKILL.md` link to a commit,
  lists every skill under it (name, description, path, size, extra files,
  skipped files with reasons, warnings, clash status) and writes nothing.
  `POST /api/skills/import {url, paths, ref}` installs the chosen folders at the
  previewed commit; `POST /api/skills/{id}/update` re-installs from the newest
  commit of the recorded branch. Anonymous by default; the GitHub connector's
  token is used only when it is connected, and only towards `api.github.com`.
- Guards: only `api.github.com`, `raw.githubusercontent.com` and `github.com` over
  https, redirects checked hop by hop; 50 skills and 5 MB per import, 100 000
  per file, 20 resources per skill, 12 s per request; only SKILL.md plus
  `.md`/`.txt` next to it are written (scripts, binaries, executables, symlinks,
  submodules are listed as skipped); paths validated and written into a folder
  the store creates and renames into place; an installed id is never overwritten.
- `SkillsStore`: `Skill.source`, `install()` (atomic folder with resources and a
  `.source.json` sidecar), `exists()`, `source_manifest()`; `list()` skips
  dot-entries; `delete()` also removes the resource files an install wrote.
- `GET /api/skills` items now carry `invoke` and, for GitHub skills, `source`
  `{repo, commit, path, url}`.
- Measured on `https://github.com/anthropics/skills` (anonymous, commit
  `2c5958e`): 20 skills found with 3 API calls; 19 installable, `claude-api` is
  over the 100 000-character cap. 19 of the 20 descriptions are longer than 200
  characters (shortened, warned), and 13 of the 19 installable bodies are longer
  than the 4 000 characters a turn injects (`invoke.MAX_SKILL_CHARS`), so those skills reach
  the model cut; raising that cap is a follow-up, not done here.
- A security review then found, and this fixes: O(n²) discovery (a huge repo
  could stall a preview for minutes), tree paths with `..` reaching other API
  paths through URL normalisation, frontmatter injection through Unicode line
  separators or `---` in a folder name, Update taking a different skill when a
  root skill moved, Update write errors surfacing as 500 tracebacks, and
  `delete()` following a symlinked subfolder.
- Tests: `tests/test_skills_github.py` (68) against a fake GitHub on 127.0.0.1.

## 2026-11-03 — time to first token

Symptom (owner, 8B): "the response is fast once it starts, but the first token
takes 17, 30, even 60 seconds". Measured on the 1B with the full prompt: 1,930
prompt tokens, 0 cached, 15 s prefill before the first token; the 8B prefills
at ~23 tok/s with 19/32 layers on the GPU, so the same prompt is a minute.
Three causes on the server side, all fixed here (engine prefill speed itself
is the engine owner's current task, `docs/engine/REQUESTS.md`):

- **The research planner's routing generation** was a second prefill of a few
  hundred tokens plus ~60 generated tokens *and* it evicted the engine's KV
  prefix cache for the real prompt (turn 2 showed `cached=0`). The model
  planner now runs only when the last measured prefill rate is at least
  `PLANNER_MIN_PREFILL_TPS` (400 tok/s, `AgentLoop._prefill_fast`); slower
  or unknown → the heuristic + conceptual pre-filter route.
- **The pre-fetched Context block sat inside the system message**, so any turn
  that fetched something changed the prefix and paid a full prefill. It now
  rides with the user prompt, after the stable system text and history
  (`_initial_messages`), so the shared prefix stays cached.
- `Turn.first_token_seconds` (turn start → first visible token) is on the turn
  view and in `stats` so the UI and the benchmarks can show the number the
  user actually waits for.
- Still open: the system prompt is ~1,000 tokens even without persona or skills
  (house style 328, tool catalogue 427 + 283 of tool instructions); a shorter
  catalogue for slow-prefill models is a follow-up. Tests: `tests/test_ttft.py`.

## 2026-11-02 — reply language, dictated prompts

- `POST /api/turns` accepts `language` (ISO 639-1 from `/api/voice/transcribe`);
  `TurnRunner.start(language=)` stores it on the turn (`Turn.view().language`)
  and `AgentLoop(language=)` adds `voice.languages.answer_directive()` — one
  line "The user is speaking Hindi (हिन्दी). Answer in Hindi, in its own
  script." — after the house style; empty for English/unknown (REQUESTS #13).
- House style gains `DICTATION_RULE`: prompts may be speech-to-text, read them
  by meaning and never comment on spelling (the owner dictates prompts).

## 2026-11-01 — native permission levels for connector / MCP tools

Done on request of the connectors owner (`docs/connectors/REQUESTS.md` L1–L4, R1);
the `ConnectorToolPolicy` wrapper and the approval-event hook are no longer
installed (`tool_policy.install` sees `PermissionPolicy.native_levels` and
returns; the module stays for its own tests).

### Added
- **Per-call levels in `PermissionPolicy`** (`permissions.py`): categories
  `connector-read`, `connector-write`, `mcp` (added to `base.CATEGORIES`) and
  any tool with `permission_level(args) -> (level, reason)` are decided by the
  level table `allow | read | write | ask | always | deny` (see the module
  docstring). `always` asks in every mode and ignores remembered grants; a
  level tool without the probe, or with a broken probe, asks. `level_of()`,
  `tool_toggles()`, `policy.level()`, `policy.rememberable()`;
  `remember(..., tool=, args=)` refuses deletes and `always` tools.
- **Tool toggles**: a tool may name its toggles (`toggle = ("network",
  "sendEmail")`); a network tool without one is gated by `network`.
  `sendEmail` is now part of the policy's values/update/view.
- **Approval cards carry `level` and `rememberable`** natively
  (`approvals.Approval`, `ApprovalBroker.request(level=, rememberable=)`,
  also on `GET /api/tools/approvals`). The executor passes both; a broker
  built with `levels=` (the server does) fills them for cards requested
  without (manual route, tests). The executor only calls `policy.remember`
  when the call was rememberable.
- `ToolRegistry.unregister(name)` — MCP tools of a removed/disabled server
  are removed instead of lingering as "retired" (`services/mcp/registry.py`
  already called it when present).
- Aliases for connector tools (`send_email`→`gmail_send`, `read_email`→
  `gmail_read`, `create_issue`→`github_create_issue`, `slack_message`→
  `slack_post`, …) in `calls.ALIASES`.
- Tool prompt (`protocol.tool_prompt`): when connector/MCP tools are offered,
  "use the matching tool for the user's accounts", "never claim you sent,
  posted, deployed or changed something without a [tool result]", and
  "prefer gmail_draft unless the user said send" when both Gmail tools exist.
- Tests: `tests/test_permission_levels.py`.

- **L5 — connector tools offered per service** (`services/tools/offer.py`,
  `AgentLoop._offer(prompt)`): with more than 8 connector tools available, the
  prompt lists only the services the question or the last 6 history messages
  point at (hint words per service in `SERVICE_HINTS`; unknown services match
  on their own name); the hidden services are named in one prompt line so the
  model can ask for them. Hidden tools still execute when named. Built-in and
  MCP tools are never hidden. Tests: `tests/test_tool_offer.py`.

## 2026-10-31 — model-driven tools, permissions, skills, sub-agents

### Added
- **Model-driven tool calling.** `AgentLoop` is now a multi-step loop: default
  6 steps, configurable through `agent.maxSteps`, `IMURUN_MAX_STEPS` or
  `TurnRunner(max_steps=)`, capped at 20. It parses tool calls from the model,
  runs them through one executor, feeds results back as `[tool result: name]`
  messages, keeps within the context window and honours Stop/kill between
  steps (`services/response/loop.py`, `services/response/context.py`).
- Forgiving tool-call parser covering the fenced `tool` block, Qwen
  `<tool_call>`, Llama 3.1 `<|python_tag|>` (JSON and `.call()`),
  `<function=…>`, bare JSON, `name(...)`, plus JSON repair, aliases and R1
  `<think>` handling (`services/tools/calls.py`). Tool prompt and result
  formats are in `services/tools/protocol.py`.
- `StreamGuard` keeps tool-call JSON out of the live token stream. The `step`
  event gains `kind` and readable text for tool steps.
- Tools: `file_read` (offset/cap, binary refused), `file_list` (tree),
  `file_write` (overwrite/create/append, atomic), `make_dir`, `file_move`,
  `file_copy`, `file_delete`, and a rewritten `shell` (workspace cwd, process-tree
  kill on timeout, output cap, credential env vars scrubbed, deny-list), plus
  `run_agent` (`services/tools/builtin/files.py`, `shell.py`, `agent.py`).
- `web_fetch`: block-aware text extraction, title, `max_chars` (default 8000)
  and an explicit "page cut" note.
- Security model: `Workspace` (`workspace.py`), `PermissionPolicy` with modes
  `ask` / `allow-reads` / `allow-all`, toggles and session "remember" grants
  (`permissions.py`), approval cards over SSE (`approvals.py`), and the
  `ToolExecutor` gate with schema validation, events and `G3-tool` audit
  (`executor.py`). The destructive-command deny-list was extended
  (`services/safety/decisions.py`).
- Skills: `/name` invocation injects the skill into the system prompt
  (`services/skills/invoke.py`). `SkillsStore.delete` was added.
- Sub-agents: nested loops with their own budget, a tool restriction, depth ≤ 2
  and `agent.start`/`agent.end` events.
- Routes: `GET /api/tools` (schemas + permission), `POST /api/tools/run`,
  `GET /api/tools/approvals`, `POST /api/tools/approvals/{id}`,
  `GET|POST /api/permissions`, `GET|POST /api/agent`, `GET /api/skills/{id}`
  (`services/tools/routes.py`).
- Events: `tool.call`, `tool.result`, `tool.approval_needed`, `tool.approved`,
  `tool.denied`, `agent.start`, `agent.end`, `skill.used`, `permissions`.
- Real-time correctness: `LIVE_DATA_RULE` in the house style, today's date in
  the system prompt, a realtime nudge step, and the pre-fetch offline sentence
  aligned with the house rule (`policy.OFFLINE_SENTENCE`).
- Tests: `tests/test_agentic_*.py` and `tests/conftest.py`, 81 tests passing.

### Changed
- All system content is merged into **one** system message. Templates without
  a system role (Gemma) and small models both handle that better.
- The pre-fetch (`ToolRunner.observe_full`) returns its own results instead of
  sharing `last_results` between concurrent turns. It skips categories that
  are switched off and passes `failed=` to the instructions.
- The transcript stores only final answers and delivered queued prompts.
- `POST /api/turns` returns `503 model_unavailable` when the engine has no
  model configured. It used to start a turn that died with `turn.error`.
- `web_search` treats a provider bot-check page as a failure.
- `file_list` no longer emits emoji. The old `shell.py` file tools were replaced
  by the workspace-bound ones; the old names are re-exported.

### Removed
- The dead first `do_PUT` in `api.py`. The second definition always replaced
  it.
