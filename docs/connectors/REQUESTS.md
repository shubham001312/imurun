# Requests from the connectors / MCP owner

Open items for other owners. Nothing here was edited outside the
connectors/MCP scope.

## Frontend (`frontend/design-lab/**`)

### F1. Connectors panel (Settings → Connectors) — updated 2026-11-01
One card per entry of `GET /api/connectors` (8 services). Read
`GET /api/connectors/{id}/config` for each card; `auth_mode` decides the card:

**OAuth cards** (`auth_mode: "oauth"`: Gmail, GitHub, Slack, Vercel, Supabase, Canva)
- Default view = status + one **Connect** button when `clientId` is set
  (`credential_source` is `builtin`, `env` or `user`). When
  `credential_source == "none"`, show "Not available yet" with the
  `registration_todo` text and an "Use your own app" link into Advanced.
- **Connect**: `POST /api/connectors/{id}/connect {"open": true}` — the server
  opens the system browser (never sign in inside pywebview: Google blocks
  embedded webviews). Show "Finish signing in in your browser…"; flip to
  connected on the `connector` event (`{connector, id, connected, account,
  message, via}`) — no polling needed; show `message` when `connected` is
  false (cancelled / failed). 409 = already connected.
- Status line: `account` ("Connected as …"), `scopes`, `expires_in` from
  `GET /api/connectors/{id}`.
- **GitHub "Connect without a browser"** (`deviceFlow`): `POST
  /api/connectors/github/device` → show `user_code` large +
  `verification_uri`; poll `GET .../device` until `connected`, `denied`,
  `expired` or `error`.
- **Advanced** (collapsed by default, hide all credential fields until
  opened): show `credential_source`; Client ID, Client secret (password,
  "saved" when `clientSecretSet`, never prefilled), `redirectUri` with copy
  button, tool checklist (`availableTools` / `tools`), `scopes` (read-only),
  options (Gmail `maxResults`, `bodyChars`; Supabase `keyRole` with a warning
  that `service_role` bypasses RLS). Save → `POST .../config` (changed fields
  only; `clientId: ""` returns to the built-in app). Gmail: "Import client
  file…" → `POST /api/connectors/gmail/import-client {"path"}` (pywebview file
  dialog or a path field; never read the file in the browser); show
  `client_id`, `project_id`, `warnings`. Port note for Slack/Vercel/Supabase/
  Canva: the built-in apps only work on port 8732.
- "How it works" link → `GET /api/connectors/{id}/doc` (markdown).

**Token cards** (`auth_mode: "token"`: Cloudflare, Render)
- Password field with paste, **Verify & save** → `POST /api/connectors/{id}/token
  {"token"}`. On 200 show `account` and `scopes_or_permissions` (and
  `expires`); on 401 show `error` and keep the field (nothing was stored).
  Clear the field after either answer; the token is never sent back.
