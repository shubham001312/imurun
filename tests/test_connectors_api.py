"""Gmail + MCP routes on a real in-process ApiServer (fake Google, fake MCP server)."""
from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from fakes_google import FakeGoogle
from services.connectors.tokens import MemoryTokenStore
from services.connectors.wiring import wire_server
from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills.store import SkillsStore
from services.tools import ToolRunner, default_registry

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


class FakeModel:
    available = True

    def __init__(self):
        self.replies: list[str] = []
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else "ok"

    def context_limit(self):
        return 4096


@pytest.fixture()
def api(tmp_path, monkeypatch):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "SCOPES", "REDIRECT_URI"):
        monkeypatch.delenv(f"IMURUN_OAUTH_GMAIL_{key}", raising=False)
    monkeypatch.delenv("IMURUN_OAUTH_REDIRECT_BASE", raising=False)
    model = FakeModel()
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions", data_root=tmp_path, model=model,
        skills=SkillsStore(tmp_path / "skills"), tools=ToolRunner(default_registry()), static_root=None,
    ).start()
    server._settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    google = FakeGoogle()
    tokens = MemoryTokenStore()
    wired = wire_server(server, tmp_path, transport=google, tokens=tokens, start_mcp=False)
    yield server, model, google, tokens, wired
    server._mcp.close_all()
    server.stop()


