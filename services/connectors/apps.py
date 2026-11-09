"""ImuTechz's built-in OAuth apps, so the user only clicks Connect.

Resolution order for a service's OAuth client (``OAuthSetup.credentials``):

1. **user** - a client id the user entered in Settings -> Connectors ->
   Advanced (secret in the connector secret store);
2. **env**  - ``IMURUN_<SERVICE>_CLIENT_ID`` / ``_CLIENT_SECRET`` (also the
   older ``IMURUN_OAUTH_<SERVICE>_CLIENT_ID`` / ``_CLIENT_SECRET``);
3. **builtin** - this module: ``BUILTIN`` merged with ``apps.local.json``
   next to it (gitignored via ``*.local.json``; the release build packages
   it). Maintainers fill it with
   ``python -m services.connectors.apps import-google <client_secret_*.json>``
   or by editing it per ``docs/connectors/REGISTER-APPS.md``.

These are *installed-app* clients (RFC 8252 section 8.5): a client secret
shipped inside a desktop app is not confidential by design. Google (Desktop
type) and Canva (Basic auth) still require one; PKCE, the loopback
redirect and the one-time ``state`` are what protect the flow. Never put a
secret of a confidential (server-side) client here.

The secret is never printed by the command and never returned by any route.
"""
from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

# Next to this module in a checkout; in the data root (~/.imurun) for a pip
# install, where site-packages is never written to (CODE-REQUESTS 9).
from ..paths import local_file as _local_file

LOCAL_FILE = _local_file("apps.local.json", Path(__file__).parent)
REDIRECT = "http://127.0.0.1:8732/oauth/callback"

BUILTIN: dict[str, dict] = {
    "gmail": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": "Google Desktop client: any http://127.0.0.1:<port>/... loopback redirect",
        "registration_todo": "run: python -m services.connectors.apps import-google <client_secret_*.json>",
    },
    "github": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": f"callback {REDIRECT}; loopback redirects may use another port",
        "registration_todo": "register a GitHub OAuth App (docs/connectors/REGISTER-APPS.md#github)",
    },
    "slack": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": f"exact match: {REDIRECT} (Slack documents HTTPS-only redirects)",
        "registration_todo": "register a Slack app (docs/connectors/REGISTER-APPS.md#slack)",
    },
    "vercel": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": f"exact match: {REDIRECT}",
        "registration_todo": "create a Vercel integration (docs/connectors/REGISTER-APPS.md#vercel)",
    },
    "supabase": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": f"exact match: {REDIRECT}",
        "registration_todo": "create a Supabase OAuth app (docs/connectors/REGISTER-APPS.md#supabase)",
    },
    "canva": {
        "client_id": "", "client_secret": "", "registered_by": "ImuTechz",
        "redirect_rules": f"exact match: {REDIRECT} (127.0.0.1, not localhost)",
        "registration_todo": "create a Canva Connect integration (docs/connectors/REGISTER-APPS.md#canva)",
    },
}
FIELDS = ("client_id", "client_secret", "project_id", "registered_by", "redirect_rules", "registration_todo")


def _load_local(path: Path = LOCAL_FILE) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def builtin_app(service: str, *, local_path: Path | None = None) -> dict:
    """The built-in entry for ``service`` (``{}`` when none is known)."""
    entry = deepcopy(BUILTIN.get(service, {}))
    local = _load_local(local_path or LOCAL_FILE).get(service)
    if isinstance(local, dict):
        entry.update({k: str(v) for k, v in local.items() if k in FIELDS and v is not None})
    return entry


def env_client(service: str) -> tuple[str, str]:
    key = service.upper()
    client_id = (os.getenv(f"IMURUN_{key}_CLIENT_ID") or os.getenv(f"IMURUN_OAUTH_{key}_CLIENT_ID") or "").strip()
    secret = (os.getenv(f"IMURUN_{key}_CLIENT_SECRET") or os.getenv(f"IMURUN_OAUTH_{key}_CLIENT_SECRET") or "").strip()
    return client_id, secret


# ------------------------------------------------------------ Google client file
MAX_CLIENT_FILE = 64 * 1024


def parse_google_client(path: str) -> dict:
    """Parse a Google ``client_secret_*.json`` ("installed" or "web").

    Returns ``{client_id, client_secret, project_id, redirect_uris, type}``.
    Errors never quote the file's content.
    """
    raw_path = str(path or "").strip().strip('"')
    target = Path(raw_path)
    if not raw_path or not target.is_absolute():
        raise ValueError("path must be an absolute path to the downloaded client JSON")
    if target.suffix.lower() != ".json":
        raise ValueError("the file must be a .json file")
    if not target.is_file():
        raise ValueError("no such file")
    try:
        if target.stat().st_size > MAX_CLIENT_FILE:
            raise ValueError("the file is too large to be a Google OAuth client file")
        text = target.read_text(encoding="utf-8")
    except OSError:
        raise ValueError("the file could not be read") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("the file is not valid JSON") from None
    kind = next((k for k in ("installed", "web") if isinstance((data or {}).get(k), dict)), "")
    if not kind:
        raise ValueError('not a Google OAuth client file (no "installed" or "web" section)')
    block = data[kind]
    client_id = str(block.get("client_id") or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("the file has no Google OAuth client_id")
    return {
        "client_id": client_id,
        "client_secret": str(block.get("client_secret") or "").strip(),
        "project_id": str(block.get("project_id") or ""),
        "redirect_uris": [str(u) for u in block.get("redirect_uris") or [] if isinstance(u, str)][:20],
        "type": kind,
    }


def write_local(service: str, values: dict, *, local_path: Path | None = None) -> Path:
    path = local_path or LOCAL_FILE
    data = _load_local(path)
    entry = dict(data.get(service) or {})
    entry.update({k: v for k, v in values.items() if k in FIELDS})
    entry["registered_by"] = "ImuTechz"
    data[service] = entry
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return path


def main(argv: list[str] | None = None, *, local_path: Path | None = None, out=None) -> int:
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "import-google":
        try:
            client = parse_google_client(args[1])
        except ValueError as exc:
            print(f"import-google: {exc}", file=sys.stderr)
            return 2
        if client["type"] != "installed":
            print("import-google: warning: this is a Web client; a Desktop app client is recommended "
                  "(any loopback port works only for Desktop clients)", file=sys.stderr)
        path = write_local("gmail", {k: client[k] for k in ("client_id", "client_secret", "project_id")},
                           local_path=local_path)
        # The secret is never printed.
        print(f"built-in Gmail app set: client_id={client['client_id']} project_id={client['project_id']} "
              f"secret={'yes' if client['client_secret'] else 'no'} -> {path}", file=out)
        return 0
    if args == ["list"]:
        for service in BUILTIN:
            entry = builtin_app(service, local_path=local_path)
            state = entry.get("client_id") or f"(none: {entry.get('registration_todo', '')})"
            print(f"{service:9} {state}", file=out)
        return 0
    print("usage: python -m services.connectors.apps import-google <client_secret_*.json> | list", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
