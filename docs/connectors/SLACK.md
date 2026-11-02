# Slack connector

Code: `services/connectors/providers/slack.py`, tools in
`services/tools/builtin/service_tools.py`. Shared rules: [README.md](README.md).

## Connect (users)

Settings → Connectors → Slack → **Connect** → pick the workspace → **Allow** →
"Connected as <user> @ <team>". Then invite the app to the channels it should
read (`/invite @IMURUN`). Uses ImuTechz's built-in Slack app once registered
([REGISTER-APPS.md](REGISTER-APPS.md#slack)). IMURUN must run on port 8732:
Slack matches the redirect exactly.

> **Caveat, not verified:** Slack documents HTTPS-only redirect URLs. If the
> app settings refuse `http://127.0.0.1:8732/oauth/callback`, the browser flow
> cannot complete until IMURUN has an HTTPS loopback callback (REQUESTS.md S2).

### Advanced: your own Slack app

<https://api.slack.com/apps> → Create New App → Redirect URL
`http://127.0.0.1:8732/oauth/callback` → bot scopes below →
`POST /api/connectors/slack/config {"clientId": "...", "clientSecret": "..."}`.

## Scopes (bot token)

Only those the enabled tools need: `channels:read`, `groups:read`
(`slack_channels`); `channels:history`, `groups:history`, `users:read`
(`slack_history`); `chat:write` (`slack_post`). If token rotation is on, the
refresh token is stored and refreshed like every other service.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `slack_channels` | `limit?` | channels the app sees, with "member" flag | read |
| `slack_history` | `channel` (id), `limit?` | latest messages | read |
| `slack_post` | `channel`, `text`, `thread_ts?` | posts (in a thread when `thread_ts`) | **always asks** |

Slack's HTTP-200 `{"ok": false, "error": ...}` answers become the tool's error
(`not_in_channel`, `missing_scope`…). Disconnect calls `auth.revoke`.

## Verified

Offline: every tool, comma-joined scopes, `ok:false` errors. Not verified
against slack.com.
