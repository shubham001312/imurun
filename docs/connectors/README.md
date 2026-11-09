# Connectors: linked accounts the model can use

IMURUN can act on eight external accounts — Gmail, GitHub, Slack, Vercel,
Render, Cloudflare, Supabase and Canva — through tools named
`<service>_<verb>` in the agentic tool loop (`docs/agentic/README.md`), and
through MCP (`MCP.md`).

| Service | Page | Tools | How the user connects |
|---|---|---|---|
| Gmail | [GMAIL.md](GMAIL.md) | 7 | **Connect** → Google sign-in (built-in ImuTechz app) |
| GitHub | [GITHUB.md](GITHUB.md) | 8 | **Connect** → GitHub sign-in; device code when there is no browser |
| Slack | [SLACK.md](SLACK.md) | 3 | **Connect** → Slack sign-in (see the HTTPS caveat) |
| Vercel | [VERCEL.md](VERCEL.md) | 4 | **Connect** → Vercel sign-in |
| Supabase | [SUPABASE.md](SUPABASE.md) | 4 | **Connect** → Supabase sign-in |
| Canva | [CANVA.md](CANVA.md) | 2 | **Connect** → Canva sign-in |
| Cloudflare | [CLOUDFLARE.md](CLOUDFLARE.md) | 7 | paste a scoped **API token** → Verify & save |
| Render | [RENDER.md](RENDER.md) | 5 | paste an **API key** → Verify & save |

Maintainers: the built-in OAuth apps are registered once per provider —
[REGISTER-APPS.md](REGISTER-APPS.md). Today only the Google app exists; for
the others Connect says "IMURUN has no built-in <Service> app yet" until it is
registered (or the user enters their own client under Advanced).

## OAuth services: Connect, sign in, done

1. The user presses **Connect**: the UI calls
   `POST /api/connectors/<id>/connect {"open": true}`. The server opens the
   provider's sign-in page in the **system browser** (Google refuses OAuth in
   embedded webviews) and returns `{url, state, opened}`.
2. The user signs in / "Continue with Google" and approves.
3. The provider redirects to `http://127.0.0.1:<port>/oauth/callback`. IMURUN
   exchanges the code (PKCE), stores the tokens, looks up the account name,
   publishes `connector {connector, connected: true, account, via: "oauth"}`
   (the UI flips to connected without a refresh) and shows "Connected as
   <account> — you can close this tab"; the tab closes itself after 2 s where
   the browser allows it.
4. `GET /api/connectors/<id>/test` does one small real read;
   `POST /api/connectors/<id>/disconnect` forgets the tokens (Google: revoked,
   Slack: `auth.revoke`).

**Which OAuth app is used** (`GET /api/connectors/<id>/config` →
`credential_source`): `user` (a client id entered under Settings →
Advanced; the secret goes to the secret store) → `env`
(`IMURUN_<SERVICE>_CLIENT_ID` / `_CLIENT_SECRET`, or the older
`IMURUN_OAUTH_<SERVICE>_…`) → `builtin` (`services/connectors/apps.py` +
`apps.local.json`) → `none`. The pair is always taken from one source.
Credentials resolve when Connect is pressed, not at start-up.

Built-in apps are installed-app clients (RFC 8252): their ids are public, and
the secrets Google (Desktop) and Canva require for such apps are not
confidential by design — PKCE, the loopback redirect and the one-time state
protect the flow.

**Port.** `start.py` listens on **8732** by default (`--port N` changes it).
Google and GitHub accept a loopback redirect on any port; Slack, Vercel,
Supabase and Canva match it exactly, so the built-in apps work on 8732 only
(use your own app under Advanced for another port).
`IMURUN_OAUTH_REDIRECT_BASE` / `IMURUN_OAUTH_<ID>_REDIRECT_URI` pin the
redirect; it must stay loopback.

**No browser?** GitHub's device flow: `POST /api/connectors/github/device`
shows a code to type at github.com/login/device from any device.

## Token services: Cloudflare and Render

