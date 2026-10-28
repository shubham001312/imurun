"""M2/M3/M4 verification through the real C ABI.

Loads fish.dll the same way the Python server will, renders the model's own
chat prompt, streams tokens, and checks the answer. Also proves that the
tokenizer round-trips and that the chat template is correct.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

class Stats(ctypes.Structure):
    _fields_ = [
        ("load_seconds", ctypes.c_double),
        ("prefill_seconds", ctypes.c_double),
        ("decode_seconds", ctypes.c_double),
        ("tokens_per_second", ctypes.c_double),
        ("prompt_tokens", ctypes.c_int),
        ("generated_tokens", ctypes.c_int),
        ("context_used", ctypes.c_int),
        ("context_size", ctypes.c_int),
        ("weights_bytes", ctypes.c_ulonglong),
        ("kv_bytes", ctypes.c_ulonglong),
        ("peak_rss_mb", ctypes.c_double),
    ]

class ModelInfo(ctypes.Structure):
    _fields_ = [
        ("arch", ctypes.c_char * 32),
        ("n_layers", ctypes.c_int),
        ("n_embd", ctypes.c_int),
        ("n_ff", ctypes.c_int),
        ("n_head", ctypes.c_int),
        ("n_head_kv", ctypes.c_int),
        ("head_dim", ctypes.c_int),
        ("n_ctx", ctypes.c_int),
        ("vocab", ctypes.c_int),
        ("tied_output", ctypes.c_int),
        ("quant", ctypes.c_char * 64),
    ]

CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)

failures = 0

def check(cond: bool, what: str) -> None:
    global failures
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures += 1


def load_lib(path: str):
    lib = ctypes.CDLL(path)
    lib.fish_create.restype = ctypes.c_void_p
    lib.fish_destroy.argtypes = [ctypes.c_void_p]
    lib.fish_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.fish_load.restype = ctypes.c_int
    lib.fish_last_error.argtypes = [ctypes.c_void_p]
    lib.fish_last_error.restype = ctypes.c_char_p
    lib.fish_model_info_of.argtypes = [ctypes.c_void_p, ctypes.POINTER(ModelInfo)]
    lib.fish_tokenize.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_int32), ctypes.c_int]
    lib.fish_tokenize.restype = ctypes.c_int
    lib.fish_detokenize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                                    ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.fish_detokenize.restype = ctypes.c_int
    lib.fish_chat_prompt.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_char_p),
                                     ctypes.POINTER(ctypes.c_char_p),
                                     ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.fish_chat_prompt.restype = ctypes.c_int
    lib.fish_chat_format.argtypes = [ctypes.c_void_p]
    lib.fish_chat_format.restype = ctypes.c_char_p
    lib.fish_generate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_float, ctypes.c_int,
                                  ctypes.c_float, ctypes.c_float, CB, ctypes.c_void_p]
    lib.fish_generate.restype = ctypes.c_int
    lib.fish_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(Stats)]
    lib.fish_kernel_name.restype = ctypes.c_char_p
    lib.fish_specs_json.argtypes = [ctypes.c_void_p]
    lib.fish_specs_json.restype = ctypes.c_char_p
    return lib


def main() -> int:
    dll = os.path.join(ROOT, "bin", "fish.dll")
    model = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "models", "Llama-3.2-1B-Instruct-Q4_K_M.gguf")
    if not os.path.exists(dll):
        print(f"[FAIL] missing {dll}")
        return 1

    lib = load_lib(dll)
    print(f"kernel: {lib.fish_kernel_name().decode()}")

    ctx = lib.fish_create()
    check(bool(ctx), "context created")

    t0 = time.time()
    rc = lib.fish_load(ctx, model.encode(), 0)
    if rc != 0:
        print(f"  [FAIL] load rc={rc}: {lib.fish_last_error(ctx).decode()}")
        return 1
    print(f"loaded in {time.time() - t0:.2f}s")

    import json
    specs = json.loads(lib.fish_specs_json(ctx).decode())
    hw, plan = specs["hardware"], specs["plan"]
    print(f"  cpu: {hw['cpu']} ({hw['physical_cores']}C/{hw['logical_cores']}T) "
          f"avx2={hw['avx2']}")
    print(f"  ram: {hw['ram_total_bytes'] / 1e9:.1f} GB total, "
          f"{hw['ram_available_bytes'] / 1e9:.1f} GB free")
    print(f"  gpu: {hw['gpu'] or 'none detected'} "
          f"({hw['vram_free_bytes'] / 1e9:.1f} GB free VRAM)")
    print(f"  disk: {hw['disk_read_mb_s']:.0f} MB/s   "
          f"ram bw: {hw['ram_bandwidth_mb_s']:.0f} MB/s")
    print(f"  plan: {plan['mode']} ctx={plan['n_ctx']} threads={plan['n_threads']} "
          f"resident={plan['resident_layers']} window={plan['window_layers']} "
          f"predicted={plan['predicted_tok_s']:.1f} tok/s")
    print(f"  why: {plan['rationale']}")
    check(plan["mode"] == "resident", "1B fits entirely in RAM")
    check(hw["disk_read_mb_s"] > 0, "disk bandwidth actually measured")
    check(hw["ram_total_bytes"] > 0, "RAM size actually measured")

    info = ModelInfo()
    lib.fish_model_info_of(ctx, ctypes.byref(info))
    print(f"  {info.arch.decode()} layers={info.n_layers} embd={info.n_embd} "
          f"ff={info.n_ff} head={info.n_head}/{info.n_head_kv} ctx={info.n_ctx} "
          f"vocab={info.vocab} tied={info.tied_output}")
    print(f"  quant: {info.quant.decode()}")
    check(info.n_layers == 16, "model info exposed to the caller")
    check(info.n_head_kv < info.n_head, "GQA exposed")

    # ---- tokenizer round trip
    text = "Hello, world! The capital of France is"
    buf = (ctypes.c_int32 * 256)()
    n = lib.fish_tokenize(ctx, text.encode(), 0, buf, 256)
    check(n > 0, f"tokenize returns ids ({n})")
    ids = list(buf[:n])
    out = ctypes.create_string_buffer(512)
    lib.fish_detokenize(ctx, (ctypes.c_int32 * n)(*ids), n, out, 512)
    decoded = out.value.decode()
    check(decoded == text, f"detokenize round-trips: {decoded!r}")

    # ---- chat template
    roles = (ctypes.c_char_p * 2)(b"system", b"user")
    contents = (ctypes.c_char_p * 2)(
        b"Answer in one short sentence.",
        b"What is the capital of France?")
    pbuf = ctypes.create_string_buffer(4096)
    np = lib.fish_chat_prompt(ctx, roles, contents, 2, pbuf, 4096)
    prompt = pbuf.value.decode()
    fmt = lib.fish_chat_format(ctx).decode()
    print(f"  chat format: {fmt}")
    check(np > 0, "chat prompt rendered")
    check("<|start_header_id|>" in prompt or fmt != "llama3",
          "llama3 template uses header tokens")
    check(prompt.rstrip().endswith("<|end_header_id|>") or fmt != "llama3",
          "prompt ends ready for the assistant turn")

    # ---- generation
    print("\nstreaming: ", end="", flush=True)
    chunks: list[str] = []

    def on_token(piece, token_id, _user):
        s = piece.decode("utf-8", "replace") if piece else ""
        chunks.append(s)
        print(s, end="", flush=True)

    cb = CB(on_token)
    t1 = time.time()
    n_gen = lib.fish_generate(ctx, prompt.encode(), 0, 48, 0.3, 40, 0.9, 1.1, cb, None)
    elapsed = time.time() - t1
    print()
    answer = "".join(chunks)
    print(f"\ngenerated {n_gen} tokens in {elapsed:.2f}s "
          f"({n_gen / elapsed if elapsed else 0:.2f} tok/s)")

    st = Stats()
    lib.fish_get_stats(ctx, ctypes.byref(st))
    print(f"  prefill {st.prefill_seconds:.2f}s  decode {st.decode_seconds:.2f}s  "
          f"tok/s {st.tokens_per_second:.2f}  rss {st.peak_rss_mb:.0f} MB")

    check(n_gen > 0, "generated tokens")
    check("Paris" in answer, "answers 'Paris'")

    lib.fish_destroy(ctx)
    print(f"\n{'M2/M3/M4 FAILED' if failures else 'M2/M3/M4 PASSED'} "
          f"({failures} failure{'s' if failures != 1 else ''})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
