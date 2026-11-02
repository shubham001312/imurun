"""Generate through the real server model client with server-style messages.

This mirrors what a turn sends to the engine (house-style directive + platform
note + user prompt) so prompt/tokenisation bugs show up outside the HTTP layer.

Run from the ``imurun`` directory:

    python tests/test_engine_server_prompt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.config import load_config  # noqa: E402
# Import the server composition first; it initialises the package graph in the
# order the runtime uses and avoids the loop<->server circular import.
from services.server.app import build_server  # noqa: E402,F401
from services.response.style import DEFAULT_DIRECTIVE  # noqa: E402
from model.engine_client import EngineModelClient  # noqa: E402
from model.fish_client import SyncFishModelClient  # noqa: E402


def main() -> int:
    cfg = load_config()
    client = EngineModelClient(fish=SyncFishModelClient(cfg.fish))
    if not client.available:
        print(f"FAIL: engine unavailable ({client.status()})")
        return 2

    messages = [
        {"role": "system", "content": DEFAULT_DIRECTIVE},
        {"role": "system",
         "content": "Model: Llama-3.2-1B-Instruct-Q4_K_M.gguf | "
                    "Tools: time, weather, web_search | Engine: ready"},
        {"role": "user", "content": "Say hello in one short sentence."},
    ]

    text = client.generate(messages)
    try:
        prompt = client._fish._messages_to_prompt(messages)
        print("--- prompt ---")
        print(repr(prompt))
    except Exception as exc:
        print(f"(prompt dump failed: {exc})")
    print("--- output ---")
    print(text)
    print("--- repr ---")
    print(repr(text))
    print("--- codepoints (first 20) ---")
    print([f"U+{ord(c):04X}" for c in text[:20]])
    print(f"\nengine stats: {client.stats()}")

    client.close()
    if "\ufffd" in text:
        print("\nFAIL: output contains U+FFFD (replacement character)")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