def req(server, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=60)
    headers = {"Authorization": f"Bearer {server.token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data) if data else {}
    except ValueError:
        return resp.status, {"html": data}


def test_wiring_summary(api):
    _, _, _, _, wired = api
    assert "gmail" in wired["connectors"] and "gmail" not in wired["oauth"]
    from services.connectors.setup import SERVICES

    assert set(wired["tools"]) == {name for spec in SERVICES.values() for name in spec.tools}
    assert {"gmail_search", "gmail_send", "github_repos", "slack_post", "canva_export"} <= set(wired["tools"])
    assert wired["mcp"]["servers"] == 2 and wired["mcp"]["enabled"] == 0


def test_gmail_setup_connect_test_and_disconnect(api):
    server, _, google, tokens, _ = api
    status, view = req(server, "GET", "/api/connectors/gmail/config")
    assert status == 200 and view["configured"] is False and view["oauth"] is False
    assert req(server, "POST", "/api/connectors/gmail/connect")[0] == 404     # no client yet
    assert req(server, "POST", "/api/connectors/gmail/config", {"clientId": "has space"})[0] == 400
    status, view = req(server, "POST", "/api/connectors/gmail/config",
                       {"clientId": "42-x.apps.googleusercontent.com", "clientSecret": "GOCSPX-top"})
    assert status == 200 and view["configured"] and view["clientSecretSet"] and view["oauth"]
    assert "GOCSPX" not in json.dumps(view)
    assert "GOCSPX" not in json.dumps(req(server, "GET", "/api/settings/connectors")[1])
    assert view["redirectUri"] == f"http://127.0.0.1:{server.port}/oauth/callback"

    # Tools are offered only once connected.
    tools = {t["name"]: t for t in req(server, "GET", "/api/tools")[1]["tools"]}
    assert tools["gmail_search"]["available"] is False and "not connected" in tools["gmail_search"]["reason"]
    assert tools["gmail_search"]["permission"] == "allow" and tools["gmail_send"]["permission"] == "ask"

    status, begun = req(server, "POST", "/api/connectors/gmail/connect")
    assert status == 200 and begun["url"].startswith("https://accounts.google.com/")
    state = parse_qs(urlsplit(begun["url"]).query)["state"][0]
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    conn.request("GET", f"/oauth/callback?state={state}&code=the-code")
    response = conn.getresponse()
    html = response.read().decode()
    assert response.status == 200 and "Connected as me@example.com" in html and "window.close" in html
    assert tokens.get("gmail") == "access-1" and tokens.get("gmail:refresh") == "refresh-1"

    status, card = req(server, "GET", "/api/connectors/gmail")
    assert card["connected"] and card["refreshable"] and card["oauth"]
    assert "https://www.googleapis.com/auth/gmail.readonly" in card["scopes"]
    status, test = req(server, "GET", "/api/connectors/gmail/test")
    assert status == 200 and test["ok"] and test["account"] == "me@example.com"
    assert test["threads"][0]["subject"] == "Quarterly report" and test["threads"][0]["messages"] == 2

    tools = {t["name"]: t for t in req(server, "GET", "/api/tools")[1]["tools"]}
    assert tools["gmail_search"]["available"] is True
    # The token response granted readonly + compose only (compose covers send):
    # label changes need gmail.modify, so they are honestly unavailable.
    assert tools["gmail_send"]["available"] is True
    assert tools["gmail_modify"]["available"] is False and "not granted" in tools["gmail_modify"]["reason"]

    status, result = req(server, "POST", "/api/tools/run", {"name": "gmail_search", "args": {"query": "is:unread"}})
    assert status == 200 and result["result"]["result"]["ok"] and "[m1]" in result["result"]["result"]["content"]
    status, ask = req(server, "POST", "/api/tools/run",
                      {"name": "gmail_draft", "args": {"to": "a@example.com", "body": "hi"}})
    assert status == 403 and ask["decision"] == "ask" and "a@example.com" in ask["reasons"][0]
    status, done = req(server, "POST", "/api/tools/run",
                       {"name": "gmail_draft", "args": {"to": "a@example.com", "body": "hi"}, "approved": True})
    assert status == 200 and done["result"]["result"]["ok"]

    status, body = req(server, "POST", "/api/connectors/gmail/disconnect")
    assert status == 200 and body["disconnected"] is True
    assert tokens.get("gmail") is None and google.forms[-1][0].endswith("/revoke")
    assert req(server, "GET", "/api/connectors/gmail/test")[0] == 409


def test_model_turn_uses_gmail(api):
    server, model, _, tokens, _ = api
    tokens.set("gmail", "access-1")
    model.replies = ['```tool\n{"name": "gmail_search", "args": {"query": "from:alice"}}\n```',
                     "You have a quarterly report from Alice."]
    status, session = req(server, "POST", "/api/sessions", {"first_command": "mail"})
    status, turn = req(server, "POST", "/api/turns", {"session_id": session["id"], "text": "any mail from alice?"})
    assert status == 200
    view = {}
    for _ in range(200):
        status, view = req(server, "GET", f"/api/turns/{turn['turn_id']}")
        if view.get("state") != "running":
            break
        time.sleep(0.05)
    assert view["state"] == "done" and view["tool_calls"] == 1, view
    system = model.seen[0][0]["content"]
    assert "gmail_search" in system and "gmail_send" in system
    fed_back = model.seen[1][-1]["content"]
    assert fed_back.startswith("[tool result: gmail_search]") and "Quarterly report" in fed_back


def test_mcp_routes_end_to_end(api):
    server, *_ = api
    sub = server._events.subscribe()
    status, body = req(server, "GET", "/api/mcp/servers")
    assert status == 200 and body["servers"][0]["id"] == "gmail" and body["servers"][0]["status"] == "disabled"
    assert req(server, "POST", "/api/mcp/servers/gmail/connect")[0] == 409             # disabled
    assert req(server, "POST", "/api/mcp/servers", {"id": "bad id"})[0] == 400
    config = {"id": "fake", "name": "Fake", "transport": "stdio", "command": sys.executable,
              "args": [FAKE], "enabled": True, "env": {"FAKE_KEY": "s3cret"}, "timeout": 5}
    status, view = req(server, "POST", "/api/mcp/servers", config)
    assert status == 200 and view["env"] == {"FAKE_KEY": "********"}
    status, view = req(server, "POST", "/api/mcp/servers/fake/connect")
    assert status == 200 and view["status"] == "connected" and view["tools"] == 5, view
    status, tools = req(server, "GET", "/api/mcp/servers/fake/tools")
    names = {t["name"]: t for t in tools["tools"]}
    assert names["mcp_fake_echo"]["permission_level"] == "read"
    assert names["mcp_fake_wipe_disk"]["permission_level"] == "always"
    listing = {t["name"]: t for t in req(server, "GET", "/api/tools")[1]["tools"]}
    assert listing["mcp_fake_echo"]["category"] == "mcp" and listing["mcp_fake_echo"]["permission"] == "allow"
    assert listing["mcp_fake_add"]["permission"] == "ask"
    status, result = req(server, "POST", "/api/tools/run", {"name": "mcp_fake_echo", "args": {"text": "yo"}})
    assert status == 200 and "echo: yo" in result["result"]["result"]["content"]
    status, ask = req(server, "POST", "/api/tools/run", {"name": "mcp_fake_add", "args": {"a": 1, "b": 2}})
    assert status == 403 and ask["decision"] == "ask"
    assert req(server, "GET", "/api/mcp/servers/nope/tools")[0] == 404
    status, view = req(server, "POST", "/api/mcp/servers/fake/disconnect")
    assert status == 200 and view["status"] == "disconnected"
    assert req(server, "DELETE", "/api/mcp/servers/fake") == (200, {"deleted": "fake"})
    assert req(server, "DELETE", "/api/mcp/servers/fake")[0] == 404
    kinds = []
    while True:
        event = sub.get(timeout=0.2)
        if event is None:
            break
        kinds.append(event.type)
    assert "mcp.connected" in kinds and "mcp.disconnected" in kinds
    server._events.unsubscribe(sub)


def test_generic_connector_routes_and_device_flow(api):
    server, _, google, tokens, _ = api
    status, view = req(server, "GET", "/api/connectors/github/config")
    assert status == 200 and view["configured"] is False and view["deviceFlow"] is True
    assert view["redirectUri"] == f"http://127.0.0.1:{server.port}/oauth/callback"
    assert req(server, "GET", "/api/connectors/nope/config")[0] == 404
    assert req(server, "POST", "/api/connectors/github/device")[0] == 400        # no client id yet
    status, view = req(server, "POST", "/api/connectors/github/config",
                       {"clientId": "Iv1.abc", "clientSecret": "gh-secret"})
    assert status == 200 and view["oauth"] and view["clientSecretSet"] and "gh-secret" not in json.dumps(view)
    status, begun = req(server, "POST", "/api/connectors/github/connect")
    assert status == 200 and begun["url"].startswith("https://github.com/login/oauth/authorize?")
    # Device flow: the fake Google transport doubles as the form transport.
    google.post_form = lambda url, data, headers=None: {
        "device_code": "dc", "user_code": "WXYZ-0000", "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900}
    server._device_flows["github"]._background = False
    status, device = req(server, "POST", "/api/connectors/github/device")
    assert status == 200 and device["user_code"] == "WXYZ-0000" and "dc" not in json.dumps(device)
    assert req(server, "GET", "/api/connectors/github/device")[1]["state"] == "pending"
    assert req(server, "GET", "/api/connectors/github/test")[0] == 409
    tokens.set("github", "gho_1")
    assert req(server, "POST", "/api/connectors/github/device")[0] == 409        # already connected
    assert req(server, "POST", "/api/connectors/slack/device")[0] == 404         # no device flow


def test_import_google_client_file(api, tmp_path):
    server, _, _, tokens, _ = api
    client = tmp_path / "client_secret_42-x.apps.googleusercontent.com.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "42-x.apps.googleusercontent.com", "project_id": "imurun-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "GOCSPX-fromfile", "redirect_uris": ["http://localhost"]}}))
    status, body = req(server, "POST", "/api/connectors/gmail/import-client", {"path": str(client)})
    assert status == 200, body
    assert body == {"client_id": "42-x.apps.googleusercontent.com", "project_id": "imurun-test",
                    "redirect_uris": ["http://localhost"], "type": "installed", "warnings": []}
    assert tokens.get("gmail:client_secret") == "GOCSPX-fromfile"
    status, view = req(server, "GET", "/api/connectors/gmail/config")
    assert view["clientId"] == "42-x.apps.googleusercontent.com" and view["clientSecretSet"] and view["oauth"]
    assert "GOCSPX" not in json.dumps(view) and "GOCSPX" not in json.dumps(req(server, "GET", "/api/settings/connectors")[1])
    # Web clients get a redirect warning; bad input never echoes the file.
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "7-w.apps.googleusercontent.com", "client_secret": "GOCSPX-w",
                                       "redirect_uris": ["https://example.com/cb"]}}))
    status, body = req(server, "POST", "/api/connectors/gmail/import-client", {"path": str(web)})
    assert status == 200 and body["type"] == "web" and "redirect" in body["warnings"][0]
    bad = tmp_path / "bad.json"
    bad.write_text('{"installed": {"client_id": "nope", "client_secret": "GOCSPX-leak"}}')
    for path in (str(bad), "relative.json", str(tmp_path / "missing.json"), str(tmp_path)):
        status, body = req(server, "POST", "/api/connectors/gmail/import-client", {"path": path})
        assert status == 400 and "GOCSPX" not in json.dumps(body), (path, body)
    assert req(server, "POST", "/api/connectors/github/import-client", {"path": str(client)})[0] == 404


