"""Per-service OAuth set-up: the user's own OAuth app, scopes, flow and refresh.

Every connector is connected with the OAuth 2.0 authorization-code flow
(PKCE where the provider supports it) through ``OAuthFlow`` and the server's
``/oauth/callback``. The user registers their own OAuth app with the provider
(docs/connectors/<SERVICE>.md) and enters it in Settings:

* the client id -> settings section ``connectors`` -> ``<id>.clientId``;
* the client secret -> the connector token store, key ``<id>:client_secret``
  (keychain / DPAPI-protected file), never the settings file, never a log,
  never an API response. A ``clientSecret`` typed into the settings file by
  hand is still read (and dropped from the file the next time it is saved).

``IMURUN_OAUTH_<ID>_CLIENT_ID`` / ``_CLIENT_SECRET`` / ``_AUTHORIZE_URL`` /
``_TOKEN_URL`` / ``_SCOPES`` / ``_REDIRECT_URI`` override per service.

Scopes are the minimum the enabled tools need (``connectors.<id>.tools``).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .oauth import OAuthError, OAuthFlow, OAuthProvider, refresh_access_token


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    label: str
    authorize_url: str
    token_url: str
    tools: tuple[str, ...]
    # Scopes requested for each tool; base scopes are always requested.
    tool_scopes: dict = field(default_factory=dict)
    base_scopes: tuple[str, ...] = ()
    # Granted scopes that satisfy each tool (defaults to tool_scopes).
    satisfies: dict = field(default_factory=dict)
    scope_separator: str = " "
    authorize_params: tuple[tuple[str, str], ...] = ()
    token_params: tuple[tuple[str, str], ...] = ()
    pkce: bool = True
    client_auth: str = "body"
    device_code_url: str = ""
    device_token_url: str = ""
    test_tool: str = ""
    console_url: str = ""
    doc: str = ""
    # Extra settings keys: name -> (default, allowed values tuple or None)
    extra_settings: dict = field(default_factory=dict)
    # How the user signs in: "oauth" (authorization-code flow) and/or "token"
    # (an API token pasted in Settings, verified, kept in the secret store).
    auth: tuple[str, ...] = ("oauth",)
    # Token mode: the minimum token permission each tool needs (shown in
    # availability reasons and 403 errors).
    token_permissions: dict = field(default_factory=dict)
    token_console: str = ""


def _env(service_id: str, suffix: str) -> str:
    return os.getenv(f"IMURUN_OAUTH_{service_id.upper()}_{suffix}", "").strip()


class OAuthSetup:
    """Reads the client config on demand, so a Settings change applies at once."""

    def __init__(self, spec: ServiceSpec, *, settings: Any, tokens: Any, form_transport: Any,
                 redirect_uri: str, mark=None, clock=time.time) -> None:
        self.spec = spec
        self.settings = settings
        self.tokens = tokens
        self.form_transport = form_transport
        self.redirect_uri = _env(spec.id, "REDIRECT_URI") or redirect_uri
        self.mark = mark
        self.clock = clock
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def secret_key(self) -> str:
        return f"{self.spec.id}:client_secret"

    # ------------------------------------------------------------ config
    def section(self) -> dict:
        if self.settings is None:
            return {}
        try:
            return dict((self.settings.read("connectors") or {}).get(self.spec.id) or {})
        except Exception:  # noqa: BLE001
            return {}

    def tools(self) -> list[str]:
        raw = self.section().get("tools")
        if not isinstance(raw, list):
            return list(self.spec.tools)
        return [name for name in raw if name in self.spec.tools]

    def option(self, name: str):
        default, allowed = self.spec.extra_settings.get(name, (None, None))
        value = self.section().get(name, default)
        if allowed is not None and value not in allowed:
            return default
        return value

    def credentials(self) -> tuple[str, str, str]:
        """``(client_id, client_secret, source)``; source is user|env|builtin|none.

        The secret always comes from the same source as the id, so a user's
        own client never gets paired with the built-in secret.
        """
        from . import apps

        section = self.section()
        user_id = str(section.get("clientId") or "").strip()
        if user_id:
            try:
                stored = self.tokens.get(self.secret_key) or ""
            except Exception:  # noqa: BLE001
                stored = ""
            return user_id, stored or str(section.get("clientSecret") or "").strip(), "user"
        env_id, env_secret = apps.env_client(self.spec.id)
        if env_id:
            return env_id, env_secret, "env"
        if self.oauth_enabled:
            app = apps.builtin_app(self.spec.id)
            if app.get("client_id"):
                return app["client_id"], app.get("client_secret", ""), "builtin"
        return "", "", "none"

    def client_id(self) -> str:
        return self.credentials()[0]

    def client_secret(self) -> str:
        return self.credentials()[1]

    def credential_source(self) -> str:
        return self.credentials()[2]

    @property
    def oauth_enabled(self) -> bool:
        return "oauth" in self.spec.auth

    @property
    def token_enabled(self) -> bool:
        return "token" in self.spec.auth

    def auth_mode(self) -> str:
        if self.oauth_enabled and self.token_enabled:
            return "oauth+token"
        return "token" if self.token_enabled else "oauth"

    def configured(self) -> bool:
        # A token-mode service needs nothing but the token itself.
        return self.token_enabled or bool(self.client_id())

    def has_token(self) -> bool:
        try:
            return bool(self.tokens.get(self.spec.id))
        except Exception:  # noqa: BLE001
            return False

    def missing_reason(self, tool: str) -> str:
        """Why a not-connected tool cannot run, in the user's terms."""
        label = self.spec.label
        if self.token_enabled and not self.client_id():
            needs = self.spec.token_permissions.get(tool, "")
            return (f"paste a {label} API token in Settings -> Connectors -> {label}"
                    + (f" (this tool needs the token permission {needs})" if needs else ""))
        if not self.client_id():
            return (f"IMURUN has no built-in {label} app yet: add your own {label} OAuth client id in "
                    f"Settings -> Connectors -> {label} -> Advanced, then press Connect")
        return f"{label} is not connected: press Connect in Settings -> Connectors -> {label}"

    def save_token(self, connector, token: str) -> dict:
        """Verify ``token`` with the provider, then store it. Never returns it."""
        if not self.token_enabled:
            raise ValueError(f"{self.spec.label} signs in with OAuth, not a pasted token")
        token = str(token or "").strip()
        if not token or len(token) > 4096 or any(ch.isspace() or ord(ch) < 32 for ch in token):
            raise ValueError("the token must be one line without spaces")
        verify = getattr(connector, "verify_token", None)
        if not callable(verify):
            raise ValueError(f"{self.spec.label} cannot verify tokens")
        info = verify(token)                      # ConnectorError when refused
        connector.forget()
        self.tokens.set(self.spec.id, token)
        if info.get("account"):
            self.tokens.set(f"{self.spec.id}:account", str(info["account"])[:200])
        permissions = [str(p)[:120] for p in info.get("permissions") or []][:100]
        if permissions:
            self.tokens.set(f"{self.spec.id}:permissions", "\n".join(permissions))
        if info.get("expires_at"):
            self.tokens.set(f"{self.spec.id}:expires_at", str(info["expires_at"]))
        return {"connected": True, "account": info.get("account", ""),
                "scopes_or_permissions": permissions, "expires": info.get("expires"),
                "token_id": info.get("token_id", "")}

    def scopes(self) -> tuple[str, ...]:
        override = _env(self.spec.id, "SCOPES")
        if override:
            return tuple(part for part in override.replace(",", " ").split() if part)
        wanted = list(self.spec.base_scopes)
        for name in self.tools():
            for scope in self.spec.tool_scopes.get(name, ()):
                if scope not in wanted:
                    wanted.append(scope)
        return tuple(wanted)

    def tool_allowed(self, tool: str, granted) -> bool:
        """Do the granted scopes cover ``tool``? Unknown grants count as yes."""
        granted = tuple(granted or ())
        if not granted:
            return True
        needed = self.spec.satisfies.get(tool) or self.spec.tool_scopes.get(tool) or ()
        if not needed:
            return True
        return all(any(option in granted for option in str(group).split("|")) for group in needed)

    def provider(self) -> OAuthProvider | None:
        client_id = self.client_id()
        if not client_id:
            return None
        return OAuthProvider(
            id=self.spec.id,
            authorize_url=_env(self.spec.id, "AUTHORIZE_URL") or self.spec.authorize_url,
            token_url=_env(self.spec.id, "TOKEN_URL") or self.spec.token_url,
            scopes=self.scopes(),
            client_id=client_id,
            client_secret=self.client_secret(),
            scope_separator=self.spec.scope_separator,
            authorize_params=self.spec.authorize_params,
            token_params=self.spec.token_params,
            pkce=self.spec.pkce,
            client_auth=self.spec.client_auth,
        )

    def update(self, payload: dict) -> dict:
        """Apply ``{clientId?, clientSecret?, tools?, <extra settings>?}``."""
        changes: dict = {}
        if "clientId" in payload:
            client_id = str(payload.get("clientId") or "").strip()
            if client_id and (len(client_id) > 300 or any(ch.isspace() for ch in client_id)):
                raise ValueError(f"clientId does not look like a {self.spec.label} OAuth client id")
            changes["clientId"] = client_id
        if "tools" in payload:
            tools = payload.get("tools")
            if not isinstance(tools, list) or any(name not in self.spec.tools for name in tools):
                raise ValueError(f"tools must be a list drawn from {', '.join(self.spec.tools)}")
            changes["tools"] = list(dict.fromkeys(tools))
        for name, (default, allowed) in self.spec.extra_settings.items():
            if name not in payload:
                continue
            value = payload[name]
            if allowed is None and isinstance(default, int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{name} must be a number") from None
            elif allowed is not None and value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(map(str, allowed))}")
            changes[name] = value
        secret = None
        if "clientSecret" in payload:
            secret = str(payload.get("clientSecret") or "").strip()
            if secret and (len(secret) > 500 or any(ch.isspace() for ch in secret)):
                raise ValueError(f"clientSecret does not look like a {self.spec.label} client secret")
        if not changes and secret is None:
            raise ValueError("nothing to change")
        with self._lock:
            if changes and self.settings is not None:
                current = self.section()
                current.pop("clientSecret", None)   # never keep a secret in settings
                current.update(changes)
                self.settings.write("connectors", {self.spec.id: current})
            if secret is not None:
                if secret:
                    self.tokens.set(self.secret_key, secret)
                else:
                    try:
                        self.tokens.delete(self.secret_key)
                    except Exception:  # noqa: BLE001
                        pass
        return self.view()

    def view(self) -> dict:
        """Safe view: the client id is shown, the secret only as set / not set."""
        client_id, secret, source = self.credentials()
        view = {
            "id": self.spec.id,
            "label": self.spec.label,
            "clientId": client_id,
            "credential_source": source,
            # Kept for older clients: settings | environment | builtin | "".
            "clientIdSource": {"user": "settings", "env": "environment", "builtin": "builtin"}.get(source, ""),
            "clientSecretSet": bool(secret),
            "tools": self.tools(),
            "availableTools": list(self.spec.tools),
            "scopes": list(self.scopes()),
            "redirectUri": self.redirect_uri,
            "configured": self.configured(),
            "pkce": self.spec.pkce,
            "deviceFlow": bool(self.spec.device_code_url),
            "console": self.spec.console_url,
            "doc": self.spec.doc,
            "auth_mode": self.auth_mode(),
            "has_token": self.has_token(),
        }
        if self.oauth_enabled and source == "none":
            from . import apps

            view["registration_todo"] = apps.builtin_app(self.spec.id).get("registration_todo", "")
        if self.token_enabled:
            view["token_console"] = self.spec.token_console
            view["token_permissions"] = dict(self.spec.token_permissions)
            try:
                view["account"] = self.tokens.get(f"{self.spec.id}:account") or ""
                stored = self.tokens.get(f"{self.spec.id}:permissions") or ""
            except Exception:  # noqa: BLE001
                view["account"], stored = "", ""
            view["scopes_or_permissions"] = [p for p in stored.split("\n") if p]
        from .tokens import store_view

        store = store_view(self.tokens)
        view["store"] = store["backend"]
        view["store_encrypted"] = store["encrypted"]
        if store["warning"]:
            view["warning"] = store["warning"]
        for name in self.spec.extra_settings:
            view[name] = self.option(name)
        return view

    # ------------------------------------------------------------ flows
    def flow(self) -> OAuthFlow | None:
        provider = self.provider()
        if provider is None:
            return None
        return OAuthFlow(provider, self.form_transport, self.tokens, self.redirect_uri, mark=self.mark)

    def register(self, server: Any) -> bool:
        """Install (or remove) this service's flow on the server."""
        flow = self.flow() if self.oauth_enabled else None
        if flow is None:
            lock = getattr(server, "_oauth_lock", None)
            if lock is not None:
                with lock:
                    server._oauth_flows.pop(self.spec.id, None)
            return False
        server.add_oauth_flow(self.spec.id, flow)
        return True

    def refresh(self) -> str:
        provider = self.provider()
        if provider is None:
            raise OAuthError(f"{self.spec.label} has no OAuth client id configured; add it in Settings")
        return refresh_access_token(provider, self.form_transport, self.tokens, clock=self.clock, mark=self.mark)


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
G = "https://www.googleapis.com/auth/gmail."

