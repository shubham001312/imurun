# Agentic layer: tools, permissions, skills, sub-agents

How the assistant calls tools itself, what it may touch, and how the UI sees
and approves it. Code lives in `services/tools/`, `services/response/`,
`services/skills/`, and the route handlers in `services/tools/routes.py`
(dispatched from `services/server/api.py`). The exact HTTP contract is in
`services/server/doc/README.md`.

## 1. The turn loop

`services/response/loop.py` (`AgentLoop.run`), one per turn, runs in the turn thread:

```
pre-fetch (ToolRunner.observe_full: regex intents -> time/weather/web_search, concurrent, 10 s deadline)
build ONE system message: persona + house style + "Today is <date>" + tool prompt + invoked skills + pre-fetch Context
+ last 6 history messages (assistant <think> stripped) + the user prompt
for step in 1..max_steps (default 6; Settings agent.maxSteps; env IMURUN_MAX_STEPS; cap 20):
    stop?  -> status "stopped"   (Stop button, kill switch, or the parent agent stopped)
    last step and tools were used -> add "[note] No more tool calls ... answer now"
    context.fit(messages, context_limit())     # shrink old tool results / compact history / trim
    raw = model.generate(messages)
    visible, thought = split_think(raw)        # tool calls are read from `visible` only
    calls -> executor.execute each (max 3/step), feed back "[tool result: name]" user messages, continue
    unreadable call (marker but no usable JSON) -> one retry message, continue
    thought but no answer (R1)                 -> one "give your final answer now", continue
    live-data question, no tool result, no "can't check" in the answer -> one realtime nudge, continue
    answer -> queued prompt waiting? deliver it at this boundary and continue; else "done"
```

- **Connector tools are offered per service** (`services/tools/offer.py`): when
  more than 8 connector tools are available, the prompt lists only the
  services the question or the last 6 messages point at ("inbox" → Gmail,
  "open an issue" → GitHub, "DNS" → Cloudflare…); the other connected services
  are named in one line. Hidden tools still run when the model names them
  (aliases such as `send_email` → `gmail_send` apply). Built-in and MCP tools
  are never hidden.
- Tool results go back as **`user` messages** headed `[tool result: <name>]`. The
  engine passes role strings straight into the chat template; only Llama 3
  (`ipython`) and ChatML (`tool`) have a tool role, so `user` is the one format
  every template handles.
