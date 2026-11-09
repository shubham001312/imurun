"""Permission levels for connector and MCP tools, on top of ``PermissionPolicy``.

``services/tools/permissions.py`` (owned by the agentic loop) knows the
categories ``info``/``network``/``read``/``write``/``delete``/``shell``/
``agent``. Connector tools need two more rules it cannot express:

* a decision that depends on the arguments (``gmail_modify`` that removes a
  label asks, one that only adds a label follows the mode);
* "always ask, in every mode, and never remember" (``gmail_send``,
  ``gmail_trash``, destructive MCP tools), the same approval card the shell
  tool uses, but without the ``allow-all`` escape.

``ConnectorToolPolicy`` wraps the live policy. Tools whose category is in
``CATEGORIES`` must implement ``permission_level(args) -> (level, reason)``;
everything else is passed straight to the wrapped policy, and every other
attribute (``values``, ``view``, ``update``, ``workspace``...) is delegated,
so ``/api/permissions`` and the executor see no difference.

Levels:

=========  =====  ===========  =========  ==================
level      ask    allow-reads  allow-all  remembered grant
=========  =====  ===========  =========  ==================
allow      allow  allow        allow      -
read       ask    allow        allow      honoured
write      ask    ask          allow      honoured
ask        ask    ask          ask        honoured
always     ask    ask          ask        ignored
deny       deny   deny         deny       -
=========  =====  ===========  =========  ==================

Toggles: the ``network`` toggle off denies every connector tool (and HTTP MCP
tools); ``permissions.sendEmail`` off denies ``gmail_send``.

See docs/connectors/REQUESTS.md: once the loop owner adds this natively the
wrapper can go.
"""
from __future__ import annotations

from typing import Any

from ..safety.decisions import Decision

CATEGORIES = frozenset({"connector-read", "connector-write", "mcp"})
LEVELS = ("allow", "read", "write", "ask", "always", "deny")
RULE = "G3-connector-tool"


def _level_of(tool: Any, args: dict) -> tuple[str, str]:
    probe = getattr(tool, "permission_level", None)
    if not callable(probe):
        return "ask", "connector tool without a permission level"
    try:
        level, reason = probe(args or {})
    except Exception as exc:  # noqa: BLE001 - a broken probe asks
        return "ask", f"permission check failed ({type(exc).__name__})"
    if level not in LEVELS:
        return "ask", reason or "needs approval"
    return level, reason


class ConnectorToolPolicy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        # Only called for attributes not found on the wrapper itself.
        return getattr(self._inner, name)

    # ------------------------------------------------------------ toggles
    def _toggle_denial(self, tool: Any, values: dict) -> str:
        toggle = getattr(tool, "toggle", None)
        if toggle is None and getattr(tool, "requires_network", False):
            toggle = "network"
        if isinstance(toggle, str):
            toggle = (toggle,)
        for key in toggle or ():
            if not values.get(key, True):
                return f"{getattr(tool, 'category', 'connector')} tools are turned off in Settings ({key})"
        return ""

    def _values(self) -> dict:
        try:
            values = dict(self._inner.values())
        except Exception:  # noqa: BLE001
            values = {"mode": "ask"}
        raw = {}
        settings = getattr(self._inner, "settings", None)
        if settings is not None:
            try:
                raw = dict(settings.read("permissions") or {})
            except Exception:  # noqa: BLE001
                raw = {}
        # ``sendEmail`` exists in the settings defaults but not in the
        # policy's own view; read it straight from the section.
        values.setdefault("sendEmail", bool(raw.get("sendEmail", True)))
        return values

    # ------------------------------------------------------------ decide
    def decide(self, tool: Any, args: dict, *, session_id: str = "") -> Decision:
        category = getattr(tool, "category", "info")
        if category not in CATEGORIES:
            return self._inner.decide(tool, args, session_id=session_id)
        values = self._values()
        denial = self._toggle_denial(tool, values)
        if denial:
            return Decision("deny", RULE, (denial,))
        level, reason = _level_of(tool, args)
        reasons = (reason,) if reason else ("needs approval",)
        name = getattr(tool, "name", "")
        mode = values.get("mode", "ask")
        if level == "deny":
            return Decision("deny", RULE, reasons)
        if level == "allow":
            return Decision("allow", RULE, (reason or "allowed",))
        if level == "always":
            return Decision("ask", RULE, reasons)
        granted = self._granted(session_id, name)
        if granted:
            return Decision("allow", RULE, ("remembered for this session",))
        if level == "read" and mode != "ask":
            return Decision("allow", RULE, (f"allowed by mode {mode}",))
        if level == "write" and mode == "allow-all":
            return Decision("allow", RULE, (f"allowed by mode {mode}",))
        return Decision("ask", RULE, reasons)

    def _granted(self, session_id: str, name: str) -> bool:
        probe = getattr(self._inner, "_granted", None)
        if not callable(probe):
            return False
        try:
            return bool(probe(session_id, name))
        except Exception:  # noqa: BLE001
            return False

    def generic(self, tool: Any) -> str:
        category = getattr(tool, "category", "info")
        if category not in CATEGORIES:
            return self._inner.generic(tool)
        values = self._values()
        if self._toggle_denial(tool, values):
            return "deny"
        level, _ = _level_of(tool, getattr(tool, "generic_args", {}) or {})
        mode = values.get("mode", "ask")
        if level == "deny":
            return "deny"
        if level == "allow" or (level == "read" and mode != "ask") or (level == "write" and mode == "allow-all"):
            return "allow"
        return "ask"

    def remember(self, session_id: str, tool_name: str) -> None:
        # "always" tools ignore grants in decide(), so remembering them is
        # harmless; the wrapped policy keeps its own rules (no deletes).
        self._inner.remember(session_id, tool_name)


