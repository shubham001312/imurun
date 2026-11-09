"""Plan mode: look, don't touch. The composer's PLAN toggle used to change
nothing (the mode was only counted in usage stats)."""
from __future__ import annotations

import json

from services.response.loop import AgentLoop
from services.server.settings import SettingsStore
from services.tools.base import Tool, ToolResult
from services.tools.executor import ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


def call(name, **args):
    return "```tool\n" + json.dumps({"name": name, "args": args}) + "\n```"


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "(out of script)"


def tool(name, category):
    class T(Tool):
        description = f"{name} tool"
        parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

        def __init__(self):
            self.calls = 0

        def run(self, args):
            self.calls += 1
            return ToolResult(ok=True, content=f"{name} ran")
    T.name, T.category = name, category
    return T()


def setup(tmp_path):
    reader, writer, shell = tool("lister", "read"), tool("writer", "info"), tool("runner", "info")
    writer.category, shell.category = "write", "shell"
    registry = ToolRegistry()
    registry.extend([reader, writer, shell])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws"), "mode": "allow-all"})
    return ToolExecutor(registry, policy=PermissionPolicy(settings)), reader, writer, shell


def test_plan_mode_offers_only_reading_tools_and_asks_for_a_plan(tmp_path):
    executor, *_ = setup(tmp_path)
    model = FakeModel(["1. Goal ... Switch to Build to carry out this plan."])
    AgentLoop(model, executor=executor, session_id="s", mode="plan").run("make a website on my desktop")
    system = model.seen[0][0]["content"]
    assert "# Plan mode" in system and "- lister" in system
    assert "- writer" not in system and "- runner" not in system


def test_plan_mode_refuses_writes_even_when_the_model_names_them(tmp_path):
    executor, reader, writer, shell = setup(tmp_path)
    model = FakeModel([call("writer", x="index.html"), call("runner", x="rm"), "Here is the plan."])
    AgentLoop(model, executor=executor, session_id="s", mode="plan").run("build it")
    assert writer.calls == 0 and shell.calls == 0  # enforced by the executor, not just the prompt


def _bridge_setup(tmp_path):
    shell = tool("shell", "shell")
    registry = ToolRegistry()
    registry.extend([shell])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws"), "mode": "allow-all"})
    return ToolExecutor(registry, policy=PermissionPolicy(settings)), shell


def test_plan_mode_skips_the_command_bridge(tmp_path):
    executor, shell = _bridge_setup(tmp_path)
    AgentLoop(FakeModel(["plan"]), executor=executor, session_id="s", mode="plan", compact=True).run("!ls")
    assert shell.calls == 0


def test_build_mode_runs_the_bridged_command(tmp_path):
    """Control for the test above: the same setup runs the command in Build mode."""
    executor, shell = _bridge_setup(tmp_path)
    AgentLoop(FakeModel(["done"]), executor=executor, session_id="s", mode="build", compact=True).run("!ls")
    assert shell.calls == 1


def test_build_mode_is_unchanged(tmp_path):
    executor, _, writer, _ = setup(tmp_path)
    model = FakeModel([call("writer", x="index.html"), "Wrote it."])
    result = AgentLoop(model, executor=executor, session_id="s", mode="build").run("write index.html")
    assert writer.calls == 1 and result.answer == "Wrote it."
    assert "# Plan mode" not in model.seen[0][0]["content"]
