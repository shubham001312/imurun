# Connect and persona

## `/persona` popup

Opened by the user only. Fields:

| Field | Required | Meaning |
|---|---|---|
| Imurun Name | yes | the name used in email signatures |
| Imurun Role in email | yes | the role line under the name |
| Description | no | custom behaviour the user wants |

Stored at `~/.imurun/persona.json` (`0600`, folder `0700`).

**The model never reads this file.** Two controlled bridges exist:

- `PersonaTool.read()` - the only way the model may read persona data. It
  returns a small structured view (`configured`, name, role, description)
  and never raises: a missing or corrupted file becomes `configured=False`
  with a reason, so a failure costs no extra turn, compute or latency. The
  view never carries the path;
- `PersonaComposer` - `directive()` builds a short style instruction (name,
  role, behaviour, honesty line) and `apply_signature()` appends the name and
  role by code, not by asking the model to reproduce them.

## `/connect`

One adapter per provider with a common shape (`connect`, `status`, `read`,
`write`, `report`). Each capability is classified:

| Kind | Rule |
|---|---|
| read | allowed without approval |
| write | ask; runs only with per-action approval |
| admin | ask; runs only with per-action approval |

Adapters: GitHub, Gmail, Slack, Vercel, Render, Cloudflare, Supabase, Canva.
OAuth start flows and the `/connect` popup come with the server/UI.

### Safety defaults (approved)

- Every outbound action is ask by default (V4).
- Email is draft-first: read -> draft in the user's voice -> a popup asks
  **Send / Cancel** -> only Send sends. Nothing sends without that decision,
  and a Cancel is audited as `refused`, not silently dropped.
- If someone asks directly whether they are talking to an assistant, the
  assistant says so honestly. It never claims to be a human.
- OAuth tokens live in the OS keychain (`KeyringTokenStore`); no plain-file
  fallback; tokens never appear in logs (`redact`).
- Every action is audited as `G3-connector` with `provider:action` and
  `ok` / `refused` / `failed` - never the token or the payload.
- The kill switch (G6) stops connectors with everything else.

### Backend shape (contract for the frontends)

```text
GET  /api/connectors                 -> [{id,label,connected,capabilities[]}]
POST /api/connectors/{id}/connect    -> OAuth start (user gesture only)
POST /api/connectors/{id}/disconnect
POST /api/connectors/{id}/actions/{action}
     body: {params, approved:false}  -> ask / deny / result
     write/admin without approved -> ask (the popup), never a run
     gmail send -> popup Send / Cancel -> approved true/false
GET  /api/persona                    -> {name,email_role,description}
PUT  /api/persona                    -> save (user gesture only)
```

The model can request an action; only the gate decides, and a write/admin
request without approval returns `ask` to the user - never a silent run.
