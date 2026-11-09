"""McpManager + bridge: settings -> stdio servers -> mcp_<id>_<tool> model tools."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from services.connectors.tool_policy import ConnectorToolPolicy
from services.mcp.registry import REDACTED, McpManager, ServerConfig, expand
from services.server.settings import SettingsStore
from services.tools.builtin.mcp_bridge import bridged_name
from services.tools.executor import CallContext, ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent
FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def fake_server(server_id="fake", mode="normal", **extra):
    return {"id": server_id, "name": "Fake", "transport": "stdio", "command": "${python}",
            "args": [FAKE, mode], "enabled": True, "timeout": 2, **extra}


@pytest.fixture()
def env(tmp_path):
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    registry = ToolRegistry()
    events: list[tuple[str, dict]] = []
    manager = McpManager(settings, registry, publish=lambda k, d: events.append((k, d)), home=tmp_path)
    policy = ConnectorToolPolicy(PermissionPolicy(settings))
    executor = ToolExecutor(registry, policy=policy)
    yield {"settings": settings, "registry": registry, "manager": manager, "events": events,
           "policy": policy, "executor": executor, "home": tmp_path}
    manager.close_all()


def run(env, name, **args):
    ctx = CallContext(session_id="s1", approved=True, interactive=False, source="user")
    return env["executor"].execute(name, args, ctx).result


def test_default_example_is_the_builtin_gmail_server_and_off(env):
    configs = env["manager"].configs()
    assert [c.id for c in configs] == ["gmail", "connectors"]
    assert not any(c.enabled for c in configs) and configs[0].transport == "stdio"
    assert expand(configs[0].args[0], env["home"]).endswith("gmail_mcp.py")
    env["manager"].sync(background=False)
    assert {v["status"] for v in env["manager"].servers_view()} == {"disabled"}
    assert env["registry"].names() == []


def test_names_and_validation():
    assert bridged_name("my-server", "Wipe-Disk") == "mcp_my_server_wipe_disk"
    assert bridged_name("x", "a.b/c d") == "mcp_x_a_b_c_d"
    for bad in ({"id": "Bad Id", "command": "x"}, {"id": "ok", "transport": "ws"},
                {"id": "ok", "transport": "http"}, {"id": "ok", "transport": "http", "url": "http://example.com"},
                {"id": "ok"}, {"id": "ok", "command": "x", "args": "notalist"}):
        with pytest.raises(ValueError):
            ServerConfig.parse(bad)


def test_connect_discovers_and_registers_tools(env):
    manager = env["manager"]
    view = manager.upsert(fake_server(), connect=False)
    assert view["id"] == "fake"
    manager.sync(background=False)
    view = manager.connect("fake")
    assert view["status"] == "connected" and view["tools"] == 5
    assert view["protocolVersion"] == "2025-06-18"
    names = set(env["registry"].names())
    assert {"mcp_fake_echo", "mcp_fake_add", "mcp_fake_slow", "mcp_fake_boom", "mcp_fake_wipe_disk"} <= names
    assert ("mcp.connected", {"id": "fake", "name": "Fake", "tools": 5, "protocolVersion": "2025-06-18",
                              "server": "fake"}) in env["events"]
    echo = env["registry"].get("mcp_fake_echo")
    assert echo.parameters["required"] == ["text"] and echo.description.startswith("[MCP Fake]")
    assert {t.name for t in env["executor"].offered()} >= {"mcp_fake_echo", "mcp_fake_add"}

    result = run(env, "mcp_fake_echo", text="hello")
    assert result.ok and "echo: hello" in result.content and result.source == "mcp:Fake"
    result = run(env, "mcp_fake_add", a="2", b=3)            # schema coercion by the executor
    assert result.ok and result.content == "5.0" and result.data == {"sum": 5.0}
    failed = run(env, "mcp_fake_boom")
    assert not failed.ok and "it broke" in failed.content
    timed = run(env, "mcp_fake_slow")
    assert not timed.ok and "timed out" in timed.content


def test_permission_levels_from_annotations_and_auto_approve(env):
    manager = env["manager"]
    manager.upsert(fake_server(auto_approve=["add", "Wipe-Disk"]), connect=False)
    manager.sync(background=False)
    manager.connect("fake")
    policy, registry, settings = env["policy"], env["registry"], env["settings"]

    def kind(name, **args):
        return policy.decide(registry.get(name), args, session_id="s1").kind

    assert kind("mcp_fake_echo", text="x") == "allow"           # readOnlyHint, mode allow-reads
    assert kind("mcp_fake_add") == "allow"                      # auto_approve
    assert kind("mcp_fake_boom") == "ask"                       # no hints -> ask
    assert kind("mcp_fake_wipe_disk") == "ask"                  # destructive beats auto_approve
    settings.write("permissions", {"mode": "allow-all"})
    assert kind("mcp_fake_boom") == "ask"                       # mcp asks even in allow-all
    assert kind("mcp_fake_wipe_disk") == "ask"
    policy.remember("s1", "mcp_fake_boom")
    assert kind("mcp_fake_boom") == "allow"                     # "remember" works for plain ask
    policy.remember("s1", "mcp_fake_wipe_disk")
    assert kind("mcp_fake_wipe_disk") == "ask"                  # but never for destructive
    settings.write("permissions", {"mode": "ask"})
    assert kind("mcp_fake_echo", text="x") == "ask"
    # Stdio MCP tools do not depend on the network toggle.
    settings.write("permissions", {"network": False})
    assert kind("mcp_fake_add") == "allow"


def test_config_change_rediscovers_and_disable_retires(env):
    manager, registry = env["manager"], env["registry"]
    manager.upsert(fake_server(), connect=False)
    manager.sync(background=False)
    manager.connect("fake")
    assert registry.get("mcp_fake_echo").available()
    manager.upsert({"id": "fake", "enabled": False})
    assert registry.get("mcp_fake_echo") is None                 # unregistered (ToolRegistry.unregister)
    assert any(kind == "mcp.disconnected" for kind, _ in env["events"])
    assert "mcp_fake_echo" not in {t.name for t in env["executor"].offered()}
    assert not run(env, "mcp_fake_echo", text="x").ok
    manager.upsert({"id": "fake", "enabled": True}, connect=False)
    manager.sync(background=False)
    assert manager.server_view("fake")["status"] == "connected"
    assert registry.get("mcp_fake_echo").available()
    assert manager.delete("fake") and manager.server_view("fake") is None


def test_lazy_reconnect_after_the_server_dies(env):
    manager = env["manager"]
    manager.upsert(fake_server(), connect=False)
    manager.sync(background=False)
    manager.connect("fake")
    state = manager._states["fake"]
    state.client.transport._process.kill()
    state.client.transport._process.wait()
    result = run(env, "mcp_fake_add", a=1, b=2)                # reconnects on use
    assert result.ok and result.content == "3.0"


def test_connect_failure_is_reported(env):
    manager = env["manager"]
    manager.upsert(fake_server("broken", "future"), connect=False)
    manager.sync(background=False)
    view = manager.server_view("broken")
    assert view["status"] == "error" and "2099" in view["error"]
    assert any(kind == "mcp.error" and data["id"] == "broken" for kind, data in env["events"])


def test_secrets_are_redacted_and_kept(env, monkeypatch):
    manager = env["manager"]
    manager.upsert(fake_server(env={"API_KEY": "s3cret"}, enabled=False), connect=False)
    view = manager.server_view("fake")
    assert view["env"] == {"API_KEY": REDACTED} and "s3cret" not in json.dumps(manager.servers_view())
    manager.upsert({"id": "fake", "env": {"API_KEY": REDACTED}, "name": "Renamed"})
    stored = env["settings"].read("mcp")["servers"]
    assert next(s for s in stored if s["id"] == "fake")["env"] == {"API_KEY": "s3cret"}
    monkeypatch.setenv("IMURUN_TEST_TOKEN", "from-env")
    assert expand("${env:IMURUN_TEST_TOKEN}", env["home"]) == "from-env"


# ----------------------------------------------------- built-in Gmail server
def test_builtin_gmail_mcp_server_over_stdio(tmp_path):
    """The shipped server speaks MCP: initialize, tools/list, tools/call (not connected)."""
    env = {"IMURUN_TOKEN_STORE": "memory", "PYTHONIOENCODING": "utf-8"}
    import os

    process = subprocess.Popen(
        [sys.executable, str(ROOT / "services" / "mcp" / "servers" / "gmail_mcp.py"), "--home", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **env}, cwd=str(tmp_path),
    )
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "gmail_search", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ]
    payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
    out, err = process.communicate(payload, timeout=60)
    replies = {r["id"]: r for r in (json.loads(l) for l in out.decode().splitlines() if l.strip())}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18", err
    tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
    assert set(tools) == {"gmail_search", "gmail_read", "gmail_labels", "gmail_draft",
                          "gmail_send", "gmail_modify", "gmail_trash"}
    assert tools["gmail_search"]["annotations"]["readOnlyHint"] is True
    assert tools["gmail_send"]["annotations"]["destructiveHint"] is True
    assert tools["gmail_read"]["inputSchema"]["required"] == ["message_id"]
    call = replies[3]["result"]
    assert call["isError"] is True and "not" in call["content"][0]["text"].lower()
    assert replies[4]["error"]["code"] == -32602


def test_builtin_gmail_server_through_the_bridge(env):
    """End to end: IMURUN's MCP client -> gmail_mcp.py -> bridged tools with honest permissions."""
    manager = env["manager"]
    manager.upsert({"id": "gmail", "enabled": True,
                    "env": {"IMURUN_TOKEN_STORE": "memory"}, "args": [
                        "${repo}/services/mcp/servers/gmail_mcp.py", "--home", "${home}"], "timeout": 60}, connect=False)
    manager.sync(background=False)
    view = manager.server_view("gmail")
    assert view["status"] == "connected", view
    assert view["serverInfo"]["name"] == "imurun-gmail" and view["tools"] == 7
    policy, registry = env["policy"], env["registry"]
    env["settings"].write("permissions", {"mode": "allow-all"})
    assert policy.decide(registry.get("mcp_gmail_gmail_search"), {}).kind == "allow"
    assert policy.decide(registry.get("mcp_gmail_gmail_send"), {}).kind == "ask"
    assert policy.decide(registry.get("mcp_gmail_gmail_trash"), {}).kind == "ask"
    result = run(env, "mcp_gmail_gmail_search", query="x")
    assert not result.ok and "not" in result.content.lower()


