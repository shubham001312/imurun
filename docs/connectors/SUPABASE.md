# Supabase connector

Code: `services/connectors/providers/supabase.py`, tools in
`services/tools/builtin/service_tools.py`. Shared rules: [README.md](README.md).

## Connect (users)

Settings → Connectors → Supabase → **Connect** → sign in → choose the
organisation → **Authorize** → "Connected as <organisation>". Uses ImuTechz's
Supabase OAuth app once registered
([REGISTER-APPS.md](REGISTER-APPS.md#supabase)). IMURUN must run on port 8732
(exact redirect match).

### Advanced: your own OAuth app

Supabase dashboard → organisation → **OAuth Apps** → Add application →
callback `http://127.0.0.1:8732/oauth/callback`, permissions below →
`POST /api/connectors/supabase/config {"clientId": "...", "clientSecret": "..."}`.

Endpoints: `https://api.supabase.com/v1/oauth/authorize`,
`https://api.supabase.com/v1/oauth/token` (PKCE). Scopes are fixed in the app
registration (IMURUN sends none); the tools need projects read,
organisations read and the project's API keys read. Access tokens expire and
are refreshed.

## How table access works

The OAuth token is a **Management API** token. For rows, IMURUN fetches the
project's API keys (`GET /v1/projects/{ref}/api-keys`) and calls
`https://<ref>.supabase.co/rest/v1/...` with:

* the **`anon` key by default** → Row Level Security applies;
* `connectors.supabase.keyRole = "service_role"` → bypasses RLS (full access).

Keys are cached in memory for 10 minutes, never stored or logged.

## Tools

| Tool | Arguments | Does | Permission |
|---|---|---|---|
| `supabase_projects` | – | projects with ref, region, status | read |
| `supabase_tables` | `project` | tables exposed by PostgREST | read |
| `supabase_select` | `project`, `table`, `columns?`, `filters?` (`["status=eq.open"]`), `limit?` ≤ 100 | rows as JSON | read |
| `supabase_insert` | `project`, `table`, `rows` (objects) ≤ 100 | inserts, returns the rows | **always asks** |

Table and column names must be plain identifiers; filter operators are
`eq neq gt gte lt lte like ilike is`.

## Verified

Offline: every tool, key choice, identifier and filter validation. Not
verified against supabase.com.