- "Create a token" link → `token_console`; show the per-tool minimum
  permissions from `token_permissions` (Cloudflare template: Zone:Read,
  DNS:Edit, Workers Scripts:Read, scoped to the user's zones).
- When `has_token`: "Connected as `account`" + **Forget token** →
  `DELETE /api/connectors/{id}/token`.

**Storage badge** (every card, from `store` / `store_encrypted` / `warning`):
"Encrypted with Windows DPAPI" (`Windows DPAPI file`), "macOS Keychain",
"libsecret", "AES-GCM file" (`AES-256-GCM file`), "OS keychain" (`keyring (…)`),
and "⚠ not encrypted" with the `warning` text when `store_encrypted` is false.

- **Test**: `GET /api/connectors/{id}/test`; **Disconnect**:
  `POST /api/connectors/{id}/disconnect`.

### F2. Approval-card copy for connector tools
`tool.approval_needed.reasons[0]` already names the action, e.g.
"sends an email to bob@example.com with subject "Hi"; sent mail cannot be
unsent", "posts to Slack channel C1: "…"", "deletes DNS record … - this
cannot be undone". Please:
- render `gmail_send` / `gmail_draft` args as an email preview (To, Cc,
  Subject, Body) instead of raw JSON;
- for tools whose level is `always` (send, trash, label removal, every
  non-Gmail write, destructive MCP tools) **hide the "remember" checkbox
  when `tool.approval_needed.rememberable` is `false`** (also `level`:
  `read`/`write`/`ask`/`always`); `GET /api/tools` has `permission_level` per
  tool for the tools list;
- title: "<Service> wants to …"; buttons "Send" / "Post" / "Deploy" / "Delete"
  rather than a generic "Approve" when the tool name says so.

### F3. MCP servers panel (Settings → MCP)
- List `GET /api/mcp/servers`: name, transport, `status` (`disabled`,
  `connecting`, `connected`, `disconnected`, `error` + `error`), tool count,
  `serverInfo.name`, `protocolVersion`. Live: `mcp.connected`,
  `mcp.disconnected`, `mcp.error`.
- Add / edit form → `POST /api/mcp/servers`: id, name, transport; stdio:
  command, args (one per line), env (key/value, values masked; leave
  `********` untouched to keep), cwd; http: url, headers (masked); enabled;
  auto-approve (multi-select from the server's tools); timeout.
- Buttons: Connect (`POST .../connect`, show 502 reason), Disconnect, Delete
  (`DELETE /api/mcp/servers/{id}`), Tools (`GET .../tools`: name,
  description, `permission_level`, annotations).
- Show the two built-in entries (`gmail`, `connectors`) with a note that they
  reuse the accounts connected in the Connectors panel.

## Agentic loop owner (`services/tools/{permissions,executor,calls,protocol}.py`)

> **2026-11-01: L1, L2, L3, L4 and R1 are done** (`docs/agentic/CHANGELOG.md`).
> `PermissionPolicy` gates connector/MCP tools itself; `tool_policy.install`
> now returns without wrapping (`native_levels`), and the approval hook is not
> installed. The wrapper module can be deleted together with the tests that
> construct it directly (`tests/test_connectors_gmail.py`,
> `test_connectors_services.py`, `test_mcp_bridge.py`). L5 is done too.

- ~~**L1. Native connector permissions.**~~ `connectors/tool_policy.py` wraps
  `PermissionPolicy` because it cannot express per-argument levels or
  "always ask even in allow-all, never remember". Please add a tool hook
  (`permission_level(args) -> (level, reason)`) to `PermissionPolicy.decide`
  / `generic`, plus the categories `connector-read`, `connector-write`, `mcp`
  to `base.CATEGORIES`; then `install()` can be deleted.
- ~~**L2. `remember` for "always" tools.**~~ The executor calls
  `policy.remember()` even when the decision came from an "always" level; it
  is harmless (the wrapper ignores grants for them). `rememberable` / `level`
  are now added to `tool.approval_needed` by wrapping the broker's publish
  callback (`tool_policy.install_approval_hints`); please add them natively in
  `ApprovalBroker.request` / `Approval.view` (also for `GET
  /api/tools/approvals`, which the hook does not cover) and drop the hook.
- ~~**L3. Aliases**~~ in `calls.ALIASES`: `send_email`/`email_send` →
  `gmail_send`, `search_email`/`read_email` → `gmail_search`/`gmail_read`,
  `create_issue` → `github_create_issue`, `post_message`/`slack_message` →
  `slack_post`.
- ~~**L4. Tool prompt rule**~~ in `protocol.tool_prompt`: when any connector tool
  is offered, add "- The user's email, repositories, chat and cloud accounts:
  use the matching tool; never claim you sent, posted or changed something
  without a [tool result]." and "Prefer gmail_draft unless the user said send".
- ~~**L5. Many tools for small models.**~~ (done 2026-11-01: `services/tools/offer.py`
  offers connector tools per service the conversation points at.) With all eight services connected the
  prompt lists 40+ connector tools. Consider offering a service's tools only
  when the prompt mentions it (like the pre-fetch intents), or a
  per-session tool allow-list.

- ~~**L6. Stale web_search test**~~ — fixed by the research owner (suite green).

## Tools registry (`services/tools/registry.py`)

- ~~**R1. `ToolRegistry.unregister(name)`**~~ — done 2026-11-01; MCP tools of
  a removed/disabled server are removed from the registry.

## Server (`services/server/api.py`)

- **S1.** `ApiServer.stop()` should call `server._mcp.close_all()` when
  present (today only `atexit` closes MCP child processes).
- **S2. HTTPS loopback callback** (optional TLS listener with a self-signed
  certificate) — Slack documents HTTPS-only redirect URLs; without it Slack's
  browser flow may be impossible. The alternative is a Slack-specific
  "install to workspace" token, which the owner ruled out (OAuth only).

## Setup (`requirements*.txt`)

- **D1.** Optional: add `keyring` (OS keychain everywhere) and/or
  `cryptography` (AES-256-GCM file on macOS/Linux machines without
  Keychain/libsecret). Neither is required on Windows (DPAPI). Without them a
  Linux box with no `secret-tool` stores secrets unencrypted — warned in the UI.
- **D2. Release build** must package `services/connectors/apps.local.json`
  (gitignored by `*.local.json`) next to `apps.py`; see REGISTER-APPS.md.

## Product owner

- ~~P1. Render / Cloudflare~~ — decided: token path (done).
- **P2. Gmail Testing mode**: only listed test users can sign in to the
  built-in Google app and sign-ins expire after 7 days, until the app is
  published, which requires **Google's verification of the restricted Gmail
  scopes** (security assessment). Plan before public release.
- **P3. Register the five remaining built-in apps** (GitHub, Slack, Vercel,
  Supabase, Canva) — one checklist each in REGISTER-APPS.md; then run
  `python -m services.connectors.apps import-google <file>` once for Gmail.
