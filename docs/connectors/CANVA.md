# Canva connector

Code: `services/connectors/providers/canva.py`, tools in
`services/tools/builtin/service_tools.py`. Shared rules: [README.md](README.md).

## Connect (users)

Settings → Connectors → Canva → **Connect** → sign in → **Allow** → "Canva is
connected". Uses ImuTechz's Canva Connect integration once registered
([REGISTER-APPS.md](REGISTER-APPS.md#canva)). IMURUN must run on port 8732:
Canva matches the redirect exactly and wants `127.0.0.1`, not `localhost`.

### Advanced: your own integration

<https://www.canva.com/developers/integrations/connect-api> → Create an
integration → scopes `design:meta:read`, `design:content:read` → redirect
`http://127.0.0.1:8732/oauth/callback` → generate secret →
`POST /api/connectors/canva/config {"clientId": "OC-...", "clientSecret": "..."}`.

Endpoints: `https://www.canva.com/api/oauth/authorize`,
`https://api.canva.com/rest/v1/oauth/token`. Canva requires PKCE (sent) and
authenticates the client with **HTTP Basic** (`client_auth="basic"`): the
secret goes in the `Authorization` header, not the form. For the built-in app
that secret is shipped with IMURUN and is not confidential by design (RFC
8252); PKCE protects the flow. Access tokens last about 4 hours; refresh
tokens are single-use and rotated — IMURUN stores the new one each time.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `canva_designs` | `query?`, `limit?` | designs with id and edit link | read |
| `canva_export` | `design_id`, `format?` (pdf, png, jpg, gif, pptx, mp4) | starts an export, waits up to 20 s, returns download links (they expire) | read — an export changes nothing |

## Verified

Offline: authorize URL, Basic client auth, refresh with rotation, refused
refresh, both tools. Not verified against canva.com.
