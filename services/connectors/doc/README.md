# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\`

## Purpose

Connectors are the only path from IMURUN to an external service. Each provider is
a `Connector` subclass that declares typed capabilities (`read`, `write`,
`admin`); `ConnectorGate` decides whether an action may run, `OAuthFlow` performs
the authorization-code + PKCE exchange, and a token store keeps the resulting
credential in the OS keychain (or a private file when no keychain exists).
`ConnectorRegistry` holds the live set, and `wiring.py` is the production
composition that registers all eight providers and every OAuth flow whose client
id is configured.

> **2026-11-01:** every connector now signs in with OAuth against the user's
> own app (`setup.py`: `ServiceSpec` catalogue + `OAuthSetup`; `gmail_setup.py`
> is the Gmail subclass), refreshes tokens (`oauth.refresh_access_token`,
> `providers/_rest.py`), supports GitHub's device flow (`device.py`), exposes
> `<service>_<verb>` model tools (registered by `wiring.wire_server`; levels in
> `tool_policy.py`) and the set-up routes in `routes.py`. The provider action
> tables in `providers/doc/README.md` predate this; the current actions,
> tools, scopes and per-service registration steps are in
> `docs/connectors/` (README + one page per service). `FileTokenStore`
> encrypts values with DPAPI on Windows. `wire_server(server, home, *,
> transport=None, tokens=None, form_transport=None, start_mcp=True)` also
> wires the MCP manager (`services/mcp/`).

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\base.py`

| Symbol | Signature | Notes |
|---|---|---|
| `READ`, `WRITE`, `ADMIN` | `"read"`, `"write"`, `"admin"` | Capability kinds. |
| `KINDS` | `tuple[str, ...]` | `(READ, WRITE, ADMIN)`. |
| `SAFE_TOKEN` | `re.Pattern` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$`. |
| `SAFE_REPO` | `re.Pattern` | `owner/name`, 1–100 chars per side. |
| `SAFE_CHANNEL` | `re.Pattern` | Optional leading `#`, then 1–200 safe chars. |
| `ConnectorError` | `class ConnectorError(RuntimeError)` | Raised for refused or failed connector actions. |
| `require_token` | `(value, field: str) -> str` | Strips and validates against `SAFE_TOKEN`; raises `ConnectorError`. |
| `require_repo` | `(value, field: str = "repo") -> str` | Validates `SAFE_REPO`; raises `ConnectorError`. |
| `require_channel` | `(value) -> str` | Validates `SAFE_CHANNEL`; raises `ConnectorError`. |
| `quote_query` | `(value) -> str` | `urllib.parse.quote(str(value), safe="")`; a query value cannot add parameters. |
| `Capability` | `@dataclass(frozen=True) Capability(action: str, kind: str)` | |
| `HttpTransport` | `Protocol.request(method, url, headers, json=None) -> dict` | Structural type only. |
| `Connector` | `ABC` with `id: str`, `label: str`, `capabilities: tuple[Capability, ...]` | |
| `Connector.capability` | `(action: str) -> Capability \| None` | Linear lookup. |
| `Connector.connected` | `() -> bool` (abstract) | |
| `Connector.execute` | `(action: str, params: dict) -> dict` (abstract) | |
| `Connector.status` | `() -> dict` | `{id, label, connected, capabilities:[{action, kind}]}`. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\gate.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ConnectorGate.__init__` | `(mark=None)` | `mark` is called as `mark("G3-connector", subject, why)`. |
| `ConnectorGate.check` | `(connector: Connector, action: str, approved: bool = False) -> Decision` | Unknown action → `deny`; `READ` → `allow`; otherwise `allow` only when `approved`, else `ask`. |
| `ConnectorGate.execute` | `(connector, action, params, *, approved: bool = False) -> dict` | Re-checks, enforces with `require()`, calls `connector.execute`, wraps any exception in `ConnectorError`, and marks `ok`/`refused`/`failed`. |

Side effects: one `mark` per executed action; never logs the token or payload.
Failure modes: `PermissionRequired` for `ask`/`deny`; `ConnectorError` for any
provider failure (the original exception is chained).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\oauth.py`

