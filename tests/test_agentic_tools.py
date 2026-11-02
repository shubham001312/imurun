"""File and shell tools in a temporary workspace."""
from __future__ import annotations

import os

import pytest

from services.tools.builtin.files import (
    FileCopyTool,
    FileDeleteTool,
    FileListTool,
    FileMoveTool,
    FileReadTool,
    FileWriteTool,
    MakeDirTool,
)
from services.tools.builtin.shell import ShellTool
from services.tools.builtin.web_fetch import extract_text
from services.tools.workspace import Workspace


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace.fixed(root), root


def test_write_modes(ws):
    workspace, root = ws
    write = FileWriteTool(workspace)
    assert write.run({"path": "notes/a.txt", "content": "one"}).ok
    assert (root / "notes" / "a.txt").read_text() == "one"
    assert write.run({"path": "notes/a.txt", "content": "+two", "mode": "append"}).ok
    assert (root / "notes" / "a.txt").read_text() == "one+two"
    again = write.run({"path": "notes/a.txt", "content": "x", "mode": "create"})
    assert not again.ok and "already exists" in again.content
    over = write.run({"path": "notes/a.txt", "content": "new"})
    assert over.ok and over.data["existed"] and (root / "notes" / "a.txt").read_text() == "new"
    assert not write.run({"path": "../escape.txt", "content": "x"}).ok


def test_read_offset_truncation_and_binary(ws):
    workspace, root = ws
    (root / "big.txt").write_text("abcdefghij" * 10)
    read = FileReadTool(workspace)
    first = read.run({"path": "big.txt", "max_chars": 15})
    assert first.ok and first.data["truncated"] and "offset 15" in first.content
    second = read.run({"path": "big.txt", "offset": 95, "max_chars": 50})
    assert second.content.endswith("fghij")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert not read.run({"path": "bin.dat"}).ok
    assert not read.run({"path": "missing.txt"}).ok
    assert not read.run({"path": "."}).ok  # a folder


def test_list_tree(ws):
    workspace, root = ws
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "deep.txt").write_text("x")
    (root / "top.txt").write_text("hi")
    listing = FileListTool(workspace)
    shallow = listing.run({})
    assert shallow.ok and "a/" in shallow.content and "deep.txt" not in shallow.content
    deep = listing.run({"depth": 3})
    assert "deep.txt" in deep.content
    assert {e["path"] for e in deep.data["entries"]} >= {"a", "a/b", "a/b/deep.txt", "top.txt"}


def test_make_move_copy_delete(ws):
    workspace, root = ws
    assert MakeDirTool(workspace).run({"path": "x/y"}).ok and (root / "x" / "y").is_dir()
    (root / "x" / "f.txt").write_text("data")
    assert FileCopyTool(workspace).run({"src": "x/f.txt", "dst": "x/g.txt"}).ok
    assert not FileCopyTool(workspace).run({"src": "x/f.txt", "dst": "x/g.txt"}).ok  # exists
    assert FileCopyTool(workspace).run({"src": "x/f.txt", "dst": "x/g.txt", "overwrite": True}).ok
    assert FileMoveTool(workspace).run({"src": "x/g.txt", "dst": "moved.txt"}).ok
    assert (root / "moved.txt").read_text() == "data" and not (root / "x" / "g.txt").exists()
    assert FileMoveTool(workspace).run({"src": "x/f.txt", "dst": "x/y"}).ok  # into a folder
    assert (root / "x" / "y" / "f.txt").exists()
    delete = FileDeleteTool(workspace)
    refused = delete.run({"path": "x"})
    assert not refused.ok and "recursive" in refused.content
    assert delete.run({"path": "x", "recursive": True}).ok and not (root / "x").exists()
    assert not delete.run({"path": "."}).ok  # never the workspace itself


def test_shell_runs_in_workspace(ws):
    workspace, root = ws
    (root / "marker.txt").write_text("m")
    shell = ShellTool(workspace)
    listing = "Get-ChildItem -Name" if os.name == "nt" else "ls"
    result = shell.run({"command": listing})
    assert result.ok, result.content
    assert "marker.txt" in result.content and result.data["returncode"] == 0


def test_shell_timeout_kills(ws):
    workspace, _ = ws
    sleep = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
    result = ShellTool(workspace).run({"command": sleep, "timeout": 2})
    assert not result.ok and result.data["timed_out"]


def test_shell_denies_and_scrubs_secrets(ws, monkeypatch):
    workspace, _ = ws
    shell = ShellTool(workspace)
    denied = shell.run({"command": "format c:"})
    assert not denied.ok and "deny-list" in denied.content
    monkeypatch.setenv("IMURUN_TEST_API_KEY", "s3cr3t-value")
    echo = "Write-Output \"[$env:IMURUN_TEST_API_KEY]\"" if os.name == "nt" else 'echo "[$IMURUN_TEST_API_KEY]"'
    result = shell.run({"command": echo})
    assert result.ok and "s3cr3t-value" not in result.content
    assert not shell.run({"command": "echo x", "cwd": ".."}).ok


def test_shell_nonzero_exit_is_a_failed_result(ws):
    workspace, _ = ws
    result = ShellTool(workspace).run({"command": "exit 3"})
    assert not result.ok and result.data["returncode"] == 3


def test_blocked_search_is_a_failure_not_an_empty_answer():
    # web_search is the research engine now: every source refusing (bot
    # check) must surface as a failure, never as "nothing found".
    from services.research.engine import ResearchEngine
    from services.research.sources import Source, SourceError
    from services.tools.builtin import web_search as ws_mod

    class Refusing(Source):
        name = "refusing"

        def search(self, query, plan, fetcher, timeout):
            raise SourceError("refusing: challenge page", blocked=True)

    engine = ResearchEngine(sources=[Refusing()], fetcher=object(), online=lambda: True)
    result = ws_mod.WebSearchTool(engine=engine).run({"query": "latest news"})
    assert not result.ok and "bot check" in result.content


def test_web_fetch_text_extraction():
    page = ("<html><head><title>T</title><style>.x{}</style></head><body><nav>menu</nav>"
            "<h1>Hello</h1><p>World &amp; more</p><script>alert(1)</script></body></html>")
    text = extract_text(page, None)
    assert "Hello" in text and "World & more" in text
    assert "alert" not in text and "menu" not in text and ".x" not in text
