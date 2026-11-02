# Cloudflare connector (API token)

Cloudflare's OAuth is partner-only, so Cloudflare connects with a **scoped
API token** the user creates and pastes into IMURUN. The token is verified
before it is saved, kept in the encrypted secret store, and never shown again.
Code: `services/connectors/providers/cloudflare.py`, tools in
`services/tools/builtin/service_tools.py`, routes in
`services/connectors/routes.py`.

## 1. Create the token

1. <https://dash.cloudflare.com/profile/api-tokens> → **Create Token** →
   **Create Custom Token**.
2. Name: `IMURUN`.
3. Permissions (the minimum for every tool):

   | Type | Permission | Access | Used by |
   |---|---|---|---|
   | Zone | Zone | **Read** | `cloudflare_zones` |
   | Zone | DNS | **Edit** (Read is enough without DNS writes) | `cloudflare_dns_records`, `cloudflare_dns_create`, `_update`, `_delete` |
   | Account | Workers Scripts | **Read** | `cloudflare_workers` |
   | Zone | Cache Purge | Purge (optional) | `cloudflare_purge_cache` |
   | User | API Tokens | Read (optional) | lets IMURUN list the token's own permissions after verifying |

4. **Zone Resources**: *Include → Specific zone → your zones* (or *All zones
   from an account*). **Account Resources**: your account.
5. Optional: Client IP filtering, TTL (an expiry date). **Continue to summary
   → Create Token** and copy it (Cloudflare shows it once).

## 2. Paste it in IMURUN

Settings → Connectors → Cloudflare → paste → **Verify & save** (UI pending,
REQUESTS.md). By hand:

```powershell
$body = @{ token = "<paste>" } | ConvertTo-Json
Invoke-RestMethod "$b/api/connectors/cloudflare/token" -Method Post -Headers $h -ContentType application/json -Body $body
# -> {connected: true, account: "Your Account", scopes_or_permissions: [...], expires: "2027-..."|null, token_id}
```

IMURUN calls `GET /client/v4/user/tokens/verify` (status must be `active`),
then `GET /accounts` (or `/user`) for the display name, and
`GET /user/tokens/{id}` for the permission list when the token may read it
(otherwise it says "unknown"). A refused token → `401`, and **nothing is
stored**. `GET /api/connectors/cloudflare/config` then shows
`auth_mode: "token"`, `has_token: true`, `account`, `scopes_or_permissions`,
the storage backend and `token_permissions` (what each tool needs).

## 3. Where it is stored

In the connector secret store (`services/connectors/tokens.py`), key
`cloudflare` (plus `cloudflare:account`, `cloudflare:permissions`,
`cloudflare:expires_at`):

| OS | Backend (first available) |
|---|---|
| any, `keyring` installed | the OS keychain through `keyring` |
| Windows | `~/.imurun/secrets/connectors.json`, each value encrypted with **DPAPI** (your Windows user) — what this machine uses |
| macOS | **Keychain** (`security`, service `imurun`; the token goes through stdin) |
| Linux | **libsecret** (`secret-tool`, GNOME Keyring / KWallet) |
| otherwise | the file with **AES-256-GCM** if `cryptography` is installed, else **not encrypted** — flagged as a warning in the config view |

## 4. Revoke

* In IMURUN: `DELETE /api/connectors/cloudflare/token` (or
  `POST /api/connectors/cloudflare/disconnect`) forgets it locally.
* At Cloudflare: **My Profile → API Tokens → … → Delete** (or *Roll*). Do this
  too if the token may have leaked; IMURUN cannot revoke it for you.

## Tools

| Tool | Arguments | Does | Permission in IMURUN | Token permission |
|---|---|---|---|---|
| `cloudflare_zones` | `name?`, `limit?` | zones with id, status, account id | read | Zone:Read |
| `cloudflare_dns_records` | `zone`, `type?`, `limit?` | DNS records | read | DNS:Read |
| `cloudflare_workers` | `account` | Workers scripts | read | Workers Scripts:Read |
| `cloudflare_dns_create` | `zone`, `type`, `name`, `content`, `ttl?`, `proxied?` | creates a record | **always asks** | DNS:Edit |
| `cloudflare_dns_update` | `zone`, `record_id`, fields | changes a record | **always asks** | DNS:Edit |
| `cloudflare_dns_delete` | `zone`, `record_id` | deletes a record | **always asks** | DNS:Edit |
| `cloudflare_purge_cache` | `zone` | purges the zone cache | **always asks** | Cache Purge:Purge |

Without a token the tools say "paste a Cloudflare API token in Settings →
Connectors → Cloudflare (this tool needs the token permission …)". A 403
from Cloudflare adds "The Cloudflare token needs the permission …".

## Verified

Offline (fake HTTP): verify (active / refused / disabled), account and
permission lookup, nothing stored on refusal, every tool, the 403 hint, the
routes. Not verified against the real Cloudflare API from here.
