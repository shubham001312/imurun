# MCP: any Model Context Protocol server's tools as model tools

Code: `services/mcp/client.py` (protocol), `services/mcp/registry.py`
(settings → clients → tools), `services/tools/builtin/mcp_bridge.py` (the
tool class), `services/mcp/routes.py` (HTTP), `services/mcp/servers/`
(built-in servers). Standard library only.

## Configuration (settings section `mcp`)

```json
{"servers": [
  {"id": "files", "name": "Files", "transport": "stdio",
   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/notes"],
   "env": {"SOME_KEY": "${env:SOME_KEY}"}, "cwd": "",
   "enabled": true, "auto_approve": ["read_file", "list_directory"], "timeout": 60},
  {"id": "remote", "name": "Remote", "transport": "http",
   "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${env:REMOTE_TOKEN}"},
   "enabled": false}
]}
```

| Field | Meaning |
|---|---|
| `id` | 1–32 chars `a-z 0-9 - _`; part of every tool name |
| `transport` | `stdio` (spawn a process) or `http` (Streamable HTTP) |
| `command`, `args`, `env`, `cwd` | stdio only; `cwd` defaults to the imurun folder |
| `url`, `headers` | http only; `https://`, or `http://` on 127.0.0.1/localhost/::1 only |
| `enabled` | connect at start-up and offer the tools |
| `auto_approve` | MCP tool names (as the server names them) that run without a card |
| `timeout` | seconds per tool call (1–600, default 60); connect has 20 s |

Placeholders in command/args/cwd/env/headers/url: `${python}` (this
interpreter), `${repo}` (the imurun folder), `${home}` (`~/.imurun`),
`${env:NAME}` (an environment variable, so a key need not sit in the
settings file). The settings file is `0600` but plain text; `GET` views
replace every env and header value with `********`, and posting
`********` back keeps the stored value.

The child process gets the parent environment **minus** credential-looking
names (`*TOKEN*`, `*SECRET*`, `*KEY`…, same rule as the shell tool) plus its
own `env`.

## Protocol

* JSON-RPC 2.0. **stdio**: newline-delimited JSON on stdin/stdout; a stray
  non-JSON stdout line is ignored; stderr is kept (last 20 lines) for error
  messages only. **Streamable HTTP**: each message is POSTed with
  `Accept: application/json, text/event-stream`; a JSON or SSE answer is read
  until the matching response; `Mcp-Session-Id` is kept from `initialize` and
  sent afterwards with `MCP-Protocol-Version`; `DELETE` on close.
* `initialize` asks for `2025-06-18`; `2025-03-26` and `2024-11-05` are
  accepted when the server answers with them; anything else is refused.
  Then `notifications/initialized`.
* `tools/list` follows `nextCursor` (20 pages, 500 tools max).
* `tools/call`: text kept; image/audio → `[image: type, N bytes, not shown]`;
  embedded text resources inlined; binary resources and links summarised;
  `structuredContent` used when there is no text; capped at 12 000 chars.
  `isError: true` → a failed tool result with the server's text.
* The server's own `ping` requests are answered; other server requests get
  `-32601` (no sampling / roots / elicitation). `notifications/tools/list_changed`
  re-lists the tools.
* Timeouts everywhere; on a timeout `notifications/cancelled` is sent. A dead
  process fails the pending calls with its exit code and stderr tail. Nothing
  raises out of the tool loop: every failure is a failed tool result.
* Close: stdin closed, 2 s grace, then the process tree is killed
  (`taskkill /T` on Windows). Servers are also closed at interpreter exit.

## Lifecycle

* Enabled servers connect in a background thread when the server starts.
* A dropped connection is re-established **lazily on the next tool call**.
* Saving a server (`POST /api/mcp/servers`) disconnects and re-discovers it.
* A disabled or removed server's tools are retired: they report unavailable
  and are not offered (the registry has no `unregister` yet, REQUESTS.md).

## Tool names and permissions

`mcp_<server id>_<tool>`, lower-cased, non `[a-z0-9_]` → `_` (e.g.
`mcp_files_read_file`); a clash gets `_2`. Schema: the server's `inputSchema`
passed through (the executor coerces types). Description: `[MCP <name>] …`.

