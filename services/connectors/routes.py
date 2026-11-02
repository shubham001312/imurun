"""HTTP handlers for connector set-up (every service in ``setup.SERVICES``).

Dispatched from ``services/tools/routes.py`` (which ``api.py`` already
calls). Status, connect (authorization-code flow), disconnect and the OAuth
callback stay on the existing generic routes in ``api.py``.

GET  /api/connectors/{id}/config    client id (shown), secret (set / not set), tools, scopes, redirect
POST /api/connectors/{id}/config    {clientId?, clientSecret?, tools?, <service options>?}
GET  /api/connectors/{id}/test      a small real read (Gmail: 3 recent threads) proving the token works
POST /api/connectors/{id}/token     token mode (Cloudflare, Render): verify, then store; never echoed
DELETE /api/connectors/{id}/token   forget the token
POST /api/connectors/{id}/import-client  {"path": "<abs path>"}: Google client_secret_*.json -> settings + token store
POST /api/connectors/{id}/device    start the device flow (GitHub): {user_code, verification_uri, ...}
GET  /api/connectors/{id}/device    device flow state: pending | connected | denied | expired | error
"""
from __future__ import annotations

import base64
import hashlib
import html
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .base import ConnectorError
from .oauth import OAuthError
from .providers.gmail_mime import header_map, short_date

Reply = tuple[int, dict]
ACTIONS = ("config", "test", "device", "doc")
DOCS_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2] / "docs" / "connectors"
MAX_DOC = 256 * 1024


def _target(server: Any, path: str):
    parts = [p for p in path.split("/") if p]
    if len(parts) != 4 or parts[:2] != ["api", "connectors"] or parts[3] not in ACTIONS:
        return None, None
    setups = getattr(server, "_connector_setups", None) or {}
    return parts[2], parts[3] if parts[2] in setups or setups == {} else None


def handle_get(server: Any, path: str) -> Reply | None:
    service, action = _target(server, path)
    if service is None or action is None:
        return None
    setup = (getattr(server, "_connector_setups", None) or {}).get(service)
    if setup is None:
        return 501, {"error": "connectors are not wired"}
    if action == "config":
        return 200, _config_view(server, setup)
    if action == "doc":
        return _doc(setup)
    if action == "device":
        flow = (getattr(server, "_device_flows", None) or {}).get(service)
        if flow is None:
            return 404, {"error": f"{setup.spec.label} has no device flow"}
        return 200, flow.status()
    if service == "gmail":
        return _gmail_test(server)
    return _tool_test(server, setup)


