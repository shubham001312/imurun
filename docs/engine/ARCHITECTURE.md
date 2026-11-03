# FISH engine — architecture (as built)

FISH is a self-contained CPU inference engine for GGUF models: its own GGUF
reader, quantised kernels, tokenizer, sampler, KV cache and hardware planner.
It links nothing from llama.cpp or any other runtime. It is compiled to one
shared library (`fish.dll` / `libfish.so` / `libfish.dylib`) with a small C ABI
(`model/fish/include/fish/c_api.h`) that the Python server loads in-process.

This document describes the code that exists. For what is planned, see
`docs/ROADMAP.md`.

## Source map (`model/fish/`)

| File | Role |
|---|---|
| `src/gguf.cpp`, `include/fish/gguf.h` | GGUF v2/v3 reader: header, metadata, tensor table; whole file memory-mapped read-only; every read bounds-checked. Supports 1–4-D tensors (3-D for MoE experts). |
| `src/quant.cpp`, `include/fish/quant.h` | Scalar reference kernels: dequantisers for F32/F16/BF16/Q4_0/Q4_1/Q5_0/Q5_1/Q8_0/Q2_K…Q8_K/IQ4_NL/IQ4_XS/MXFP4; `vec_dot` (weights × f32); `vec_dot_q8`/`vec_dot_q8k` (weights × int8); activation quantisers. The dispatch table `QuantKernels` starts scalar. |
| `src/quantize.cpp` | Writing blocks (the inverse of `quant.cpp`): `quantize_row` / `merge_row` for F32, F16, BF16, Q8_0, Q4_0, Q4_K, Q5_K, Q6_K. Used by the LoRA merge. |
| `src/calib.cpp`, `include/fish/calib.h` | Load-time calibration for device re-quantisation: the built-in calibration text, the per-column importance (E[x²]) file and the cache directory key (`~/.imurun/cache/fish/`). Used by the residency planner (`LlamaModel::plan_resident`, `docs/engine/GPU.md`). |
| `src/lora.cpp`, `include/fish/lora.h` | LoRA adapter GGUF validation (`lora_plan`) and row-by-row merge (`lora_merge`); see "LoRA adapters" below. |
| `src/quant_x86.cpp` | AVX2+FMA+F16C kernels installed at startup after a CPUID check. Each function carries its own `target` attribute so the TU compiles for baseline x86-64. |
| `src/ops.cpp`, `include/fish/ops.h` | RMSNorm, SiLU/GELU, RoPE (precomputed cos/sin table, normal & NeoX pairing, linear/YaRN/longrope/Llama-3 factors), `matmul_batch` (threaded, int8 fast path), `attention_batch` (threaded over token×head, softcap, sliding window). |
| `src/threadpool.cpp` | Persistent pool, one thread per physical core by default (`FISH_THREADS` overrides), chunked work distribution. |
| `src/tokenizer.cpp` | Byte-level BPE (llama-bpe / qwen2 / gpt2 pre-tokenizer flavours) and SentencePiece (scores, byte fallback); special-token handling; chat templates applied structurally. |
| `src/sampler.cpp` | Repetition penalty, temperature, top-k, top-p. |
| `src/model.cpp`, `include/fish/model.h` | The transformer: load (metadata by architecture prefix, tensor binding, RoPE table, KV cache), `forward()` for a batch of tokens, dense and MoE FFN, layer prefetch thread. |
| `src/specs.cpp` | Hardware probe (cores, RAM, disk read MB/s, RAM bandwidth) and the execution planner (context size that fits, resident vs streamed layers, honest speed prediction). |
| `src/c_api.cpp` | The C ABI: load, tokenize, chat, generate (raw prompt or structured chat), cancel, stats, specs JSON. Owns the KV prefix cache. |
| `src/server.cpp` | Optional standalone HTTP/SSE server for containers (loopback, Host check, optional bearer token, cancels on client disconnect). |
| `test/test_m1.cpp` | GGUF parsing, block layouts, **AVX2 kernels vs scalar reference**, int8 vs f32 error bound, throughput. |
| `test/test_m2.cpp` | Full forward pass ("The capital of France is" → Paris), tokenizer round trips, special-token safety, BOS discipline. |
| `test/test_m5.cpp` | LoRA merge at load: synthetic adapter (merged rows vs the exact target, scale 0 bit-exact, merged tensors through GPU placement, refusals) and, optionally, a real adapter measured against an f16-merge oracle. |
| `test/test_fish.py` | Through the C ABI from Python: specs, model info, chat generation, prefix cache. |

Files `src/engine.cpp, platform.cpp, hardware_probe.cpp, layer_planner.cpp,
resident_window.cpp, prefetcher.cpp, expert_streamer.cpp, adapter_mgr.cpp,
compression_policy.cpp, gguf_inspect.cpp` are an earlier skeleton and are
**not compiled** (see `CMakeLists.txt`). Their `doc/*.md` describe that design.

## Data flow for one request

