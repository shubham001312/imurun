"""GitHub, Slack, Vercel, Render, Cloudflare, Supabase, Canva: OAuth set-up,
providers, model tools and permissions against a fake HTTP layer (no network)."""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from services.connectors.base import ConnectorError
from services.connectors.device import DeviceFlow
from services.connectors.setup import SERVICES, OAuthSetup
from services.connectors.tokens import MemoryTokenStore
from services.connectors.tool_policy import ConnectorToolPolicy
from services.connectors.wiring import attach_refreshers, build_registry, build_setups, make_tools
from services.server.settings import SettingsStore
from services.tools.executor import CallContext, ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


class FakeHttp:
    """Routes ``(METHOD, url regex)`` to a response (or a callable)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []
        self.forms: list[tuple[str, dict, dict | None]] = []
        self.form_answers: list = []

    def on(self, method: str, pattern: str, answer) -> "FakeHttp":
        self.routes.insert(0, (method, re.compile(pattern), answer))
        return self

    def request(self, method, url, headers=None, json_body=None, *, json=None):
        body = json_body if json_body is not None else json
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body})
        for m, pattern, answer in self.routes:
            if m == method and pattern.search(url):
                return answer(url, headers, body) if callable(answer) else answer
        raise ConnectorError(f"{method} {urlsplit(url).netloc} answered 404: not faked {url}")

    def post_form(self, url, data, headers=None):
        self.forms.append((url, dict(data), headers))
        if not self.form_answers:
            raise ConnectorError("no form answer queued")
        answer = self.form_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def last(self, method=None) -> dict:
        calls = [c for c in self.calls if method is None or c["method"] == method]
        return calls[-1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IMURUN_OAUTH_"):
            monkeypatch.delenv(key)
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    tokens = MemoryTokenStore()
    http = FakeHttp()
    connectors = build_registry(transport=http, tokens=tokens)
    setups = build_setups(settings, tokens, http, "http://127.0.0.1:8732/oauth/callback")
    attach_refreshers(connectors.get, setups)
    registry = ToolRegistry()
    for tool in make_tools(connectors.get, setups, settings):
        registry.register(tool)
    policy = ConnectorToolPolicy(PermissionPolicy(settings))
    return {"settings": settings, "tokens": tokens, "http": http, "connectors": connectors,
            "setups": setups, "registry": registry, "policy": policy,
            "executor": ToolExecutor(registry, policy=policy)}


def run(env, tool_name, /, **args):
    ctx = CallContext(session_id="s1", approved=True, interactive=False, source="user")
    return env["executor"].execute(tool_name, args, ctx).result


def decide(env, tool_name, /, **args):
    return env["policy"].decide(env["registry"].get(tool_name), args, session_id="s1").kind


def connect(env, service, token="tok-1"):
    env["tokens"].set(service, token)


# ------------------------------------------------------------- catalogue
def test_every_connector_has_tools_and_a_setup(env):
    names = set(env["registry"].names())
    for sid, spec in SERVICES.items():
        assert set(spec.tools) <= names, sid
        assert spec.test_tool in spec.tools and spec.doc.endswith(".md")
        for tool in spec.tools:
            assert tool.startswith(sid + "_")
    for name in names:
        tool = env["registry"].get(name)
        assert tool.category in ("connector-read", "connector-write"), name


def test_unavailable_reasons_are_honest(env):
    tool = env["registry"].get("github_repos")
    assert "add your own GitHub OAuth client id" in tool.unavailable_reason()
    env["setups"]["github"].update({"clientId": "Iv1.abc"})
    assert "press Connect in Settings -> Connectors -> GitHub" in tool.unavailable_reason()
    connect(env, "github")
    assert tool.available()
    env["setups"]["github"].update({"tools": ["github_issues"]})
    assert "turned off" in tool.unavailable_reason()


def test_every_write_always_asks(env):
    env["settings"].write("permissions", {"mode": "allow-all"})
    writes = {"github_create_issue", "github_comment", "slack_post", "vercel_redeploy", "render_deploy",
              "render_restart", "cloudflare_dns_create", "cloudflare_dns_update", "cloudflare_dns_delete",
              "cloudflare_purge_cache", "supabase_insert"}
    for sid in SERVICES:
        if sid == "gmail":
            continue
        for name in SERVICES[sid].tools:
            tool = env["registry"].get(name)
            expected = "ask" if name in writes else "allow"
            assert decide(env, name) == expected, name
            assert tool.category == ("connector-write" if name in writes else "connector-read"), name
    env["policy"].remember("s1", "slack_post")
    assert decide(env, "slack_post", channel="C1", text="hi") == "ask"
    reasons = env["policy"].decide(env["registry"].get("slack_post"), {"channel": "C1", "text": "ship it"}).reasons
    assert "C1" in reasons[0] and "ship it" in reasons[0]
    env["settings"].write("permissions", {"mode": "ask"})
    assert decide(env, "github_repos") == "ask"
    env["settings"].write("permissions", {"network": False})
    assert decide(env, "github_repos") == "deny"


def test_writes_never_run_without_approval(env):
    connect(env, "github")
    ctx = CallContext(session_id="s1", interactive=False, source="model")
    execution = env["executor"].execute("github_create_issue", {"repo": "a/b", "title": "x"}, ctx)
    assert execution.decision.kind == "ask" and not env["http"].calls


# ---------------------------------------------------------------- OAuth
def test_oauth_urls_scopes_and_client_auth(env):
    http, setups = env["http"], env["setups"]
    setups["github"].update({"clientId": "Iv1.gh", "clientSecret": "gh-secret"})
    url = urlsplit(setups["github"].flow().begin()["url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "github.com" and query["scope"] == "repo read:user"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "http://127.0.0.1:8732/oauth/callback"

    setups["slack"].update({"clientId": "123.456", "tools": ["slack_channels", "slack_post"]})
    query = parse_qs(urlsplit(setups["slack"].flow().begin()["url"]).query)
    assert query["scope"] == ["channels:read,groups:read,chat:write"]

    setups["supabase"].update({"clientId": "sb-client"})
    query = parse_qs(urlsplit(setups["supabase"].flow().begin()["url"]).query)
    assert "scope" not in query                 # scopes live in the Supabase app registration

    setups["canva"].update({"clientId": "OC-canva", "clientSecret": "cnv-secret"})
    flow = setups["canva"].flow()
    state = flow.begin()["state"]
    http.form_answers.append({"access_token": "cv-1", "refresh_token": "cr-1", "expires_in": 14400,
                              "scope": "design:meta:read design:content:read"})
    flow.complete(state, "code-1")
    url_used, form, headers = http.forms[-1]
    assert url_used == "https://api.canva.com/rest/v1/oauth/token"
    assert "client_secret" not in form and form["code_verifier"]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"OC-canva:cnv-secret").decode()
    assert env["tokens"].get("canva") == "cv-1" and env["tokens"].get("canva:refresh") == "cr-1"
    assert "cnv-secret" not in str(setups["canva"].view())


def test_refresh_on_expiry_with_rotation_and_rejection(env):
    http, tokens = env["http"], env["tokens"]
    env["setups"]["canva"].update({"clientId": "OC-canva", "clientSecret": "s"})
    tokens.set("canva", "old")
    tokens.set("canva:refresh", "r1")
    tokens.set("canva:expires_at", "1")              # long expired
    http.on("GET", r"/rest/v1/designs", lambda url, headers, body: {"items": [
        {"id": "D1", "title": f"seen with {headers['Authorization']}"}]})
    http.form_answers.append({"access_token": "new", "refresh_token": "r2", "expires_in": 14400})
    result = run(env, "canva_designs")
    assert result.ok and "Bearer new" in result.content
    assert tokens.get("canva:refresh") == "r2"       # Canva rotates refresh tokens
    assert http.forms[-1][1]["grant_type"] == "refresh_token"
    # 401 -> refresh -> retry; a refused refresh forgets the account.
    http.on("GET", r"/rest/v1/designs", lambda url, headers, body: (_ for _ in ()).throw(
        ConnectorError("GET api.canva.com answered 401: invalid token")))
    http.form_answers.append(ConnectorError("POST api.canva.com answered 400: invalid_grant"))
    result = run(env, "canva_designs")
    assert not result.ok and not env["connectors"].get("canva").connected()


def test_github_device_flow(env):
    http, setup = env["http"], env["setups"]["github"]
    flow = DeviceFlow(setup, background=False, clock=lambda: 1000.0)
    with pytest.raises(ValueError, match="client id"):
        flow.start()
    setup.update({"clientId": "Iv1.gh"})
    http.form_answers += [
        {"device_code": "dev-secret", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
         "interval": 5, "expires_in": 900},
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": "gho_x", "scope": "repo,read:user", "token_type": "bearer"},
    ]
    view = flow.start()
    assert view["state"] == "pending" and view["user_code"] == "ABCD-1234" and "dev-secret" not in str(view)
    assert http.forms[0][0] == "https://github.com/login/device/code"
    assert flow.poll_once() == "pending"
    assert flow.poll_once() == "pending" and flow.status()["interval"] == 10
    assert flow.poll_once() == "connected"
    assert http.forms[-1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert env["tokens"].get("github") == "gho_x"
    assert env["connectors"].get("github").granted_scopes() == ("repo", "read:user")
    http.form_answers += [{"device_code": "d2", "user_code": "X", "interval": 1, "expires_in": 60},
                          {"error": "access_denied"}]
    flow.start()
    assert flow.poll_once() == "denied"


# ---------------------------------------------------------------- GitHub
def test_github_tools(env):
    http = env["http"]
    connect(env, "github")
    http.on("GET", r"/user/repos\?", [{"full_name": "me/app", "private": True, "language": "Python",
                                       "stargazers_count": 3, "open_issues_count": 1, "description": "x"}])
    http.on("GET", r"/repos/me/app/issues\?", [{"number": 7, "state": "open", "title": "Bug", "user": {"login": "a"}},
                                               {"number": 8, "title": "PR", "pull_request": {}}])
    http.on("GET", r"/repos/me/app/issues/7$", {"number": 7, "title": "Bug", "state": "open", "body": "It breaks"})
    http.on("GET", r"/repos/me/app/issues/7/comments", [{"user": {"login": "b"}, "body": "me too"}])
    http.on("GET", r"/repos/me/app/pulls\?", [{"number": 9, "state": "open", "title": "Fix", "head": {"ref": "fix"},
                                               "base": {"ref": "main"}, "user": {"login": "c"}}])
    http.on("GET", r"/repos/me/app/pulls/9$", {"number": 9, "title": "Fix", "state": "open", "body": "fixes #7",
                                               "head": {"ref": "fix"}, "base": {"ref": "main"}})
    http.on("GET", r"/pulls/9/files", [{"filename": "a.py", "additions": 2, "deletions": 1, "status": "modified"}])
    http.on("GET", r"/contents/src/main\.py", {"encoding": "base64", "content": base64.b64encode(b"print(1)\n").decode(),
                                               "size": 9, "sha": "abc"})
    http.on("POST", r"/repos/me/app/issues$", {"number": 10, "html_url": "https://github.com/me/app/issues/10"})
    http.on("POST", r"/issues/7/comments", {"id": 1, "html_url": "https://github.com/me/app/issues/7#c1"})
    assert "me/app (private" in run(env, "github_repos").content
    issues = run(env, "github_issues", repo="me/app").content
    assert "#7" in issues and "#8" not in issues
    issue = run(env, "github_issue", repo="me/app", number=7).content
    assert "It breaks" in issue and "me too" in issue and "not instructions" in issue
    assert "fix -> main" in run(env, "github_pulls", repo="me/app").content
    assert "a.py (+2 -1" in run(env, "github_pull", repo="me/app", number="9").content
    file = run(env, "github_file", repo="me/app", path="src/main.py", ref="main")
    assert "print(1)" in file.content and "ref=main" in http.last()["url"]
    assert http.last()["headers"]["Accept"] == "application/vnd.github+json"
    assert "issues/10" in run(env, "github_create_issue", repo="me/app", title="New", body="b").content
    assert http.last("POST")["body"] == {"title": "New", "body": "b"}
    assert run(env, "github_comment", repo="me/app", number=7, body="thanks").ok
    assert not run(env, "github_file", repo="me/app", path="../etc/passwd").ok
    assert not run(env, "github_issues", repo="not a repo").ok


# ----------------------------------------------------------------- Slack
def test_slack_tools(env):
    http = env["http"]
    connect(env, "slack")
    http.on("GET", r"conversations\.list", {"ok": True, "channels": [
        {"id": "C1", "name": "general", "is_member": True, "num_members": 5}]})
    http.on("GET", r"conversations\.history\?channel=C1", {"ok": True, "messages": [{"ts": "1.2", "user": "U1", "text": "hello"}]})
    http.on("POST", r"chat\.postMessage", {"ok": True, "channel": "C1", "ts": "1.3"})
    assert "#general [C1]" in run(env, "slack_channels").content
    assert "U1: hello" in run(env, "slack_history", channel="C1").content
    assert run(env, "slack_post", channel="C1", text="hi").ok
    assert http.last("POST")["body"] == {"channel": "C1", "text": "hi"}
    http.on("GET", r"conversations\.history", {"ok": False, "error": "not_in_channel"})
    result = run(env, "slack_history", channel="C2")
    assert not result.ok and "not_in_channel" in result.content


# ---------------------------------------------------------------- Vercel
def test_vercel_tools(env):
    http = env["http"]
    connect(env, "vercel")
    http.on("GET", r"/v9/projects", {"projects": [{"id": "prj_1", "name": "site", "framework": "nextjs"}]})
    http.on("GET", r"/v6/deployments\?.*projectId=prj_1", {"deployments": [
        {"uid": "dpl_1", "name": "site", "state": "READY", "target": "production", "url": "site.vercel.app"}]})
    http.on("GET", r"/v3/deployments/dpl_1/events", [{"type": "stdout", "text": "Build done"}])
    http.on("POST", r"/v13/deployments", {"id": "dpl_2", "url": "site-2.vercel.app"})
    assert "site [prj_1]" in run(env, "vercel_projects").content
    assert "dpl_1 site [READY" in run(env, "vercel_deployments", project="prj_1").content
    assert "Build done" in run(env, "vercel_logs", deployment="dpl_1").content
    assert run(env, "vercel_redeploy", deployment="dpl_1", name="site").ok
    assert http.last("POST")["body"] == {"name": "site", "deploymentId": "dpl_1", "target": "preview"}
    reasons = env["policy"].decide(env["registry"].get("vercel_redeploy"),
                                   {"deployment": "dpl_1", "name": "site", "target": "production"}).reasons
    assert "production" in reasons[0]


# ---------------------------------------------------------------- Render
def test_render_tools(env):
    http = env["http"]
    connect(env, "render")
    http.on("GET", r"/v1/services\?", [{"service": {"id": "srv-1", "name": "api", "type": "web_service"}}])
    http.on("GET", r"/v1/services/srv-1/deploys", [{"deploy": {"id": "dep-1", "status": "live",
                                                              "commit": {"message": "ship"}}}])
    http.on("GET", r"/v1/services/srv-1$", {"id": "srv-1", "ownerId": "tea-1"})
    http.on("GET", r"/v1/logs\?ownerId=tea-1&resource=srv-1", {"logs": [{"timestamp": "t", "message": "started"}]})
    http.on("POST", r"/services/srv-1/deploys", {"id": "dep-2", "status": "created"})
    http.on("POST", r"/services/srv-1/restart", {})
    assert "api [srv-1]" in run(env, "render_services").content
    assert "dep-1 [live] ship" in run(env, "render_deploys", service="srv-1").content
    assert "started" in run(env, "render_logs", service="srv-1").content
    assert "dep-2" in run(env, "render_deploy", service="srv-1").content
    assert run(env, "render_restart", service="srv-1").ok


# ------------------------------------------------------------ Cloudflare
def test_cloudflare_tools(env):
    http = env["http"]
    connect(env, "cloudflare")
    zone = "0123456789abcdef0123456789abcdef"
    http.on("GET", r"/zones\?", {"success": True, "result": [{"id": zone, "name": "example.com", "status": "active",
                                                              "account": {"id": "acc1"}}]})
    http.on("GET", rf"/zones/{zone}/dns_records", {"success": True, "result": [
        {"id": "rec1", "type": "A", "name": "www.example.com", "content": "1.2.3.4", "proxied": True}]})
    http.on("GET", r"/accounts/acc1/workers/scripts", {"success": True, "result": [{"id": "hello"}]})
    http.on("POST", r"/dns_records$", {"success": True, "result": {"id": "rec2", "type": "A", "name": "api"}})
    http.on("PATCH", r"/dns_records/rec1", {"success": True, "result": {"id": "rec1", "type": "A", "name": "www",
                                                                         "content": "5.6.7.8"}})
    http.on("DELETE", r"/dns_records/rec1", {"success": True, "result": {"id": "rec1"}})
    http.on("POST", r"/purge_cache", {"success": True, "result": {"id": zone}})
    assert "example.com" in run(env, "cloudflare_zones").content
    assert "www.example.com -> 1.2.3.4" in run(env, "cloudflare_dns_records", zone=zone).content
    assert "hello" in run(env, "cloudflare_workers", account="acc1").content
    assert run(env, "cloudflare_dns_create", zone=zone, type="a", name="api", content="9.9.9.9").ok
    assert http.last("POST")["body"] == {"type": "A", "name": "api", "content": "9.9.9.9"}
    assert "5.6.7.8" in run(env, "cloudflare_dns_update", zone=zone, record_id="rec1", content="5.6.7.8").content
    assert run(env, "cloudflare_dns_delete", zone=zone, record_id="rec1").ok
    assert run(env, "cloudflare_purge_cache", zone=zone).ok
    http.on("GET", r"/zones\?", {"success": False, "errors": [{"message": "Authentication error"}]})
    result = run(env, "cloudflare_zones")
    assert not result.ok and "Authentication error" in result.content


# -------------------------------------------------------------- Supabase
def test_supabase_tools(env):
    http = env["http"]
    connect(env, "supabase")
    ref = "abcdefghijklmnopqrst"
    http.on("GET", r"api\.supabase\.com/v1/projects$", [{"id": ref, "name": "app", "region": "eu", "status": "ACTIVE_HEALTHY"}])
    http.on("GET", rf"/projects/{ref}/api-keys", [{"name": "anon", "api_key": "anon-key"},
                                                 {"name": "service_role", "api_key": "service-key"}])
    http.on("GET", rf"{ref}\.supabase\.co/rest/v1/$", {"paths": {"/": {}, "/todos": {}, "/rpc/fn": {}}})
    http.on("GET", rf"{ref}\.supabase\.co/rest/v1/todos\?", [{"id": 1, "title": "x"}])
    http.on("POST", rf"{ref}\.supabase\.co/rest/v1/todos", [{"id": 2, "title": "y"}])
    assert f"app [ref {ref}]" in run(env, "supabase_projects").content
    assert "todos" in run(env, "supabase_tables", project=ref).content
    rows = run(env, "supabase_select", project=ref, table="todos", columns="id,title", filters=["done=eq.false"], limit=5)
    assert rows.ok and '"title": "x"' in rows.content
    call = http.last("GET")
    assert "select=id,title" in call["url"] and "done=eq.false" in call["url"] and "limit=5" in call["url"]
    assert call["headers"]["apikey"] == "anon-key"                      # RLS applies by default
    assert run(env, "supabase_insert", project=ref, table="todos", rows=[{"title": "y"}]).ok
    post = http.last("POST")
    assert post["body"] == [{"title": "y"}] and post["headers"]["Prefer"] == "return=representation"
    assert not run(env, "supabase_select", project=ref, table="todos; drop", limit=1).ok
    assert not run(env, "supabase_select", project=ref, table="todos", filters=["x=evil.1"]).ok
    env["setups"]["supabase"].update({"keyRole": "service_role"})
    run(env, "supabase_select", project=ref, table="todos")
    assert http.last("GET")["headers"]["apikey"] == "service-key"
    with pytest.raises(ValueError):
        env["setups"]["supabase"].update({"keyRole": "root"})


# ----------------------------------------------------------------- Canva
def test_canva_tools(env):
    http = env["http"]
    connect(env, "canva")
    canva = env["connectors"].get("canva")
    canva._sleep = lambda s: None
    http.on("GET", r"/rest/v1/designs", {"items": [{"id": "DAF1", "title": "Poster",
                                                    "urls": {"edit_url": "https://canva.com/e"}}]})
    states = iter([{"job": {"id": "job1", "status": "in_progress"}},
                   {"job": {"id": "job1", "status": "success", "urls": ["https://export/1.pdf"]}}])
    http.on("POST", r"/rest/v1/exports$", lambda *a: next(states))
    http.on("GET", r"/rest/v1/exports/job1", lambda *a: next(states))
    assert "Poster [DAF1]" in run(env, "canva_designs", query="poster").content
    result = run(env, "canva_export", design_id="DAF1", format="pdf")
    assert result.ok and "https://export/1.pdf" in result.content
    assert http.calls[1]["body"] == {"design_id": "DAF1", "format": {"type": "pdf"}}
    assert decide(env, "canva_export", design_id="DAF1") == "allow"     # an export changes nothing


def test_granted_scopes_gate_tools(env):
    connect(env, "slack")
    env["tokens"].set("slack:scope", "channels:read,chat:write")
    assert env["registry"].get("slack_channels").available()
    assert env["registry"].get("slack_post").available()
    assert "not granted" in env["registry"].get("slack_history").unavailable_reason()


def test_disconnect_forgets_tokens(env):
    connect(env, "render")
    env["tokens"].set("render:refresh", "r")
    assert env["connectors"].get("render").disconnect() is True
    assert env["tokens"].get("render") is None and env["tokens"].get("render:refresh") is None


def test_setup_is_generic(env, tmp_path):
    setup = OAuthSetup(SERVICES["vercel"], settings=env["settings"], tokens=env["tokens"],
                       form_transport=env["http"], redirect_uri="http://127.0.0.1:8732/oauth/callback")
    with pytest.raises(ValueError):
        setup.update({"tools": ["github_repos"]})
    with pytest.raises(ValueError):
        setup.update({})
    view = setup.update({"clientId": "oac_1", "clientSecret": "vs"})
    assert view["clientSecretSet"] and "vs" not in str(view) and view["console"]
    assert "clientSecret" not in env["settings"].read("connectors")["vercel"]


# ----------------------------------------------------------- token mode
def test_cloudflare_token_mode(env):
    http, setup, tokens = env["http"], env["setups"]["cloudflare"], env["tokens"]
    cf = env["connectors"].get("cloudflare")
    tool = env["registry"].get("cloudflare_dns_create")
    assert setup.auth_mode() == "token" and setup.flow() is None or not setup.oauth_enabled
    assert "paste a Cloudflare API token" in tool.unavailable_reason() and "DNS:Edit" in tool.unavailable_reason()
    seen = []

    def verify(url, headers, body):
        seen.append(headers["Authorization"])
        good = headers["Authorization"] == "Bearer cf-good"
        return {"success": good, "errors": [] if good else [{"message": "Invalid API Token"}],
                "result": {"id": "tok123", "status": "active", "expires_on": "2030-01-01T00:00:00Z"} if good else None}

    http.on("GET", r"/user/tokens/verify", verify)
    http.on("GET", r"/accounts\?", {"success": True, "result": [{"id": "acc1", "name": "Shubh's Account"}]})
    http.on("GET", r"/user/tokens/tok123", {"success": True, "result": {"policies": [
        {"permission_groups": [{"name": "Zone Read"}, {"name": "DNS Write"}]}]}})
    with pytest.raises(ConnectorError):
        setup.save_token(cf, "cf-bad")
    assert tokens.get("cloudflare") is None                       # refused -> nothing stored
    with pytest.raises(ValueError):
        setup.save_token(cf, "has space")
    result = setup.save_token(cf, "cf-good")
    assert result == {"connected": True, "account": "Shubh's Account", "token_id": "tok123",
                      "scopes_or_permissions": ["Zone Read", "DNS Write"], "expires": "2030-01-01T00:00:00Z"}
    assert "cf-good" not in str(result) and "cf-good" not in str(setup.view())
    assert tokens.get("cloudflare") == "cf-good" and float(tokens.get("cloudflare:expires_at")) > 1.8e9
    view = setup.view()
    assert view["has_token"] and view["auth_mode"] == "token" and view["account"] == "Shubh's Account"
    assert view["scopes_or_permissions"] == ["Zone Read", "DNS Write"] and view["store"] == "memory"
    assert "warning" in view                                      # memory store warns too
    assert tool.available()
    # Same approval rules as OAuth; a 403 names the missing permission.
    env["settings"].write("permissions", {"mode": "allow-all"})
    assert decide(env, "cloudflare_dns_create", zone="z", type="A", name="a", content="1") == "ask"
    http.on("POST", r"/dns_records$", lambda *a: (_ for _ in ()).throw(
        ConnectorError("POST api.cloudflare.com answered 403: Authentication error")))
    failed = run(env, "cloudflare_dns_create", zone="0123456789abcdef0123456789abcdef", type="A", name="a", content="1")
    assert not failed.ok and "needs the permission DNS:Edit" in failed.content
    cf.forget()
    assert not setup.has_token() and tokens.get("cloudflare:account") is None


def test_cloudflare_token_without_self_read(env):
    http, setup = env["http"], env["setups"]["cloudflare"]
    http.on("GET", r"/user/tokens/verify", {"success": True, "result": {"id": "t9", "status": "active"}})
    result = setup.save_token(env["connectors"].get("cloudflare"), "cf-zone-only")
    assert result["account"] == "" and result["scopes_or_permissions"][0].startswith("unknown")
    http.on("GET", r"/user/tokens/verify", {"success": True, "result": {"id": "t9", "status": "disabled"}})
    with pytest.raises(ConnectorError, match="disabled"):
        setup.save_token(env["connectors"].get("cloudflare"), "cf-off")


def test_render_token_mode(env):
    http, setup = env["http"], env["setups"]["render"]
    assert "paste a Render API token" in env["registry"].get("render_services").unavailable_reason()
    http.on("GET", r"/v1/owners", lambda url, headers, body: [{"owner": {"id": "tea-1", "name": "Shubh", "type": "user"}}]
            if headers["Authorization"] == "Bearer rnd_ok" else (_ for _ in ()).throw(
                ConnectorError("GET api.render.com answered 401: unauthorized")))
    with pytest.raises(ConnectorError):
        setup.save_token(env["connectors"].get("render"), "rnd_bad")
    result = setup.save_token(env["connectors"].get("render"), "rnd_ok")
    assert result["account"] == "Shubh" and "not scoped" in result["scopes_or_permissions"][0]
    assert env["registry"].get("render_services").available()


def test_oauth_services_refuse_pasted_tokens(env):
    with pytest.raises(ValueError, match="OAuth"):
        env["setups"]["github"].save_token(env["connectors"].get("github"), "ghp_x")
    assert env["setups"]["github"].view()["auth_mode"] == "oauth"
