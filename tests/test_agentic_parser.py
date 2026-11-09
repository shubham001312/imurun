"""Tool-call parser: the formats small models produce, and malformed input."""
from __future__ import annotations

from services.tools.calls import (
    StreamGuard,
    display_step,
    loads_forgiving,
    parse_tool_calls,
    split_think,
)

SCHEMAS = {
    "web_search": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "file_read": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    "file_write": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path"]},
    "time_now": {"type": "object", "properties": {"timezone": {"type": "string"}}},
    "shell": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
}


def one(text):
    parsed = parse_tool_calls(text, SCHEMAS)
    assert len(parsed.calls) == 1, (text, parsed)
    return parsed.calls[0], parsed


def test_canonical_fenced_block():
    call, parsed = one('```tool\n{"name": "web_search", "args": {"query": "python 3.13"}}\n```')
    assert call.name == "web_search" and call.args == {"query": "python 3.13"}
    assert parsed.prefix == "" and parsed.marker


def test_prefix_kept_and_invented_result_dropped():
    text = ('Let me search.\n```tool\n{"name": "web_search", "args": {"query": "x"}}\n```\n'
            "Result: Python 4 was released today.")
    call, parsed = one(text)
    assert parsed.prefix == "Let me search."
    assert "Python 4" not in parsed.prefix


def test_single_quotes_and_python_literals():
    call, _ = one("```tool\n{'name': 'file_write', 'args': {'path': 'a.txt', 'content': 'hi', 'x': True}}\n```")
    assert call.name == "file_write" and call.args["path"] == "a.txt" and call.args["x"] is True


def test_missing_closing_braces_and_unclosed_fence():
    call, _ = one('```tool\n{"name": "web_search", "args": {"query": "weather berlin"')
    assert call.args == {"query": "weather berlin"}


def test_trailing_commas_and_unquoted_keys():
    call, _ = one('```tool\n{name: "web_search", args: {query: "a",},}\n```')
    assert call.args == {"query": "a"}


def test_llama31_python_tag_json_parameters():
    call, _ = one('<|python_tag|>{"name": "web_search", "parameters": {"query": "news"}}<|eom_id|>')
    assert call.name == "web_search" and call.args == {"query": "news"}


def test_llama31_builtin_call_syntax():
    call, _ = one('<|python_tag|>brave_search.call(query="euro to inr")')
    assert call.name == "web_search" and call.args == {"query": "euro to inr"}


def test_qwen_tool_call_tags():
    call, _ = one('<tool_call>\n{"name": "time_now", "arguments": {}}\n</tool_call>')
    assert call.name == "time_now" and call.args == {}


def test_function_tag():
    call, _ = one('<function=web_search>{"query": "x"}</function>')
    assert call.name == "web_search" and call.args == {"query": "x"}


def test_bare_json_known_tool_with_trailing_text():
    call, parsed = one('{"name": "shell", "args": {"command": "dir"}} and then I will report')
    assert call.args == {"command": "dir"}


def test_bare_json_unknown_name_is_just_an_answer():
    parsed = parse_tool_calls('Here is the record: {"name": "Alice", "age": 3}', SCHEMAS)
    assert not parsed.calls and parsed.error is None


def test_aliases_and_string_arguments():
    call, _ = one('```tool\n{"name": "read_file", "args": "notes/todo.md"}\n```')
    assert call.name == "file_read" and call.args == {"path": "notes/todo.md"}


def test_openai_style_function_object():
    call, _ = one('```json\n{"function": {"name": "web_search", "arguments": "{\\"query\\": \\"q\\"}"}}\n```')
    assert call.name == "web_search" and call.args == {"query": "q"}


def test_marker_with_garbage_reports_error():
    parsed = parse_tool_calls("```tool\nplease search the web for me\n```", SCHEMAS)
    assert not parsed.calls and parsed.marker and parsed.error


def test_unknown_tool_in_marked_block_reports_names():
    parsed = parse_tool_calls('```tool\n{"name": "launch_rockets", "args": {}}\n```', SCHEMAS)
    assert not parsed.calls and "web_search" in (parsed.error or "")


def test_plain_code_block_is_not_a_call():
    text = "Use this:\n```python\nprint({'name': 'x'})\n```"
    parsed = parse_tool_calls(text, SCHEMAS)
    assert not parsed.calls and parsed.error is None


def test_list_of_calls_is_capped():
    text = ('```tool\n[{"name": "time_now", "args": {}}, {"name": "web_search", "args": {"query": "a"}},'
            ' {"name": "web_search", "args": {"query": "b"}}, {"name": "web_search", "args": {"query": "c"}}]\n```')
    parsed = parse_tool_calls(text, SCHEMAS, max_calls=3)
    assert [c.name for c in parsed.calls] == ["time_now", "web_search", "web_search"]


def test_empty_and_none_inputs():
    assert not parse_tool_calls("", SCHEMAS).calls
    assert not parse_tool_calls(None, SCHEMAS).calls  # type: ignore[arg-type]
    assert loads_forgiving("")[0] is None
    assert loads_forgiving("no json here")[0] is None


def test_split_think_variants():
    assert split_think("<think>plan</think>Answer") == ("Answer", "plan")
    assert split_think("plan first</think>\nAnswer") == ("Answer", "plan first")  # template ate <think>
    assert split_think("Hi <think>still thinking") == ("Hi", "still thinking")
    assert split_think("no thought") == ("no thought", "")


def test_call_inside_think_is_ignored():
    raw = '<think>maybe ```tool\n{"name": "shell", "args": {"command": "x"}}\n```</think>The answer is 4.'
    visible, _ = split_think(raw)
    assert not parse_tool_calls(visible, SCHEMAS).calls


def test_stream_guard_hides_split_marker():
    guard = StreamGuard()
    shown = "".join(guard.feed(t) for t in ["Sure. ", "``", "`to", "ol\n{\"name\"", ": 1}"]) + guard.flush()
    assert shown == "Sure. "
    plain = StreamGuard()
    text = "".join(plain.feed(t) for t in ["Use ", "``", "`py", "thon\nx=1\n```"]) + plain.flush()
    assert text == "Use ```python\nx=1\n```"


def test_display_step_is_readable():
    raw = '<think>need data</think>Checking.\n```tool\n{"name": "web_search", "args": {"query": "q"}}\n```'
    parsed = parse_tool_calls(split_think(raw)[0], SCHEMAS)
    shown = display_step(raw, parsed)
    assert "<think>need data</think>" in shown and "Using `web_search`" in shown and "```tool" not in shown