```
fish_generate_chat(roles, contents)
  │  Tokenizer::encode_chat — template text with specials ON, user content with specials OFF,
  │  BOS once if the model expects it
  ▼
fit to context: keep head (system) + tail (latest turns), reserve room for the answer
  ▼
KV prefix cache: longest common prefix with the previous request is skipped
  ▼
prefill in batches of ≤256 tokens ── LlamaModel::forward(tokens, n, pos0)
  ▼
decode loop: sample → emit UTF-8-complete text via callback → forward(1 token)
```

`forward()` per layer:

```
xb = rmsnorm(x) → ActBatch.quantize(xb)          (int8, both 32- and 256-block layouts)
q,k,v = matmul(Wq|Wk|Wv or fused Wqkv, xb)       (+ biases; Qwen3 per-head q/k norm)
rope(q,k) → write k,v into the layer's KV cache
attention_batch (scale, Gemma soft-cap, sliding window on even Gemma layers)
proj = matmul(Wo, attn) (+ post-attention norm for Gemma) ; x += proj
xb = rmsnorm(x) → quantize
FFN: dense  down(act(gate(xb)) * up(xb))   act = SiLU or GELU; Phi-3 fused gate|up
     MoE    router softmax → top-k → per-expert small batches over the 3-D expert
            tensors (quantised, in place) → weighted scatter; optional shared expert
(+ post-FFN norm for Gemma) ; x += ffn
```
Final norm → output projection for the last token only → optional logit soft-cap.

## Why it is fast on a CPU

1. **Weights never leave their quantised form.** No matrix is dequantised to f32;
   the dot product unpacks nibbles inside SIMD registers.
2. **Activations are quantised to int8 once per matmul** (`ActBatch`). The kernel
   then uses `vpmaddubsw`/`vpmaddwd`: 32 multiply-adds per instruction. For
   K-quants the activation scale is per 256 values with per-32 integer sums, so a
   whole super-block accumulates in int32 and converts to float once (the Q8_K
   idea). For Q4_0/Q8_0 the scale is per 32.
3. **Scale decoding is branch-free bit twiddling + one byte shuffle per group**;
   fp16 scales are converted with the F16C instruction (a cross-TU function call
   here cost a third of the block time).
4. **Prefill tiles rows so a ~24 KB slab of weights stays in L1 while tokens are
   the outer loop**; every weight byte is read from DRAM exactly once per matmul.
5. **Decode is memory-bound** (every weight byte once per token). One thread per
   physical core; SMT siblings only add contention.
6. **RoPE angles are a table**, attention is threaded over (token, head), and the
   KV prefix cache makes a repeated system prompt free.

Correctness is pinned by `fish_test_m1`: the AVX2 integer kernel must equal the
scalar integer reference to ~1e-8 of Σ|w·x|, and the int8 path must be within
1% of the f32 path (activation quantisation error; measured 0.03%).

## Layer streaming (models larger than free RAM)

The GGUF is memory-mapped, so the OS pages weights in on demand and evicts by
LRU. When `weights_bytes > 0.75 × available RAM`, `LlamaModel` starts a
prefetch thread: before computing layer *i* it issues `PrefetchVirtualMemory`
(Windows) / `madvise(MADV_WILLNEED)` (POSIX) for layer *i+1*'s byte range
(computed at load from the tensor table), and for layer 0 after the last layer.
Disk reads then overlap compute instead of stalling it. Throughput in that
regime is disk-bound (`specs` predicts it from the measured read speed).

There is no VRAM arena, staging ring or expert cache yet — see the roadmap.

## GPU residency planner

`LlamaModel::place_layers()` first tries `plan_resident()`: every layer and the output
projection on the GPU as one span, giving up file precision (importance-weighted
re-quantisation to Q4_K / FQ3 / FQ3S device copies), context length (down to
`FISH_GPU_MIN_CTX`) and f16 KV (int8) before it gives up residency. The CPU KV cache is
allocated after placement and only for CPU layers (`alloc_cpu_kv`). Details and
measurements: `docs/engine/GPU.md`, "Residency planner and device formats".

## Planner (`specs.cpp`)

Measured at load: physical/logical cores, AVX2/AVX-512, total and available RAM,
sequential read speed of the model file (unbuffered), RAM copy bandwidth. The
plan picks a context size whose KV cache fits a quarter of the usable RAM
budget, reports resident vs streamed layers and a predicted tok/s with the
rationale as text (`fish_specs_json`). `fish_load(…, n_ctx=0)` uses this plan.

## LoRA adapters (merge at load, ABI 3)

C: `fish_set_adapter(ctx, path, scale)`, `fish_clear_adapter`, `fish_adapter_info_of`.
Python: `FishNativeClient.select(model, adapter=..., scale=...)`,
`set_adapter(path, scale)`, `model_info()["adapter"]`. The input is the GGUF
written by llama.cpp's `convert_lora_to_gguf.py`. It is merged into the weights
inside `LlamaModel::load`, after the tensors are bound and **before**
placement. GPU upload, spans, the RAM/disk tiers and the kernels therefore see
ordinary tensors, and decode speed does not change.

