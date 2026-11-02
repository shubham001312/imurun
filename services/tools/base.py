"""The tool contract.

A tool is a small, named capability the model can draw on: the current
time, the weather in a city, the web, the user's files, a shell. Every tool
returns the same shape so the agent loop never has to special-case one.

The rules that matter:

* a tool never invents data. When it cannot reach the network or the source
  is gone, it says so in the result and the model is told to say so too;
* a tool result carries both a short human sentence (``content``) and the
  raw numbers (``data``) so the model can quote the facts without parsing
  prose;
* ``available()`` tells the caller whether the tool can run right now, so
  the shell can show honest state instead of a promise;
* ``category`` tells the permission policy what kind of action this is
  (see ``permissions.py``). The policy, not the tool, decides whether it
  may run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Permission categories. "info" and "network" never touch the disk; "read",
# "write", "delete" and "shell" are gated by the workspace policy; "agent"
# runs a nested loop whose own tool calls are gated one by one.
# "connector-read", "connector-write" and "mcp" tools decide their own level
# per call through ``permission_level(args) -> (level, reason)``
# (``permissions.py`` LEVELS): a Gmail tool that removes a label asks, one
# that adds a label follows the mode; sending mail always asks.
CATEGORIES = ("info", "network", "read", "write", "delete", "shell", "agent",
              "connector-read", "connector-write", "mcp")


@dataclass
class ToolResult:
    """The outcome of one tool call."""

    ok: bool
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    source: str = ""
    latency_ms: int = 0
    offline: bool = False

    def view(self) -> dict[str, Any]:
        """JSON-safe view for the API and the shell."""
        return {
            "ok": self.ok,
            "content": self.content,
            "data": self.data,
            "error": self.error,
            "source": self.source,
            "latency_ms": self.latency_ms,
            "offline": self.offline,
        }


def failed(source: str, message: str, **extra: Any) -> ToolResult:
    """A failed result whose content repeats the error, so the model sees it."""
    return ToolResult(ok=False, error=message, content=message, source=source, **extra)


class Tool:
    """Base class for every tool.

    Subclasses set the class attributes and implement ``run``. The
    ``description`` is what the model reads, so it must say when the tool
    applies and what it returns - not how it works. Keep the first sentence
    self-contained: small-context prompts only show that sentence.
    """

    name: str = "tool"
    description: str = ""
    parameters: dict[str, Any] = {}
    requires_network: bool = False
    category: str = "info"

    def available(self) -> bool:
        """Whether the tool can run right now."""
        return True

    def unavailable_reason(self) -> str:
        """Why the tool cannot run. Empty when it can."""
        return "" if self.available() else "not available"

    def run(self, args: dict[str, Any]) -> ToolResult:  # pragma: no cover - interface
        raise NotImplementedError

    # ------------------------------------------------------------ describing
    def schema(self) -> dict[str, Any]:
        """The JSON schema record served on ``GET /api/tools``."""
        params = dict(self.parameters or {})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        params.setdefault("required", [])
        return {
            "name": self.name,
            "description": " ".join(str(self.description).split()),
            "parameters": params,
            "category": self.category,
            "requires_network": self.requires_network,
        }

    def summary(self) -> str:
        """The first sentence of the description."""
        text = " ".join(str(self.description).split())
        head, dot, _rest = text.partition(". ")
        return head + ("." if dot else "")
