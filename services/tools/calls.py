"""Reading tool calls out of what a small local model writes.

The bundled models are 1B-8B Llama 3 / Qwen / DeepSeek-R1 distills. None of
them has a reliable native function-calling channel through the engine, so
the contract is plain text (see ``protocol.py``)::

    ```tool
    {"name": "web_search", "args": {"query": "latest python release"}}
    ```

Small models drift from any format, so this parser also accepts what they
actually produce, in this order of preference:

1. a fenced block labelled ``tool`` / ``tool_call`` / ``tool_code`` /
   ``function`` (or an unlabelled / ``json`` fence whose object names a
   known tool);
2. ``<tool_call>{...}</tool_call>`` - the Qwen 2.5 / Hermes training format;
3. ``<|python_tag|>{...}`` or ``<|python_tag|>name.call(k="v")`` - Llama 3.1;
4. ``<function=name>{...}</function>`` - Llama 3.1's custom-tool format;
5. a bare JSON object (or list of objects) whose ``name`` is a known tool;
6. a reply that is exactly ``name(k="v")`` for a known tool.

The JSON inside is repaired best-effort: trailing text, single quotes,
Python literals, unquoted keys, trailing commas and missing closing braces.
When a call marker is present but nothing usable comes out, the result
carries an ``error`` and the loop asks the model to retry once.

Everything after the first call is dropped: models love to continue with an
invented "Result: ..." and that text must never reach the user.

DeepSeek-R1 distills think first (``<think>...</think>``; the chat template
often swallows the opening tag, leaving only ``</think>``). ``split_think``
separates the thought from the visible reply; tool calls are only read from
the visible part, and only the visible part goes back into history.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# ----------------------------------------------------------------- thinking
_THINK_PAIR = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)


def split_think(text: str) -> tuple[str, str]:
    """Return ``(visible, thought)`` for a raw model reply."""
    if not text:
        return "", ""
    rest = str(text)
    thoughts: list[str] = []
    lower = rest.lower()
    close = lower.find("</think>")
    opening = lower.find("<think>")
    # Orphan close: the template already emitted <think>, so the reply
    # starts inside the thought.
    if close >= 0 and (opening < 0 or close < opening):
        thoughts.append(rest[:close])
        rest = rest[close + len("</think>"):]
    thoughts.extend(_THINK_PAIR.findall(rest))
    rest = _THINK_PAIR.sub("", rest)
    unclosed = _THINK_OPEN.search(rest)
    if unclosed:  # generation stopped while thinking
        thoughts.append(rest[unclosed.end():])
        rest = rest[: unclosed.start()]
    thought = "\n".join(t.strip() for t in thoughts if t.strip())
    return rest.strip(), thought


def strip_think(text: str) -> str:
    return split_think(text)[0]


# ---------------------------------------------------------------- the calls
@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    raw: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def key(self) -> str:
        """Identity for duplicate detection: name plus canonical arguments."""
        return self.name + ":" + json.dumps(self.args, sort_keys=True, ensure_ascii=False, default=str)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args}


@dataclass
class ParsedReply:
    calls: list[ToolCall] = field(default_factory=list)
    prefix: str = ""          # visible text before the first call
    marker: bool = False      # a call marker was seen
    error: str | None = None  # set when a marker was seen but no usable call came out

    @property
    def wants_tools(self) -> bool:
        return bool(self.calls)


# Names small models reach for instead of ours. Lower-case, ``-``/space as ``_``.
ALIASES: dict[str, str] = {
    "search": "web_search", "websearch": "web_search", "google": "web_search",
    "google_search": "web_search", "brave_search": "web_search", "bing_search": "web_search",
    "internet_search": "web_search", "duckduckgo": "web_search", "search_web": "web_search",
    "fetch": "web_fetch", "browse": "web_fetch", "open_url": "web_fetch", "read_url": "web_fetch",
    "fetch_url": "web_fetch", "get_url": "web_fetch", "web_browse": "web_fetch",
    "read_file": "file_read", "cat": "file_read", "open_file": "file_read", "readfile": "file_read",
    "write_file": "file_write", "create_file": "file_write", "save_file": "file_write",
    "writefile": "file_write", "append_file": "file_write",
    "list_files": "file_list", "list_dir": "file_list", "ls": "file_list", "dir": "file_list",
    "list_directory": "file_list", "tree": "file_list", "directory_tree": "file_list",
    "mkdir": "make_dir", "create_dir": "make_dir", "create_directory": "make_dir",
    "create_folder": "make_dir", "make_directory": "make_dir",
    "move": "file_move", "mv": "file_move", "rename": "file_move", "move_file": "file_move",
    "copy": "file_copy", "cp": "file_copy", "copy_file": "file_copy",
    "delete": "file_delete", "rm": "file_delete", "remove": "file_delete", "delete_file": "file_delete",
    "bash": "shell", "sh": "shell", "cmd": "shell", "powershell": "shell", "terminal": "shell",
    "run": "shell", "exec": "shell", "execute": "shell", "run_command": "shell",
    "run_shell": "shell", "command": "shell", "shell_command": "shell",
    "time": "time_now", "get_time": "time_now", "current_time": "time_now", "datetime": "time_now",
    "get_date": "time_now", "date": "time_now", "now": "time_now",
    "get_weather": "weather", "weather_now": "weather", "forecast": "weather",
    "get_location": "location", "where_am_i": "location",
    "agent": "run_agent", "subagent": "run_agent", "sub_agent": "run_agent", "delegate": "run_agent",
    # Connector tools (docs/connectors/REQUESTS.md L3).
    "send_email": "gmail_send", "email_send": "gmail_send", "send_mail": "gmail_send",
    "gmail": "gmail_search", "search_email": "gmail_search", "search_mail": "gmail_search",
    "list_emails": "gmail_search", "read_email": "gmail_read", "get_email": "gmail_read",
    "draft_email": "gmail_draft", "create_draft": "gmail_draft",
    "create_issue": "github_create_issue", "github_issue": "github_create_issue",
    "post_message": "slack_post", "slack_message": "slack_post", "send_slack": "slack_post",
}

_NAME_KEYS = ("name", "tool", "tool_name", "function", "action", "command_name")
_ARG_KEYS = ("args", "arguments", "parameters", "params", "input", "action_input", "tool_input")
_TOOL_FENCE_LABELS = {"tool", "tool_call", "tool_code", "toolcall", "function", "function_call", "call"}

_FENCE = re.compile(r"```[ \t]*([A-Za-z_]*)[ \t]*\r?\n?(.*?)(?:```|\Z)", re.DOTALL)
_XML_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.DOTALL | re.IGNORECASE)
_PY_TAG = re.compile(r"<\|python_tag\|>\s*(.*?)\s*(?:<\|eom_id\|>|<\|eot_id\|>|\Z)", re.DOTALL)
_FUNC_TAG = re.compile(r"<function=([\w.\-]+)>\s*(.*?)\s*(?:</function>|\Z)", re.DOTALL)
_PY_CALL = re.compile(r"^\s*([A-Za-z_][\w.]*?)(?:\.call)?\s*\((.*)\)\s*;?\s*$", re.DOTALL)


def canonical_name(name: Any, known: Iterable[str] = ()) -> str:
    text = str(name or "").strip().strip("`'\"").lower()
    text = re.sub(r"^(functions|tools|tool)\.", "", text)
    text = re.sub(r"\.call$", "", text)
    text = re.sub(r"[\s\-]+", "_", text)
    known_set = set(known)
    if text in known_set:
        return text
    return ALIASES.get(text, text)


def parse_tool_calls(
    text: str,
    tools: Mapping[str, Mapping[str, Any]] | Iterable[str] | None = None,
    *,
    max_calls: int = 3,
) -> ParsedReply:
    """Find tool calls in the *visible* part of a reply.

    ``tools`` maps tool names to their JSON-schema ``parameters`` (a plain
    list of names also works). It is used to accept bare JSON only when it
    names a real tool, and to map a bare string argument onto the tool's
    single required parameter.
    """
    schemas: dict[str, Mapping[str, Any]]
    if tools is None:
        schemas = {}
    elif isinstance(tools, Mapping):
        schemas = dict(tools)
    else:
        schemas = {name: {} for name in tools}
    known = list(schemas)
    visible = str(text or "")
    reply = ParsedReply()

    candidates: list[tuple[int, str, bool, str | None]] = []  # (start, body, marked, forced_name)
    for match in _FENCE.finditer(visible):
        label = match.group(1).lower()
        body = match.group(2)
        if label in _TOOL_FENCE_LABELS:
            candidates.append((match.start(), body, True, None))
        elif label in ("", "json", "javascript", "js") and _looks_like_call(body):
            candidates.append((match.start(), body, False, None))
    for match in _XML_CALL.finditer(visible):
        candidates.append((match.start(), match.group(1), True, None))
    for match in _PY_TAG.finditer(visible):
        candidates.append((match.start(), match.group(1), True, None))
    for match in _FUNC_TAG.finditer(visible):
        candidates.append((match.start(), match.group(2), True, match.group(1)))

    if not candidates:
        start = _bare_json_start(visible)
        if start >= 0:
            candidates.append((start, visible[start:], False, None))
        else:
            single = _PY_CALL.match(visible.strip()) if visible.strip() else None
            if single and canonical_name(single.group(1), known) in schemas:
                candidates.append((0, visible.strip(), False, None))

    candidates.sort(key=lambda item: item[0])
    errors: list[str] = []
    first_start: int | None = None
    for start, body, marked, forced in candidates:
        if len(reply.calls) >= max_calls:
            break
        found, error = _calls_from_body(body, schemas, forced)
        if marked:
            reply.marker = True
        if found:
            if first_start is None:
                first_start = start
            reply.calls.extend(found[: max_calls - len(reply.calls)])
        elif marked and error:
            errors.append(error)
            if first_start is None:
                first_start = start

    if first_start is not None:
        reply.prefix = visible[:first_start].strip()
    else:
        reply.prefix = visible.strip()
    if not reply.calls and reply.marker:
        reply.error = errors[0] if errors else "the tool block was empty"
    return reply


def _looks_like_call(body: str) -> bool:
    return bool(re.search(r"""["']?(name|tool|function)["']?\s*:""", body))


def _bare_json_start(text: str) -> int:
    """Index of the first ``{``/``[`` that opens something call-shaped."""
    for match in re.finditer(r"[\[{]", text):
        window = text[match.start(): match.start() + 200]
        if re.match(r"""[\[{]\s*\{?\s*["']?(name|tool|function)["']?\s*:""", window):
            return match.start()
    return -1


def _calls_from_body(
    body: str, schemas: Mapping[str, Mapping[str, Any]], forced: str | None
) -> tuple[list[ToolCall], str | None]:
    body = (body or "").strip()
    known = list(schemas)
    if forced is not None:
        obj, error = loads_forgiving(body) if body else ({}, None)
        if obj is None:
            return [], error
        name = canonical_name(forced, known)
        args = obj if isinstance(obj, dict) else {}
        return _accept(name, args, body, schemas)

    if not body:
        return [], "the tool block was empty"
    obj, error = loads_forgiving(body)
    if obj is None:
        py = _PY_CALL.match(body)
        if py:
            name = canonical_name(py.group(1), known)
            args = _python_kwargs(py.group(2), name, schemas)
            if args is not None:
                return _accept(name, args, body, schemas)
        return [], error or "the tool block is not valid JSON"
    items = obj if isinstance(obj, list) else [obj]
    calls: list[ToolCall] = []
    problem: str | None = None
    for item in items:
        if not isinstance(item, dict):
            problem = "a tool call must be a JSON object"
            continue
        name, args = _normalize(item, schemas)
        if not name:
            problem = 'the tool call has no "name"'
            continue
        found, err = _accept(name, args, body, schemas)
        calls.extend(found)
        problem = problem or err
    return calls, (None if calls else problem)


def _accept(
    name: str, args: Any, raw: str, schemas: Mapping[str, Mapping[str, Any]]
) -> tuple[list[ToolCall], str | None]:
    if schemas and name not in schemas:
        return [], f"there is no tool called {name!r}; use one of: {', '.join(sorted(schemas))}"
    if not isinstance(args, dict):
        args = _single_arg(name, args, schemas)
    return [ToolCall(name=name, args=args, raw=raw)], None


def _normalize(item: dict, schemas: Mapping[str, Mapping[str, Any]]) -> tuple[str, Any]:
    known = list(schemas)
    raw_name: Any = None
    name_key = None
    for key in _NAME_KEYS:
        if key in item and item[key]:
            raw_name, name_key = item[key], key
            break
    args: Any = None
    if isinstance(raw_name, dict):  # OpenAI style {"function": {"name", "arguments"}}
        inner = raw_name
        raw_name = inner.get("name")
        args = inner.get("arguments", inner.get("parameters"))
    for key in _ARG_KEYS:
        if args is None and key in item:
            args = item[key]
    if isinstance(args, str):
        parsed, _ = loads_forgiving(args) if args.strip()[:1] in "{[" else (None, None)
        args = parsed if isinstance(parsed, dict) else args
    if args is None:
        args = {k: v for k, v in item.items() if k != name_key and k not in _ARG_KEYS}
    return canonical_name(raw_name, known), args


def _single_arg(name: str, value: Any, schemas: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Map a bare value onto the tool's only (or first required) parameter."""
    if value is None or value == "":
        return {}
    params = schemas.get(name) or {}
    required = list(params.get("required") or [])
    props = list((params.get("properties") or {}).keys())
    target = required[0] if required else (props[0] if props else "input")
    return {target: value}


def _python_kwargs(text: str, name: str, schemas: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """``query="x", n=3`` or a single positional ``"x"`` -> dict."""
    try:
        tree = ast.parse(f"f({text})", mode="eval")
    except SyntaxError:
        return None
    call = tree.body
    if not isinstance(call, ast.Call):
        return None
    out: dict[str, Any] = {}
    try:
        for kw in call.keywords:
            if kw.arg:
                out[kw.arg] = ast.literal_eval(kw.value)
        if call.args and not out:
            out = _single_arg(name, ast.literal_eval(call.args[0]), schemas)
    except (ValueError, SyntaxError):
        return None
    return out


# ------------------------------------------------------------- JSON repair
_SMART = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def loads_forgiving(text: str) -> tuple[Any | None, str | None]:
    """Parse JSON the way a small model writes it. ``(value, error)``."""
    source = (text or "").strip().translate(_SMART)
    if not source:
        return None, "empty"
    try:
        return json.loads(source), None
    except ValueError:
        pass
    start = min([i for i in (source.find("{"), source.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None, "no JSON object found"
    candidate = _balanced(source[start:])
    attempts = [candidate, _fix(candidate)]
    for attempt in attempts:
        try:
            return json.loads(attempt), None
        except ValueError:
            continue
    for attempt in attempts:
        try:
            return ast.literal_eval(_pythonish(attempt)), None
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
    return None, "the tool block is not valid JSON"


def _balanced(text: str) -> str:
    """The first complete top-level value; close it if the text ends early."""
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack and stack[-1] == char:
                stack.pop()
            if not stack:
                return text[: index + 1]
    tail = text.rstrip().rstrip(",")
    if quote:
        tail += quote
    return tail + "".join(reversed(stack))


def _fix(text: str) -> str:
    fixed = re.sub(r",\s*([}\]])", r"\1", text)                      # trailing commas
    fixed = re.sub(r"([{,]\s*)([A-Za-z_][\w\-]*)\s*:", r'\1"\2":', fixed)  # unquoted keys
    if '"' not in text:                                              # all single quotes
        fixed = fixed.replace("'", '"')
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    return fixed


def _pythonish(text: str) -> str:
    out = re.sub(r",\s*([}\]])", r"\1", text)
    out = re.sub(r"\btrue\b", "True", out)
    out = re.sub(r"\bfalse\b", "False", out)
    return re.sub(r"\bnull\b", "None", out)


# --------------------------------------------------------------- streaming
STREAM_MARKERS = ("```tool", "<tool_call>", "<|python_tag|>", "<function=", '{"name"', "{'name'")


class StreamGuard:
    """Hide a tool-call block from the live token stream.

    Tokens are passed through until one of ``STREAM_MARKERS`` appears; from
    then on the step is silent (the authoritative ``step`` event replaces the
    text anyway). A short tail that could be the start of a marker is held
    back so a marker split across tokens is still caught.
    """

    def __init__(self, markers: tuple[str, ...] = STREAM_MARKERS) -> None:
        self._markers = tuple(m.lower() for m in markers)
        self._held = ""
        self.suppressed = False

    def feed(self, token: str) -> str:
        if self.suppressed:
            return ""
        self._held += token
        lower = self._held.lower()
        hits = [lower.find(m) for m in self._markers if m in lower]
        if hits:
            cut = min(hits)
            out, self._held = self._held[:cut], ""
            self.suppressed = True
            return out
        keep = 0
        for marker in self._markers:
            for size in range(min(len(marker) - 1, len(lower)), 0, -1):
                if lower.endswith(marker[:size]):
                    keep = max(keep, size)
                    break
        out = self._held[: len(self._held) - keep] if keep else self._held
        self._held = self._held[len(out):]
        return out

    def flush(self) -> str:
        out, self._held = ("" if self.suppressed else self._held), ""
        return out


def display_step(raw: str, parsed: ParsedReply) -> str:
    """What the UI shows for a step that called tools.

    The thought (if any) stays as a ``<think>`` block so the frontend's
    reasoning splitter still finds it; the JSON is replaced by one line per
    call.
    """
    _visible, thought = split_think(raw)
    lines = []
    if thought:
        lines.append(f"<think>{thought}</think>")
    if parsed.prefix:
        lines.append(parsed.prefix)
    for call in parsed.calls:
        args = json.dumps(call.args, ensure_ascii=False)
        if len(args) > 300:
            args = args[:300] + "..."
        lines.append(f"Using `{call.name}` {args}")
    return "\n\n".join(lines)