def test_token_routes(api):
    server, _, _, tokens, _ = api
    from test_connectors_services import FakeHttp

    http = FakeHttp()
    http.on("GET", r"/user/tokens/verify", lambda url, headers, body: {
        "success": headers["Authorization"] == "Bearer cf-good",
        "errors": [{"message": "Invalid API Token"}],
        "result": {"id": "t1", "status": "active"}})
    http.on("GET", r"/accounts\?", {"success": True, "result": [{"name": "Acme"}]})
    server._connectors.get("cloudflare")._transport = http
    status, view = req(server, "GET", "/api/connectors/cloudflare/config")
    assert status == 200 and view["auth_mode"] == "token" and view["has_token"] is False and view["store"]
    assert view["token_permissions"]["cloudflare_dns_create"] == "DNS:Edit"
    status, body = req(server, "POST", "/api/connectors/cloudflare/token", {"token": "cf-bad"})
    assert status == 401 and body["connected"] is False and "cf-bad" not in json.dumps(body)
    assert tokens.get("cloudflare") is None
    status, body = req(server, "POST", "/api/connectors/cloudflare/token", {"token": "cf-good"})
    assert status == 200 and body["connected"] and body["account"] == "Acme" and "cf-good" not in json.dumps(body)
    status, card = req(server, "GET", "/api/connectors/cloudflare")
    assert card["connected"] and card["account"] == "Acme" and "cf-good" not in json.dumps(card)
    assert req(server, "GET", "/api/connectors/cloudflare/config")[1]["has_token"] is True
    assert req(server, "POST", "/api/connectors/github/token", {"token": "x"})[0] == 409
    assert req(server, "POST", "/api/connectors/cloudflare/connect")[0] == 404          # no OAuth flow
    status, body = req(server, "DELETE", "/api/connectors/cloudflare/token")
    assert status == 200 and body == {"forgotten": True, "connected": False}
    assert tokens.get("cloudflare") is None


