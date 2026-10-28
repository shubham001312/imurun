"""Acceptance test for the native backend used by the product.

Loads fish.dll in-process (no subprocess, no HTTP), renders the model's real
chat template, streams an answer, and checks the measured statistics.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from model.fish_native import FishNativeClient  # noqa: E402

failures = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global failures
    mark = "PASS" if ok else "FAIL"
    if not ok:
        failures += 1
    print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""))


async def main() -> int:
    client = FishNativeClient()
    if not client.configured:
        print(f"  [SKIP] no engine available (library={client.library_path}, "
              f"model={client.model_path})")
        return 0

    t0 = time.time()
    if not client.health():
        print(f"  [FAIL] engine did not load: {client.error}")
        return 1
    print(f"loaded in {time.time() - t0:.2f}s from {client.library_path.name}")
    check(True, "native engine loaded in-process")

    info = client.model_info()
    print(f"  model: {info['arch']} layers={info['layers']} ctx={info['context']} "
          f"kernel={info['kernel']} chat={info['chat_format']}")
    check(info["layers"] > 0, "model info available")

    specs = client.specs()
    hw, plan = specs["hardware"], specs["plan"]
    print(f"  cpu: {hw['cpu']} ({hw['physical_cores']}C/{hw['logical_cores']}T)")
    print(f"  plan: {plan['mode']} ctx={plan['n_ctx']} predicted={plan['predicted_tok_s']:.1f} tok/s")
    check(plan["mode"] in ("resident", "windowed", "streamed"), "planner produced a plan")

    messages = [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "What is the capital of France? Answer in one word."},
    ]
    prompt = client.chat_prompt(messages)
    check("<|start_header_id|>" in prompt, "llama3 chat template applied",
          repr(prompt[:48]))
    check(prompt.rstrip().endswith("<|start_header_id|>assistant<|end_header_id|>"),
          "prompt ends at the assistant turn")

    chunks: list[str] = []
    t0 = time.time()
    async for piece in client.generate_stream(messages, None):
        chunks.append(piece)
    text = "".join(chunks)
    elapsed = time.time() - t0
    print(f"\nstreamed: {text.strip()}\n")

    check(len(chunks) > 0, "tokens streamed incrementally", f"{len(chunks)} chunks")
    check("Paris" in text, "answer is correct")
    check(elapsed < 60, "finished promptly", f"{elapsed:.1f}s")

    stats = client.stats()
    print(f"  {stats['generated_tokens']} tokens, prefill {stats['prefill_seconds']:.2f}s, "
          f"decode {stats['decode_seconds']:.2f}s, {stats['tokens_per_second']:.1f} tok/s")
    check(stats["tokens_per_second"] > 1.0, "decode speed above 1 tok/s")
    check(stats["context_size"] >= 1024, "context sized by the specs engine")

    client.close()
    print("\nNATIVE CLIENT " + ("PASSED" if failures == 0 else f"FAILED ({failures})"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