def install(server: Any) -> ConnectorToolPolicy | None:
    """Wrap the server's policy once and point every user of it at the wrapper."""
    policy = getattr(server, "_policy", None)
    if policy is None:
        return None
    if isinstance(policy, ConnectorToolPolicy):
        return policy
    if getattr(policy, "native_levels", False):
        # The loop's PermissionPolicy gates connector/MCP tools itself (levels,
        # toggles, never-remember "always"), and the approval broker carries
        # ``level`` / ``rememberable`` natively — nothing to wrap (L1/L2 done).
        return None
    wrapper = ConnectorToolPolicy(policy)
    server._policy = wrapper
    executor = getattr(server, "_executor", None)
    if executor is not None and getattr(executor, "policy", None) is policy:
        executor.policy = wrapper
    tools = getattr(server, "_tools", None)
    if tools is not None and getattr(tools, "policy", None) is policy:
        tools.policy = wrapper
    install_approval_hints(server)
    return wrapper


def rememberable(tool: Any, args: dict) -> bool:
    """False when "remember for this session" would not apply to this call."""
    name = getattr(tool, "name", "")
    if name == "file_delete":
        return False                       # PermissionPolicy never remembers deletes
    if getattr(tool, "category", "") in CATEGORIES:
        level, _ = _level_of(tool, args)
        return level not in ("always", "allow", "deny")
    return True


def install_approval_hints(server: Any) -> bool:
    """Add ``rememberable`` and ``level`` to ``tool.approval_needed`` events.

    The approval broker (owned by the loop) publishes the card without
    knowing the level; this wraps its publish callback so the UI can hide the
    "remember" box for always-ask tools. Idempotent.
    """
    broker = getattr(server, "_approvals", None)
    executor = getattr(server, "_executor", None)
    registry = getattr(executor, "registry", None)
    publish = getattr(broker, "_publish", None)
    if broker is None or registry is None or publish is None or getattr(publish, "_imurun_hints", False):
        return False

    def hinted(kind: str, data: dict):
        if kind == "tool.approval_needed" and isinstance(data, dict):
            tool = registry.get(str(data.get("tool") or ""))
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            data = dict(data)
            data["rememberable"] = rememberable(tool, args) if tool is not None else True
            if tool is not None and getattr(tool, "category", "") in CATEGORIES:
                data["level"] = _level_of(tool, args)[0]
        return publish(kind, data)

    hinted._imurun_hints = True  # type: ignore[attr-defined]
    broker._publish = hinted
    return True
