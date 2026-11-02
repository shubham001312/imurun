"""Keeping a turn inside the model's context window.

A multi-step tool turn grows with every result, and the bundled models have
2k-8k windows. Before every generation the loop calls ``fit``; it frees room
cheapest-first:

1. shorten old tool results (all but the two newest) to a short head;
2. summarise earlier conversation history with one side generation
   (emits ``compaction.start`` / ``compaction.progress`` / ``compaction.done``
   so the UI can show it) - or, with no event bus or no side model, drop the
   oldest history;
3. cut the newest tool result down;
4. as a last resort, trim the longest remaining message.

The budget is ``context_limit - reserve``, where ``reserve`` is room left for
the answer. Token counts are estimated (characters / 3.5): the engine's
tokenizer is not reachable from here, and over-estimating is the safe side.

Messages may carry private bookkeeping keys starting with ``_`` (``_kind``:
``history`` / ``tool_result`` / ``system``); ``public`` strips them before
anything is sent to the engine.
"""
from __future__ import annotations

from typing import Any, Callable

CHARS_PER_TOKEN = 3.5
PER_MESSAGE_OVERHEAD = 6  # role header and end-of-turn tokens


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += int(len(str(message.get("content", ""))) / CHARS_PER_TOKEN) + PER_MESSAGE_OVERHEAD
    return max(1, total)


def reserve_for(limit: int) -> int:
    """Tokens kept free for the reply."""
    return max(256, min(2048, limit // 4))


def public(messages: list[dict]) -> list[dict]:
    """Only ``role`` and ``content`` reach the engine."""
    return [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in messages]


def result_chars(limit: int) -> int:
    """Characters one tool result may use in a window of ``limit`` tokens."""
    return int(max(800, min(6000, limit * CHARS_PER_TOKEN / 4)))


def _shorten(message: dict, keep: int) -> bool:
    content = str(message.get("content", ""))
    if len(content) <= keep + 80:
        return False
    message["content"] = content[:keep] + f"\n... [shortened to fit the context window; {len(content) - keep} characters dropped]"
    return True


def fit(
    messages: list[dict],
    limit: int,
    *,
    summarize: Callable[[list[dict]], str] | None = None,
    publish: Callable[[str, dict], Any] | None = None,
    session_id: str = "",
) -> list[dict]:
    """Return ``messages`` trimmed to fit ``limit`` tokens (mutates copies only)."""
    budget = max(256, limit - reserve_for(limit))
    messages = [dict(m) for m in messages]
    if estimate_tokens(messages) <= budget:
        return messages

    # 1. old tool results
    results = [m for m in messages if m.get("_kind") == "tool_result"]
    for message in results[:-2]:
        _shorten(message, 400)
    if estimate_tokens(messages) <= budget:
        return messages

    # 2. history
    history = [m for m in messages if m.get("_kind") == "history"]
    if history:
        if summarize is not None and publish is not None and len(history) > 2:
            messages = _compact(messages, history, summarize, publish, session_id, budget)
        while estimate_tokens(messages) > budget:
            remaining = [m for m in messages if m.get("_kind") == "history"]
            if not remaining:
                break
            messages.remove(remaining[0])
    if estimate_tokens(messages) <= budget:
        return messages

    # 3. the newest tool results
    for message in reversed([m for m in messages if m.get("_kind") == "tool_result"]):
        over = estimate_tokens(messages) - budget
        if over <= 0:
            break
        keep = max(300, len(str(message.get("content", ""))) - int(over * CHARS_PER_TOKEN) - 100)
        _shorten(message, keep)

    # 4. the longest message, whatever it is (never the final user prompt)
    guard = 0
    while estimate_tokens(messages) > budget and guard < 8:
        guard += 1
        candidates = [m for m in messages[:-1] if len(str(m.get("content", ""))) > 600]
        if not candidates:
            break
        longest = max(candidates, key=lambda m: len(str(m.get("content", ""))))
        over = estimate_tokens(messages) - budget
        content = str(longest.get("content", ""))
        keep = max(400, len(content) - int(over * CHARS_PER_TOKEN) - 100)
        if not _shorten(longest, keep):
            break
    return messages


def _compact(
    messages: list[dict],
    history: list[dict],
    summarize: Callable[[list[dict]], str],
    publish: Callable[[str, dict], Any],
    session_id: str,
    budget: int,
) -> list[dict]:
    keep_recent = 2
    to_summarize = history[:-keep_recent]
    if not to_summarize:
        return messages
    _emit(publish, "compaction.start", {
        "session_id": session_id, "original_messages": len(history), "target_tokens": budget,
    })
    _emit(publish, "compaction.progress", {
        "session_id": session_id, "stage": "summarizing",
        "messages_to_summarize": len(to_summarize), "messages_kept": keep_recent,
    })
    lines = [
        "Summarize this conversation segment in a few bullet points. Keep facts, "
        "decisions, file names and tool results. Output only the summary.",
        "",
    ]
    for message in to_summarize:
        lines.append(f"{str(message.get('role', 'user')).capitalize()}: {message.get('content', '')}")
    try:
        summary = summarize([
            {"role": "system", "content": "You are a precise summarizer."},
            {"role": "user", "content": "\n".join(lines)[:12000]},
        ])
    except Exception:  # noqa: BLE001 - a failed summary falls back to dropping
        summary = ""
    from ..tools.calls import strip_think

    summary = strip_think(summary or "").strip()
    _emit(publish, "compaction.progress", {
        "session_id": session_id, "stage": "rebuilding", "summary_tokens": len(summary) // 4,
    })
    out: list[dict] = []
    inserted = False
    dropped = {id(m) for m in to_summarize}  # identity: two messages may be equal
    for message in messages:
        if id(message) in dropped:
            if not inserted and summary:
                out.append({"role": "user", "content": f"[Summary of the earlier conversation]\n{summary}",
                            "_kind": "history"})
                out.append({"role": "assistant", "content": "Understood.", "_kind": "history"})
                inserted = True
            continue
        out.append(message)
    _emit(publish, "compaction.done", {
        "session_id": session_id, "original_messages": len(messages),
        "compacted_messages": len(out), "estimated_tokens": estimate_tokens(out),
    })
    return out


def _emit(publish: Callable[[str, dict], Any], kind: str, data: dict) -> None:
    try:
        publish(kind, data)
    except Exception:  # noqa: BLE001 - the UI feed must not break the turn
        pass