def test_builtin_app_connect_opens_browser_and_publishes(api, monkeypatch, tmp_path):
    server, _, google, tokens, _ = api
    from services.connectors import apps

    status, view = req(server, "GET", "/api/connectors/gmail/config")
    assert view["credential_source"] == "none" and "import-google" in view["registration_todo"]
    apps.write_local("gmail", {"client_id": "99-builtin.apps.googleusercontent.com", "client_secret": "GOCSPX-shipped"})
    status, view = req(server, "GET", "/api/connectors/gmail/config")
    assert view["credential_source"] == "builtin" and view["clientId"].startswith("99-builtin")
    assert view["clientSecretSet"] and "GOCSPX" not in json.dumps(view)
    assert req(server, "GET", "/api/connectors/github/config")[1]["credential_source"] == "none"
    # Env beats built-in; a user id beats env.
    monkeypatch.setenv("IMURUN_GMAIL_CLIENT_ID", "88-env.apps.googleusercontent.com")
    assert req(server, "GET", "/api/connectors/gmail/config")[1]["credential_source"] == "env"
    req(server, "POST", "/api/connectors/gmail/config", {"clientId": "77-user.apps.googleusercontent.com"})
    assert req(server, "GET", "/api/connectors/gmail/config")[1]["credential_source"] == "user"
    req(server, "POST", "/api/connectors/gmail/config", {"clientId": ""})
    monkeypatch.delenv("IMURUN_GMAIL_CLIENT_ID")
    # Connect with the built-in app: the server opens the system browser.
    opened = []
    server._open_url = opened.append
    sub = server._events.subscribe()
    status, begun = req(server, "POST", "/api/connectors/gmail/connect", {"open": True})
    assert status == 200 and begun["opened"] is True and "client_id=99-builtin" in begun["url"]
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == [begun["url"]]
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    conn.request("GET", f"/oauth/callback?state={begun['state']}&code=c1")
    response = conn.getresponse()
    page = response.read().decode()
    assert response.status == 200 and "Connected as me@example.com" in page
    assert "script-src 'sha256-" in response.getheader("Content-Security-Policy")
    assert google.forms[-1][1]["client_secret"] == "GOCSPX-shipped"       # the built-in pair
    events = []
    while (event := sub.get(timeout=0.2)) is not None:
        events.append((event.type, event.data))
    assert ("connector", {"connector": "gmail", "id": "gmail", "connected": True, "account": "me@example.com",
                          "message": "Connected", "via": "oauth"}) in events
    assert req(server, "GET", "/api/connectors/gmail")[1]["account"] == "me@example.com"
    # A replayed callback fails and Cloudflare has no OAuth connect.
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    conn.request("GET", f"/oauth/callback?state={begun['state']}&code=c1")
    assert conn.getresponse().status == 400
    assert "token" in req(server, "POST", "/api/connectors/cloudflare/connect")[1]["error"]
    server._events.unsubscribe(sub)


