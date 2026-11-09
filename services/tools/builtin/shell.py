"""Run one shell command on this computer, starting in the workspace.

What this tool guarantees (and what it does not):

* the working directory is the workspace, or a folder inside it (a folder
  outside it only after an approval card for that call, see
  ``permissions.py``);
* Windows: PowerShell by default; a command written for cmd.exe
  (``%VAR%``, ``&&``/``||``, ``dir /b``, ``where``...) runs in cmd.exe,
  because PowerShell 5.1 prints ``%USERNAME%`` literally and rejects ``&&``.
  The ``shell`` argument forces one. Output is decoded as UTF-8 when valid,
  else from the OEM/ANSI code page, so ``café.txt`` survives either shell;
* commands matching the destructive deny-list
  (``safety.decisions.DANGEROUS_COMMANDS``) are refused before they start -
  the permission policy checks this, and the tool checks again so a direct
  ``run`` cannot skip it;
* a timeout (default 30 s, max 300 s) kills the whole process tree, not just
  the shell, so a hung child cannot outlive the turn;
* output is capped (``MAX_OUTPUT`` characters, head and tail kept);
* a missing program is reported as ``command not found: <name>``;
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

import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from ..base import Tool, ToolResult, failed
from ..workspace import Workspace, WorkspaceError, default_workspace
from .files import FileListTool, FileReadTool, FileWriteTool  # noqa: F401 - re-exported

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|_KEY$|CREDENTIAL|COOKIE|SESSION)", re.IGNORECASE)

# Windows: commands written for cmd.exe that PowerShell 5.1 gets wrong.
# ``echo %USERNAME%`` printed the literal text "%USERNAME%", ``a && b`` is a
# parse error in 5.1, ``dir /b`` fails, and ``where`` is Where-Object (it
# printed nothing for ``where python``). These go to cmd.exe instead.
_CMD_VAR = re.compile(r"%[A-Za-z_][A-Za-z0-9_()]*%")
_CMD_CHAIN = re.compile(r"&&|\|\|")
_CMD_SWITCH = re.compile(
    r"^\s*(?:dir|del|erase|copy|xcopy|move|ren|rename|type|rd|rmdir|md|mkdir|attrib|findstr|tree)\b"
    r"[^|;]*?\s/[A-Za-z?]", re.IGNORECASE)
_CMD_ONLY = re.compile(r"^\s*(?:where|ver|vol|set|assoc|ftype|mklink)(?:\s|$)", re.IGNORECASE)

# Prelude and trailer around a PowerShell command:
# * UTF-8 for PowerShell's own output and for what it pipes to native
#   programs (5.1 under the server writes the OEM code page: "café" came
#   back as b"caf\x82");
# * no progress records (they arrive on stderr as "#< CLIXML" noise);
# * the exit code of a failed native program is kept (``-Command`` turns
#   every failure into 1; ``cmd /c exit 3`` now reports 3).
# The prelude ends with a newline so error positions point at the user's
# command ("At line:2 char:1"), not at the prelude. ``-EncodedCommand``
# was tried and rejected: it turns every error into CLIXML on stderr.
_PS_PRELUDE = ("$ProgressPreference = 'SilentlyContinue'; "
               "$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n")
_PS_TRAILER = "\n$imurunOk = $?; if (-not $imurunOk) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"

_NOT_FOUND = (
    re.compile(r"^\s*(?P<name>\S+) : The term '(?P=name)' is not recognized", re.MULTILINE),
    re.compile(r"^'(?P<name>[^']+)' is not recognized as an internal or external command", re.MULTILINE),
    re.compile(r"^(?:\S*sh: )?(?:(?:line )?\d+: )?(?P<name>[^:\s]+): (?:command )?not found", re.MULTILINE),
)

_LABELS = {"powershell": "PowerShell", "cmd": "cmd.exe", "sh": "sh"}


def shell_name() -> str:
    return "PowerShell" if os.name == "nt" else "sh"


def _windows_exe(*candidates: str) -> str:
    """First of ``candidates`` found (absolute, on PATH, or under %SystemRoot%)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    for name in candidates:
        if os.path.isabs(name):
            if os.path.isfile(name):
                return name
            continue
        found = shutil.which(name)
        if found:
            return found
    fallback = {
        "powershell.exe": os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "cmd.exe": os.path.join(root, "System32", "cmd.exe"),
    }
    for name in candidates:
        path = fallback.get(name)
        if path and os.path.isfile(path):
            return path
    return ""


