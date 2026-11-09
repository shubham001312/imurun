"""One gate for every tool call, from the model or from the UI.

``ToolExecutor.execute`` is the only path that runs a tool on behalf of the
model (the agent loop) or the user (``POST /api/tools/run``). In order:

1. look the tool up (unknown name -> failed result listing the real ones);
2. check the sub-agent restriction (``allowed``);
3. validate and coerce the arguments against the tool's JSON schema;
4. ask the permission policy: allow / ask / deny;
5. on "ask": use a pre-approval (manual run), else open an approval card
   and wait, else refuse;
6. run the tool - exceptions become a failed result, never a raise;
7. publish ``tool.call`` / ``tool.result`` and mark the audit (rule
   ``G3-tool``: tool name, decision and outcome; never argument values
   such as commands or file bodies, which may hold secrets).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..safety.decisions import Decision
from .base import Tool, ToolResult, failed

MAX_DEPTH = 2          # main loop = 0, a sub-agent = 1, its sub-agent = 2 (no run_agent there)
EVENT_PREVIEW = 2000   # characters of a result sent to the UI


@dataclass
class CallContext:
    turn_id: str = ""
    session_id: str = ""
    depth: int = 0
    step: int = 0
    stop: Callable[[], bool] | None = None
    approved: bool = False                  # the caller already approved (manual run)
    interactive: bool = True                # may open an approval card and wait
    allowed: frozenset[str] | None = None   # sub-agent tool restriction
    spawn: Callable[..., ToolResult] | None = None  # run_agent hook, set by the loop
    source: str = "model"                   # model | user | prefetch


@dataclass
class Execution:
    call_id: str
    tool: str
    args: dict[str, Any]
    result: ToolResult
    decision: Decision
    approval_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "decision": self.decision.kind,
            "reasons": list(self.decision.reasons),
            "approval_id": self.approval_id,
            "result": self.result.view(),
        }


# Tool categories whose results may hold the user's private data.
PRIVATE_READ_CATEGORIES = frozenset({"read", "connector-read", "shell", "mcp"})


class ToolExecutor:
    def __init__(
        self,
        registry: Any,
        *,
        policy: Any = None,
        approvals: Any = None,
        publish: Callable[[str, dict], int] | None = None,
        mark: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self._publish = publish
        self._mark = mark
        # Turns that have read private data (files, connector content, command
        # output). Afterwards a network call in the same turn asks first, so
        # injected text cannot read a file and quietly send it out (security
        # review H1). Bounded: oldest turns are forgotten.
        self._read_turns: dict[str, None] = {}

    # ------------------------------------------------------------- offering
    def offered(self, *, depth: int = 0, allowed: frozenset[str] | None = None) -> list[Tool]:
        """Tools the model may be told about right now.

        Unavailable tools (offline, no location) and categories switched off
        in Settings are left out, so the model is never offered something
        that can only fail. ``run_agent`` is dropped at the depth limit.
        """
        tools = []
        for tool in self.registry.available_tools():
            if allowed is not None and tool.name not in allowed:
                continue
            if tool.name == "run_agent" and depth >= MAX_DEPTH:
                continue
            if self.policy is not None:
                try:
                    if self.policy.generic(tool) == "deny":
                        continue
                except Exception:  # noqa: BLE001 - a broken policy offers nothing risky
                    if tool.category not in ("info", "network"):
                        continue
            tools.append(tool)
        return tools

    @staticmethod
    def schemas(tools: list[Tool]) -> dict[str, dict]:
        return {tool.name: dict(tool.parameters or {}) for tool in tools}

    # ------------------------------------------------------------- checking
    def check(self, name: str, args: dict[str, Any], ctx: CallContext | None = None) -> Decision:
        """The decision alone, without running (used by the manual route)."""
        ctx = ctx or CallContext()
        tool = self.registry.get(name)
        if tool is None:
            return Decision("deny", "G3-tool", (f"there is no tool called {name}",))
        clean, error = validate_args(tool, args)
        if error:
            return Decision("deny", "G3-tool", (error,))
        return self._decide(tool, clean, ctx)

    def _decide(self, tool: Tool, args: dict[str, Any], ctx: CallContext) -> Decision:
        if ctx.allowed is not None and tool.name not in ctx.allowed:
            return Decision("deny", "G3-grant", (f"{tool.name} is not granted to this agent",))
        if tool.name == "run_agent" and ctx.depth >= MAX_DEPTH:
            return Decision("deny", "G3-grant", ("sub-agents cannot start more sub-agents here",))
        if self.policy is None:
            if tool.category in ("info", "network", "agent"):
                return Decision("allow", "G3-tool", ("no policy: read-only tool",))
            return Decision("ask", "G3-tool", ("no permission policy is configured",))
        try:
            decision = self.policy.decide(tool, args, session_id=ctx.session_id)
        except Exception as exc:  # noqa: BLE001 - a broken policy denies
            return Decision("deny", "G3-tool", (f"permission check failed: {type(exc).__name__}",))
        return self._after_private_read(tool, ctx, decision)

    def _after_private_read(self, tool: Tool, ctx: CallContext, decision: Decision) -> Decision:
        """A network call after private data was read in this turn asks (not in Auto)."""
        if decision.kind != "allow" or tool.category != "network" or not ctx.turn_id:
            return decision
        if ctx.turn_id not in self._read_turns:
            return decision
        try:
            values = self.policy.values()
            granted = self.policy._granted(ctx.session_id, tool.name)
        except Exception:  # noqa: BLE001 - unsure means ask
            values, granted = {}, False
        if values.get("mode") == "allow-all" or granted:
            return decision
        return Decision("ask", "G3-exfiltration", (
            "sends a request to the internet after reading your files or messages in this turn",))

    def _note_private_read(self, tool: Tool | None, ctx: CallContext, result: ToolResult) -> None:
        if tool is None or not ctx.turn_id or not result.ok:
            return
        if tool.category in PRIVATE_READ_CATEGORIES:
            self._read_turns[ctx.turn_id] = None
            while len(self._read_turns) > 64:
                self._read_turns.pop(next(iter(self._read_turns)))

    # ------------------------------------------------------------- running
    def execute(self, name: str, args: dict[str, Any] | None, ctx: CallContext | None = None) -> Execution:
        ctx = ctx or CallContext()
        call_id = uuid.uuid4().hex[:10]
        raw_args = dict(args or {}) if isinstance(args, dict) else {}
        tool = self.registry.get(name) if self.registry is not None else None
        if tool is None:
            names = ", ".join(sorted(self.registry.names())) if self.registry is not None else ""
            result = failed(name or "tool", f"There is no tool called {name!r}. Available: {names}.")
            decision = Decision("deny", "G3-tool", ("unknown tool",))
            return self._finish(call_id, name, raw_args, result, decision, ctx, None, announce=True)

        clean, error = validate_args(tool, raw_args)
        self._emit("tool.call", {
            **self._ids(ctx), "call_id": call_id, "tool": tool.name,
            "args": _preview(clean if not error else raw_args), "category": tool.category,
        })
        if error:
            result = failed(tool.name, f"Bad arguments for {tool.name}: {error}. "
                                       f"Schema: {_schema_hint(tool)}")
            return self._finish(call_id, tool.name, raw_args, result,
                                Decision("deny", "G3-tool", (error,)), ctx, None)

        decision = self._decide(tool, clean, ctx)
        approval_id = None
        if decision.kind == "ask":
            decision, approval_id = self._approve(tool, clean, decision, ctx, call_id)
        if decision.kind != "allow":
            reason = "; ".join(decision.reasons) or "not permitted"
            result = failed(tool.name, f"Permission denied for {tool.name}: {reason}")
            return self._finish(call_id, tool.name, clean, result, decision, ctx, approval_id)

        if ctx.stop is not None and _safe(ctx.stop):
            result = failed(tool.name, "The turn was stopped before the tool ran.")
            return self._finish(call_id, tool.name, clean, result, decision, ctx, approval_id)

        started = time.monotonic()
        try:
            runner = getattr(tool, "run_with_context", None)
            result = runner(clean, ctx) if callable(runner) else tool.run(clean)
            if not isinstance(result, ToolResult):
                result = failed(tool.name, "the tool returned no result")
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result = failed(tool.name, f"The {tool.name} tool failed: {type(exc).__name__}: {exc}")
        result.latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
        result.source = result.source or tool.name
        self._note_private_read(tool, ctx, result)
        return self._finish(call_id, tool.name, clean, result, decision, ctx, approval_id)

    def _approve(
        self, tool: Tool, args: dict, decision: Decision, ctx: CallContext, call_id: str
    ) -> tuple[Decision, str | None]:
        if ctx.approved:
            return Decision("allow", "G3-tool", ("approved by the user for this call",)), None
        if not ctx.interactive or self.approvals is None:
            return Decision("ask", decision.rule, decision.reasons), None
        timeout = 180.0
        if self.policy is not None:
            try:
                timeout = float(self.policy.values().get("approvalTimeout", 180))
            except Exception:  # noqa: BLE001
                timeout = 180.0
        level, rememberable = "", True
        if self.policy is not None:
            try:
                level = str(self.policy.level(tool, args)) if hasattr(self.policy, "level") else ""
                if hasattr(self.policy, "rememberable"):
                    rememberable = bool(self.policy.rememberable(tool, args))
            except Exception:  # noqa: BLE001 - the card still shows; remembering stays allowed
                level, rememberable = "", True
        approval = self.approvals.request(
            tool.name, args, decision.reasons, timeout=timeout,
            turn_id=ctx.turn_id, session_id=ctx.session_id, call_id=call_id,
            level=level, rememberable=rememberable,
        )
        ok = self.approvals.wait(approval, ctx.stop)
        if ok:
            if approval.remember and rememberable and self.policy is not None:
                self.policy.remember(ctx.session_id, tool.name)
            return Decision("allow", "G3-tool", ("approved by the user",)), approval.id
        return Decision("deny", "G3-tool", (approval.reason or "declined",)), approval.id

    def _finish(
        self, call_id: str, name: str, args: dict, result: ToolResult, decision: Decision,
        ctx: CallContext, approval_id: str | None, *, announce: bool = False,
    ) -> Execution:
        if announce:
            self._emit("tool.call", {**self._ids(ctx), "call_id": call_id, "tool": name,
                                     "args": _preview(args), "category": "unknown"})
        content = result.content or ""
        self._emit("tool.result", {
            **self._ids(ctx), "call_id": call_id, "tool": name, "ok": result.ok,
            "decision": decision.kind, "error": result.error, "offline": result.offline,
            "latency_ms": result.latency_ms, "approval_id": approval_id,
            "content": content[:EVENT_PREVIEW] + ("..." if len(content) > EVENT_PREVIEW else ""),
        })
        outcome = "ok" if result.ok else ("refused" if decision.kind != "allow" else "failed")
        self._audit(name, f"{ctx.source} call, {decision.kind}: {outcome}")
        return Execution(call_id, name, args, result, decision, approval_id)

    # -------------------------------------------------------------- plumbing
    @staticmethod
    def _ids(ctx: CallContext) -> dict[str, Any]:
        return {"turn_id": ctx.turn_id, "session_id": ctx.session_id,
                "depth": ctx.depth, "step": ctx.step, "source": ctx.source}

    def _emit(self, kind: str, data: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(kind, data)
        except Exception:  # noqa: BLE001 - the UI feed must not break a tool
            pass

    def _audit(self, subject: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-tool", subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not break a tool
            pass


# ------------------------------------------------------------- validation
def validate_args(tool: Tool, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Coerce arguments to the schema types; report a missing required one."""
    params = tool.parameters or {}
    props: dict[str, Any] = params.get("properties") or {}
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        spec = props.get(key)
        if spec is None:
            # Unknown keys are dropped rather than refused: small models add
            # junk like "reason"; the tool would ignore it anyway.
            continue
        coerced, ok = _coerce(value, str(spec.get("type") or ""))
        if not ok:
            return out, f"argument {key!r} must be {spec.get('type')}"
        enum = spec.get("enum")
        if enum and coerced not in enum:
            return out, f"argument {key!r} must be one of {enum}"
        out[key] = coerced
    if len(props) == 1 and not out and args:
        # {"q": "..."} for a one-argument tool: map it onto the only parameter.
        only = next(iter(props))
        value = next(iter(args.values()))
        coerced, ok = _coerce(value, str(props[only].get("type") or ""))
        if ok:
            out[only] = coerced
    for key in params.get("required") or []:
        if key not in out or out[key] in (None, ""):
            return out, f"missing required argument {key!r}"
    return out, None


def _coerce(value: Any, kind: str) -> tuple[Any, bool]:
    if kind in ("", "any"):
        return value, True
    if kind == "string":
        if isinstance(value, (dict, list)):
            return None, False
        return ("" if value is None else str(value)), True
    if kind == "integer":
        if isinstance(value, bool):
            return int(value), True
        try:
            return int(float(value)), True
        except (TypeError, ValueError):
            return None, False
    if kind == "number":
        try:
            return float(value), True
        except (TypeError, ValueError):
            return None, False
    if kind == "boolean":
        if isinstance(value, bool):
            return value, True
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "y"):
            return True, True
        if text in ("false", "0", "no", "n", "", "none", "null"):
            return False, True
        return None, False
    if kind == "array":
        if isinstance(value, list):
            return value, True
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()], True
        return None, False
    if kind == "object":
        return (value, True) if isinstance(value, dict) else (None, False)
    return value, True


def _schema_hint(tool: Tool) -> str:
    from .protocol import _compact_args

    return _compact_args(tool.parameters)


def _preview(args: dict[str, Any], limit: int = 2000) -> dict[str, Any]:
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + f"... [{len(value) - limit} more characters]"
        else:
            out[key] = value
    return out


def _safe(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False
