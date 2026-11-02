"""Run one shell command in the workspace.

What this tool guarantees (and what it does not):

* the working directory is the workspace, or a folder inside it;
* commands matching the destructive deny-list
  (``safety.decisions.DANGEROUS_COMMANDS``) are refused before they start -
  the permission policy checks this, and the tool checks again so a direct
  ``run`` cannot skip it;
* a timeout (default 30 s, max 300 s) kills the whole process tree, not just
  the shell, so a hung child cannot outlive the turn;
* output is capped (``MAX_OUTPUT`` characters, head and tail kept);
* stdin is closed, and environment variables whose names look like
  credentials (``*TOKEN*``, ``*KEY*``, ``*SECRET*``, ``*PASSWORD*``...) are
  removed from the child's environment so a proposed command cannot read
  the user's API keys.

It is **not** a sandbox: a command can reach any file the user can. That is
why every shell call asks for approval unless the user chose ``allow-all``.

The file tools that used to live here moved to ``files.py``; they are
re-exported for old imports.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from typing import Any

from ..base import Tool, ToolResult, failed
from ..workspace import Workspace, WorkspaceError, default_workspace
from .files import FileListTool, FileReadTool, FileWriteTool  # noqa: F401 - re-exported

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|_KEY$|CREDENTIAL|COOKIE|SESSION)", re.IGNORECASE)


def shell_name() -> str:
    return "PowerShell" if os.name == "nt" else "sh"


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


def _cap(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-(limit // 3):]
    return f"{head}\n... [{len(text) - len(head) - len(tail)} characters cut] ...\n{tail}"


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - fall back to killing the shell itself
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


class ShellTool(Tool):
    name = "shell"
    category = "shell"
    description = (
        f"Run one {shell_name()} command in the workspace and return its output "
        "and exit code. Use it for scripts, builds, git and anything the file "
        "tools cannot do. Destructive commands are refused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": f"The {shell_name()} command to run."},
            "cwd": {"type": "string", "description": "Folder to run in, relative to the workspace (default: the workspace)."},
            "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                        "minimum": 1, "maximum": MAX_TIMEOUT, "default": DEFAULT_TIMEOUT},
        },
        "required": ["command"],
    }

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.workspace = workspace or Workspace(default_workspace)

    def run(self, args: dict[str, Any]) -> ToolResult:
        from ..permissions import command_denied

        command = str(args.get("command") or "").strip()
        if not command:
            return failed(self.name, "No command was given.")
        if command_denied(command):
            return failed(self.name, "This command is on the deny-list of destructive commands and was not run.")
        try:
            timeout = max(1, min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        try:
            self.workspace.ensure()
            cwd = self.workspace.resolve(str(args.get("cwd") or "."), kind="file_read")
        except (WorkspaceError, OSError) as exc:
            return failed(self.name, str(exc))
        if not cwd.is_dir():
            return failed(self.name, f"Folder not found: {self.workspace.relative(cwd)}")

        if os.name == "nt":
            # PowerShell 5.1 writes the OEM code page to a pipe; ask for UTF-8
            # so non-ASCII file names survive the decode below.
            wrapped = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapped]
            extra: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        else:
            argv = ["/bin/sh", "-c", command]
            extra = {"start_new_session": True}
        try:
            process = subprocess.Popen(
                argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_clean_env(), **extra,
            )
        except OSError as exc:
            return failed(self.name, f"The shell could not start: {exc.strerror or exc}")
        timed_out = False
        try:
            out_bytes, err_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
            try:
                out_bytes, err_bytes = process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - keep whatever we have
                out_bytes, err_bytes = b"", b""
        encoding = "utf-8"
        stdout = (out_bytes or b"").decode(encoding, "replace")
        stderr = (err_bytes or b"").decode(encoding, "replace")
        code = process.returncode
        parts = [f"$ {command}", f"(exit code {code}{', killed after ' + str(timeout) + ' s' if timed_out else ''}; cwd {self.workspace.relative(cwd)})"]
        if stdout.strip():
            parts.append(_cap(stdout.rstrip()))
        if stderr.strip():
            parts.append("[stderr]\n" + _cap(stderr.rstrip(), MAX_OUTPUT // 2))
        if not stdout.strip() and not stderr.strip():
            parts.append("(no output)")
        content = "\n".join(parts)
        result = ToolResult(
            ok=(code == 0 and not timed_out),
            content=content,
            data={"returncode": code, "timed_out": timed_out, "cwd": self.workspace.relative(cwd),
                  "stdout_chars": len(stdout), "stderr_chars": len(stderr), "shell": shell_name(),
                  "platform": sys.platform},
            source=self.name,
        )
        if not result.ok:
            result.error = (f"timed out after {timeout} s" if timed_out else f"exit code {code}")
        return result
