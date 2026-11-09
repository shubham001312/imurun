"""HTTP handlers for the agentic routes.

``api.py`` is a stdlib ``BaseHTTPRequestHandler`` and already ~1200 lines,
so the logic for the tool, permission, approval and agent routes lives here
and the handler only dispatches. Every function takes the ``ApiServer`` and
returns ``(status, payload)``; ``None`` means "not my route".

Routes (all behind the bearer token):

GET  /api/tools                     tools with schemas, availability, permission
GET  /api/tools/approvals           pending approval cards
GET  /api/permissions               permission mode, workspace, toggles
GET  /api/agent                     step budget and sub-agent limits
POST /api/tools/run                 run one tool by hand
POST /api/tools/approvals/{id}      answer an approval card
POST /api/permissions               change mode / workspace / toggles
POST /api/agent                     change agent.maxSteps

Also dispatched from here: the Gmail set-up routes
(``services/connectors/routes.py``) and ``/api/mcp/*``
(``services/mcp/routes.py``, including ``handle_delete`` for DELETE).
"""
from __future__ import annotations

from typing import Any

from .base import ToolResult, failed
from .builtin.agent import DEFAULT_AGENT_STEPS, MAX_AGENT_STEPS
from .executor import MAX_DEPTH, CallContext

Reply = tuple[int, dict]


def _policy_view(server: Any) -> dict:
    policy = getattr(server, "_policy", None)
    if policy is None:
        return {}
    try:
        view = policy.view()
    except Exception as exc:  # noqa: BLE001 - a status read must not fail
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {k: view[k] for k in ("mode", "workspace", "workspaceExists") if k in view}


def tools_view(server: Any) -> dict:
    runner = getattr(server, "_tools", None)
    if runner is None:
        return {"tools": [], "network": False, "permissions": _policy_view(server)}
    status = runner.status()
    policy = getattr(server, "_policy", None)
    registry = getattr(runner, "registry", None)
    if policy is not None and registry is not None:
        for item in status.get("tools", []):
            tool = registry.get(item.get("name"))
            try:
                item["permission"] = policy.generic(tool) if tool is not None else "deny"
            except Exception:  # noqa: BLE001
                item["permission"] = "deny"
            item["permission_level"] = permission_level(tool)
    status["permissions"] = _policy_view(server)
    status["format"] = '```tool\n{"name": "<tool>", "args": {...}}\n```'
    return status


# Built-in categories -> the same level names connector tools use
# (connectors/tool_policy.py): allow | read | write | ask | always.
_CATEGORY_LEVEL = {"info": "allow", "network": "allow", "agent": "allow", "read": "read",
                   "write": "write", "delete": "write", "shell": "write"}


def permission_level(tool: Any) -> str:
    """How a tool is gated, independent of the current mode (for the UI)."""
    if tool is None:
        return "deny"
    probe = getattr(tool, "permission_level", None)
    if callable(probe):
        try:
            return str(probe(getattr(tool, "generic_args", {}) or {})[0])
        except Exception:  # noqa: BLE001
            return "ask"
    return _CATEGORY_LEVEL.get(getattr(tool, "category", "info"), "ask")


def agent_view(server: Any) -> dict:
    turns = getattr(server, "_turns", None)
    steps = getattr(turns, "_max_steps", None)
    try:
        steps = turns._steps_for_turn() if turns is not None else steps
    except Exception:  # noqa: BLE001
        pass
    return {
        "maxSteps": steps,
        "maxStepsLimit": 20,
        "maxDepth": MAX_DEPTH,
        "subAgentSteps": DEFAULT_AGENT_STEPS,
        "subAgentStepsLimit": MAX_AGENT_STEPS,
    }


def _extensions():
    # Gmail set-up and MCP routes live with their modules; this file only
    # dispatches so api.py needs no new branches (see docs/connectors/).
    from ..connectors import routes as connector_routes
    from ..mcp import routes as mcp_routes

    return connector_routes, mcp_routes


def handle_callback(server: Any, raw_path: str):
    """``GET /oauth/callback`` page from the connectors module, or None."""
    if not hasattr(server, "_connector_setups"):
        return None          # connectors not wired: api.py's own callback page
    connector_routes, _ = _extensions()
    return connector_routes.handle_callback(server, raw_path)


def handle_delete(server: Any, path: str) -> Reply | None:
    for module in _extensions():
        handled = module.handle_delete(server, path)
        if handled is not None:
            return handled
    return None


def handle_get(server: Any, path: str) -> Reply | None:
    for module in _extensions():
        handled = module.handle_get(server, path)
        if handled is not None:
            return handled
    if path == "/api/tools":
        return 200, tools_view(server)
    if path == "/api/tools/approvals":
        broker = getattr(server, "_approvals", None)
        return 200, {"approvals": broker.pending() if broker is not None else []}
    if path == "/api/permissions":
        policy = getattr(server, "_policy", None)
        if policy is None:
            return 501, {"error": "permissions are not configured"}
        return 200, policy.view()
    if path == "/api/agent":
        return 200, agent_view(server)
    return None


