# Render connector (API key)

Render has no third-party OAuth, so it connects with an **API key** the user
creates and pastes into IMURUN. The key is verified before it is saved, kept
in the encrypted secret store, and never shown again. Code:
`services/connectors/providers/render.py`, tools in
`services/tools/builtin/service_tools.py`.

## 1. Create the key

<https://dashboard.render.com/u/settings#api-keys> (Account Settings → **API
Keys**) → **Create API Key** → name `IMURUN` → copy it (shown once).

Render API keys are **not scoped**: a key can do everything your account can
in every workspace you belong to. IMURUN's own rules still apply (deploy and
restart always ask), but treat the key like a password.

## 2. Paste it in IMURUN

Settings → Connectors → Render → paste → **Verify & save** (UI pending). By hand:

```powershell
$body = @{ token = "rnd_..." } | ConvertTo-Json
Invoke-RestMethod "$b/api/connectors/render/token" -Method Post -Headers $h -ContentType application/json -Body $body
# -> {connected: true, account: "<workspace names>", scopes_or_permissions: ["full access ..."], expires: null}
```

IMURUN calls `GET /v1/owners` with the key; a refused key → `401` and nothing
is stored.

## 3. Storage and revoking

Stored like every connector secret (key `render`; backend table in
[CLOUDFLARE.md](CLOUDFLARE.md#3-where-it-is-stored) and README.md).
Forget it with `DELETE /api/connectors/render/token`; revoke it at Render
(Account Settings → API Keys → delete) — IMURUN cannot revoke it for you.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `render_services` | `limit?` | services with id, type, state | read |
| `render_deploys` | `service`, `limit?` | recent deploys with commit message | read |
| `render_logs` | `service`, `limit?` | latest log lines (`/v1/logs`, owner looked up from the service) | read |
| `render_deploy` | `service`, `clear_cache?` | starts a deploy | **always asks** |
| `render_restart` | `service` | restarts the service | **always asks** |

## Verified

Offline: verify (accepted / refused), every tool, the routes. Not verified
against the real Render API from here.
