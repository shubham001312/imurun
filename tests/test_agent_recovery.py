"""Agent recovery: the command bridge, up-to-3 retries, and the issue log.

User requirement: running commands on this computer must work; when a step
fails it is retried up to three times, and every problem is written to
logs/agent-issues.log so it can be fixed.
"""
from __future__ import annotations

import json

import pytest

from services.response import issues
from services.response.bridge import extract_command
from services.response.loop import MAX_RETRIES, AgentLoop
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


class FakeShell(Tool):
    """Stands in for the shell tool. ``category`` is 'info' so no approval card waits."""
    name = "shell"
    category = "info"
    description = "Run a shell command."
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def __init__(self, outcomes=None):
        self.commands: list[str] = []
        self.outcomes = list(outcomes or [])

    def run(self, args):
        self.commands.append(args["command"])
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "raise":
            raise OSError("could not start the process")
        if outcome == "denied":
            return ToolResult(ok=False, content="Permission denied for shell: off",
                              error="Permission denied for shell: off")
        if outcome == "exit3":
            return ToolResult(ok=False, content="(exit code 3)", error="exit code 3")
        return ToolResult(ok=True, content=f"$ {args['command']}\n(exit code 0)\nfile-a\nfile-b")


@pytest.fixture()
def log(tmp_path, monkeypatch):
    path = tmp_path / "issues.log"
    monkeypatch.setenv("IMURUN_ISSUES_LOG", str(path))

    def read():
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return read


def make(tmp_path, shell, *, run_shell=True):
    registry = ToolRegistry()
    registry.extend([shell])
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws"), "runShell": run_shell})
    return ToolExecutor(registry, policy=PermissionPolicy(settings))


def loop(model, executor, **kw):
    return AgentLoop(model, executor=executor, session_id="s", turn_id="t", **kw)


# ------------------------------------------------------------------ bridge

@pytest.mark.parametrize("prompt, command", [
    ("!ls ~/Desktop", "ls ~/Desktop"),
    ("!git status", "git status"),
    ("run `npm test`", "npm test"),
    ("please run `ls -la` and tell me what is there", "ls -la"),
    ("Execute: `pytest -q`", "pytest -q"),
    ("run this\n```bash\nls\npwd\n```", "ls\npwd"),
    ("run this\n```\necho hi\n```", "echo hi"),
])
def test_bridge_recognises_explicit_commands(prompt, command):
    assert extract_command(prompt) == command


@pytest.mark.parametrize("prompt", [
    "what does ls do?", "I ran `ls` yesterday and it was slow", "!", "",
    "run this\n```python\nprint(1)\n```", "!echo a\necho b",  # a hidden second line is not run
    # Security review L2: punctuation, not commands.
    "!important", "! wow that worked", "!!!", "$ git status", "!Nice job",
])
def test_bridge_never_guesses(prompt):
    assert extract_command(prompt) is None


def test_small_model_gets_the_command_run_and_explains_it(tmp_path, log):
    shell = FakeShell()
    model = FakeModel(["Your Desktop has file-a and file-b."])
    result = loop(model, make(tmp_path, shell), compact=True).run("!ls ~/Desktop")
    assert shell.commands == ["ls ~/Desktop"]
    first_user = [m for m in model.seen[0] if m["role"] == "user"][-1]["content"]
    assert "IMURUN ran it for them" in first_user and "file-a" in first_user
    assert result.answer == "Your Desktop has file-a and file-b."
    assert log() == []


def test_bridge_retries_a_command_that_could_not_start(tmp_path, log):
    shell = FakeShell(["raise", "raise", "ok"])
    loop(FakeModel(["done"]), make(tmp_path, shell), compact=True).run("!make build")
    assert shell.commands == ["make build"] * 3
    assert [e["kind"] for e in log()] == ["bridge-retry", "bridge-retry"]


def test_bridge_gives_up_after_three_start_failures_and_logs_it(tmp_path, log):
    shell = FakeShell(["raise"] * 5)
    model = FakeModel(["It could not start."])
    loop(model, make(tmp_path, shell), compact=True).run("!make build")
    assert len(shell.commands) == MAX_RETRIES
    kinds = [e["kind"] for e in log()]
    assert kinds == ["bridge-retry", "bridge-retry", "bridge-failed"]
    assert "FAILED" in [m for m in model.seen[0] if m["role"] == "user"][-1]["content"]


def test_bridge_does_not_rerun_a_command_that_ran_and_failed(tmp_path, log):
    shell = FakeShell(["exit3"])
    loop(FakeModel(["It exited with 3."]), make(tmp_path, shell), compact=True).run("!false")
    assert shell.commands == ["false"]  # side effects are never repeated automatically
    assert [e["kind"] for e in log()] == ["bridge-failed"]


def test_bridge_respects_the_shell_switch(tmp_path, log):
    shell = FakeShell()
    shell.category = "shell"
    loop(FakeModel(["Shell is turned off."]), make(tmp_path, shell, run_shell=False),
         compact=True).run("!ls")
    assert shell.commands == []  # denied by policy: not run, not retried
    assert [e["kind"] for e in log()] == ["bridge-failed"]


