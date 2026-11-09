"""Command bridge: run a command the user explicitly asked for, whatever the model.

Models under ``profile.SMALL_MODEL_PARAMS`` get no tool catalogue (they copy
the protocol example instead of answering), so with a small local model the
agent could never run anything - it could only say it cannot. The bridge
closes that gap for the unambiguous case: when the message itself names the
command, the loop runs it through the ``shell`` tool before the model
answers (same executor: permissions, approval card, audit, events) and puts
the output in front of the model to explain.

Recognised forms (the command must be explicit - nothing is guessed):

* ``!<command>`` as the whole message, the ``!`` directly before a program
  that exists on this computer (``!ls ~/Desktop``; not ``!important`` or
  ``! wow``);
* ``run `<command>``` / ``execute `<command>``` (also "please run", "can you
  run"), one inline-code command;
* "run this" (or "execute this") followed by one fenced block tagged as a
  shell (``bash``, ``sh``, ``zsh``, ``shell``, ``console``, ``powershell``,
  ``ps1``, ``cmd``, ``bat``) or untagged.
"""
from __future__ import annotations

import os
import re
import shutil

MAX_COMMAND_CHARS = 4000
SHELL_FENCES = {"", "bash", "sh", "zsh", "shell", "console", "powershell", "pwsh", "ps1", "cmd", "bat"}

_BANG = re.compile(r"^\s*!(?P<cmd>[A-Za-z0-9_./~\\-]\S*(?:[ \t].*)?)$", re.DOTALL)
# Shell built-ins (not files on PATH) a bang command may start with.
_BUILTINS = {"cd", "echo", "pwd", "type", "set", "export", "dir", "cls", "ver", "where", "alias"}
# Everyday programs, accepted even when this machine's PATH lacks them (the
# shell tool reports "not found" itself), so "!ls" works the same everywhere.
_COMMON = {"ls", "cat", "head", "tail", "grep", "find", "git", "npm", "npx", "node", "pnpm", "yarn",
           "python", "python3", "py", "pip", "pip3", "pytest", "make", "cmake", "mkdir", "touch", "cp",
           "mv", "rm", "curl", "wget", "du", "df", "ps", "top", "whoami", "date", "uname", "which",
           "code", "open", "start", "xdg-open", "docker", "cargo", "go", "java", "tree", "wc", "sort",
           "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "ipconfig", "ifconfig", "ping", "tasklist"}
_INLINE = re.compile(
    r"^\s*(?:(?:please|pls|can you|could you|would you)\s+)?(?:run|execute|exec)\s*:?\s*`(?P<cmd>[^`\n]+)`"
    r"\s*(?:(?:and|then)\b.*)?[.!?]?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_RUN_THIS = re.compile(
    r"^\s*(?:(?:please|pls|can you|could you|would you)\s+)?(?:run|execute)\s+(?:this|these|the following)"
    r"[^`\n]*\n?```(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n(?P<cmd>.*?)\n?```\s*$",
    re.IGNORECASE | re.DOTALL,
)


def extract_command(prompt: str) -> str | None:
    """The command the message explicitly asks to run, or None."""
    text = (prompt or "").strip()
    if not text:
        return None
    command = None
    match = _RUN_THIS.match(text)
    if match and match.group("lang").lower() in SHELL_FENCES:
        command = match.group("cmd")
    if command is None:
        match = _INLINE.match(text)
        if match:
            command = match.group("cmd")
    if command is None:
        match = _BANG.match(text)
        if match and "\n" not in match.group("cmd").strip() and _is_program(match.group("cmd")):
            command = match.group("cmd")
    if command is None:
        return None
    command = command.strip()
    if not command or len(command) > MAX_COMMAND_CHARS:
        return None
    return command


def _is_program(command: str) -> bool:
    """The first word is a program on this computer or a shell built-in."""
    first = (command or "").split(None, 1)[0] if (command or "").strip() else ""
    if not first:
        return False
    if first.lower() in _BUILTINS or first.lower() in _COMMON:
        return True
    if os.sep in first or "/" in first:
        return os.path.isfile(os.path.expanduser(first))
    return shutil.which(first) is not None
