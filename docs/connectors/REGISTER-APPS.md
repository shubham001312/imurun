# Registering ImuTechz's built-in OAuth apps (maintainers)

Users only press **Connect** and sign in. That works because the release
ships **ImuTechz's own OAuth app** for each OAuth service in
`services/connectors/apps.local.json` (gitignored through `*.local.json`;
the release build packages it next to `apps.py`). This page is the one-sitting
checklist to register them. Cloudflare and Render need no app: they use the
token path (`CLOUDFLARE.md`, `RENDER.md`).

Resolution order at runtime (`GET /api/connectors/{id}/config` →
`credential_source`): **user** (Settings → Advanced) → **env**
(`IMURUN_<SERVICE>_CLIENT_ID` / `_CLIENT_SECRET`) → **builtin** (this file) →
`none`.

## Before you start

* These are **installed-app (public) clients** (RFC 8252 §8.5). Any secret a
  provider makes you ship inside a desktop app — Google's Desktop client
  secret, Canva's client secret — is **not confidential by design**: anyone
  with the app can read it. The flow is protected by PKCE (S256), the
  loopback redirect and the one-time `state`, all enforced by `OAuthFlow`.
  Never put a confidential server-side client here.
* Redirect URI to register everywhere: **`http://127.0.0.1:8732/oauth/callback`**.
  IMURUN runs on 8732 by default (`start.py` `DEFAULT_PORT`). Google and
  GitHub accept a loopback redirect on another port; Slack, Vercel, Supabase
  and Canva match the redirect **exactly**, so users who run `--port N` with
  a different port must use their own app (Advanced) or go back to 8732.
  Register `http://localhost:8732/oauth/callback` as a second URI where the
  console allows it — some browsers rewrite `127.0.0.1` links (harmless if
  unused).
* `apps.local.json` shape (one entry per service; omit what you don't have):

```json
{
  "gmail":    {"client_id": "...apps.googleusercontent.com", "client_secret": "GOCSPX-...", "project_id": "..."},
  "github":   {"client_id": "Ov23li...", "client_secret": "..."},
  "slack":    {"client_id": "123.456", "client_secret": "..."},
  "vercel":   {"client_id": "oac_...", "client_secret": "..."},
  "supabase": {"client_id": "...", "client_secret": "..."},
  "canva":    {"client_id": "OC-...", "client_secret": "..."}
}
```

* Check: `python -m services.connectors.apps list` (prints ids, never secrets).

---

## Gmail (Google) — already have a client

The owner's Desktop client JSON exists. One command, run by a maintainer,
writes it into `apps.local.json` without printing the secret:

```
python -m services.connectors.apps import-google "C:\path\to\client_secret_<id>.apps.googleusercontent.com.json"
```

Keep the JSON out of the repository (the `*secret*` gitignore rule also
catches it). For a new client: Google Cloud console → **Google Auth Platform →
Clients → Create client → Desktop app**; enable the **Gmail API**; Audience
**External**; scopes `gmail.readonly`, `gmail.compose`, `gmail.send`,
`gmail.modify` under **Data Access**. Desktop clients need no redirect URI
(any loopback port works). **Before release:** the app is in *Testing* (only
listed test users, refresh tokens expire after 7 days); publishing it to
users requires **Google's verification of the restricted Gmail scopes**
(security assessment) — plan for it.

- [ ] Gmail API enabled
- [ ] Consent screen: name "IMURUN", support email, logo, privacy policy URL, ImuTechz domain
- [ ] Desktop client created, JSON imported with the command above
- [ ] Verification submitted (restricted scopes) before public release

## GitHub

Console: <https://github.com/organizations/ImuTechz/settings/applications>
(org-owned) → **New OAuth App**.

- [ ] Application name **IMURUN**, Homepage `https://imutechz.com` (or the product page)
- [ ] Authorization callback URL **`http://127.0.0.1:8732/oauth/callback`**
- [ ] **Enable Device Flow** ✔ (the headless fallback)
- [ ] Register → **Generate a new client secret**
- [ ] Paste `client_id` (`Ov23li…` / `Iv1.…`) and `client_secret` into `apps.local.json` → `github`

Scopes are requested at sign-in (`repo read:user`); nothing to set in the console.

## Slack

Console: <https://api.slack.com/apps> → **Create New App → From scratch** →
pick the ImuTechz workspace (it becomes the app's home; other workspaces
install it through OAuth once **public distribution** is on).

- [ ] **OAuth & Permissions → Redirect URLs**: `http://127.0.0.1:8732/oauth/callback`.
      ⚠ Slack documents HTTPS-only redirect URLs. If it refuses this URL, stop:
      Slack needs the HTTPS loopback callback (REQUESTS.md S2) before it can ship.
- [ ] **Bot Token Scopes**: `channels:read`, `groups:read`, `channels:history`, `groups:history`, `users:read`, `chat:write`
- [ ] **Manage Distribution → Activate Public Distribution** (needed for other workspaces)
- [ ] **Basic Information → App Credentials**: paste Client ID and Client Secret into `apps.local.json` → `slack`

## Vercel

Console: <https://vercel.com/dashboard/integrations/console> → **Create**.

- [ ] Name **IMURUN**, URL slug, logo, short description
- [ ] **Redirect URL**: `http://127.0.0.1:8732/oauth/callback`
- [ ] API scopes: Projects **read**, Deployments **read/write** (redeploy), Logs/Events **read**
- [ ] Visibility: public (so any user can install it)
- [ ] Paste the integration's **Client ID** (`oac_…`) and **Client Secret** into `apps.local.json` → `vercel`
- [ ] Try one sign-in: if Vercel requires the install to start at
      `https://vercel.com/integrations/<slug>/new`, set that as the authorize URL
      in `setup.py` (`SERVICES["vercel"].authorize_url`) — unverified today.

## Supabase

Console: <https://supabase.com/dashboard/org/_/apps> (ImuTechz org) → **Add application**.

- [ ] Name **IMURUN**, website URL
- [ ] **Authorization callback URL**: `http://127.0.0.1:8732/oauth/callback`
- [ ] Permissions: **Projects: read**, **Organizations: read**, **Secrets / API keys: read**
      (the tools fetch the project's anon key to read tables through PostgREST)
- [ ] Paste Client ID and Client Secret into `apps.local.json` → `supabase`

## Canva

Console: <https://www.canva.com/developers/integrations/connect-api> → **Create an integration** → **Public**.

- [ ] Name **IMURUN**, description, icon
- [ ] **Scopes**: `design:meta:read`, `design:content:read`
- [ ] **Authentication → Authorized redirects → URL 1**: `http://127.0.0.1:8732/oauth/callback`
      (Canva wants `127.0.0.1`, not `localhost`)
- [ ] **Generate secret**; paste Client ID (`OC-…`) and the secret into `apps.local.json` → `canva`
      (Canva authenticates the client with HTTP Basic; the shipped secret is
      public by design, PKCE protects the flow)
- [ ] Submit for **Canva review** before public release (public integrations are reviewed)

## After registering

1. `python -m services.connectors.apps list` shows every id.
2. Start `python start.py --port 8732`, then for each service
   `POST /api/connectors/<id>/connect {"open": true}` → sign in → the tab says
   "Connected as …"; `GET /api/connectors/<id>/test`.
3. Ship `services/connectors/apps.local.json` with the release build.