def is_post_route(path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if path in ("/api/tools/run", "/api/permissions", "/api/agent", "/api/permissions/pick-folder"):
        return True
    if any(module.is_post_route(path) for module in _extensions()):
        return True
    return len(parts) == 4 and parts[:3] == ["api", "tools", "approvals"]


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    for module in _extensions():
        if module.is_post_route(path):
            return module.handle_post(server, path, payload)
    parts = [p for p in path.split("/") if p]
    if path == "/api/tools/run":
        return _run(server, payload)
    if path == "/api/permissions":
        policy = getattr(server, "_policy", None)
        if policy is None:
            return 501, {"error": "permissions are not configured"}
        try:
            view = policy.update(payload)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        _publish(server, "permissions", {k: view[k] for k in ("mode", "workspace", "readFiles",
                                                             "writeFiles", "runShell", "network")})
        _record(server, "permissions", f"mode {view.get('mode')}")
        return 200, view
    if path == "/api/permissions/pick-folder":
        return _pick_folder(server)
    if path == "/api/agent":
        settings = getattr(server, "_settings", None)
        if settings is None:
            return 501, {"error": "settings are not configured"}
        if "maxSteps" not in payload:
            return 400, {"error": "maxSteps is required"}
        try:
            steps = int(payload["maxSteps"])
        except (TypeError, ValueError):
            return 400, {"error": "maxSteps must be an integer"}
        if not 1 <= steps <= 20:
            return 400, {"error": "maxSteps must be between 1 and 20"}
        settings.write("agent", {"maxSteps": steps})
        return 200, agent_view(server)
    if len(parts) == 4 and parts[:3] == ["api", "tools", "approvals"]:
        broker = getattr(server, "_approvals", None)
        if broker is None:
            return 501, {"error": "approvals are not configured"}
        approval = broker.resolve(parts[3], bool(payload.get("approved", False)),
                                  remember=bool(payload.get("remember", False)))
        if approval is None:
            return 404, {"error": "unknown or already answered approval"}
        return 200, {"approval_id": approval.id, "approved": bool(approval.approved),
                     "remember": approval.remember}
    return 404, {"error": "not found"}


def _run(server: Any, payload: dict) -> Reply:
    executor = getattr(server, "_executor", None)
    if executor is None:
        return 503, {"error": "tools are not available"}
    name = str(payload.get("name") or payload.get("tool") or "").strip()
    if not name:
        return 400, {"error": "name is required"}
    args = payload.get("args", payload.get("arguments", {}))
    if not isinstance(args, dict):
        return 400, {"error": "args must be an object"}
    approved = bool(payload.get("approved", False))
    session_id = str(payload.get("session_id") or "manual")
    ctx = CallContext(session_id=session_id, approved=approved, interactive=False,
                      source="user", spawn=_spawner(server, session_id))
    decision = executor.check(name, args, ctx)
    if decision.kind == "deny":
        return 403, {"decision": "deny", "reasons": list(decision.reasons), "tool": name}
    if decision.kind == "ask" and not approved:
        # Same pattern as connector actions: show a card, then POST again
        # with "approved": true.
        return 403, {"decision": "ask", "reasons": list(decision.reasons), "tool": name, "args": args}
    execution = executor.execute(name, args, ctx)
    return 200, {"result": execution.view()}


def _spawner(server: Any, session_id: str):
    turns = getattr(server, "_turns", None)
    spawn_agent = getattr(turns, "spawn_agent", None)
    if not callable(spawn_agent) or not getattr(turns, "available", False):
        return None

    def spawn(task: str, tools: list[str] | None, steps: int) -> ToolResult:
        try:
            result = spawn_agent(task, tools, steps, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - reported as a failed tool
            return failed("run_agent", f"The sub-agent could not run: {type(exc).__name__}: {exc}")
        from .calls import strip_think

        summary = strip_think(result.answer).strip() or "(no summary)"
        ok = result.status in ("done", "max-steps")
        return ToolResult(ok=ok, content=f"Sub-agent result ({result.status}):\n{summary}",
                          data={"status": result.status, "steps": len(result.steps),
                                "tool_calls": result.tool_calls},
                          error=None if ok else f"sub-agent {result.status}", source="run_agent")

    return spawn


def _publish(server: Any, kind: str, data: dict) -> None:
    events = getattr(server, "_events", None)
    if events is None:
        return
    try:
        events.publish(kind, data)
    except Exception:  # noqa: BLE001
        pass


def _record(server: Any, subject: str, why: str) -> None:
    record = getattr(server, "_record", None)
    if callable(record):
        record(subject, why)


def _pick_folder(server: Any) -> Reply:
    """Home > "Change folder": the native folder dialog, then the usual
    validated workspace change. ``{"cancelled": true}`` when closed."""
    from .folder_picker import PickerUnavailable, pick_folder

    policy = getattr(server, "_policy", None)
    if policy is None:
        return 501, {"error": "permissions are not configured"}
    try:
        start = str(policy.workspace.root)
    except Exception:  # noqa: BLE001
        start = None
    try:
        chosen = pick_folder(start)
    except PickerUnavailable as exc:
        return 409, {"error": str(exc), "unavailable": True}
    except Exception as exc:  # noqa: BLE001 - a dialog failure is a reply, not a crash
        return 500, {"error": f"the folder dialog failed ({type(exc).__name__})"}
    if not chosen:
        return 200, {"cancelled": True}
    try:
        view = policy.update({"workspaceRoot": chosen})
    except ValueError as exc:
        return 400, {"error": str(exc)}
    _publish(server, "permissions", {k: view[k] for k in ("mode", "workspace", "readFiles",
                                                         "writeFiles", "runShell", "network")})
    _record(server, "permissions", "workspace changed from the folder dialog")
    return 200, view
