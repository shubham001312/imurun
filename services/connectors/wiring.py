"""Wire the eight connectors into the running server.

Every provider is registered even when OAuth is not configured yet, so the
shell can show the card and explain what is missing. A provider gets an OAuth
flow once its client id is configured - in Settings
(``POST /api/connectors/<id>/config``, see ``setup.py``) or with
``IMURUN_OAUTH_<ID>_CLIENT_ID``. ``wire_server`` also registers the
``<service>_<verb>`` model tools, the permission wrapper and the MCP manager.

Environment (one block per provider):

    IMURUN_OAUTH_GITHUB_CLIENT_ID
    IMURUN_OAUTH_GITHUB_CLIENT_SECRET          (optional for public clients)
    IMURUN_OAUTH_GITHUB_AUTHORIZE_URL          (override)
    IMURUN_OAUTH_GITHUB_TOKEN_URL              (override)
    IMURUN_OAUTH_GITHUB_SCOPES                 (space or comma separated)

``IMURUN_OAUTH_REDIRECT_BASE`` overrides the loopback callback base.
``IMURUN_TOKEN_STORE`` is ``keyring`` (default), ``file`` or ``memory``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .base import Connector
from .oauth import OAuthFlow, OAuthProvider
from .providers import (
    CanvaConnector,
    CloudflareConnector,
    GitHubConnector,
    GmailConnector,
    RenderConnector,
    SlackConnector,
    SupabaseConnector,
    VercelConnector,
)
from .registry import ConnectorRegistry
from .tokens import (  # noqa: F401 - re-exported for older imports
    FileTokenStore,
    KeyringTokenStore,
    MemoryTokenStore,
    TokenStoreUnavailable,
    build_secret_store,
    dpapi_available,
)
from .transport import UrllibTransport


def build_token_store(home: Path | str):
    """The connector secret store (see ``tokens.build_secret_store``)."""
    return build_secret_store(home)



# --------------------------------------------------------------------------- #
# Provider catalogue
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProviderSpec:
    id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    scope_separator: str = " "
    authorize_params: tuple[tuple[str, str], ...] = ()
    token_params: tuple[tuple[str, str], ...] = ()
    connector: type[Connector] = field(default=None)  # type: ignore[assignment]


DEFAULT_PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="gmail",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        # The full set the seven Gmail tools can use. The flow actually
        # registered for Gmail is built by gmail_setup.GmailSetup with only
        # the scopes the enabled tools need.
        scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ),
        authorize_params=(
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("include_granted_scopes", "true"),
        ),
        connector=GmailConnector,
    ),
    ProviderSpec(
        id="github",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=("repo", "read:user", "user:email", "read:org"),
        connector=GitHubConnector,
    ),
    ProviderSpec(
        id="slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=("channels:read", "chat:write", "users:read", "files:read", "files:write"),
        scope_separator=",",
        connector=SlackConnector,
    ),
    ProviderSpec(
        id="vercel",
        authorize_url="https://vercel.com/oauth/authorize",
        token_url="https://api.vercel.com/v2/oauth/access_token",
        scopes=("openid", "email", "profile", "offline_access"),
        connector=VercelConnector,
    ),
    ProviderSpec(
        id="render",
        authorize_url="https://dashboard.render.com/oauth/authorize",
        token_url="https://api.render.com/oauth/token",
        scopes=(),
        connector=RenderConnector,
    ),
    ProviderSpec(
        id="cloudflare",
        authorize_url="https://dash.cloudflare.com/oauth2/auth",
        token_url="https://dash.cloudflare.com/oauth2/token",
        scopes=("account:read", "zone:read", "offline_access"),
        connector=CloudflareConnector,
    ),
    ProviderSpec(
        id="supabase",
        authorize_url="https://api.supabase.com/v1/oauth/authorize",
        token_url="https://api.supabase.com/v1/oauth/token",
        scopes=(),
        connector=SupabaseConnector,
    ),
    ProviderSpec(
        id="canva",
        authorize_url="https://www.canva.com/api/oauth/authorize",
        token_url="https://api.canva.com/rest/v1/oauth/token",
        scopes=(
            "design:read",
            "design:content:write",
            "asset:read",
            "asset:write",
        ),
        connector=CanvaConnector,
    ),
)


def _env(provider_id: str, suffix: str, default: str = "") -> str:
    key = f"IMURUN_OAUTH_{provider_id.upper()}_{suffix}"
    return os.getenv(key, default).strip()


def _scopes(provider_id: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(provider_id, "SCOPES")
    if not raw:
        return fallback
    return tuple(part for part in raw.replace(",", " ").split() if part)


def provider_specs() -> tuple[ProviderSpec, ...]:
    specs: list[ProviderSpec] = []
    for base in DEFAULT_PROVIDERS:
        specs.append(
            ProviderSpec(
                id=base.id,
                authorize_url=_env(base.id, "AUTHORIZE_URL", base.authorize_url),
                token_url=_env(base.id, "TOKEN_URL", base.token_url),
                scopes=_scopes(base.id, base.scopes),
                scope_separator=base.scope_separator,
                authorize_params=base.authorize_params,
                token_params=base.token_params,
                connector=base.connector,
            )
        )
    return tuple(specs)


def build_registry(
    transport=None,
    tokens=None,
) -> ConnectorRegistry:
    transport = transport or UrllibTransport()
    tokens = tokens or MemoryTokenStore()
    registry = ConnectorRegistry()
    for spec in provider_specs():
        registry.register(spec.connector(transport, tokens))
    return registry


def build_oauth_flows(
    tokens,
    redirect_uri: str,
    *,
    mark=None,
) -> dict[str, OAuthFlow]:
    flows: dict[str, OAuthFlow] = {}
    for spec in provider_specs():
        client_id = _env(spec.id, "CLIENT_ID")
        if not client_id:
            continue
        provider = OAuthProvider(
            id=spec.id,
            authorize_url=spec.authorize_url,
            token_url=spec.token_url,
            scopes=spec.scopes,
            client_id=client_id,
            client_secret=_env(spec.id, "CLIENT_SECRET"),
            scope_separator=spec.scope_separator,
            authorize_params=spec.authorize_params,
            token_params=spec.token_params,
        )
        flows[spec.id] = OAuthFlow(
            provider,
            UrllibTransport(),
            tokens,
            _env(spec.id, "REDIRECT_URI", redirect_uri),
            mark=mark,
        )
    return flows


def wire_server(
    server,
    home: Path | str,
    *,
    transport=None,
    tokens=None,
    form_transport=None,
    start_mcp: bool = True,
) -> dict:
    """Register connectors, OAuth flows, Gmail tools and MCP on a live ApiServer.

    ``transport`` / ``tokens`` / ``form_transport`` are injectable for tests;
    production uses ``UrllibTransport`` and ``build_token_store(home)``.
    """
    tokens = tokens if tokens is not None else build_token_store(home)
    transport = transport or UrllibTransport()
    form_transport = form_transport or transport
    registry = build_registry(transport=transport, tokens=tokens)
    for connector_id in registry.ids():
        if server._connectors.get(connector_id) is None:
            server._connectors.register(registry.get(connector_id))

    base = os.getenv("IMURUN_OAUTH_REDIRECT_BASE", "").strip()
    redirect = base.rstrip("/") + "/oauth/callback" if base else (
        f"http://127.0.0.1:{server.port}/oauth/callback"
    )
    mark = getattr(server, "_record", None)
    flows = build_oauth_flows(tokens, redirect, mark=mark)
    for connector_id, flow in flows.items():
        server.add_oauth_flow(connector_id, flow)

    # Every service: the user's own OAuth app from Settings, refresh, model tools.
    from .device import DeviceFlow
    from .tool_policy import install as install_policy

    settings = getattr(server, "_settings", None)
    setups = build_setups(settings, tokens, form_transport, redirect, mark=mark)
    server._connector_setups = setups
    server._gmail_setup = setups["gmail"]
    events = getattr(server, "_events", None)
    server._device_flows = {
        sid: DeviceFlow(setup, publish=getattr(events, "publish", None))
        for sid, setup in setups.items() if setup.spec.device_code_url
    }
    oauth = set(flows)
    for sid, setup in setups.items():
        if setup.register(server):
            oauth.add(sid)
        else:
            oauth.discard(sid)
    attach_refreshers(server._connectors.get, setups)

    install_policy(server)
    tool_registry = _tool_registry(server)
    tools_added: list[str] = []
    if tool_registry is not None:
        gate = getattr(server, "_gate", None)
        for tool in make_tools(server._connectors.get, setups, settings, gate):
            tool_registry.register(tool)
            tools_added.append(tool.name)

    mcp_view: dict = {}
    try:
        from ..mcp.wiring import wire_mcp

        mcp_view = wire_mcp(server, home, start=start_mcp)
    except Exception as exc:  # noqa: BLE001 - MCP must never stop the connectors
        mcp_view = {"error": f"{type(exc).__name__}: {exc}"}

    server._token_store = tokens
    store = tokens.backend_name() if hasattr(tokens, "backend_name") else type(tokens).__name__
    return {
        "connectors": registry.ids(),
        "oauth": sorted(oauth),
        "token_store": store,
        "tools": tools_added,
        "mcp": mcp_view,
    }


def build_setups(settings, tokens, form_transport, redirect: str, *, mark=None) -> dict:
    """One ``OAuthSetup`` per catalogued service (Gmail keeps its subclass)."""
    from .gmail_setup import GmailSetup
    from .setup import SERVICES, OAuthSetup

    setups = {}
    for sid, spec in SERVICES.items():
        kwargs = dict(settings=settings, tokens=tokens, form_transport=form_transport,
                      redirect_uri=redirect, mark=mark)
        setups[sid] = GmailSetup(**kwargs) if sid == "gmail" else OAuthSetup(spec, **kwargs)
    return setups


def attach_refreshers(get_connector, setups: dict) -> None:
    for sid, setup in setups.items():
        connector = get_connector(sid)
        if connector is None:
            continue
        if hasattr(connector, "refresher"):
            connector.refresher = setup.refresh
        if sid == "supabase" and hasattr(connector, "key_role"):
            connector.key_role = lambda setup=setup: setup.option("keyRole")


def make_tools(get_connector, setups: dict, settings, gate=None) -> list:
    """Every connector tool (Gmail + the seven services), sharing one gate."""
    from ..tools.builtin.connector_tools import ServiceContext
    from ..tools.builtin.gmail import GmailContext, gmail_tools
    from ..tools.builtin.service_tools import service_tools
    from .gate import ConnectorGate

    gate = gate or ConnectorGate()
    tools = list(gmail_tools(GmailContext(lambda: get_connector("gmail"), settings, gate,
                                          configured=setups["gmail"].configured)))
    for sid, setup in setups.items():
        if sid == "gmail":
            continue
        context = ServiceContext(sid, setup.spec.label, lambda sid=sid: get_connector(sid), setup, gate)
        tools += service_tools(sid, context)
    return tools


def _tool_registry(server):
    executor = getattr(server, "_executor", None)
    registry = getattr(executor, "registry", None)
    if registry is None:
        registry = getattr(getattr(server, "_tools", None), "registry", None)
    return registry
