"""User report 2026-11-09 ("this site is not proper, fix it"): Nemotron read the
files, ended with "Let me fix both." and stopped; earlier a file_write cut off
by the output limit was "repaired" and wrote half a CSS file to disk."""
from __future__ import annotations

import pytest

from services.response.loop import AgentLoop, _announced_work
from services.server.settings import SettingsStore
from services.tools.base import Tool, ToolResult
from services.tools.calls import CUT_OFF, parse_tool_calls
from services.tools.executor import ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


@pytest.mark.parametrize("text", [
    "The CSS is incomplete and there is no script.js. Let me fix both.",
    "I'll create script.js next.", "Now I'll run the tests.",
])
def test_announced_but_not_done(text):
    assert _announced_work(text)


@pytest.mark.parametrize("text", [
    "Done. I fixed the CSS and created script.js.", "Want me to fix it?",
    "Let me know if you need anything else.", "Run npm install, then npm start.",
])
def test_finished_answers_are_left_alone(text):
    assert not _announced_work(text)


class Writer(Tool):
    name = "file_write"
    category = "info"  # no approval card in the test
    description = "Write a file."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path"]}

    def __init__(self):
        self.writes = []

    def run(self, args):
        self.writes.append(args)
        return ToolResult(ok=True, content="written")


class Model:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def generate(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "(out of script)"


def test_the_loop_sends_it_back_to_do_what_it_announced(tmp_path):
    writer = Writer()
    registry = ToolRegistry()
    registry.extend([writer])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    model = Model([
        "The CSS is cut off. Let me fix it.",
        '```tool\n{"name": "file_write", "args": {"path": "style.css", "content": "a {}"}}\n```',
        "Fixed: style.css is complete now.",
    ])
    loop = AgentLoop(model, executor=ToolExecutor(registry, policy=PermissionPolicy(settings)),
                     session_id="s", turn_id="t", max_steps=6)
    result = loop.run("this site is not proper, fix it")
    assert writer.writes == [{"path": "style.css", "content": "a {}"}]
    assert result.answer == "Fixed: style.css is complete now."
    notes = [m["content"] for m in model.seen[1] if m["role"] == "user"]
    assert any("did not do it" in n for n in notes)


def test_a_cut_off_file_write_is_not_run():
    reply = parse_tool_calls('```tool\n{"name": "file_write", "args": {"path": "a.css", '
                             '"content": ".x { transform: translateY(-', {"file_write": {}})
    assert reply.calls == [] and reply.error == CUT_OFF


def test_the_name_before_the_arguments_is_understood():
    reply = parse_tool_calls('```tool\nfile_list {"path": "~/Desktop/site", "depth": 1}\n```',
                             {"file_list": {}})
    assert [(c.name, c.args) for c in reply.calls] == [("file_list", {"path": "~/Desktop/site", "depth": 1})]