```
lora_plan    general.type == "adapter", adapter.type == "lora", same architecture,
             every tensor a .lora_a/.lora_b pair, every target present and 2-D,
             shapes checked as llama.cpp checks them, base type writable -> else load error
per target   P = scale * alpha / r * B   (r = lora_b ne0; alpha 0 -> scale as is, as in llama.cpp)
             rows on the thread pool: base = dequant(W row); delta = P[o] . A;
             merge_row(base type) -> owned 64-byte-aligned buffer
rebind       a copy of the TensorInfo pointing at the buffer replaces the pointer
             in LayerTensors / embd_ / out_w_; the file mapping is never written;
             the original pages are evicted
```

Orientation: ordinary weights use `a [n_in, r]`, `b [r, n_out]`, Δ = B·A. For
`token_embd` the converter transposes `lora_a` (`a [r, n_vocab]`,
`b [r, n_embd]`), and only the input lookup is replaced. With tied embeddings
the output projection keeps the base tensor, as llama.cpp and PEFT do at
runtime.

**Re-quantisation (`merge_row`, `src/quantize.cpp`).** Each merged tensor keeps
its own type. Writable types: **F32, F16, BF16 (element-wise, nearest), Q8_0, Q4_0,
Q4_K, Q5_K, Q6_K**. Anything else (Q2_K, Q3_K, Q4_1, Q5_0, Q5_1, IQ4_NL,
IQ4_XS, MXFP4, 3-D MoE expert tensors) is refused with a load error that names
the tensor. Nothing is skipped silently. Per block:

- *grid*: the source block's scales are kept and each code moves by Δ/step,
  rounded **stochastically**: up with probability equal to the fraction, drawn
  from a hash of tensor, row and element so merges are reproducible.
  Nearest rounding exists (`FISH_ADAPTER_ROUNDING=nearest`) but it is a trap.
  A trained LoRA delta is usually far below half a Q4_K/Q6_K step, so nearest
  rounding snaps every weight back to its old code. Measured: 0.0% of the
  tuning channel's 5-step adapter kept. Stochastic rounding keeps the merge
  unbiased (E[merged] = W + Δ; measured retention 0.95) at the cost of
  zero-mean noise.
- *refit*: a fresh quantisation of W + Δ, tried only when Δ pushes a code out
  of range. Whichever candidate has the lower error is written.
- a block (or element) whose Δ is exactly 0 is copied byte for byte, so
  scale 0 reproduces the file exactly (pinned by `fish_test_m5`).

`FISH_ADAPTER_MERGE_TYPE=f16|bf16|f32|q8_0` writes the targeted tensors in a
wider type instead. This is for diagnostics. Q8_0 keeps more of a small
adapter, but those tensors take ~1.8× the bytes of Q4_K, which costs decode
speed.

**Memory.** The merged copies are private RAM: they cannot be paged back to
the file. They must fit the RAM budget together with the KV cache, or the load
fails and reports the numbers. GPU layers keep their host copy, because the CPU
fallback of a failed device call reads it. For the 1B with all 7 projections
merged, that is 557 MB.

## C ABI (v3)

See `c_api.h`. ABI 3 adds `fish_set_adapter`, `fish_clear_adapter`,
`fish_adapter_info_of` and the `fish_adapter_info` struct. That struct is
separate, so `fish_model_info` keeps its size. Notable: `fish_generate_chat` (structural template, injection-safe),
`fish_stats` adds `truncated`, `cached_tokens`, `n_threads`, `streaming`,
`prefill_tokens_per_second`; `fish_model_info` adds experts, training context,
chat format and tokenizer kind. Structs only grow at the end; Python mirrors them
in `model/fish_native.py` (`EXPECTED_ABI`).

## Adding an architecture

1. Add the GGUF `general.architecture` name to `kArchs` in `model.cpp`.
2. Metadata is read with the `<arch>.` prefix automatically; add variant flags
   to `LlamaConfig` (norm style, activation, RoPE pairing, soft-caps…).
3. Bind any new tensors in `bind_layer()`; put the behaviour behind a flag in
   `forward()`; keep the single forward function.
4. Add the chat template in `tokenizer.cpp` (`chat_segments`) and EOG tokens.
5. Add a model to `services/model_manager.py` catalogue + `_ARCH_BY_ID_PREFIX`.
6. Run `fish_test_m2` with a small GGUF of that family; document it in
   `docs/engine/ARCHITECTURES.md`.

## Adding a quant type

Block struct in `quant.h` (`#pragma pack(1)`, `static_assert` the size), sizes
in `gguf.cpp` (`ggml_type_size`, `ggml_blck_size`), scalar dequantiser +
`dequant_row`/`vec_dot` cases in `quant.cpp`, then optionally an AVX2 int8
kernel in `quant_x86.cpp` wired through `QuantKernels` + `has_q8*_kernel`.
`fish_test_m1` picks the type up automatically if a tensor of that type exists
in the test model.
