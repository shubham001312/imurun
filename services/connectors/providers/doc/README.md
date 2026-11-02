# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\`

## Purpose

One adapter per external service. Every class is a `Connector` (see
`services/connectors/doc/README.md`) that declares its actions with a capability
kind and translates an action plus `params` into a single HTTP call through an
injected transport. No provider decides authorization, stores tokens itself, or
retries; the gate and the token store own those concerns. `_rest.py` holds the
shared `BearerConnector` base used by six of the eight providers.

> **Superseded 2026-11-01:** the tables below describe the original stubs.
> Every provider was rewritten (more actions, token refresh through
> `_rest.BearerConnector`, `disconnect()`); the current actions are listed in
> each provider module and the tools/scopes in `docs/connectors/<SERVICE>.md`.
> New helper: `gmail_mime.py` (MIME decode / RFC 2822 build).

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\_rest.py`

| Symbol | Signature | Notes |
|---|---|---|
| `BearerConnector.base` | `str = ""` | Class attribute; the provider root URL. |
| `BearerConnector.__init__` | `(transport, tokens)` | Stores both; no I/O. |
| `BearerConnector.connected` | `() -> bool` | `tokens.get(self.id) is not None`. |
| `BearerConnector._headers` | `() -> dict` | `{"Authorization": "Bearer <token>"}`; raises `ConnectorError("<id> is not connected")`. |
| `BearerConnector._request` | `(method, path, body=None) -> dict` | Joins `base + path`; sends `body` as JSON when given. |

`_rest.py` is a private module (leading underscore); the class is imported by the
provider modules, not by the package `__init__`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\github.py`

`GitHubConnector` (`id="github"`, `label="GitHub"`), direct `Connector`
implementation, `Accept: application/vnd.github+json`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_repos` | read | – | `GET https://api.github.com/user/repos` |
| `list_issues` | read | `repo` (`owner/name`) | `GET /repos/{repo}/issues` |
| `create_issue` | write | `repo`, `title` (required), `body` | `POST /repos/{repo}/issues` |
| `comment` | write | `repo`, `number` (digits), `body` (required) | `POST /repos/{repo}/issues/{number}/comments` |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\gmail.py`

`GmailConnector` (`id="gmail"`, `label="Gmail"`), base
`https://gmail.googleapis.com/gmail/v1/users/me`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_messages` | read | `q` (optional, quoted with `quote_query`) | `GET /messages?q=...` |
| `get_message` | read | `id` (`require_token`) | `GET /messages/{id}` |
| `create_draft` | write | `raw` (required) | `POST /drafts` `{"message": {"raw": raw}}` |
| `send` | write | `raw` (required) | `POST /messages/send` `{"raw": raw}` |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\slack.py`

`SlackConnector(BearerConnector)`, base `https://slack.com/api`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_channels` | read | – | `GET /conversations.list` |
| `get_history` | read | `channel` (`require_channel`) | `GET /conversations.history?channel=...` |
| `post_message` | write | `channel`, `text` (required) | `POST /chat.postMessage` |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\vercel.py`

`VercelConnector(BearerConnector)`, base `https://api.vercel.com`, module constant
`TARGETS = ("production", "preview")`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_projects` | read | – | `GET /v9/projects` |
| `list_deployments` | read | `project` (`require_token`) | `GET /v6/deployments?projectId=...` |
| `create_deployment` | write | `name` (required), `project`, `target` in `TARGETS` | `POST /v13/deployments` |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\render.py`

`RenderConnector(BearerConnector)`, base `https://api.render.com/v1`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_services` | read | – | `GET /services` |
| `list_deploys` | read | `service` (`require_token`) | `GET /services/{service}/deploys` |
| `trigger_deploy` | write | `service` | `POST /services/{service}/deploys` `{}` |
| `restart_service` | write | `service` | `POST /services/{service}/restart` `{}` |

`service` is validated before the action branch, so every non-`list_services`
action fails fast on a missing or unsafe id.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\cloudflare.py`

`CloudflareConnector(BearerConnector)`, base
`https://api.cloudflare.com/client/v4`, `RECORD_TYPE = ^[A-Za-z]{1,10}$`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_zones` | read | – | `GET /zones` |
| `list_dns_records` | read | `zone` (`require_token`) | `GET /zones/{zone}/dns_records` |
| `create_dns_record` | write | `zone`, `type`, `name`, `content` | `POST /zones/{zone}/dns_records` |
| `update_dns_record` | write | `zone`, `record_id`, optional `type`/`name`/`content` | `PATCH /zones/{zone}/dns_records/{record_id}` |
| `purge_cache` | write | `zone` | `POST /zones/{zone}/purge_cache` `{"purge_everything": true}` |

`zone` is validated before the action branch; `type` is matched against
`RECORD_TYPE`; `name` and `content` must be non-empty for creation.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\supabase.py`

`SupabaseConnector(BearerConnector)`, base `https://api.supabase.com/v1`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_projects` | read | – | `GET /projects` |
| `list_organizations` | read | – | `GET /organizations` |
| `create_project` | **admin** | `name`, `organization`, `db_pass` | `POST /projects` |

`create_project` is the only `ADMIN` capability in the tree; it is gated exactly
like a write (ask unless approved) but is classified separately so a UI can
label it.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\canva.py`

`CanvaConnector(BearerConnector)`, base `https://api.canva.com/rest/v1`.

| Action | Kind | Params | Request |
|---|---|---|---|
| `list_designs` | read | – | `GET /designs` |
| `create_design` | write | `design_type` (`require_token`) | `POST /designs` |
| `export_design` | write | `design_id`, `format` | `POST /exports` `{"design_id", "format": {"type": fmt}}` |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\providers\__init__.py`

Re-exports the eight classes: `CanvaConnector`, `CloudflareConnector`,
`GitHubConnector`, `GmailConnector`, `RenderConnector`, `SlackConnector`,
`SupabaseConnector`, `VercelConnector`.

## Connections

- Imported by `services/connectors/wiring.py` (registration and OAuth specs) and
  re-exported by `services/connectors/__init__.py`.
- Import `..base` helpers and, for six of them, `. _rest.BearerConnector`.
- Reachable from HTTP only through `POST /api/connectors/{id}/actions/{action}`
  (gate first). The Connect UI in `frontend/design-lab/app.js` lists the same
  eight providers.
- Tests: `test/services/connectors/test_providers.py`,
  `test_more_providers.py` (every provider: no token, read, write asks, write
  approved, capability kinds, unique ids, injection refusal).

## Invariants

- Every action not declared in `capabilities` must raise `ConnectorError`; the
  gate already denies it, but `execute` must stay total.
- A provider never reads a token itself; it goes through the injected store and
  only inside `_headers()`.
- Every id, repo, channel, zone, service and record value interpolated into a URL
  is validated first. Do not add an f-string URL with a raw `params` value.
- Request bodies contain only the fields listed above; do not forward the whole
  `params` dict to a provider.
- Capability kinds stay within `READ`/`WRITE`/`ADMIN`; the gate and the UI switch
  on those exact strings.
- Adding a provider means: one module, one entry in `providers/__init__.py`, one
  `ProviderSpec` in `wiring.py`, one card in the Connect UI, and a test row in
  `test_more_providers.py`.