def is_post_route(path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    return len(parts) == 4 and parts[:2] == ["api", "connectors"] and parts[3] in ("config", "device",
                                                                                   "import-client", "token",
                                                                                   "connect")


def handle_delete(server: Any, path: str) -> Reply | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) != 4 or parts[:2] != ["api", "connectors"] or parts[3] != "token":
        return None
    setup = (getattr(server, "_connector_setups", None) or {}).get(parts[2])
    connector = server._connectors.get(parts[2])
    if setup is None or connector is None:
        return 404, {"error": "unknown connector"}
    had = setup.has_token()
    connector.forget()
    _record(server, f"connector:{parts[2]}", "token forgotten")
    _publish(server, "connector", {"connector": parts[2], "connected": False, "message": "Token forgotten"})
    return 200, {"forgotten": had, "connected": False}


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    parts = [p for p in path.split("/") if p]
    service, action = parts[2], parts[3]
    setup = (getattr(server, "_connector_setups", None) or {}).get(service)
    if action == "connect":
        return _connect(server, service, setup, payload)
    if setup is None:
        if not getattr(server, "_connector_setups", None):
            return 501, {"error": "connectors are not wired"}
        return 404, {"error": "unknown connector"}
    if action == "token":
        connector = server._connectors.get(service)
        if connector is None:
            return 404, {"error": "unknown connector"}
        if not setup.token_enabled:
            return 409, {"error": f"{setup.spec.label} signs in with OAuth: use /connect"}
        try:
            result = setup.save_token(connector, payload.get("token"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except ConnectorError as exc:
            # The provider refused the candidate token; nothing was stored.
            return 401, {"connected": False, "error": f"{setup.spec.label} refused the token: {str(exc)[:200]}"}
        except Exception as exc:  # noqa: BLE001 - e.g. the secret store refused
            return 500, {"error": f"could not save the token: {type(exc).__name__}"}
        _record(server, f"connector:{service}", "token verified and saved")
        _publish(server, "connector", {"connector": service, "connected": True,
                                       "message": f"{setup.spec.label} token saved", "via": "token"})
        return 200, result
    if action == "import-client":
        importer = getattr(setup, "import_client", None)
        if importer is None:
            return 404, {"error": f"{setup.spec.label} has no client file to import; "
                                  "enter the client id and secret instead"}
        try:
            result = importer(str(payload.get("path") or ""))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - e.g. the token store refused
            return 500, {"error": f"could not import: {type(exc).__name__}"}
        setup.register(server)
        _record(server, f"connector:{service}", "client file imported")
        _publish(server, "connector", {"connector": service, "configured": True,
                                       "connected": bool(server._connectors.get(service) and
                                                         server._connectors.get(service).connected()),
                                       "message": f"{setup.spec.label} OAuth client imported"})
        return 200, result
    if action == "device":
        flow = (getattr(server, "_device_flows", None) or {}).get(service)
        if flow is None:
            return 404, {"error": f"{setup.spec.label} has no device flow"}
        connector = server._connectors.get(service)
        if connector is not None and connector.connected():
            return 409, {"error": "already connected"}
        try:
            view = flow.start()
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except ConnectorError as exc:
            return 502, {"error": str(exc)[:300]}
        _record(server, f"connector:{service}", "device flow started")
        return 200, view
    try:
        setup.update(payload)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - e.g. the token store refused
        return 500, {"error": f"could not save: {type(exc).__name__}"}
    installed = setup.register(server)
    _record(server, f"connector:{service}", "config saved" + ("" if installed else " (no client id)"))
    view = _config_view(server, setup)
    _publish(server, "connector", {"connector": service, "connected": view["connected"],
                                   "configured": view["configured"], "message": f"{setup.spec.label} settings saved"})
    return 200, view


# --------------------------------------------------------------- connect
def _connect(server: Any, service: str, setup: Any, payload: dict) -> Reply:
    """``POST /api/connectors/{id}/connect {"open": true?}`` -> ``{url, state, opened}``.

    Same contract as the old api.py route (404 without a flow, 409 when
    connected), plus ``open``: the server opens the URL in the **system**
    browser (Google refuses OAuth inside embedded webviews).
    """
    connector = server._connectors.get(service)
    if connector is None:
        return 404, {"error": "unknown connector"}
    if setup is not None and not setup.oauth_enabled:
        return 404, {"error": f"{setup.spec.label} uses an API token: POST /api/connectors/{service}/token"}
    if setup is not None:
        # Credentials resolve now (user -> env -> built-in), not at start-up.
        setup.register(server)
    flow = getattr(server, "_oauth_flows", {}).get(service)
    if flow is None:
        reason = setup.missing_reason(setup.spec.tools[0]) if setup is not None else "no oauth flow"
        return 404, {"error": "no oauth flow for this connector", "reason": reason}
    if connector.connected():
        return 409, {"error": "already connected"}
    begun = flow.begin()
    with server._oauth_lock:
        server._oauth_states[begun["state"]] = (service, flow)
    opened = False
    if payload.get("open"):
        opener = getattr(server, "_open_url", None)
        if opener is None:
            import webbrowser

            opener = webbrowser.open
        try:
            threading.Thread(target=opener, args=(begun["url"],), name="oauth-browser", daemon=True).start()
            opened = True
        except Exception:  # noqa: BLE001 - the UI can still open the url itself
            opened = False
    _record(server, f"connector:{service}", "oauth started")
    return 200, {**begun, "opened": opened}


ACCOUNT_PROBES: dict[str, tuple[str, Any]] = {
    "gmail": ("profile", lambda r: r.get("emailAddress", "")),
    "github": ("user", lambda r: r.get("login", "")),
    "slack": ("auth_test", lambda r: " @ ".join(x for x in (r.get("user"), r.get("team")) if x)),
    "vercel": ("user", lambda r: (r.get("user") or {}).get("username") or (r.get("user") or {}).get("email", "")),
    "supabase": ("list_organizations", lambda r: ", ".join(str(o.get("name")) for o in (r.get("items") or [])[:3]
                                                          if isinstance(o, dict))),
    "canva": ("me", lambda r: ""),
}


def account_name(server: Any, service: str) -> str:
    """Best effort: who just connected (stored as ``<id>:account``)."""
    probe = ACCOUNT_PROBES.get(service)
    connector = server._connectors.get(service)
    if probe is None or connector is None:
        return ""
    try:
        result = server._gate.execute(connector, probe[0], {})
        name = str(probe[1](result if isinstance(result, dict) else {}) or "")[:200]
    except Exception:  # noqa: BLE001 - connected even when the probe fails
        return ""
    tokens = getattr(server, "_token_store", None)
    if name and tokens is not None:
        try:
            tokens.set(f"{service}:account", name)
        except Exception:  # noqa: BLE001
            pass
    return name


_CLOSE_SCRIPT = "setTimeout(function(){window.close();},2000);"
_CLOSE_HASH = base64.b64encode(hashlib.sha256(_CLOSE_SCRIPT.encode()).digest()).decode()


def _page(code: int, title: str, message: str, *, close: bool = False) -> tuple[int, dict, bytes]:
    script = f"<script>{_CLOSE_SCRIPT}</script>" if close else ""
    body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:16px system-ui,sans-serif;margin:3rem auto;max-width:32rem;padding:0 1rem}</style>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{script}").encode("utf-8")
    csp = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'" + (f"; script-src 'sha256-{_CLOSE_HASH}'" if close else ""))
    headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
               "Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
               "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
    return code, headers, body


def handle_callback(server: Any, raw_path: str) -> tuple[int, dict, bytes]:
    """``GET /oauth/callback``: finish the flow, name the account, tell the UI."""
    query = parse_qs(urlsplit(raw_path).query)
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    error = (query.get("error") or [""])[0]
    with server._oauth_lock:
        entry = server._oauth_states.pop(state, None)
    if entry is None:
        return _page(400, "Connection failed", "This link is unknown or already used. Press Connect again.")
    service, flow = entry
    label = getattr(getattr(flow, "provider", None), "id", service)
    setups = getattr(server, "_connector_setups", None) or {}
    if service in setups:
        label = setups[service].spec.label
    if error or not code:
        _publish(server, "connector", {"connector": service, "connected": False,
                                       "message": "Sign-in was cancelled", "via": "oauth"})
        return _page(400, "Connection cancelled", f"{label} returned no authorization. Press Connect again.")
    try:
        flow.complete(state, code)
    except OAuthError as exc:
        _publish(server, "connector", {"connector": service, "connected": False,
                                       "message": str(exc)[:200], "via": "oauth"})
        return _page(400, "Connection failed", str(exc)[:300])
    account = account_name(server, service)
    _record(server, f"connector:{service}", "connected")
    _publish(server, "connector", {"connector": service, "id": service, "connected": True, "account": account,
                                   "message": "Connected", "via": "oauth"})
    who = f"Connected as {account}" if account else f"{label} is connected"
    return _page(200, "Connected", f"{who} — you can close this tab.", close=True)


def _doc(setup: Any) -> Reply:
    """``GET /api/connectors/{id}/doc``: the service's setup page (read-only).

    Only the file named in the catalogue, resolved inside docs/connectors/.
    """
    name = __import__("pathlib").Path(setup.spec.doc or "").name
    target = (DOCS_ROOT / name).resolve()
    try:
        target.relative_to(DOCS_ROOT.resolve())
    except ValueError:
        return 404, {"error": "no setup page"}
    if not name.endswith(".md") or not target.is_file() or target.stat().st_size > MAX_DOC:
        return 404, {"error": "no setup page"}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return 404, {"error": "no setup page"}
    return 200, {"id": setup.spec.id, "path": f"docs/connectors/{name}", "format": "markdown", "markdown": text}


def _config_view(server: Any, setup: Any) -> dict:
    view = setup.view()
    connector = server._connectors.get(setup.spec.id)
    view["connected"] = bool(connector is not None and connector.connected())
    view["oauth"] = setup.spec.id in getattr(server, "_oauth_flows", {})
    return view


def _tool_test(server: Any, setup: Any) -> Reply:
    connector = server._connectors.get(setup.spec.id)
    if connector is None:
        return 404, {"error": "unknown connector"}
    if not connector.connected():
        return 409, {"ok": False, "error": f"{setup.spec.id} is not connected"}
    registry = getattr(getattr(server, "_executor", None), "registry", None)
    tool = registry.get(setup.spec.test_tool) if registry is not None else None
    if tool is None:
        return 501, {"error": "the test tool is not registered"}
    result = tool.run({"limit": 3})
    if not result.ok:
        return 502, {"ok": False, "error": (result.error or result.content)[:300]}
    return 200, {"ok": True, "tool": tool.name, "content": result.content[:2000]}


def _gmail_test(server: Any) -> Reply:
    connector = server._connectors.get("gmail")
    if connector is None:
        return 404, {"error": "unknown connector"}
    if not connector.connected():
        return 409, {"ok": False, "error": "gmail is not connected"}
    gate = server._gate
    try:
        listing = gate.execute(connector, "list_threads", {"max_results": 3})
        threads = []
        for ref in (listing.get("threads") or [])[:3]:
            thread = gate.execute(connector, "get_thread", {"id": ref.get("id", ""), "format": "metadata"})
            messages = thread.get("messages") or [{}]
            first, last = messages[0], messages[-1]
            threads.append({
                "id": str(thread.get("id") or ref.get("id") or ""),
                "subject": header_map(first.get("payload")).get("subject", "")[:200] or "(no subject)",
                "from": header_map(last.get("payload")).get("from", "")[:200],
                "date": short_date(last),
                "messages": len(messages),
                "snippet": str(ref.get("snippet") or last.get("snippet") or "")[:200],
            })
        try:
            account = str(gate.execute(connector, "profile", {}).get("emailAddress") or "")
        except ConnectorError:
            account = ""
    except ConnectorError as exc:
        return 502, {"ok": False, "error": str(exc)[:300]}
    return 200, {"ok": True, "account": account, "threads": threads}


def _publish(server: Any, kind: str, data: dict) -> None:
    events = getattr(server, "_events", None)
    if events is None:
        return
    try:
        events.publish(kind, data)
    except Exception:  # noqa: BLE001
        pass


def _record(server: Any, subject: str, why: str) -> None:
    record = getattr(server, "_record", None)
    if callable(record):
        try:
            record(subject, why)
        except Exception:  # noqa: BLE001
            pass