SERVICES: dict[str, ServiceSpec] = {
    "gmail": ServiceSpec(
        id="gmail", label="Gmail",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        tools=("gmail_search", "gmail_read", "gmail_labels", "gmail_draft", "gmail_send",
               "gmail_modify", "gmail_trash"),
        base_scopes=(G + "readonly",),
        tool_scopes={"gmail_draft": (G + "compose",), "gmail_send": (G + "send",),
                     "gmail_modify": (G + "modify",), "gmail_trash": (G + "modify",)},
        authorize_params=(("access_type", "offline"), ("prompt", "consent"),
                          ("include_granted_scopes", "true")),
        test_tool="gmail_search", console_url="https://console.cloud.google.com/auth/clients",
        doc="docs/connectors/GMAIL.md",
        extra_settings={"maxResults": (10, None), "bodyChars": (4000, None)},
    ),
    "github": ServiceSpec(
        id="github", label="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        tools=("github_repos", "github_issues", "github_issue", "github_pulls", "github_pull",
               "github_file", "github_create_issue", "github_comment"),
        # OAuth apps have no read-only repo scope: "repo" covers read and write.
        base_scopes=("repo", "read:user"),
        satisfies={name: ("repo|public_repo",) for name in (
            "github_repos", "github_issues", "github_issue", "github_pulls", "github_pull",
            "github_file", "github_create_issue", "github_comment")},
        device_code_url="https://github.com/login/device/code",
        device_token_url="https://github.com/login/oauth/access_token",
        test_tool="github_repos", console_url="https://github.com/settings/developers",
        doc="docs/connectors/GITHUB.md",
    ),
    "slack": ServiceSpec(
        id="slack", label="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        tools=("slack_channels", "slack_history", "slack_post"),
        tool_scopes={"slack_channels": ("channels:read", "groups:read"),
                     "slack_history": ("channels:history", "groups:history", "users:read"),
                     "slack_post": ("chat:write",)},
        satisfies={"slack_channels": ("channels:read|groups:read",),
                   "slack_history": ("channels:history|groups:history",),
                   "slack_post": ("chat:write",)},
        scope_separator=",", test_tool="slack_channels",
        console_url="https://api.slack.com/apps", doc="docs/connectors/SLACK.md",
    ),
    "vercel": ServiceSpec(
        id="vercel", label="Vercel",
        authorize_url="https://vercel.com/oauth/authorize",
        token_url="https://api.vercel.com/v2/oauth/access_token",
        tools=("vercel_projects", "vercel_deployments", "vercel_logs", "vercel_redeploy"),
        test_tool="vercel_projects", console_url="https://vercel.com/dashboard/integrations/console",
        doc="docs/connectors/VERCEL.md",
    ),
    "render": ServiceSpec(
        id="render", label="Render",
        authorize_url="https://dashboard.render.com/oauth/authorize",
        token_url="https://api.render.com/oauth/token",
        tools=("render_services", "render_deploys", "render_logs", "render_deploy", "render_restart"),
        test_tool="render_services", console_url="https://dashboard.render.com/",
        doc="docs/connectors/RENDER.md",
        # Render publishes no third-party OAuth: an API key, verified and stored encrypted.
        auth=("token",),
        token_permissions={name: "a Render API key (keys are not scoped)" for name in (
            "render_services", "render_deploys", "render_logs", "render_deploy", "render_restart")},
        token_console="https://dashboard.render.com/u/settings#api-keys",
    ),
    "cloudflare": ServiceSpec(
        id="cloudflare", label="Cloudflare",
        authorize_url="https://dash.cloudflare.com/oauth2/auth",
        token_url="https://dash.cloudflare.com/oauth2/token",
        tools=("cloudflare_zones", "cloudflare_dns_records", "cloudflare_workers",
               "cloudflare_dns_create", "cloudflare_dns_update", "cloudflare_dns_delete",
               "cloudflare_purge_cache"),
        test_tool="cloudflare_zones", console_url="https://dash.cloudflare.com/",
        doc="docs/connectors/CLOUDFLARE.md",
        # Cloudflare OAuth is partner-only: a scoped API token instead.
        auth=("token",),
        token_permissions={
            "cloudflare_zones": "Zone:Read",
            "cloudflare_dns_records": "DNS:Read (or DNS:Edit)",
            "cloudflare_workers": "Account / Workers Scripts:Read",
            "cloudflare_dns_create": "DNS:Edit",
            "cloudflare_dns_update": "DNS:Edit",
            "cloudflare_dns_delete": "DNS:Edit",
            "cloudflare_purge_cache": "Cache Purge:Purge",
        },
        token_console="https://dash.cloudflare.com/profile/api-tokens",
    ),
    "supabase": ServiceSpec(
        id="supabase", label="Supabase",
        authorize_url="https://api.supabase.com/v1/oauth/authorize",
        token_url="https://api.supabase.com/v1/oauth/token",
        tools=("supabase_projects", "supabase_tables", "supabase_select", "supabase_insert"),
        test_tool="supabase_projects",
        console_url="https://supabase.com/dashboard/org/_/apps", doc="docs/connectors/SUPABASE.md",
        extra_settings={"keyRole": ("anon", ("anon", "service_role"))},
    ),
    "canva": ServiceSpec(
        id="canva", label="Canva",
        authorize_url="https://www.canva.com/api/oauth/authorize",
        token_url="https://api.canva.com/rest/v1/oauth/token",
        tools=("canva_designs", "canva_export"),
        tool_scopes={"canva_designs": ("design:meta:read",),
                     "canva_export": ("design:meta:read", "design:content:read")},
        client_auth="basic", test_tool="canva_designs",
        console_url="https://www.canva.com/developers/integrations/connect-api",
        doc="docs/connectors/CANVA.md",
    ),
}
