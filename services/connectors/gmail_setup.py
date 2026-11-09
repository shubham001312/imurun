"""Gmail OAuth set-up: ``OAuthSetup`` with the Gmail catalogue entry.

Kept as its own name because the Gmail MCP server and older call sites
import it. Everything generic (client id in settings, secret in the token
store, minimum scopes, flow registration, refresh) lives in ``setup.py``;
see docs/connectors/GMAIL.md for the Google Cloud steps.
"""
from __future__ import annotations

from .setup import SERVICES, OAuthSetup

SPEC = SERVICES["gmail"]
SECRET_KEY = "gmail:client_secret"
ALL_TOOLS = SPEC.tools
AUTHORIZE_URL = SPEC.authorize_url
TOKEN_URL = SPEC.token_url


def scopes_for(tools) -> tuple[str, ...]:
    """Minimum Gmail scopes for the enabled tools (read is always included)."""
    wanted = list(SPEC.base_scopes)
    for name in tools or ():
        for scope in SPEC.tool_scopes.get(str(name), ()):
            if scope not in wanted:
                wanted.append(scope)
    return tuple(wanted)


MAX_CLIENT_FILE = 64 * 1024


class GmailSetup(OAuthSetup):
    def __init__(self, **kwargs) -> None:
        super().__init__(SPEC, **kwargs)

    def import_client(self, path: str) -> dict:
        """Read a Google ``client_secret_*.json`` ("installed" or "web") locally.

        The client id goes to settings, the secret to the token store. Returns
        only ``{client_id, project_id, redirect_uris, type, warnings}``; the
        secret is never returned, and error messages never quote the file.
        """
        from .apps import parse_google_client

        client = parse_google_client(path)
        client_id, secret, kind = client["client_id"], client["client_secret"], client["type"]
        redirect_uris = client["redirect_uris"]
        payload = {"clientId": client_id}
        if secret:
            payload["clientSecret"] = secret
        self.update(payload)
        warnings = []
        if kind == "web" and self.redirect_uri not in redirect_uris:
            warnings.append(f"this is a Web client: add {self.redirect_uri} to its Authorized redirect URIs "
                            "in Google Cloud (Web clients need the exact port), or create a Desktop client")
        if not secret:
            warnings.append("the file has no client_secret; Google's token endpoint usually requires it")
        return {"client_id": client_id, "project_id": client["project_id"],
                "redirect_uris": redirect_uris, "type": kind, "warnings": warnings}