def find_shells() -> dict[str, str]:
    """Shell executables on this machine; a missing one maps to ``""``."""
    if os.name == "nt":
        comspec = os.environ.get("ComSpec") or ""
        return {
            "powershell": _windows_exe("powershell.exe", "pwsh.exe"),
            "cmd": _windows_exe(*(c for c in (comspec, "cmd.exe") if c)),
        }
    return {"sh": "/bin/sh" if os.path.exists("/bin/sh") else (shutil.which("sh") or "")}


def looks_like_cmd(command: str) -> bool:
    """True for a command written for cmd.exe rather than PowerShell."""
    text = command or ""
    if re.match(r"^\s*(?:powershell|pwsh)(?:\.exe)?\b", text, re.IGNORECASE):
        return False  # "powershell Get-Date": PowerShell runs it either way
    return bool(_CMD_VAR.search(text) or _CMD_CHAIN.search(text) or _CMD_SWITCH.search(text)
                or _CMD_ONLY.match(text))


def pick_shell(command: str, requested: str = "auto", *, windows: bool | None = None) -> str:
    """``powershell`` | ``cmd`` on Windows, ``sh`` elsewhere."""
    if not (os.name == "nt" if windows is None else windows):
        return "sh"
    requested = (requested or "auto").lower()
    if requested in ("powershell", "pwsh", "ps"):
        return "powershell"
    if requested in ("cmd", "cmd.exe"):
        return "cmd"
    return "cmd" if looks_like_cmd(command) else "powershell"


def decode_output(data: bytes, fallbacks: list[str] | None = None) -> str:
    """Decode a child's output: UTF-8 when it is valid UTF-8, else the code
    page Windows programs write to a pipe (OEM first: cmd.exe and console
    programs, then ANSI). CRLF becomes LF."""
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
        for encoding in (fallbacks if fallbacks is not None else _fallback_encodings()):
            try:
                text = data.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = data.decode("latin-1")   # never fails; keeps every byte
    return text.replace("\r\n", "\n")


def _fallback_encodings() -> list[str]:
    names: list[str] = []
    if os.name == "nt":
        try:
            import ctypes

            names = [f"cp{ctypes.windll.kernel32.GetOEMCP()}", f"cp{ctypes.windll.kernel32.GetACP()}"]
        except Exception:  # noqa: BLE001
            names = ["cp437", "cp1252"]
    names.append(locale.getpreferredencoding(False) or "latin-1")
    names.append("latin-1")
    return names


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


