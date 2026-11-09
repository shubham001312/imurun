"""Native per-call permission levels in ``PermissionPolicy`` (connector / MCP
tools), approval cards that carry ``level`` / ``rememberable``, and the
connector aliases + prompt rule (docs/connectors/REQUESTS.md L1-L4, R1)."""
from __future__ import annotations

from dataclasses import dataclass, field

from services.tools.approvals import ApprovalBroker
from services.tools.calls import ALIASES
from services.tools.permissions import PermissionPolicy, level_of
from services.tools.protocol import tool_prompt
from services.tools.registry import ToolRegistry


class _Settings:
    def __init__(self, **values):
        self.values = {"permissions": dict(values)}

    def read(self, section):
        return dict(self.values.get(section, {}))

    def write(self, section, changes):
        self.values.setdefault(section, {}).update(changes)


@dataclass
class _LevelTool:
    name: str
    level: str
    category: str = "connector-write"
    requires_network: bool = True
    toggle: tuple | None = None
    description: str = "x"
    parameters: dict = field(default_factory=dict)

    def permission_level(self, args):
        if args.get("remove_label"):
            return "ask", "removes a label"
        return self.level, f"{self.name} level {self.level}"

    def summary(self):
        return self.description

    def available(self):
        return True


@dataclass
class _PlainTool:
    name: str
    category: str
    requires_network: bool = False
    description: str = "x"
    parameters: dict = field(default_factory=dict)

    def summary(self):
        return self.description


def _policy(mode="allow-reads", **extra):
    return PermissionPolicy(_Settings(mode=mode, **extra), workspace_override=".")


def test_level_table_per_mode():
    read, write = _LevelTool("c_read", "read", "connector-read"), _LevelTool("c_write", "write")
    ask, always = _LevelTool("c_ask", "ask"), _LevelTool("c_send", "always")
    allow, deny = _LevelTool("c_allow", "allow"), _LevelTool("c_deny", "deny")
    expect = {
        "ask": {"read": "ask", "write": "ask", "ask": "ask", "always": "ask", "allow": "allow", "deny": "deny"},
        "allow-reads": {"read": "allow", "write": "ask", "ask": "ask", "always": "ask", "allow": "allow", "deny": "deny"},
        "allow-all": {"read": "allow", "write": "allow", "ask": "ask", "always": "ask", "allow": "allow", "deny": "deny"},
    }
    for mode, table in expect.items():
        policy = _policy(mode)
        for tool in (read, write, ask, always, allow, deny):
            assert policy.decide(tool, {}, session_id="s").kind == table[tool.level], (mode, tool.name)
            assert policy.generic(tool) == table[tool.level], (mode, tool.name)


def test_argument_dependent_level_and_reason():
    policy = _policy("allow-all")
    tool = _LevelTool("c_modify", "write")
    assert policy.decide(tool, {"add_label": "x"}).kind == "allow"
    decision = policy.decide(tool, {"remove_label": "x"})
    assert decision.kind == "ask" and decision.reasons == ("removes a label",)


def test_always_tools_are_never_remembered_but_others_are():
    policy = _policy("ask")
    send, draft = _LevelTool("c_send", "always"), _LevelTool("c_draft", "write")
    assert not policy.rememberable(send, {}) and policy.rememberable(draft, {})
    policy.remember("s", send.name, send, {})
    policy.remember("s", draft.name, draft, {})
    assert policy.decide(send, {}, session_id="s").kind == "ask"
    assert policy.decide(draft, {}, session_id="s").kind == "allow"
    assert policy.decide(draft, {}, session_id="other").kind == "ask"
    assert not policy.rememberable(_PlainTool("file_delete", "delete"), {})
    assert policy.level(_PlainTool("shell", "shell")) == "write"
    assert policy.level(_PlainTool("time_now", "info")) == "allow"


def test_toggles_network_and_send_email():
    tool = _LevelTool("gmail_send", "always", toggle=("network", "sendEmail"))
    assert _policy("allow-all", sendEmail=False).decide(tool, {}).kind == "deny"
    assert _policy("allow-all", network=False).decide(tool, {}).kind == "deny"
    assert _policy("allow-all").decide(tool, {}).kind == "ask"
    mcp_http = _LevelTool("mcp_x", "read", "mcp")            # toggle None + network -> network toggle
    assert _policy("allow-all", network=False).decide(mcp_http, {}).kind == "deny"
    assert _policy("allow-all", network=False).generic(mcp_http) == "deny"
    mcp_stdio = _LevelTool("mcp_local", "read", "mcp", requires_network=False, toggle=())
    assert _policy("allow-all", network=False).decide(mcp_stdio, {}).kind == "allow"
    assert _policy("allow-all").update({"sendEmail": False})["sendEmail"] is False


def test_level_tool_without_probe_asks_and_broken_probe_asks():
    class NoProbe:
        name, category, requires_network = "c_raw", "connector-write", True

    class Broken(NoProbe):
        def permission_level(self, args):
            raise RuntimeError("boom")

    assert level_of(NoProbe()) == ("ask", "connector tool without a permission level")
    assert level_of(Broken())[0] == "ask"
    assert _policy("allow-all").decide(Broken(), {}).kind == "ask"


def test_approval_card_carries_level_and_rememberable():
    events = []
    registry = ToolRegistry()
    registry.register(_LevelTool("c_send", "always"))
    registry.register(_PlainTool("file_write", "write"))
    policy = _policy("ask")

    def levels(name, args):
        tool = registry.get(name)
        return policy.level(tool, args), policy.rememberable(tool, args)

    broker = ApprovalBroker(lambda kind, data: events.append((kind, data)) or 1, levels=levels)
    broker.request("c_send", {}, ("sends",), timeout=1)
    broker.request("file_write", {"path": "a"}, ("writes",), timeout=1)
    broker.request("file_write", {"path": "a"}, ("writes",), timeout=1, level="custom", rememberable=False)
    cards = [data for kind, data in events if kind == "tool.approval_needed"]
    assert cards[0]["level"] == "always" and cards[0]["rememberable"] is False
    assert cards[1]["level"] == "write" and cards[1]["rememberable"] is True
    assert cards[2]["level"] == "custom" and cards[2]["rememberable"] is False
    assert broker.pending()[0]["rememberable"] is False


def test_registry_unregister():
    registry = ToolRegistry()
    tool = registry.register(_PlainTool("x", "info"))
    assert registry.unregister("x") is tool and registry.get("x") is None
    assert registry.unregister("x") is None
    assert not registry.execute("x").ok


def test_connector_aliases_and_prompt_rule():
    assert ALIASES["send_email"] == "gmail_send" and ALIASES["read_email"] == "gmail_read"
    assert ALIASES["create_issue"] == "github_create_issue" and ALIASES["slack_message"] == "slack_post"
    plain = tool_prompt([_PlainTool("time_now", "info")], max_steps=4)
    assert "accounts" not in plain and "gmail_draft" not in plain
    tools = [_LevelTool("gmail_send", "always"), _LevelTool("gmail_draft", "write")]
    prompt = tool_prompt(tools, max_steps=4)
    assert "Never claim you sent, posted, deployed or changed something" in prompt
    assert "prefer gmail_draft" in prompt