Category `mcp` (`connectors/tool_policy.py`):

| MCP tool | `ask` | `allow-reads` | `allow-all` |
|---|---|---|---|
| `annotations.destructiveHint: true` | ask | ask | ask — **`auto_approve` does not override it**, "remember" is ignored |
| listed in `auto_approve` | allow | allow | allow |
| `annotations.readOnlyHint: true` | ask | allow | allow |
| anything else | ask | ask | ask ("remember" holds for the session) |

Hints are the server's own claims about third-party code, hence the default
of asking even in `allow-all`. HTTP servers follow the `network` toggle.

## Built-in servers (off by default)

| Entry | Script | Tools |
|---|---|---|
| `gmail` | `services/mcp/servers/gmail_mcp.py --home ${home}` | the 7 Gmail tools |
| `connectors` | `services/mcp/servers/connectors_mcp.py --home ${home} --services github,slack,...` | the 33 tools of the other seven connectors |

They read the same token store and settings as IMURUN, so accounts are
connected once, in Settings. Annotations follow IMURUN's own levels: reads
are `readOnlyHint`; every "always ask" write (send, trash, post, deploy, DNS,
insert…) is `destructiveHint`, so the bridge always asks. The scripts make no
permission decisions themselves: the MCP host that runs them must ask the
user. Any MCP host (not only IMURUN) can run them the same way.

Enable one:

```powershell
Invoke-RestMethod "$b/api/mcp/servers" -Method Post -Headers $h -ContentType application/json -Body '{"id": "gmail", "enabled": true}'
Invoke-RestMethod "$b/api/mcp/servers/gmail/connect" -Method Post -Headers $h
Invoke-RestMethod "$b/api/mcp/servers/gmail/tools" -Headers $h
```

Using both the direct `gmail_*` tools and `mcp_gmail_gmail_*` duplicates the
same capability for the model; enable the MCP entry only to test the MCP path
or to share the tools with another MCP host.

## Third-party servers (Node / npx)

This machine has no Node.js. Once Node ≥ 18 is installed (`winget install
OpenJS.NodeJS.LTS`), `npx`-based servers work as stdio entries — on Windows
`npx` resolves to `npx.cmd` through `PATH`:

```json
{"id": "fs", "name": "Filesystem", "transport": "stdio", "command": "npx",
 "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"], "enabled": true}
```

Python servers: `"command": "${python}", "args": ["-m", "some_mcp_server"]`
(installed into the same venv) or `uvx`. Remote servers: `"transport":
"http"` with their URL; servers that need their own OAuth (MCP authorization)
are **not** supported yet — pass a static header via `${env:...}` if the
server allows one.

## Routes and events

`GET /api/mcp/servers`, `GET /api/mcp/servers/{id}`,
`GET /api/mcp/servers/{id}/tools`, `POST /api/mcp/servers` (add/update),
`POST /api/mcp/servers/{id}/connect` (waits; 409 when disabled, 502 with the
reason on failure), `POST /api/mcp/servers/{id}/disconnect`,
`DELETE /api/mcp/servers/{id}`. Events: `mcp.connected {id, name, tools,
protocolVersion, server}`, `mcp.disconnected {id, name, reason}`,
`mcp.error {id, name, error}`. Contract: `services/server/doc/README.md`.

## Limits

* Tools only: MCP resources, prompts, sampling, roots and elicitation are not
  used.
* No MCP OAuth authorization for remote servers.
* No SSE-only legacy transport (`2024-11-05` HTTP+SSE); stdio and Streamable
  HTTP only.
* Tool calls to one stdio server are concurrent on the wire; a
  single-threaded server answers them in order.

## Tests

`tests/test_mcp_client.py` (fake stdio server `tests/fixtures/fake_mcp_server.py`:
initialize, paginated list, call, content kinds, isError, remote error,
timeout, crash, silence, protocol versions; in-process Streamable HTTP server
with JSON and SSE answers and sessions), `tests/test_mcp_bridge.py` (manager,
names, permission levels, re-discovery, lazy reconnect, redaction, the two
built-in servers over real stdio), `tests/test_connectors_api.py` (routes and
events).
