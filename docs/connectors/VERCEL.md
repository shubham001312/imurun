# Vercel connector

Code: `services/connectors/providers/vercel.py`, tools in
`services/tools/builtin/service_tools.py`. Shared rules: [README.md](README.md).

## Connect (users)

Settings → Connectors → Vercel → **Connect** → choose the account or team →
**Add / Connect** → "Connected as <username>". Uses ImuTechz's Vercel
integration once registered ([REGISTER-APPS.md](REGISTER-APPS.md#vercel)).
IMURUN must run on port 8732 (exact redirect match).

### Advanced: your own integration

<https://vercel.com/dashboard/integrations/console> → Create → Redirect URL
`http://127.0.0.1:8732/oauth/callback` →
`POST /api/connectors/vercel/config {"clientId": "oac_...", "clientSecret": "..."}`.

Endpoints: authorize `https://vercel.com/oauth/authorize`, token
`https://api.vercel.com/v2/oauth/access_token`. **Not verified**: if the
integration requires the install to start at
`https://vercel.com/integrations/<slug>/new`, set
`IMURUN_OAUTH_VERCEL_AUTHORIZE_URL`. No scopes are sent (permissions are
chosen in the console). Team resources need `team_id` in the tool arguments.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `vercel_projects` | `limit?`, `team_id?` | projects with id and framework | read |
| `vercel_deployments` | `project?`, `limit?`, `team_id?` | deployments: id, state, target, URL | read |
| `vercel_logs` | `deployment`, `limit?`, `team_id?` | the build log (`/v3/deployments/{id}/events`); not runtime logs | read |
| `vercel_redeploy` | `deployment`, `name`, `target?` (preview), `team_id?` | redeploys an existing deployment | **always asks** (card names the target) |

## Verified

Offline: every tool. Not verified against vercel.com.