`POST /api/connectors/<id>/token {"token": "..."}` verifies the token with the
provider first (Cloudflare `/user/tokens/verify` + accounts + its permission
list when readable; Render `/v1/owners`), stores it only when it is accepted,
and returns `{connected, account, scopes_or_permissions, expires}` — never the
token. `DELETE /api/connectors/<id>/token` forgets it. The config view shows
`auth_mode: "token"`, `has_token`, `account`, `scopes_or_permissions` and
`token_permissions` (the minimum permission per tool). Tools behave exactly
as with OAuth.

## Secret storage

Client secrets, tokens and API keys go to one store
(`services/connectors/tokens.py`, `SecretStore`: `get` / `set` / `delete` /
`backend_name()` / `encrypted` / `warning`), chosen at start-up:

| Order | Backend | `backend_name()` | When |
|---|---|---|---|
| 1 | `keyring` package | `keyring (<impl>)` | importable (not installed here) |
| 2 | macOS Keychain | `macOS Keychain` | macOS with `/usr/bin/security`; secrets written through `security -i` stdin, never on the command line |
| 3 | libsecret | `libsecret` | Linux with `secret-tool`; the secret goes through stdin |
| 4 | encrypted file | `Windows DPAPI file` | Windows: each value DPAPI-encrypted (this machine) |
|   |   | `AES-256-GCM file` | elsewhere, when `cryptography` is installed; random key in `connectors.key` (0600) |
| 5 | plain file | `plain file (not encrypted)` | none of the above: marked `"__encryption__": "none"` and **reported as `warning`** in every config view |

The file is `~/.imurun/secrets/connectors.json` (0600 in a 0700 folder),
format `{"__encryption__": "...", "<key>": "dpapi:…" | "aesgcm:…" | plain}`.
Values written by older versions in plain text are read and **re-encrypted on
the next write**. `IMURUN_TOKEN_STORE` forces `memory`, `file`, `keyring`,
`keychain` or `libsecret`. The config view reports `store`,
`store_encrypted` and `warning`.

Keys: `<id>` (access token / API token), `<id>:refresh`, `<id>:expires_at`,
`<id>:scope`, `<id>:account`, `<id>:permissions`, `<id>:client_secret` (a
user's own client). Client ids live in settings (`connectors.<id>.clientId`),
never secrets.

## Refresh

Access tokens are refreshed 60 s before `expires_at` and once more on a 401;
rotated refresh tokens are stored; a refused refresh forgets the account so
the tools say "not connected". API tokens are not refreshed.

## Permissions (decided in code, `tool_policy.py`)

| Tools | `ask` | `allow-reads` | `allow-all` |
|---|---|---|---|
| reads (search, list, read, logs, exports) | ask | allow | allow |
| `gmail_draft`, `gmail_modify` adding labels | ask | ask | allow |
| every other write (send, trash, label removal, issues/comments, Slack posts, deploys, restarts, DNS changes, cache purge, Supabase inserts) | ask | ask | **ask** — remembered grants ignored |

The `network` toggle off denies every connector tool; `permissions.sendEmail
= false` denies `gmail_send`. Unavailable tools are not offered to the model;
`GET /api/tools` shows why ("press Connect…", "paste a Cloudflare API
token… (this tool needs …)", "turned off", "not granted").

## Verification status (2026-11-01)

All offline, against fake HTTP layers (`tests/`). No real sign-in or token
was used from this machine.

| Service | Tested offline | Not verified against the real service |
|---|---|---|
| Gmail | flow, PKCE, refresh, built-in app, browser open, callback page + event, every tool, MCP | real Google sign-in (needs `apps.local.json` from the import command) |
| GitHub | flow, device flow, every tool | real sign-in (no app registered yet) |
| Slack | flow, every tool | whether Slack accepts an http loopback redirect |
| Vercel | every tool | integration OAuth endpoints |
| Supabase | every tool, key choice | real sign-in |
| Canva | flow incl. Basic auth, rotation, both tools | real sign-in |
| Cloudflare | token verify / refuse / disabled, permission lookup, every tool | a real token |
| Render | key verify / refuse, every tool | a real key |
| Secret store | DPAPI (real, this machine), migration, AES-GCM and Keychain / libsecret / keyring with fakes, the unencrypted warning | macOS and Linux on real machines |
