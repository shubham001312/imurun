"""Optional model pass over a dictation: the loaded FISH model fixes what the
deterministic clean-up cannot (homophones, a dropped word), within a budget.

Rules (docs/voice/README.md "Clean-up"):

* only when ``voice.cleanupModel`` is on, a FISH model is ALREADY loaded
  (never loads one) and the engine is idle (``_gen_lock`` not held) - a
  dictation never waits behind a running turn;
* greedy, ``max_tokens`` ~1.5x the input, stopped at the budget
  (``voice.cleanupModelMs``, default 1000) with ``fish_cancel`` - only while
  our own generation is the one running;
* the answer is rejected (raw clean text kept) when it is empty, much longer
  or shorter than the input, changes the script, or reads like a chat reply.

The raw transcript is always returned too (``text_raw``).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from types import SimpleNamespace

LOG = logging.getLogger("imurun.voice")

SYSTEM = ("You correct speech-to-text dictation. Fix misheard words, names, spelling, punctuation and "
          "capitalisation. Keep the meaning, the language and the script exactly; do not translate, answer, "
          "explain or add anything. Output only the corrected text.")


def _native(model):
    native = getattr(model, "_native", None)
    if native is None and hasattr(model, "_gen_lock"):
        native = model
    return native


def _script_key(text: str) -> str:
    counts: dict[str, int] = {}
    for ch in text:
        if ch.isalpha():
            block = "latin" if ord(ch) < 0x0250 else ("deva" if 0x0900 <= ord(ch) <= 0x097F else
                                                      "beng" if 0x0980 <= ord(ch) <= 0x09FF else "other")
            counts[block] = counts.get(block, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def acceptable(source: str, answer: str) -> bool:
    answer = answer.strip()
    if not answer:
        return False
    ratio = len(answer) / max(1, len(source))
    if ratio < 0.6 or ratio > 1.5:
        return False
    if _script_key(answer) != _script_key(source):
        return False
    lowered = answer.lower()
    if lowered.startswith(("sure", "here is", "here's", "corrected", "the corrected", "i ", "okay")) and \
            not source.lower().startswith(("sure", "here", "corrected", "the corrected", "i ", "okay")):
        return False
    return True


def polish(model, text: str, language: str, *, budget_ms: int = 1000, vocabulary=None) -> dict:
    """{"text", "used", "reason", "ms"} - ``used`` False means: keep the input."""
    started = time.perf_counter()
    out = {"text": text, "used": False, "reason": "", "ms": 0}
    if not text.strip():
        out["reason"] = "empty"
        return out
    native = _native(model)
    if native is None or not getattr(native, "loaded", False):
        out["reason"] = "no FISH model loaded"
        return out
    lock = getattr(native, "_gen_lock", None)
    if lock is None or lock.locked():
        out["reason"] = "engine busy"
        return out
    run_chat = getattr(native, "_generate_chat", None)
    if run_chat is None:
        out["reason"] = "engine has no chat generation"
        return out
    try:
        from model.fish_native import FISH_TOKEN_CB
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"engine bindings unavailable: {exc}"
        return out
    hint = ""
    if vocabulary:
        hint = " Names that may occur: " + ", ".join(list(vocabulary)[:30]) + "."
    messages = [{"role": "system", "content": SYSTEM + hint},
                {"role": "user", "content": text}]
    params = SimpleNamespace(temperature=0.0, top_p=1.0, top_k=1, repeat_penalty=1.0,
                             max_tokens=max(16, int(len(text) / 2.5)))
    state = {"done": False, "abandoned": False, "error": None}
    chunks: list[str] = []

    def on_token(piece, _token_id, _user):
        # Runs inside OUR generation only, so cancelling here can never stop
        # a user's turn (a cancel from outside could, if a turn had started
        # in between).
        if state["abandoned"]:
            native.cancel()
            return
        if piece:
            chunks.append(piece.decode("utf-8", "replace"))

    callback = FISH_TOKEN_CB(on_token)

    def work():
        try:
            run_chat(messages, params, callback)
        except Exception as exc:  # noqa: BLE001
            state["error"] = str(exc)
        finally:
            state["done"] = True

    thread = threading.Thread(target=work, daemon=True, name="voice-polish")
    thread.start()
    thread.join(max(0.05, budget_ms / 1000.0))
    if not state["done"]:
        state["abandoned"] = True   # the next token cancels it
        out["reason"] = f"over budget ({budget_ms} ms)"
        out["ms"] = int((time.perf_counter() - started) * 1000)
        return out
    state["answer"] = "".join(chunks)
    out["ms"] = int((time.perf_counter() - started) * 1000)
    if state["error"]:
        out["reason"] = f"engine error: {state['error'][:120]}"
        return out
    answer = re.sub(r"^\s*(?:corrected(?: text)?:)\s*", "", str(state["answer"] or ""), flags=re.IGNORECASE)
    answer = answer.strip().strip('"').strip()
    if not acceptable(text, answer):
        out["reason"] = "answer rejected (length/script/chatty)"
        return out
    out.update(text=answer, used=answer != text, reason="")
    return out