- The assistant's own call goes back into history in **canonical form** (a clean
  ```` ```tool ```` block). Anything the model wrote after the call, such as an
  invented "Result: ...", is dropped.
- Identical calls (same name and arguments) within a turn are served from a
  cache, which stops small models from looping on the same call.
- The last step never runs tools. If the model still asks for one, the answer
  becomes the text before the call, or a "reached the tool step limit" sentence,
  with status `max-steps`.
- `max_steps = 1` turns off model tool calling (no tool prompt is sent). The
  pre-fetch still runs.
- **Token budget** (`services/response/context.py`): estimate = characters / 3.5
  + 6 per message. Budget = `context_limit() - reserve`, where reserve =
  `clamp(limit/4, 256, 2048)`. Each tool result is capped at
  `clamp(limit*3.5/4, 800, 6000)` characters. When over budget, the loop frees
  room in this order: (1) old tool results cut to 400 chars, (2) history
  summarised with the raw model (`compaction.*` events) or dropped, (3) the
  newest tool results cut, (4) the longest message trimmed. The final user
  prompt is never trimmed.
- Side generations (history summaries, sub-agents) use the **raw** model, so
  they never stream into the user's answer bubble.

### Streaming and the `step` event (`services/server/turns.py`)

While tools are on offer, `StreamGuard` holds back tokens once a call marker
appears (```` ```tool ````, `<tool_call>`, `<|python_tag|>`, `<function=`,
`{"name"`), so JSON never flashes in the answer. The `step` event for a tool
step carries `kind: "tool"` and readable text: `<think>…</think>` (kept for the
frontend's reasoning splitter), any text before the call, then
``Using `web_search` {"query": "..."}``. The frontend already shows earlier
steps in the thinking panel and the last step as the answer. Only final answers
and delivered queued prompts go into the transcript.

## 2. Tool-call format

The system prompt teaches exactly one format:

````
```tool
{"name": "web_search", "args": {"query": "latest Python release"}}
```
````

The parser (`services/tools/calls.py::parse_tool_calls`) also accepts these:

| Model output | Example |
|---|---|
| fenced `tool` / `tool_call` / `tool_code` / `function` | as above |
| unlabelled or ```` ```json ```` fence naming a known tool | ```` ```json {"name": "time_now"} ``` ```` |
| Qwen/Hermes | `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` |
| Llama 3.1 JSON | `<|python_tag|>{"name": "...", "parameters": {...}}<|eom_id|>` |
| Llama 3.1 built-in | `<|python_tag|>brave_search.call(query="x")` -> `web_search` |
| Llama 3.1 custom | `<function=web_search>{"query": "x"}</function>` |
| bare JSON object/list whose `name` is a known tool | `{"name": "shell", "args": {"command": "dir"}} and then...` |
| a reply that is only `name(k="v")` | `web_search(query="x")` |
| OpenAI style | `{"function": {"name": "...", "arguments": "{...json string...}"}}` |

Repairs: smart quotes, single quotes, `True/False/None`, unquoted keys,
trailing commas, missing closing braces or brackets, an unterminated string,
text after the object, string `args` (JSON-decoded, or mapped onto the tool's
only required parameter), and keys `args|arguments|parameters|params|input|action_input`.
Common invented names map to real tools (`read_file`→`file_read`,
`bash`→`shell`, `google`→`web_search`, `mkdir`→`make_dir`…, see `ALIASES`).
Unknown argument keys are dropped. Types are coerced to the schema
(`"5"`→5, `"true"`→true, `"a,b"`→list).

Errors: a call marker with nothing usable, or an unknown tool name, produces
one retry message that lists the valid names. A second failure is returned as
plain text. A bare JSON object that does not name a known tool is treated as an
ordinary answer.

**DeepSeek-R1 `<think>`:** `split_think` handles `<think>…</think>`, an orphan
`…</think>` (the template already emitted the opening tag), and an unclosed
`<think>` (generation stopped mid-thought). Calls inside a thought are ignored.
The UI keeps the thought. History sent back to the model never includes it.

## 3. Tools

All tools take a JSON-schema `parameters` object (served by `GET /api/tools`),
report `available()` honestly, never raise, and are audited (`G3-tool`).

| Tool | Category | Arguments (`?` optional) | Notes |
|---|---|---|---|
| `time_now` | info | `timezone?` | machine clock |
| `location` | info | `place?` | available only after the browser shared a position (`POST /api/tools/location`) |
| `weather` | network | `place?`, `latitude?`, `longitude?` | Open-Meteo |
| `web_search` | network | `query` | provider from env: Brave / Serper / Tavily / SearXNG / DuckDuckGo (default, no key), 8 s timeout |
| `web_fetch` | network | `url`, `max_chars?` (default 8000, max 40000) | readable text (scripts/nav/footer removed), title, explicit "page cut" note; download capped at 2 MB |
| `file_read` | read | `path`, `offset?`, `max_chars?` (20000, max 200000) | refuses binary files; tells the model the next offset |
| `file_list` | read | `path?`, `depth?` (1–5), `recursive?` | indented tree with sizes, 500-entry cap |
| `file_write` | write | `path`, `content?`, `mode?` = overwrite\|create\|append | atomic temp-file write, 1 MB cap, creates parent folders |
| `make_dir` | write | `path` | creates parents |
| `file_move` | write | `src`, `dst`, `overwrite?` | a folder as `dst` moves into it |
| `file_copy` | write | `src`, `dst`, `overwrite?` | files or folders |
| `file_delete` | delete | `path`, `recursive?` | a non-empty folder needs `recursive`; the workspace root itself is never deleted |
| `shell` | shell | `command`, `cwd?`, `timeout?` (30, max 300) | PowerShell on Windows, `/bin/sh` elsewhere; see the security model |
| `run_agent` | agent | `task`, `tools?`, `max_steps?` (4, max 8) | sub-agent, see §5 |

`web_search` needs no API key: it is the research engine (`docs/research/README.md`),
which searches the whole web through the app's embedded browser plus keyless
APIs. When every source refuses, it reports a **failure**, never an empty success.

## 4. Security model

**Who decides:** code in `services/tools/permissions.py`, never the model.
Every call, whether from the model, a sub-agent or `POST /api/tools/run`, goes
through `ToolExecutor.execute` in this order: tool lookup → sub-agent
restriction → schema validation → policy → approval → run → event + audit.

**Workspace.** One folder, `permissions.workspaceRoot`, defaults to
`~/.imurun/workspace` and is created on first use. Every path argument of the
file tools (and `shell.cwd`) is resolved by `Workspace.resolve`: relative to
the root, then `realpath` (so `..` and symlinks cannot escape), then it must
still be inside the root. After that, `safety.decisions.check_action` refuses
`.git`, guardrail source, secret files (`.env*`, `.netrc`, `.npmrc`, `.pypirc`,
`.git-credentials`, `.htpasswd`) and system locations. A whole drive (`/`,
`C:\`) can never be the workspace. Paths outside the workspace are **denied in
every mode**: there is no "outside" permission.

**Modes** (`permissions.mode`, default `allow-reads`):

| category | `ask` | `allow-reads` | `allow-all` |
|---|---|---|---|
| info, network, agent | allow | allow | allow |
| read (`file_read`, `file_list`) | ask | allow | allow |
| write (`file_write`, `make_dir`, `file_move`, `file_copy`) | ask | ask | allow |
| delete (`file_delete`) | ask | ask | allow |
| shell | ask | ask | allow |

Hard denials apply before the table, in every mode:
- a category toggle is off (`readFiles`, `writeFiles` (also covers delete), `runShell`, `network`);
- the path is outside the workspace or protected;
- the shell command matches the destructive deny-list
  (`safety.decisions.DANGEROUS_COMMANDS`: `rm -rf /`, `rm -r ~`, `mkfs`, `dd of=/dev/`,
  `format c:`, `del /s ... C:\`, `rd /s /q C:\`, `Remove-Item -Recurse` on a drive or home,
  `Format-Volume`, `Clear-Disk`, `diskpart`, `bcdedit`, `reg delete`, shutdown/restart,
  `Set-ExecutionPolicy Bypass`, fork bombs, `curl|sh`, `iwr|iex`, shadow-copy deletion,
  `cipher /w`, `chmod -R 777 /`, writes to raw disks, `net user /add`, `icacls Everyone:F`,
  `takeown` on a drive). The tool checks the deny-list again itself, so a direct `run()` cannot skip it.

**Approval cards.** An "ask" inside a turn publishes `tool.approval_needed`
and blocks the turn thread until one of these happens:
`POST /api/tools/approvals/{id}` `{"approved": bool, "remember": bool}`
arrives, the timeout passes (`permissions.approvalTimeout`, default 180 s,
counts as denied), or the turn is stopped. When no SSE client is subscribed,
the call is denied at once ("no client is connected to approve it").
`remember: true` lets that tool run without asking for the rest of the session
until the server restarts. Deletes are never remembered. Manual runs follow the
connector pattern: `POST /api/tools/run` → `403 {"decision": "ask"}` → show a
card → post again with `"approved": true`.

**Shell is not a sandbox.** It runs in the workspace with stdin closed, a
timeout that kills the whole process tree (`taskkill /T` / `killpg`), output
capped at 20 000 chars (head and tail kept), and environment variables whose
names look like credentials (`*TOKEN*`, `*SECRET*`, `*PASSW*`, `*API_KEY*`,
`*_KEY`, `*CREDENTIAL*`, `*COOKIE*`, `*SESSION*`) removed. A command can still
reach any file the user can, which is why shell asks unless the mode is
`allow-all`.

**Audit.** The executor marks `G3-tool` with the tool name, the source
(`model`, `agent:N`, `user`), the decision and the outcome. Argument values
(commands, file bodies, URLs) are **not** written to the audit, because they may
hold secrets. Events to the local UI do carry arguments (needed for the card),
with long strings cut at 2000 chars.

## 5. Skills and sub-agents

**Skills** are instruction packs in `~/.imurun/skills/<id>/SKILL.md`
(`services/skills/store.py`; create with `POST /api/skills`). Writing `/<id>`
(or the slugged name) at the start of a prompt or after a space injects that
skill's body (max 4000 chars, max 3 skills) into the system prompt for that
turn. The token is removed from what the model sees. A prompt that is only
`/name` becomes "Follow the <name> skill.". Only stored names match, so
`/usr/bin` is untouched. The turn publishes `skill.used`, and the turn view
lists `skills`.

**Sub-agents**: `run_agent(task, tools?, max_steps?)` starts a nested
`AgentLoop` on the same raw model with no history, no persona, a short
"helper agent" directive, its own budget (default 4, max 8) and only the
tools requested, intersected with the parent's. Its calls go through the same
executor and policy, with `depth` in their events. It returns only its summary.
Depth: main = 0, sub-agent = 1, its sub-agent = 2, where `run_agent` is neither
offered nor allowed (`executor.MAX_DEPTH = 2`). The parent's Stop stops the
child (`parent_stop`). Events: `agent.start` / `agent.end`.

## 6. Real-time correctness

1. House rule `LIVE_DATA_RULE` (`services/response/style.py`): state live values
   only from a tool result or Context block. Otherwise say live data can't be
   checked right now, and give no current values from memory.
2. The system prompt states today's date from the machine clock.
3. The tool prompt's first "when to use a tool" rule is "anything about the present".
4. Pre-fetch offline/failure text (`policy.tool_instructions`) uses the same
   wording (`OFFLINE_SENTENCE`).
5. Failed tool results say `FAILED: ... Do not guess what the tool would have returned`.
6. The realtime nudge: a present-tense question (`policy.is_realtime` plus question
   form) answered with no tool result and no "can't check" admission gets one
   reminder step (`protocol.realtime_nudge`).
7. `web_search` reports a refused/blocked provider as a failure, not as "no results".

## 7. Events (SSE `/api/events`)

New: `tool.call`, `tool.result`, `tool.approval_needed`, `tool.approved`,
`tool.denied`, `agent.start`, `agent.end`, `skill.used`, `permissions`.
Extended: `step` (+`kind`), `turn.end` (+`tool_calls`, `skills`).
Unchanged: `tools.start`, `tools.done`, `web_search.start`, `web_search.done`
(pre-fetch), and the rest. Payloads are in `services/server/doc/README.md`.

## 8. Endpoints

`GET /api/tools`, `POST /api/tools/run`, `GET /api/tools/approvals`,
`POST /api/tools/approvals/{id}`, `GET|POST /api/permissions`,
`GET|POST /api/agent`, `GET /api/skills/{id}` (new), plus the existing
`GET|POST /api/skills` and `DELETE /api/skills/{id}` (which works now).
Bodies and status codes are in `services/server/doc/README.md`.

## 9. Tests

`.\.venv\Scripts\python.exe -m pytest tests -q`. The agentic tests:
`tests/test_agentic_parser.py` (formats and malformed input),
`tests/test_agentic_loop.py` (scripted fake model: tool→answer, budget cap,
retry, cache, stop, R1, nudge, compaction, queue, sub-agents, skills),
`tests/test_agentic_permissions.py` (modes, confinement, deny-list, toggles,
approvals), `tests/test_agentic_tools.py` (file/shell tools in a temp
workspace), and `tests/test_agentic_api.py` (in-process `ApiServer`: routes,
a tool-using turn, 503 without a model).

## 10. Known limits

- The format is still free text. A 1B model sometimes ignores the tool prompt
  entirely; the pre-fetch and the nudge are the safety net, not a guarantee.
- One turn waiting on an approval card blocks its own thread only. Other
  sessions keep working.
- `remember` grants live in memory (lost on restart) by design.
- Token counts are estimated, not tokenized (see REQUESTS.md).