# ------------------------------------------------------- loop recovery

def test_refusal_while_tools_are_offered_is_sent_back(tmp_path, log):
    shell = FakeShell()
    model = FakeModel(["I'm sorry, I can't run commands on your computer.",
                       call("shell", command="ls"), "There are two files."])
    result = loop(model, make(tmp_path, shell)).run("what files are in this folder?")
    assert [s.kind for s in result.steps] == ["retry", "tool", "answer"]
    assert shell.commands == ["ls"] and result.answer == "There are two files."
    note = model.seen[1][-1]["content"]
    assert note.startswith("[note] You do have tools") and "shell" in note
    assert [e["kind"] for e in log()] == ["refusal"]


def test_refusals_stop_after_three(tmp_path, log):
    model = FakeModel(["I cannot access your files."] * 5)
    result = loop(model, make(tmp_path, FakeShell())).run("list my files")
    assert len(model.seen) == MAX_RETRIES + 1 and result.status == "done"
    assert [e["kind"] for e in log()] == ["refusal"] * MAX_RETRIES


def test_failed_tool_gets_three_attempts_then_a_report(tmp_path, log):
    shell = FakeShell(["exit3", "exit3", "exit3"])
    model = FakeModel([call("shell", command="make a"), call("shell", command="make b"),
                       call("shell", command="make c"), "It failed three times: exit code 3."])
    result = loop(model, make(tmp_path, shell)).run("build it")
    assert shell.commands == ["make a", "make b", "make c"]
    notes = [m["content"] for m in model.seen[3] if m["content"].startswith("[note]")]
    assert "attempt 1 of 3" in notes[0] and "attempt 2 of 3" in notes[1]
    assert "has failed 3 times" in notes[2]
    assert [e["kind"] for e in log()] == ["tool-failed", "tool-failed", "tool-failed", "retries-exhausted"]
    assert result.answer.startswith("It failed three times")


def test_denied_tool_is_not_retried(tmp_path, log):
    shell = FakeShell(["denied"])
    model = FakeModel([call("shell", command="ls"), "Shell is off."])
    loop(model, make(tmp_path, shell)).run("list files")
    assert shell.commands == ["ls"]
    assert not [m for m in model.seen[1] if m["content"].startswith("[note]")]
    assert log() == []


# ----------------------------------------------------------- issue log

def test_issue_log_scrubs_secrets_and_caps_fields(tmp_path):
    path = tmp_path / "x.log"
    issues.record("tool-failed", tool="shell", error="Authorization: Bearer abcdefghijklmnop1234",
                  detail="export OPENAI_KEY=sk-abcdefghijklmnopqrstuv " + "y" * 5000, path=path)
    entry = json.loads(path.read_text())
    assert "abcdefghijklmnop1234" not in path.read_text() and "sk-abcdef" not in path.read_text()
    assert entry["error"].endswith("Bearer ****") and len(entry["detail"]) < 700


def test_issue_log_rotates(tmp_path, monkeypatch):
    path = tmp_path / "x.log"
    monkeypatch.setattr(issues, "MAX_BYTES", 200)
    for i in range(10):
        issues.record("tool-failed", error="e" * 50, attempt=i, path=path)
    assert path.with_name("x.log.1").exists() and path.stat().st_size < 600


# ------------------------------------------- the user's own transcript

def test_virtual_assistant_refusal_from_the_field_is_caught(tmp_path, log):
    shell = FakeShell()
    model = FakeModel([
        "As a virtual assistant, I don't have direct access to your physical desktop.",
        call("shell", command="ls ~/Desktop"), "Your Desktop has file-a and file-b."])
    result = loop(model, make(tmp_path, shell)).run("whats on my desktop")
    assert shell.commands == ["ls ~/Desktop"] and result.answer.startswith("Your Desktop")
    assert [e["kind"] for e in log()] == ["refusal"]


def test_explaining_instead_of_doing_is_sent_back(tmp_path, log):
    shell = FakeShell()
    model = FakeModel([
        "I can guide you through the process of creating a simple website. Step 1: ...",
        call("shell", command="mkdir -p ~/Desktop/demo"), "Created ~/Desktop/demo with the site."])
    result = loop(model, make(tmp_path, shell)).run(
        "create a website for me (demo) in a folder of desktop in html css")
    assert shell.commands == ["mkdir -p ~/Desktop/demo"]
    note = model.seen[1][-1]["content"]
    assert note.startswith("[note] The user asked you to do this on their computer")
    assert [e["kind"] for e in log()] == ["explained-instead-of-acting"]
    assert result.answer.startswith("Created")


def test_plain_questions_are_never_pushed_to_use_tools(tmp_path, log):
    model = FakeModel(["CSS grid lays out items in rows and columns."])
    result = loop(model, make(tmp_path, FakeShell())).run("explain how css grid works")
    assert len(model.seen) == 1 and result.answer.startswith("CSS grid")
    assert log() == []
