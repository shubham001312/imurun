"""Raw FISH engine generation test.

Loads ``fish.dll`` directly (no HTTP server, no agent loop) and generates from a
chat prompt. Checks output quality (garbage heuristic) and reports timings.

Run from the ``imurun`` directory:

    python tests/test_engine_generate.py
    python tests/test_engine_generate.py --model models/Llama-3.2-1B-Instruct-Q4_K_M.gguf
    python tests/test_engine_generate.py --prompt "What is the capital of France?"
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model.fish_native import FishNativeClient  # noqa: E402


def garbage_score(text: str) -> float:
    """Fraction of characters that are punctuation runs / control junk.

    A healthy answer is mostly letters, digits, spaces and normal punctuation.
    Token spam such as ``))));))`` scores high.
    """
    if not text:
        return 1.0
    bad = len(re.findall(r"[()\[\]{}<>=;\"'`\\|@#$%^&*~_]{3,}", text))
    weird = sum(1 for ch in text if ord(ch) < 9 or (13 < ord(ch) < 32))
    return (bad * 8 + weird) / max(len(text), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/Llama-3.2-1B-Instruct-Q4_K_M.gguf")
    ap.add_argument("--prompt", default="Say hello in one short sentence.")
    ap.add_argument("--max-tokens", type=int, default=96)
    ap.add_argument("--temp", type=float, default=0.3)
    args = ap.parse_args()

    model_path = (ROOT / args.model) if not Path(args.model).is_absolute() else Path(args.model)
    if not model_path.is_file():
        print(f"FAIL: model not found: {model_path}")
        return 2

    os.environ["FISH_MODEL"] = str(model_path)

    client = FishNativeClient()
    t0 = time.time()
    if not client.load():
        print(f"FAIL: load failed: {client.error}")
        return 2
    load_s = time.time() - t0

    info = client.model_info()
    print(f"model      : {model_path.name}")
    print(f"kernel     : {info.get('kernel')}")
    print(f"arch       : {info.get('arch')}  ctx={info.get('context')}  quant={info.get('quant')}")
    print(f"load       : {load_s:.2f}s")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Answer briefly."},
        {"role": "user", "content": args.prompt},
    ]

    class P:
        temperature = args.temp
        max_tokens = args.max_tokens
        top_p = 0.9
        top_k = 40
        repeat_penalty = 1.1

    t1 = time.time()
    text = client.generate_full(client.chat_prompt(messages), P())
    gen_s = time.time() - t1

    stats = client.stats()
    g = garbage_score(text)

    print("\n--- output ---")
    print(text.strip()[:1200])
    print("--- repr ---")
    print(repr(text[:200]))
    print("--------------")
    print(f"gen time   : {gen_s:.2f}s")
    print(f"tokens     : {stats.get('generated_tokens')}  ({stats.get('tokens_per_second'):.1f} tok/s)")
    print(f"prefill    : {stats.get('prefill_seconds'):.2f}s")
    print(f"garbage    : {g:.3f}")

    client.close()

    if g > 0.15:
        print("\nFAIL: output looks like token spam")
        return 1
    if not text.strip():
        print("\nFAIL: empty output")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
