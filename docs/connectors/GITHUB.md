# GitHub connector

Code: `services/connectors/providers/github.py`, `services/connectors/device.py`
(device flow), tools in `services/tools/builtin/service_tools.py`. Shared
rules: [README.md](README.md).

## Connect (users)

Settings → Connectors → GitHub → **Connect** → sign in to GitHub →
**Authorize** → "Connected as <login>". Uses ImuTechz's built-in GitHub OAuth
App once it is registered ([REGISTER-APPS.md](REGISTER-APPS.md#github));
until then Connect says "IMURUN has no built-in GitHub app yet".

### No browser on this machine: device flow

Settings → Connectors → GitHub → **Connect without a browser**, or:

```powershell
Invoke-RestMethod "$b/api/connectors/github/device" -Method Post -Headers $h
# -> user_code "ABCD-1234", verification_uri https://github.com/login/device
# open that page on any device, type the code; then:
Invoke-RestMethod "$b/api/connectors/github/device" -Headers $h   # state: pending -> connected
```

IMURUN polls at GitHub's interval (+5 s on `slow_down`) and publishes a
`connector` event when it ends. The device code never leaves the server.

### Advanced: your own OAuth App

<https://github.com/settings/developers> → **New OAuth App** → callback
`http://127.0.0.1:8732/oauth/callback` (GitHub accepts a loopback redirect on
another port), tick **Enable Device Flow** → generate a secret →
`POST /api/connectors/github/config {"clientId": "...", "clientSecret": "..."}`.

## Scopes

`repo read:user`. OAuth Apps have no read-only repository scope: `repo` grants
read and write to private repositories (writes still always ask in IMURUN).
Public repositories only: `IMURUN_OAUTH_GITHUB_SCOPES="public_repo read:user"`.
OAuth App tokens do not expire; there is no refresh token.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `github_repos` | `limit?` | your repos, most recently updated first | read |
| `github_issues` | `repo`, `state?`, `limit?` | issues (PRs filtered out) | read |
| `github_issue` | `repo`, `number` | one issue + last 10 comments | read |
| `github_pulls` | `repo`, `state?`, `limit?` | pull requests with branches | read |
| `github_pull` | `repo`, `number` | PR description + changed files | read |
| `github_file` | `repo`, `path`, `ref?`, `max_chars?` | a text file (binary refused; folders listed) | read |
| `github_create_issue` | `repo`, `title`, `body?` | opens an issue | **always asks** |
| `github_comment` | `repo`, `number`, `body` | comments on an issue or PR | **always asks** |

`repo` must be `owner/name`; paths with `..` are refused.

## Verified

Offline (fake HTTP): every tool, the authorize URL, PKCE, the device flow
(pending, slow_down, success, denied). Not verified against github.com (no app
registered yet). GitHub added PKCE support for OAuth Apps in 2025; if an app
rejects it, `ServiceSpec.pkce` turns it off.
