"""Prompt profiles: what the agent loop sends to a model of a given size.

Measured 2026-11-01 with Llama-3.2-1B-Instruct Q4_K_M. The engine itself is
correct: with no system prompt the model answers "What is the capital of
France?" with "Paris" and 17 x 23 with 391. Through the full prompt it
failed every one of five plain questions, because a ~1B model copies
whatever it sees in the system prompt:

* the tool protocol's example call (``web_search "latest Python release"``)
  was emitted verbatim in reply to "hi", the app ran it, and the answer
  became a scraped headline;
* the rule "write an unknown value as an em dash" produced answers that
  start with "—", and "as of <year>" produced "As of [2026]";
* the model was also the web-research router and said ``web: true`` to
  "What is the capital of France?", so a Wikipedia disambiguation page
  was pasted into the answer.

So a small model gets the **compact** profile: a one-line directive and the
date, no tool catalogue (it cannot use tools reliably), and the regex router
instead of the model for deciding when to fetch live data (weather, news,
prices and the time still get live context). Larger models keep the full
agentic prompt unchanged. The engine binding separately turns the repeat
penalty off for small models (``model/fish_native.py``).

``IMURUN_COMPACT_PROMPT=1`` forces compact for every model, ``0`` disables
it; anything else (the default) decides by model size.
"""
from __future__ import annotations

import os

# Below this many parameters a model gets the compact profile. 1B-1.5B
# models fall below it; Phi-3 mini (3.8B), Llama 3.2 3B and 8B models
# stay on the full prompt.
SMALL_MODEL_PARAMS = 2.5e9

# One line, tested against three wordings (6 questions x 2 runs each). Do
# not add rules: an earlier four-line version said "answer the user's last
# message", and the 1B replied "No last message from user" and invented
# "### Recent Conversations" sections, which then spread through history.
COMPACT_DIRECTIVE = "You are Imurun, a helpful assistant. Answer clearly and concisely."


COMPACT_CONTEXT_CHARS = 1200


def compact_context(results: list[dict]) -> str:
    """The live-data block for a small model: the facts only, trimmed.

    The full pre-fetch block (tool instructions, every observation, citation
    rules) was pasted verbatim into the answer by a 1B model. With just the
    facts it answered "What is the weather in Kolkata?" and "What time is it?"
    in one sentence. A narrow tool (weather, clock, location) beats a web
    search for the same question, so when one succeeded the search is
    dropped; it only added an encyclopedia excerpt the model then quoted."""
    ok = [r for r in results or [] if r.get("ok") and str(r.get("content") or "").strip()]
    if not ok:
        if results:
            return "Live information could not be fetched right now. If the question needs it, say so in one sentence."
        return ""
    narrow = [r for r in ok if r.get("tool") != "web_search"]
    chosen = (narrow or ok)[:2]
    body = "\n\n".join(str(r.get("content")).strip()[:COMPACT_CONTEXT_CHARS] for r in chosen)
    return "Live information (use it to answer):\n" + body


def estimate_params(info: dict) -> float:
    """Rough parameter count from the engine's model_info: attention + MLP per
    layer, plus the embedding table. Good to ~20%, which is all the profile
    choice needs. Returns 0 when the shape is unknown."""
    try:
        layers = int(info.get("layers") or 0)
        embd = int(info.get("embd") or 0)
        ff = int(info.get("ff") or 0)
        vocab = int(info.get("vocab") or 0)
        experts = max(1, int(info.get("experts") or 0))
    except (TypeError, ValueError):
        return 0.0
    if layers <= 0 or embd <= 0:
        return 0.0
    per_layer = 4 * embd * embd + 3 * embd * ff * experts
    return float(layers * per_layer + vocab * embd)


def is_compact(model) -> bool:
    """True when ``model`` should get the compact prompt profile.

    A remote model (``model.remote`` true) is always full-size. A local model
    is measured from its shape; if the shape cannot be read the full profile
    is kept, so an unknown model never loses its tools by accident."""
    forced = os.getenv("IMURUN_COMPACT_PROMPT", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    if model is None or getattr(model, "remote", False):
        return False
    getter = getattr(model, "model_info", None)
    if not callable(getter):
        return False
    try:
        info = dict(getter() or {})
    except Exception:  # noqa: BLE001 - an unreadable model keeps the full profile
        return False
    if "error" in info:
        return False
    params = estimate_params(info)
    return 0 < params < SMALL_MODEL_PARAMS
