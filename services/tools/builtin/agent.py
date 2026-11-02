"""run_agent: hand a self-contained sub-task to a nested agent loop.

The sub-agent is the same local model, so it is kept cheap: no chat
history, no persona, a short system prompt, its own small step budget
(default 4) and only the tools it is given. It returns a summary, which is
all the parent sees. Every tool call it makes goes through the same
executor and permission policy as the parent's.

Recursion is bounded by ``executor.MAX_DEPTH`` (2): the main loop is depth
0, a sub-agent depth 1, and a sub-agent's sub-agent depth 2, where
``run_agent`` is no longer offered or allowed.

The tool itself does not know how to build a loop; the loop that runs it
puts a ``spawn`` callable on the call context.
"""
from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult, failed

DEFAULT_AGENT_STEPS = 4
MAX_AGENT_STEPS = 8


class RunAgentTool(Tool):
    name = "run_agent"
    category = "agent"
    description = (
        "Give a self-contained sub-task to a helper agent that can use tools "
        "and returns a short summary. Use it for multi-step research or file "
        "work that would clutter this conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the helper must do, with every detail it needs."},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "Tool names the helper may use (default: the same as yours, minus run_agent at the depth limit)."},
            "max_steps": {"type": "integer", "description": f"Step budget (default {DEFAULT_AGENT_STEPS}, max {MAX_AGENT_STEPS}).",
                          "minimum": 1, "maximum": MAX_AGENT_STEPS, "default": DEFAULT_AGENT_STEPS},
        },
        "required": ["task"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        return failed(self.name, "run_agent only works inside a turn (no model is attached to this call).")

    def run_with_context(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        spawn = getattr(ctx, "spawn", None)
        if spawn is None:
            return self.run(args)
        task = str(args.get("task") or "").strip()
        if not task:
            return failed(self.name, "No task was given.")
        tools = [str(t) for t in (args.get("tools") or []) if str(t).strip()]
        try:
            steps = max(1, min(int(args.get("max_steps") or DEFAULT_AGENT_STEPS), MAX_AGENT_STEPS))
        except (TypeError, ValueError):
            steps = DEFAULT_AGENT_STEPS
        return spawn(task, tools or None, steps)