| Symbol | Signature | Notes |
|---|---|---|
| `LOOPBACK` | `frozenset` | `127.0.0.1`, `localhost`, `::1`. |
| `OAuthError` | `class OAuthError(RuntimeError)` | |
| `OAuthTransport` | `Protocol.post_form(url, data) -> dict` | |
| `OAuthProvider` | `@dataclass(frozen=True) OAuthProvider(id, authorize_url, token_url, scopes, client_id, client_secret="", scope_separator=" ", authorize_params=(), token_params=())` | |
| `OAuthFlow.__init__` | `(provider, transport, tokens, redirect_uri, *, ttl=600.0, mark=None, clock=time.time)` | Validates: non-empty `provider.id`/`client_id`, both endpoints `https`, redirect host loopback, `ttl > 0`. |
| `OAuthFlow.begin` | `() -> dict` | Returns `{"url": ..., "state": ...}`; stores the PKCE verifier in memory with an expiry. |
| `OAuthFlow.complete` | `(state: str, code: str) -> str` | Pops the state (one-time), exchanges the code, stores `access_token` (and `refresh_token` / `expires_at` when present), returns the access token string. |

Failure modes: `ValueError` at construction for an unsafe configuration;
`OAuthError` for unknown/expired state, missing code, transport failure, a
provider `ok: false`, or a response without `access_token`. The client secret is
sent only in the token POST body, never in the authorize URL. A refresh token or
expiry that the store rejects is swallowed (they are optional).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\registry.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ConnectorRegistry.register` | `(connector: Connector) -> None` | Raises `ValueError` on a duplicate id. |
| `ConnectorRegistry.get` | `(connector_id: str) -> Connector \| None` | |
| `ConnectorRegistry.ids` | `() -> list[str]` | Sorted. |
| `ConnectorRegistry.list` | `() -> list[dict]` | `Connector.status()` for each, insertion order. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\tokens.py`

| Symbol | Signature | Notes |
|---|---|---|
| `TokenStoreUnavailable` | `class TokenStoreUnavailable(RuntimeError)` | Raised when `keyring` is missing; there is deliberately no plain-file fallback here. |
| `MemoryTokenStore` | `get(provider) -> str \| None`, `set(provider, token)`, `delete(provider)` | Test double. `set` raises `ValueError` on an empty argument. |
| `KeyringTokenStore.__init__` | `(service: str = "imurun")` | Imports `keyring` lazily. |
| `KeyringTokenStore` | same three methods | Uses the OS keychain; `delete` swallows failures. |
| `redact` | `(token: str \| None) -> str` | `(none)`, `****`, or `****` + last 4 chars. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\transport.py`

| Symbol | Signature | Notes |
|---|---|---|
| `USER_AGENT` | `"IMURUN/1.0 (+local)"` | |
| `DEFAULT_TIMEOUT` | `20.0` | |
| `UrllibTransport.__init__` | `(timeout: float = DEFAULT_TIMEOUT)` | |
| `UrllibTransport.request` | `(method, url, headers=None, json_body=None, *, json=None) -> dict` | JSON body; `json` is an alias for `json_body`. |
| `UrllibTransport.post_form` | `(url, data) -> dict` | Form-encoded OAuth token exchange. |

Failure modes: every failure is converted to `ConnectorError` whose text carries
the method, netloc, status and a short provider reason — never the bearer token
or the request body. `_decode` accepts JSON, form-encoded pairs, or returns
`{"raw": text}`. Standard library only.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\connectors\wiring.py`

