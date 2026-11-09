"""Who may run which tool, decided in code (G3), never by the model.

Inputs, all read from the ``permissions`` settings section on every call so a
change in Settings applies to the next tool call:

* ``mode`` - ``ask`` | ``allow-reads`` (default) | ``allow-all``;
* ``workspaceRoot`` - the folder file and shell tools are confined to;
* the existing toggles ``readFiles``, ``writeFiles``, ``runShell`` and
  ``network``: a toggle set to ``false`` turns that category off entirely.

The decision table (after the hard checks: unknown tool, disabled category,
path outside the workspace, protected path, destructive command):

==========  =====  ===========  =========
category    ask    allow-reads  allow-all
==========  =====  ===========  =========
info        allow  allow        allow
network     allow  allow        allow
agent       allow  allow        allow
read        ask    allow        allow
write       ask    ask          allow
delete      ask    ask          allow
shell       ask    ask          allow
==========  =====  ===========  =========

"ask" becomes an approval card (``approvals.py``). An approval can carry
``remember``: the same tool then runs without asking for the rest of that
session (until the server restarts). Deletes are never remembered.

Connector and MCP tools (categories ``connector-read``, ``connector-write``,
``mcp``) decide per call: the tool's ``permission_level(args)`` returns one
of ``LEVELS`` and a reason, and the table is:

=========  =====  ===========  =========  ==================
level      ask    allow-reads  allow-all  remembered grant
=========  =====  ===========  =========  ==================
allow      allow  allow        allow      -
read       ask    allow        allow      honoured
write      ask    ask          allow      honoured
ask        ask    ask          ask        honoured
always     ask    ask          ask        ignored (never remembered)
deny       deny   deny         deny       -
=========  =====  ===========  =========  ==================

A tool may name its own toggles (``toggle = ("network", "sendEmail")``);
otherwise a network tool is gated by the ``network`` toggle.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..safety.decisions import DANGEROUS_COMMANDS, Decision
from .workspace import Workspace, WorkspaceError, default_workspace, is_filesystem_root

MODES = ("ask", "allow-reads", "allow-all")
# Folders the agent may use besides the workspace, unless the user changes it.
DEFAULT_EXTRA_ROOTS = ("~/Desktop", "~/Documents")
DEFAULT_MODE = "allow-reads"

# Setting toggle that switches each category off. Categories not listed here
# have no toggle.
TOGGLES = {
    "read": "readFiles",
    "write": "writeFiles",
    "delete": "writeFiles",
    "shell": "runShell",
    "network": "network",
}

ASK_TABLE: dict[str, frozenset[str]] = {
    "ask": frozenset({"read", "write", "delete", "shell"}),
    "allow-reads": frozenset({"write", "delete", "shell"}),
    "allow-all": frozenset(),
}

# Per-call levels (connector / MCP tools, or any tool with ``permission_level``).
LEVELS = ("allow", "read", "write", "ask", "always", "deny")
LEVEL_CATEGORIES = frozenset({"connector-read", "connector-write", "mcp"})
# Built-in categories expressed as a level, for the UI and ``rememberable``.
CATEGORY_LEVEL = {"info": "allow", "network": "allow", "agent": "allow", "read": "read",
                  "write": "write", "delete": "write", "shell": "write"}
# Extra boolean toggles tools may name in ``toggle`` (read from the section).
EXTRA_TOGGLES = ("sendEmail",)


def level_of(tool: Any, args: dict[str, Any] | None = None) -> tuple[str, str]:
    """``(level, reason)`` for one call. Tools with ``permission_level`` decide
    themselves; a level tool without the probe asks; built-ins map by category."""
    probe = getattr(tool, "permission_level", None)
    category = getattr(tool, "category", "info")
    if callable(probe):
        try:
            level, reason = probe(dict(args or {}))
        except Exception as exc:  # noqa: BLE001 - a broken probe asks
            return "ask", f"permission check failed ({type(exc).__name__})"
        if level not in LEVELS:
            return "ask", str(reason or "needs approval")
        return str(level), str(reason or "")
    if category in LEVEL_CATEGORIES:
        return "ask", "connector tool without a permission level"
    return CATEGORY_LEVEL.get(category, "ask"), ""


def tool_toggles(tool: Any) -> tuple[str, ...]:
    """The settings toggles that can switch a tool off."""
    toggle = getattr(tool, "toggle", None)
    if toggle is None:
        category = getattr(tool, "category", "info")
        if category in TOGGLES:
            return (TOGGLES[category],)
        return ("network",) if getattr(tool, "requires_network", False) else ()
    if isinstance(toggle, str):
        return (toggle,)
    return tuple(str(t) for t in toggle)

# Argument names that hold paths, per tool, and the safety kind they need.
PATH_ARGS: dict[str, tuple[tuple[str, str], ...]] = {
    "file_read": (("path", "file_read"),),
    "file_list": (("path", "file_read"),),
    "file_write": (("path", "file_write"),),
    "make_dir": (("path", "file_write"),),
    "file_move": (("src", "file_delete"), ("dst", "file_write")),
    "file_copy": (("src", "file_read"), ("dst", "file_write")),
    "file_delete": (("path", "file_delete"),),
    "shell": (("cwd", "file_read"),),
}


def command_denied(command: str) -> str:
    """The deny-list pattern a command matches, or an empty string."""
    for pattern in DANGEROUS_COMMANDS:
        if pattern.search(command or ""):
            return pattern.pattern
    return ""


@dataclass
class PermissionPolicy:
    """Reads the settings section and decides allow / ask / deny."""

    settings: Any = None  # SettingsStore-like: read(section) -> dict, write(section, dict)
    workspace_override: Path | str | None = None
    _grants: dict[str, set[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.workspace = Workspace(self._root, self._extra_roots)

    # --------------------------------------------------------------- settings
    def values(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self.settings is not None:
            try:
                raw = dict(self.settings.read("permissions") or {})
            except Exception:  # noqa: BLE001 - a broken file means defaults
                raw = {}
        mode = str(raw.get("mode") or DEFAULT_MODE)
        if mode not in MODES:
            mode = DEFAULT_MODE
        values = {
            "mode": mode,
            "workspaceRoot": str(raw.get("workspaceRoot") or ""),
            "extraRoots": _roots_list(raw.get("extraRoots", DEFAULT_EXTRA_ROOTS)),
            "readFiles": bool(raw.get("readFiles", True)),
            "writeFiles": bool(raw.get("writeFiles", True)),
            "runShell": bool(raw.get("runShell", True)),
            "network": bool(raw.get("network", True)),
            "approvalTimeout": _clamp(raw.get("approvalTimeout"), 10, 1800, 180),
        }
        for key in EXTRA_TOGGLES:
            values[key] = bool(raw.get(key, True))
        return values

    def _root(self) -> Path:
        if self.workspace_override:
            return Path(self.workspace_override)
        root = self.values()["workspaceRoot"]
        return Path(os.path.expanduser(root)) if root else default_workspace()

    def _extra_roots(self) -> list[str]:
        return list(self.values()["extraRoots"])

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a change from ``POST /api/permissions``."""
        if self.settings is None:
            raise ValueError("there is no settings store to write to")
        changes: dict[str, Any] = {}
        if "mode" in payload:
            mode = str(payload["mode"])
            if mode not in MODES:
                raise ValueError(f"mode must be one of {', '.join(MODES)}")
            changes["mode"] = mode
        if "extraRoots" in payload:
            raw = payload["extraRoots"]
            if not isinstance(raw, list) or len(raw) > 20:
                raise ValueError("extraRoots must be a list of up to 20 folders")
            cleaned = []
            for item in raw:
                text = str(item or "").strip()
                if not text:
                    continue
                path = Path(os.path.expanduser(text))
                if not path.is_absolute():
                    raise ValueError(f"{text} must be an absolute path (or start with ~)")
                if is_filesystem_root(path):
                    raise ValueError(f"{text} is a whole drive")
                if path.exists() and not path.is_dir():
                    raise ValueError(f"{text} is not a folder")
                cleaned.append(text)
            changes["extraRoots"] = cleaned
        if "workspaceRoot" in payload:
            root = str(payload["workspaceRoot"] or "").strip()
            if root:
                path = Path(os.path.expanduser(root))
                if not path.is_absolute():
                    raise ValueError("workspaceRoot must be an absolute path")
                if is_filesystem_root(path):
                    raise ValueError("workspaceRoot cannot be a whole drive")
                if path.exists() and not path.is_dir():
                    raise ValueError("workspaceRoot must be a folder")
            changes["workspaceRoot"] = root
        for key in ("readFiles", "writeFiles", "runShell", "network", *EXTRA_TOGGLES):
            if key in payload:
                changes[key] = bool(payload[key])
        if "approvalTimeout" in payload:
            changes["approvalTimeout"] = _clamp(payload["approvalTimeout"], 10, 1800, 180)
        if not changes:
            raise ValueError("nothing to change")
        self.settings.write("permissions", changes)
        return self.view()

    def view(self) -> dict[str, Any]:
        values = self.values()
        root = self.workspace.root
        return {
            **values,
            "workspace": str(root),
            "workspaceExists": root.is_dir(),
            "modes": list(MODES),
            "table": {mode: sorted(cats) for mode, cats in ASK_TABLE.items()},
            "grants": {sid: sorted(names) for sid, names in self._grants_copy().items()},
        }

    # ----------------------------------------------------------------- grants
    def remember(self, session_id: str, tool_name: str, tool: Any = None, args: dict[str, Any] | None = None) -> None:
        """Grant ``tool_name`` for the session. Deletes and "always" tools are
        never remembered (pass ``tool`` so the level can be checked)."""
        if not session_id or tool_name == "file_delete":
            return
        if tool is not None and not self.rememberable(tool, args or {}):
            return
        with self._lock:
            self._grants.setdefault(session_id, set()).add(tool_name)

    def rememberable(self, tool: Any, args: dict[str, Any] | None = None) -> bool:
        """False when "remember for this session" would not apply to this call:
        deletes, always-ask tools, and tools that never ask anyway."""
        if getattr(tool, "name", "") == "file_delete":
            return False
        return level_of(tool, args)[0] not in ("always", "allow", "deny")

    def level(self, tool: Any, args: dict[str, Any] | None = None) -> str:
        """The level of one call (``LEVELS``), for the approval card."""
        return level_of(tool, args)[0]

    # Marks a policy that gates connector/MCP tools itself, so no wrapper is needed.
    native_levels = True

    def _toggle_denial(self, tool: Any, values: dict[str, Any]) -> str:
        for key in tool_toggles(tool):
            if not values.get(key, True):
                category = getattr(tool, "category", "info")
                return f"{category} tools are turned off in Settings ({key})"
        return ""

    def _decide_level(self, tool: Any, args: dict[str, Any], values: dict[str, Any], session_id: str) -> Decision:
        """Per-call levels (connector / MCP tools, or any tool with ``permission_level``)."""
        name = getattr(tool, "name", "")
        level, reason = level_of(tool, args)
        reasons = (reason,) if reason else ("needs approval",)
        mode = values["mode"]
        if level == "deny":
            return Decision("deny", "G3-tool", reasons)
        if level == "allow":
            return Decision("allow", "G3-tool", (reason or "allowed",))
        if level == "always":
            return Decision("ask", "G3-tool", reasons)   # grants are ignored on purpose
        if self._granted(session_id, name):
            return Decision("allow", "G3-tool", ("remembered for this session",))
        if level == "read" and mode != "ask":
            return Decision("allow", "G3-tool", (f"allowed by mode {mode}",))
        if level == "write" and mode == "allow-all":
            return Decision("allow", "G3-tool", (f"allowed by mode {mode}",))
        return Decision("ask", "G3-tool", reasons)

    def _grants_copy(self) -> dict[str, set[str]]:
        with self._lock:
            return {k: set(v) for k, v in self._grants.items()}

    def _granted(self, session_id: str, tool_name: str) -> bool:
        with self._lock:
            return tool_name in self._grants.get(session_id or "", set())

    # --------------------------------------------------------------- decision
    def decide(self, tool: Any, args: dict[str, Any], *, session_id: str = "") -> Decision:
        name = getattr(tool, "name", "")
        category = getattr(tool, "category", "info")
        values = self.values()
        denial = self._toggle_denial(tool, values)
        if denial:
            return Decision("deny", "G3-tool", (denial,))
        if category in LEVEL_CATEGORIES or callable(getattr(tool, "permission_level", None)):
            return self._decide_level(tool, args, values, session_id)

        for arg, kind in PATH_ARGS.get(name, ()):
            if arg == "cwd" and not args.get(arg):
                continue
            if arg in ("src", "dst") and not args.get(arg):
                return Decision("deny", "G3-tool", (f"{arg} is required",))
            try:
                self.workspace.resolve(args.get(arg) or ".", kind=kind)
            except WorkspaceError as exc:
                return Decision("deny", "G3-outside-roots", (str(exc),))

        reasons: list[str] = []
        if category == "shell":
            pattern = command_denied(str(args.get("command") or ""))
            if pattern:
                return Decision("deny", "G3-destructive", ("this command is on the deny-list of destructive commands",))
            reasons.append("runs a command on this computer")
        elif category == "delete":
            reasons.append("deletes from the workspace")
        elif category == "write":
            reasons.append(_write_reason(self, name, args))
        elif category == "read":
            reasons.append("reads from the workspace")

        if category in ASK_TABLE[values["mode"]] and not self._granted(session_id, name):
            return Decision("ask", "G3-tool", tuple(r for r in reasons if r) or ("needs approval",))
        why = f"allowed by mode {values['mode']}"
        if self._granted(session_id, name):
            why = "remembered for this session"
        return Decision("allow", "G3-tool", (why,))

    def generic(self, tool: Any) -> str:
        """allow / ask / deny for a tool in general (for ``GET /api/tools``)."""
        category = getattr(tool, "category", "info")
        values = self.values()
        if self._toggle_denial(tool, values):
            return "deny"
        if category in LEVEL_CATEGORIES or callable(getattr(tool, "permission_level", None)):
            level, _ = level_of(tool, getattr(tool, "generic_args", {}) or {})
            mode = values["mode"]
            if level == "deny":
                return "deny"
            if level == "allow" or (level == "read" and mode != "ask") or (level == "write" and mode == "allow-all"):
                return "allow"
            return "ask"
        return "ask" if category in ASK_TABLE[values["mode"]] else "allow"


def _write_reason(policy: PermissionPolicy, name: str, args: dict[str, Any]) -> str:
    if name == "file_write":
        try:
            target = policy.workspace.resolve(args.get("path"), kind="file_write")
        except WorkspaceError:
            return "writes a file"
        mode = str(args.get("mode") or "overwrite")
        if target.exists():
            return "appends to an existing file" if mode == "append" else "overwrites an existing file"
        return "creates a new file"
    if name in ("file_move", "file_copy") and args.get("overwrite"):
        return "may overwrite the destination"
    return "changes the workspace"


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _roots_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_EXTRA_ROOTS)
    return [str(item).strip() for item in raw if str(item or "").strip()][:20]