def _cap(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-(limit // 3):]
    return f"{head}\n... [{len(text) - len(head) - len(tail)} characters cut] ...\n{tail}"


def not_found_name(stderr: str) -> str:
    """The program name when the shell said it does not exist, else ``""``.
    English messages only; on other Windows languages the exit code stays
    the signal."""
    for pattern in _NOT_FOUND:
        match = pattern.search(stderr or "")
        if match:
            return match.group("name")
    return ""


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - fall back to killing the shell itself
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


def _description() -> str:
    if os.name == "nt":
        return ("Run one command on this Windows computer and return its output and exit code. "
                "PowerShell by default; cmd.exe syntax (%VAR%, &&, dir /b) runs in cmd.exe. "
                "Use it for scripts, builds, git and anything the file tools cannot do. "
                "Destructive commands are refused.")
    return ("Run one sh command on this computer and return its output and exit code. "
            "Use it for scripts, builds, git and anything the file tools cannot do. "
            "Destructive commands are refused.")


def _parameters() -> dict[str, Any]:
    props: dict[str, Any] = {
        "command": {"type": "string", "description": (
            "The command to run (PowerShell or cmd.exe syntax)." if os.name == "nt"
            else "The sh command to run.")},
        "cwd": {"type": "string", "description": "Folder to run in, relative to the workspace (default: the workspace)."},
        "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                    "minimum": 1, "maximum": MAX_TIMEOUT, "default": DEFAULT_TIMEOUT},
    }
    if os.name == "nt":
        props["shell"] = {"type": "string", "enum": ["auto", "powershell", "cmd"], "default": "auto",
                          "description": "auto (default: cmd.exe for cmd syntax, else PowerShell), powershell or cmd."}
    return {"type": "object", "properties": props, "required": ["command"]}


class ShellTool(Tool):
    name = "shell"
    category = "shell"
    description = _description()
    parameters = _parameters()

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.workspace = workspace or Workspace(default_workspace)
        self._lock = threading.Lock()
        self._last: dict[str, Any] = {}

    # ------------------------------------------------------------ state
    def available(self) -> bool:
        return any(find_shells().values())

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        return "no shell executable found (looked for " + ", ".join(find_shells()) + ")"

    def state(self) -> dict[str, Any]:
        """Honest shell state for ``GET /api/tools``. Never the command text
        (it may hold secrets), only how the last run ended."""
        shells = find_shells()
        default = "powershell" if os.name == "nt" else "sh"
        try:
            root = str(self.workspace.root)
            exists = os.path.isdir(root)
        except Exception as exc:  # noqa: BLE001
            root, exists = f"(unreadable: {type(exc).__name__})", False
        with self._lock:
            last = dict(self._last)
        return {
            "available": any(shells.values()),
            "platform": sys.platform,
            "shell": default,
            "executable": shells.get(default, ""),
            "shells": shells,
            "auto_cmd": os.name == "nt",
            "cwd": root,
            "workspace": root,
            "workspace_exists": exists,
            "timeout_default": DEFAULT_TIMEOUT,
            "timeout_max": MAX_TIMEOUT,
            "output_cap": MAX_OUTPUT,
            "last_run": last or None,
            "last_error": (last.get("error") if last else None),
        }

    def _remember(self, **values: Any) -> None:
        values["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._last = values

    # -------------------------------------------------------------- run
    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self._run(args)
        except Exception as exc:  # noqa: BLE001 - a tool never raises
            result = failed(self.name, f"The shell tool failed: {type(exc).__name__}: {exc}")
        data = result.data or {}
        self._remember(ok=result.ok, shell=data.get("shell"), returncode=data.get("returncode"),
                       timed_out=bool(data.get("timed_out", False)), cwd=data.get("cwd"),
                       elapsed_ms=data.get("elapsed_ms"), error=None if result.ok else result.error)
        return result

    def _run(self, args: dict[str, Any]) -> ToolResult:
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

        shell = pick_shell(command, str(args.get("shell") or "auto"))
        executable = find_shells().get(shell, "")
        if not executable:
            return failed(self.name, f"The {_LABELS[shell]} executable was not found on this computer.",
                          data={"shell": shell, "platform": sys.platform})
        if shell == "powershell":
            argv: Any = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                         _PS_PRELUDE + command + _PS_TRAILER]
            extra: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        elif shell == "cmd":
            # A string, not a list: list2cmdline would escape inner quotes as
            # \" which cmd.exe does not understand. With /s cmd strips exactly
            # the outer pair and runs the rest verbatim; /d skips AutoRun.
            argv = f'"{executable}" /d /s /c "{command}"'
            extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        else:
            argv = [executable, "-c", command]
            extra = {"start_new_session": True}
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_clean_env(), **extra,
            )
        except OSError as exc:
            return failed(self.name, f"The shell could not start: {exc.strerror or exc}",
                          data={"shell": shell, "executable": executable})
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
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = decode_output(out_bytes or b"")
        stderr = decode_output(err_bytes or b"")
        code = process.returncode
        missing = "" if timed_out or code == 0 else not_found_name(stderr)
        where = self.workspace.relative(cwd)
        status = f"exit code {code}" + (f", killed after {timeout} s" if timed_out else "")
        parts = [f"$ {command}", f"({_LABELS[shell]}; {status}; cwd {where})"]
        if missing:
            parts.append(f"[error] '{missing}' is not a program, command or script on this computer "
                         "(not installed or not on PATH).")
        if stdout.strip():
            parts.append(_cap(stdout.rstrip()))
        if stderr.strip():
            parts.append("[stderr]\n" + _cap(stderr.rstrip(), MAX_OUTPUT // 2))
        if not stdout.strip() and not stderr.strip():
            parts.append("(no output)")
        result = ToolResult(
            ok=(code == 0 and not timed_out),
            content="\n".join(parts),
            data={"returncode": code, "timed_out": timed_out, "cwd": where, "cwd_abs": str(cwd),
                  "stdout_chars": len(stdout), "stderr_chars": len(stderr), "shell": shell,
                  "executable": executable, "platform": sys.platform, "elapsed_ms": elapsed_ms,
                  "not_found": missing or None},
            source=self.name,
        )
        if not result.ok:
            if timed_out:
                result.error = f"timed out after {timeout} s"
            elif missing:
                result.error = f"command not found: {missing}"
            else:
                result.error = f"exit code {code}"
        return result