| Symbol | Signature | Notes |
|---|---|---|
| `FileTokenStore.__init__` | `(path: Path \| str)` | Creates the parent and chmods it `0700` where supported. |
| `FileTokenStore` | `get` / `set` / `delete` | Atomic write via `.json.tmp` + `os.replace`, chmod `0600`. |
| `build_token_store` | `(home) -> store` | `IMURUN_TOKEN_STORE` = `memory` → `MemoryTokenStore`; `file` → `FileTokenStore(home/"secrets"/"connectors.json")`; anything else tries `KeyringTokenStore` and falls back to the file store. |
| `ProviderSpec` | `@dataclass(frozen=True) ProviderSpec(id, authorize_url, token_url, scopes=(), scope_separator=" ", authorize_params=(), token_params=(), connector=None)` | |
| `DEFAULT_PROVIDERS` | `tuple[ProviderSpec, ...]` | Gmail, GitHub, Slack, Vercel, Render, Cloudflare, Supabase, Canva (in that order). |
| `provider_specs` | `() -> tuple[ProviderSpec, ...]` | Applies `IMURUN_OAUTH_<ID>_{AUTHORIZE_URL,TOKEN_URL,SCOPES}` overrides. |
| `build_registry` | `(transport=None, tokens=None) -> ConnectorRegistry` | Defaults: `UrllibTransport()`, `MemoryTokenStore()`. |
| `build_oauth_flows` | `(tokens, redirect_uri, *, mark=None) -> dict[str, OAuthFlow]` | One flow per provider that has `IMURUN_OAUTH_<ID>_CLIENT_ID`. |
| `wire_server` | `(server, home) -> dict` | Registers providers on `server._connectors`, adds flows via `server.add_oauth_flow`, returns `{"connectors", "oauth", "token_store"}`. |

`wire_server` mutates the live `ApiServer` and computes the redirect as
`IMURUN_OAUTH_REDIRECT_BASE.rstrip("/") + "/oauth/callback"`, else
`http://127.0.0.1:{server.port}/oauth/callback`.

## Connections

- Imports `services/safety/decisions.py` (`Decision`, `PermissionRequired`,
  `require`) from the gate only. No provider imports `gate`; the API calls the
  gate, providers stay dumb transports.
- `services/server/app.py` calls `wire_server(server, home)` after
  `build_server` when no registry was injected; a failure prints
  `connectors: not wired (...)` and the shell still starts.
- `services/server/api.py` imports `ConnectorError`, `ConnectorGate`,
  `OAuthError`, `OAuthFlow`, `ConnectorRegistry`.
- HTTP routes: `GET /api/connectors`, `GET /api/connectors/{id}`,
  `POST /api/connectors/{id}/connect`, `POST /api/connectors/{id}/disconnect`,
  `POST /api/connectors/{id}/actions/{action}`, `GET /oauth/callback`.
- UI: `frontend/design-lab/app.js` renders the Connect grid and calls
  `api.js` (`listConnectors`, `connectConnector`, `disconnectConnector`,
  `getConnectorStatus`, `runConnectorAction`).
- Tests: `test/services/connectors/` (gate, registry, oauth, providers,
  more_providers).

## Invariants

- Reads run without approval; writes and admin actions always return `ask` unless
  the caller passes `approved=True` for that single action. Do not add a
  provider whose write path bypasses `ConnectorGate.execute`.
- Tokens never appear in a URL, a log, an audit mark, or a `ConnectorError`
  message. `redact()` is the only acceptable representation.
- The OAuth state is single-use and popped before the token exchange; a replayed
  callback must fail.
- OAuth endpoints must be `https` and the redirect must be loopback. This is
  enforced in `OAuthFlow.__init__`.
- Provider path/query values are validated (`require_token`, `require_repo`,
  `require_channel`, `quote_query`) before interpolation into a URL.
- `ConnectorRegistry.register` refuses duplicates; re-registering a provider must
  go through `get()` first (as `wire_server` does).
- The keychain is the default store. A file store is only chosen explicitly or
  when `keyring` is unavailable, and it must stay `0600` inside a `0700` folder.