def test_frontend_contract_doc_levels_and_rememberable(api):
    server, *_ = api
    status, doc = req(server, "GET", "/api/connectors/cloudflare/doc")
    assert status == 200 and doc["format"] == "markdown" and doc["markdown"].startswith("# Cloudflare")
    assert req(server, "GET", "/api/connectors/nope/doc")[0] == 404
    levels = {t["name"]: t["permission_level"] for t in req(server, "GET", "/api/tools")[1]["tools"]}
    assert levels["gmail_search"] == "read" and levels["gmail_draft"] == "write"
    assert levels["gmail_send"] == "always" and levels["slack_post"] == "always"
    assert levels["file_read"] == "read" and levels["shell"] == "write" and levels["time_now"] == "allow"
    sub = server._events.subscribe()
    server._approvals.request("gmail_send", {"to": "a@b.co", "body": "x"}, ("sends",), timeout=1)
    server._approvals.request("gmail_draft", {"to": "a@b.co", "body": "x"}, ("drafts",), timeout=1)
    server._approvals.request("file_write", {"path": "a.txt"}, ("writes",), timeout=1)
    cards = {}
    while (event := sub.get(timeout=0.2)) is not None:
        if event.type == "tool.approval_needed":
            cards[event.data["tool"]] = event.data
    assert cards["gmail_send"]["rememberable"] is False and cards["gmail_send"]["level"] == "always"
    assert cards["gmail_draft"]["rememberable"] is True and cards["file_write"]["rememberable"] is True
    server._events.unsubscribe(sub)