def test_builtin_connectors_server_lists_every_service(tmp_path):
    import os

    process = subprocess.Popen(
        [sys.executable, str(ROOT / "services" / "mcp" / "servers" / "connectors_mcp.py"), "--home", str(tmp_path),
         "--services", "github,slack,cloudflare"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "IMURUN_TOKEN_STORE": "memory"}, cwd=str(tmp_path))
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "github_repos", "arguments": {}}}]
    out, err = process.communicate("".join(json.dumps(l) + "\n" for l in lines).encode(), timeout=60)
    replies = {r["id"]: r for r in (json.loads(l) for l in out.decode().splitlines() if l.strip())}
    assert replies[1]["result"]["serverInfo"]["name"] == "imurun-connectors", err
    tools = {t["name"]: t["annotations"] for t in replies[2]["result"]["tools"]}
    assert {"github_repos", "slack_post", "cloudflare_dns_delete"} <= set(tools)
    assert not any(name.startswith(("gmail_", "canva_")) for name in tools)
    assert tools["github_repos"]["readOnlyHint"] is True
    assert tools["slack_post"]["destructiveHint"] is True and tools["cloudflare_dns_delete"]["destructiveHint"] is True
    assert replies[3]["result"]["isError"] is True and "GitHub" in replies[3]["result"]["content"][0]["text"]
