"""The text contract between the loop and the model for tool use.

Three pieces, kept short because small models follow short rules:

* ``tool_prompt`` - the system-prompt section: how to call a tool, the rules
  for when to call one, and one line per tool with its argument schema;
* ``result_message`` - how a tool result is fed back. It is sent as a
  ``user`` message, not a ``tool`` role: the engine passes the role string
  straight into the chat template, and only Llama 3 (``ipython``) and ChatML
  (``tool``) know a tool role - DeepSeek, Gemma, Phi-3 and Mistral do not.
  A ``user`` message with a fixed ``[tool result: name]`` header works with
  every template;
* ``retry_message`` - the one retry after an unreadable call.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

RESULT_HEADER = "[tool result: {name}]"
# Characters of one tool result fed back to the model. The loop lowers this
# further when the context window is small.
DEFAULT_RESULT_CHARS = 6000

EXAMPLE = '```tool\n{"name": "web_search", "args": {"query": "latest Python release"}}\n```'


def _compact_args(parameters: dict[str, Any]) -> str:
    props = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])
    parts = []
    for key, spec in props.items():
        kind = str((spec or {}).get("type") or "string")
        enum = (spec or {}).get("enum")
        if enum:
            kind = "|".join(str(e) for e in enum)
        parts.append(f'"{key}": {kind}{"" if key in required else "?"}')
    return "{" + ", ".join(parts) + "}"


def tool_lines(tools: Iterable[Any], *, full: bool = False) -> list[str]:
    """One line per tool: ``- name {"arg": type, "opt": type?} - summary``.

    ``full`` (large context windows) renders the real JSON schema instead.
    """
    lines = []
    for tool in tools:
        if full:
            schema = tool.schema()
            lines.append(
                "- " + json.dumps(
                    {"name": schema["name"], "description": schema["description"],
                     "parameters": schema["parameters"]},
                    ensure_ascii=False, separators=(",", ":"),
                )
            )
        else:
            lines.append(f"- {tool.name} {_compact_args(tool.parameters)} - {tool.summary()}")
    return lines


def tool_prompt(tools: list[Any], *, max_steps: int, full: bool = False, workspace: str = "",
                hidden_services: list[str] | None = None) -> str:
    if not tools:
        return ""
    names = {t.name for t in tools}
    lines = [
        "# Tools",
        "You can use tools. To call one, reply with only this block and stop:",
        EXAMPLE,
        "The result comes back in the next message, starting with [tool result: name].",
        "Then answer the user, or call another tool. "
        f"You have at most {max_steps - 1} tool calls for one question.",
        "",
        "When to use a tool:",
        "- Anything about the present: news, prices, scores, weather, today's date or time, "
        "\"latest\", \"current\", \"now\", recent releases or events. Your training data is old.",
    ]
    if names & {"file_read", "file_list", "file_write", "shell"}:
        where = f" (workspace: {workspace})" if workspace else ""
        lines.append(f"- Anything about files, folders or commands on this computer{where}. "
                     "Paths are relative to the workspace.")
    connectors = any(getattr(t, "category", "") in ("connector-read", "connector-write", "mcp") for t in tools)
    if connectors:
        lines.append("- The user's email, repositories, chat and cloud accounts: use the matching tool.")
    lines += [
        "- Not for general knowledge, maths, writing or code you can answer yourself.",
        "",
        "Rules:",
        "- Never say you searched, checked, read, ran or looked up something unless a "
        "[tool result] above shows it.",
        "- Never invent a tool result. If a tool failed or you have none, say so and do not "
        "guess live values.",
        "- One tool call per reply. Use the exact tool names below.",
    ]
    if connectors:
        lines += [
            "- Never claim you sent, posted, deployed or changed something in an account "
            "without a [tool result] that shows it.",
        ]
        if "gmail_draft" in names and "gmail_send" in names:
            lines.append("- Email: prefer gmail_draft; use gmail_send only when the user said to send.")
    lines += [
        "",
        "Tools (arguments marked ? are optional):",
    ]
    lines += tool_lines(tools, full=full)
    if hidden_services:
        lines.append(
            "Connected accounts whose tools are not listed here: " + ", ".join(hidden_services)
            + ". If the user asks about one of them, say which account and the tools will be offered."
        )
    return "\n".join(lines)


def result_message(name: str, result: Any, *, limit: int = DEFAULT_RESULT_CHARS) -> dict[str, str]:
    """The message that carries one tool result back to the model."""
    header = RESULT_HEADER.format(name=name)
    if getattr(result, "ok", False):
        body = (getattr(result, "content", "") or "").strip() or "(no output)"
    else:
        reason = getattr(result, "error", None) or getattr(result, "content", "") or "the tool failed"
        if getattr(result, "offline", False):
            reason = "no network access right now"
        body = (
            f"FAILED: {reason}\n"
            "Do not guess what the tool would have returned. Tell the user it failed, "
            "or try a different tool or input."
        )
    if len(body) > limit:
        body = body[:limit] + f"\n... [truncated, {len(body) - limit} more characters]"
    return {"role": "user", "content": f"{header}\n{body}"}


def retry_message(error: str, names: Iterable[str]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            f"[tool error] Your tool call could not be read: {error}.\n"
            "Reply again with exactly one block like this, and nothing after it:\n"
            f"{EXAMPLE}\n"
            f"Valid tool names: {', '.join(sorted(names))}. "
            "Or answer directly without a tool."
        ),
    }


def budget_message(names_used: Iterable[str]) -> dict[str, str]:
    used = ", ".join(sorted(set(names_used))) or "none"
    return {
        "role": "user",
        "content": (
            f"[note] No more tool calls are available for this question (used: {used}). "
            "Answer now from the tool results above. If they are not enough, say what "
            "you could not find. Do not write a tool block."
        ),
    }


def realtime_nudge(offline: bool) -> dict[str, str]:
    if offline:
        text = (
            "[note] This question is about live or current information, and there is no "
            "network access right now. Say plainly that you cannot check live data at the "
            "moment. Do not state current values (prices, scores, weather, news, dates of "
            "recent events) as if they were live."
        )
    else:
        text = (
            "[note] This question is about live or current information and you have no "
            "tool result for it. Call a tool (for example web_search or time_now) now, or "
            "say plainly that you could not check. Do not answer with current values from "
            "memory."
        )
    return {"role": "user", "content": text}
