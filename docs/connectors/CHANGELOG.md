# Connectors and MCP: changelog

## 2026-11-01 (later) — Connect with built-in apps, token path, secret store

### Added
- **Built-in ImuTechz OAuth apps** (`services/connectors/apps.py` +
  gitignored `apps.local.json`): credentials resolve user → env
  (`IMURUN_<SERVICE>_CLIENT_ID`/`_SECRET`) → builtin → none, reported as
  `credential_source`; resolved when Connect is pressed. Maintainer command
  `python -m services.connectors.apps import-google <file>` / `list` (never
  prints a secret). Only Gmail has a built-in client today (after the import);
  the others carry `registration_todo` → `REGISTER-APPS.md`.
- **Connect flow**: `POST /api/connectors/{id}/connect {"open": true}` opens the
  system browser server-side (`{url, state, opened}`); `/oauth/callback` page
  "Connected as <account> — you can close this tab" with a 2 s `window.close()`
  (CSP-hashed script), the account stored as `<id>:account`, and a `connector`
  event `{connector, id, connected, account, via: "oauth"}` (also on failure /
  cancel). `account` is in every connector status.
- **Token path** for Cloudflare and Render (`ServiceSpec.auth = ("token",)`):
  `POST /api/connectors/{id}/token` verifies first (Cloudflare
  `/user/tokens/verify` + accounts + policies; Render `/v1/owners`), stores only
  an accepted token, returns `{connected, account, scopes_or_permissions,
  expires}`; `DELETE /api/connectors/{id}/token`. Per-tool minimum token
  permissions in availability reasons and 403 errors.
- **Cross-platform `SecretStore`** (`tokens.py`): keyring → macOS Keychain
  (`security -i`, secret on stdin) → libsecret (`secret-tool`, stdin) →
  file with DPAPI or AES-256-GCM (`cryptography`) → plain file marked
  `"__encryption__": "none"` and warned in every config view. Plain values are
  migrated on the next write. `backend_name()` / `encrypted` / `warning`
  surface as `store`, `store_encrypted`, `warning`.
- `GET /api/connectors/{id}/doc` (the setup page as markdown);
  `permission_level` per tool in `GET /api/tools`; `rememberable` / `level` on
  `tool.approval_needed` (publish hook from `tool_policy.install_approval_hints`).
- Tests: `test_connectors_secrets.py`, `test_connectors_apps.py`, token-mode and
  connect-flow tests; `tests/conftest.py` isolates tests from a real
  `apps.local.json`. Suite: 214 passing, 1 network test skipped.

### Changed
- Cloudflare and Render no longer offer OAuth (owner decision).
- `api.py`: `/oauth/callback` dispatches to `tool_routes.handle_callback`
  (falls back to the old page when connectors are not wired) plus a `_raw`
  writer; `do_DELETE` dispatch now also covers connector tokens.
- `FileTokenStore` / DPAPI moved from `wiring.py` to `tokens.py` (re-exported).
- Availability wording: "press Connect in Settings → Connectors → X"; "IMURUN
  has no built-in X app yet … Advanced"; "paste a X API token …".

## 2026-11-01 — every connector on OAuth, MCP client

### Added
- **OAuth for all eight connectors** through `OAuthFlow` + `/oauth/callback`:
  `services/connectors/setup.py` (`ServiceSpec` catalogue, `OAuthSetup`: the
  user's own client id in settings, the secret in the token store, minimum
  scopes for the enabled tools, flow registration, refresh).
  `OAuthProvider` gained `pkce` and `client_auth` (`basic` for Canva);
  empty scope lists are no longer sent; the granted `scope` is stored.
- `oauth.refresh_access_token` + `RefreshRejected`; refresh before expiry and
  on 401 in `providers/_rest.py` (all REST providers) and `providers/gmail.py`;
  rotated refresh tokens stored; a refused refresh forgets the account.
- **GitHub device flow** (`services/connectors/device.py`,
  `POST|GET /api/connectors/github/device`).
- Providers rewritten with the operations a model uses: Gmail (threads,
  messages, labels, drafts, send, modify, trash, profile), GitHub (repos,
  issues, PRs, files, create issue, comment), Slack (auth.test, channels,
  history, post; `ok:false` handled), Vercel (projects, deployments, build
  events, redeploy), Render (services, deploys, logs, deploy, restart),
  Cloudflare (accounts, zones, DNS read/create/update/delete, Workers, purge),
  Supabase (projects, organisations, PostgREST tables/select/insert with the
  anon or service_role key), Canva (designs, export with polling).
- `providers/gmail_mime.py`: base64url / multipart / charset / HTML-to-text
  decoding, capped bodies, RFC 2822 building with reply headers and CR/LF
  checks.
- **40 model tools**: `gmail_*` (7, `services/tools/builtin/gmail.py`) and
  33 more in `services/tools/builtin/service_tools.py` on the
  `connector_tools.py` base; honest `available()` reasons.
- `services/connectors/tool_policy.py`: levels `read` / `write` / `ask` /
  `always` for categories `connector-read`, `connector-write`, `mcp`; every
  send/trash/post/deploy/DNS/insert write asks in every mode.
- `FileTokenStore` encrypts values with Windows DPAPI (`dpapi:` prefix).
- `POST /api/connectors/gmail/import-client {path}`: imports a downloaded
  Google `client_secret_*.json` (id → settings, secret → token store; only
  `{client_id, project_id, redirect_uris, type, warnings}` returned). No other
  provider issues a credentials file, so the route answers 404 for them.
- Routes: `GET|POST /api/connectors/{id}/config`, `GET /api/connectors/{id}/test`,
  `POST|GET /api/connectors/{id}/device`; MCP routes; dispatch through
  `services/tools/routes.py` (+ one `handle_delete` line in `api.py`).
- **MCP client** (`services/mcp/`): stdio and Streamable HTTP, protocol
  `2025-06-18` with fallbacks, pagination, content summaries, timeouts,
  lazy reconnect, `mcp_<server>_<tool>` bridge, events `mcp.connected`,
  `mcp.disconnected`, `mcp.error`.
- Built-in MCP servers `gmail_mcp.py` and `connectors_mcp.py` (both off by
  default in `mcp.servers`).
- Settings sections `connectors` and `mcp`; `SettingsStore.read` now deep-copies
  defaults.
- Tests: `tests/test_connectors_{oauth,gmail,services,api}.py`,
  `tests/test_mcp_{client,bridge}.py` (+ `fakes_google.py`,
  `fixtures/fake_mcp_server.py`); suite 148 tests, 147 passing — the one
  failure is the stale web_search test listed in REQUESTS.md L6 (not ours).

### Changed
- Gmail no longer requests `openid email profile`; it requests
  `gmail.readonly` + only the scopes the enabled tools need.
- `GmailConnector` and `BearerConnector` gained `disconnect()` (the existing
  disconnect route now really forgets tokens; Google tokens are revoked).
- Canva `export_design` is a read capability (it changes nothing).
- `UrllibTransport.post_form` accepts extra headers.

### Not verified
No real sign-in was possible (no client ids here). Per-service status:
`README.md` → "Verification status".
