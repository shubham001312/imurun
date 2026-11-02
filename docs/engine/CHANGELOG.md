# Engine changelog

## 2026-11-02 — manylinux engine build fix (pthread link)

- Symptom: GitHub Actions job `engine / Engine (Linux x64, manylinux_2_28)`
  failed while linking `fish_server` with:
  `libfish.so: undefined reference to pthread_create/pthread_join`.
- Cause: the engine CMake target did not explicitly link thread support, so on
  glibc 2.28 in manylinux the pthread symbols remained unresolved at link time.
- Fix: require CMake Threads and link `Threads::Threads` into the engine shared
  library and C++ engine tests (`model/fish/CMakeLists.txt:19-20,179,253`).

## 2026-11-02 — LoRA adapters merged at load (ABI 3, request E1)

Design: `ARCHITECTURE.md` → "LoRA adapters".

- **API.** New C functions `fish_set_adapter(ctx, path, scale)`,
  `fish_clear_adapter`, `fish_adapter_info_of` plus the `fish_adapter_info`
  struct; `FISH_ABI_VERSION` 3. Python mirrors them in `model/fish_native.py`
  (`EXPECTED_ABI = 3`, `FishAdapterInfo`, `select(model, adapter=, scale=)`,
  `set_adapter()`, `adapter_info()`, `model_info()["adapter"]`).
  `fish_model_info` and `fish_stats` keep their old layout.
- **Engine.** New files `src/lora.cpp` / `include/fish/lora.h` (validate the
  adapter, pair its tensors, merge rows on the thread pool) and
  `src/quantize.cpp` (`quantize_row`, `merge_row`, `fp32_to_fp16`).
  `LlamaModel::LoadOptions` gains `adapter_path`, `adapter_scale`,
  `adapter_rounding` and `adapter_merge_type`. Merged tensors are owned
  copies that are rebound in place of the file's before `place_layers()`.
  `LlamaModel::weight(name)` and `adapter()` expose them.
- **Scaling matches llama.cpp.** The effective scale is
  `scale * alpha / r` with r = `lora_b ne0`, or `scale` when alpha is 0.
  Shape checks are llama.cpp's, including the transposed `token_embd.lora_a`.
- **Refused with a load error, never skipped:** wrong `general.type` /
  `adapter.type`, another architecture, non-LoRA tensors (norm or embedding
  replacements), unpaired factors, targets the model lacks, wrong shapes, 3-D
  MoE expert targets, aLoRA, base types without a requantiser (anything except
  F32 F16 BF16 Q8_0 Q4_0 Q4_K Q5_K Q6_K), and merges that do not fit the RAM
  budget.
- **Rounding finding (measured, and the reason the default is stochastic).**
  The first version used nearest rounding on the source block's levels. With
  the tuning channel's real adapter (5 steps, r=16, alpha 32) it kept **0.0%**
  of the delta: every LoRA update was below half a Q4_K/Q6_K step, so every
  weight snapped back to its old code. Stochastic rounding is now the default.
  The adapter's effect on teacher-forced logits (55 tokens) was compared
  against an f16-merge oracle (`FISH_ADAPTER_MERGE_TYPE=f16`, scale 1 vs
  scale 0):

  | base file | merge | retention | cosine to oracle effect | rel. error |
  |---|---|---|---|---|
  | `models/…Q4_K_M` (imatrix) | same type, stochastic (default) | 0.949 | 0.280 | 1.51 |
  | | same type, nearest | 0.000 | 0.024 | 1.31 |
  | | q8_0 (`FISH_ADAPTER_MERGE_TYPE`) | 0.995 | 0.688 | 0.91 |
  | channel pipeline's own no-adapter Q4_K_M | same type, stochastic (default) | 0.949 | **0.573** | 1.33 |
  | | same type, nearest | 0.000 | −0.052 | 1.32 |
  | | q8_0 | 0.995 | 0.658 | 0.89 |
  | | **the channel's merged Q4_K_M today** (f16 merge → llama-quantize) | — | 0.438 | 1.65 |

  On this tiny adapter both pipelines carry the effect at low SNR. The oracle
  logit change is only 0.14 RMS, and quantisation noise is of the same order.
  Merge at load is about as good as, or slightly better than, the merged file
  the channel ships now. This is one adapter and one noise realisation; a
  longer-trained adapter has larger deltas and a better SNR in every column.
  Greedy "The capital of France is" → "Paris…" holds with the adapter.
- **Measured (Llama-3.2-1B Q4_K_M, 8 threads, shared machine):**
  - Merge time for the real adapter (112 tensors, 557 MB of merged copies):
    1.4–2.1 s. Load goes from ~0.7 s to ~2.4 s.
  - Decode, paired and alternating, best of 3 per round: plain 104.1 vs
    adapter 103.4 tok/s on the GPU span; 24.9 vs 25.6 on the CPU.
  - Plain decode, new build vs the pre-change build (`fish_test_gpu
    --no-kernels`, two paired rounds): span 97.0 / 107.3 vs 103.2 / 107.3,
    per-matmul 58.0 / 61.1 vs 57.8 / 64.1, CPU 27.5 / 25.0 vs 26.8 / 25.7.
    Within noise.
- `fish_test_m5`: synthetic r=4 adapter on Q4_K/Q6_K projections and
  `token_embd`, all PASS.
  - Merged vs exact target: rel RMS 0.074–0.076 (Q4_K), 0.019 (Q6_K);
    retention 0.99–1.00.
  - Nearest mode: no row is ever farther from the target than unmerged.
  - Scale 0: byte-identical tensors and bit-identical logits.
  - The GPU-span-placed merged model agrees with the CPU one (same argmax,
    2.3% max |Δlogit|).
  - Five refusal cases.

  `fish_test_m1`, `fish_test_m2`, `test/test_fish.py` and `pytest`
  (330 passed) all pass. New `tests/test_fish_adapter_binding.py` uses a fake
  library.
- Not covered:
  - Runtime (unmerged) LoRA, so switching adapters reloads the model
    (~2 s for the 1B).
  - LoRA on MoE expert tensors.
  - Adapter tensors that replace norms.
  - aLoRA.
  - More than one adapter at a time.
  - Merged copies are pinned RAM even for GPU layers (no file-backed spill).
  - No 8B adapter was available to measure. RAM = the targeted tensors'
    bytes, roughly 3.5–4 GB if all projections of an 8B Q4_K_M are
    targeted (estimated from the file layout, not measured).

## 2026-11-01 (later) — spans + re-quantisation landed; span bookkeeping

GPU agent's final state (details in `GPU.md`, `BENCHMARKS.md`): whole-layer
spans with f16 KV in VRAM (one submit per token), six span shaders, faster
K-quant dequant (Q6_K matvec 62→86 GB/s), online re-quantisation at upload
(`FISH_GPU_REQUANT`: 2 = Q6_K→Q4_K, 3 = + gate/up→FQ3 3.5 bpw, 1 = all FQ3)
with quality measured as 200-step top-1 agreement vs the CPU path.

Measured on the RTX 3050 (4 GB, shared machine, paired runs):
- **1B: 96.6 tok/s** auto (16 layers, span) vs 56 per-matmul vs 26 CPU — the
  80 tok/s target is met; ABI test 79 tok/s; prefill 408 tok/s.
- **8B Q4_K_M: 10.35 tok/s** best (mode 3, 21/32 layers, top-1 95.0%) vs 4.8
  CPU same run; mode 2 8.7 tok/s at 96.5%; mode 1 fails quality (88%).
  **20 tok/s is not reachable on this card with this file**: other programs
  hold 0.7–1.8 GB of VRAM, all 32 layers need ~3.9 GB even in mode 3, and a
  fully resident mode-3 span would still move ~3.75 GB/token (~110 GB/s needed).
  Re-quantisation stays opt-in (mode 3 sits exactly on the 95% bar).
- Speculative decoding re-measured with GPU verification (mode 3, 1B draft):
  acceptance 41%, 5.7 vs 6.2 tok/s plain — still not a win with this pairing.

Engine-side follow-ups from the agent's requests:
- `ComputeBackend::describe()` (virtual, Vulkan overrides) → `fish_stats.gpu_mix`
  ("span: 21 layers, 65 × Q6_K→Q4_K …"); Python `placement.mix`.
- CPU f32 KV is no longer allocated for span layers (saves 512 MB on an 8B at
  n_ctx 4096); a span failure after earlier success restores the full cache,
  resets KV and reports the forward as failed (the caller errors the turn; the
  next one runs per-matmul).
- Open: smaller default n_ctx / grow-on-demand device KV when the GPU is the
  bottleneck (`docs/ROADMAP.md`).

## 2026-11-01 — Vulkan stage 2: layer spans, device KV cache, online re-quantisation

Details: `docs/engine/GPU.md`; numbers: `BENCHMARKS.md` ("GPU stage 2").

- **Layer spans** (`ComputeBackend::span_*`, implemented in
  `src/gpu/vulkan_backend.cpp`): the GPU layers run entirely on the device in
  one command buffer per `forward()` — RMSNorm (+ Gemma post-norms, Qwen-3 q/k
  norms), biases, projections (existing matmul kernels addressing one device
  arena via new `x_off`/`y_off` push constants), RoPE from the uploaded table,
  KV append into an **f16 device KV cache** (n_ctx positions per span layer),
  attention with online softmax (GQA, soft-cap, sliding window), SiLU/GELU·mul,
  residuals. x is uploaded and read back once; prefill runs in 64-token chunks
  inside the same submit. `span_kv_truncate` keeps the prefix cache working;
  a forward that would leave a gap in the device KV is refused.
- New shaders `span_rmsnorm`, `span_add`, `span_rope`, `span_kv_store`,
  `span_attention`, `span_act_mul`.
- Dequantisation: K-quant `dequant32` now builds 4 values per word with masks
  and `unpackUnorm4x8` (scale × 255 folded in): Q6_K matvec 62 → 86 GB/s.
- **Online re-quantisation** (`FISH_GPU_REQUANT=1/2/3`, opt-in, default 0):
  device copies re-quantised at upload from `dequant_row()` output on the
  thread pool (`src/gpu/requant.{h,cpp}`): Q5_K/Q6_K → Q4_K, and Q4_K → **FQ3**,
  a new FISH 3-bit layout (112 B / 256, 3.5 bpw, Q4_K-style 6-bit scales/mins,
  plane-packed for 4-values-per-word decode) with its own matvec/tiled
  kernels. GGUF and CPU path untouched. `fish::gpu::vulkan_describe()` reports
  the resident mix.
- `fish_test_gpu`: three teacher-forced runs on identical inputs (span /
  per-matmul / CPU) with a span ≡ per-matmul check (same argmax every step,
  |Δlogit| ≤ 2%); `--quality N` (top-1 agreement ≥ 95% and KL vs the CPU path);
  requant kernel checks; `FISH_GPU_PROFILE=2` per-stage span profile.
- Measured (shared machine, paired with same-run CPU): 1B decode 65.1 tok/s
  (span) vs 29.9 (per-matmul) vs 19.6 (CPU), and 79.1 / 48.5 / 25.9 in a
  less contended run; prefill 408 vs 92 tok/s;
  8B 7.8 tok/s (15 layers, no requant), 10.35 tok/s with mode 3 (21 layers,
  top-1 95.0%), 12.1 tok/s with mode 1 (22 layers; quality fails at 88%),
  vs 4.8–5.3 tok/s CPU. Targets (1B 80, 8B 20) not met; what remains is in
  `GPU.md` → Measured.

## 2026-11-01 — speculative decoding (opt-in), all-position logits, placement for spans

- `LlamaModel::forward(..., all_logits=true)` + `logits_at(t)`: the output
  projection runs on every row of a small batch (what verification needs).
- `Sampler::distribution()` / `draw()`: the full sampling distribution and a
  draw from arbitrary weights.
- **Speculative decoding** in the C API: `fish_set_draft(ctx, path, k)` /
  `fish_clear_draft`; the draft (same vocabulary, checked) proposes up to k
  tokens, the target verifies them in one `all_logits` forward, acceptance uses
  the standard rejection rule (accept with min(1, p/q), resample from
  max(0, p−q)), so the output distribution is exactly the target's. Adaptive k
  (grows on full acceptance, shrinks to accepted+1 otherwise). Stats:
  `draft_proposed / draft_accepted / draft_active`; Python
  `FishNativeClient.set_draft`, `FISH_DRAFT_MODEL`, `FISH_DRAFT_K`.
- **Measured, honestly:** DeepSeek-R1-Distill-Llama-8B Q4_K_M with
  Llama-3.2-1B-Instruct as draft, greedy, 15–16 GPU layers: output identical
  with and without the draft (correctness ✓), but **1.5 tok/s vs 7.7 tok/s
  plain**. Acceptance was 12% — the R1 distill's reasoning prose is not what a
  Llama-3.2 Instruct 1B predicts — and the verify batch runs at the CPU's
  *prefill* rate (~12 tok/s on the 8B), so a round costs ~0.7 s for ~1.6
  tokens. Speculative decoding therefore stays **off by default**; it pays
  only with a same-family draft (e.g. a 1B of the same fine-tune) and with the
  verify batch on the GPU (spans make that cheap). Re-measure when both hold.
- `place_layers` reserves the span's KV (n_ctx × kv_dim × 4 B per GPU layer)
  and a 32 MB arena before placing, lets `upload()` decide instead of a
  file-size pre-check (re-quantised layers are smaller), and `prepare_span`
  releases the lowest GPU layer and retries when the span is declined.
- GPU agent milestone: 1B whole-layer spans at **65 tok/s** (vs 30 per-matmul,
  20 CPU, same run), logits equal to the non-span path (33/33 argmax).

## 2026-10-31 (night) — span API, output projection on GPU, planner VRAM

- `ComputeBackend` span API (`backend.h`): `SpanLayer`/`SpanParams`
  descriptors, `span_prepare` / `span_forward` / `span_kv_truncate` /
  `span_release`. `model.cpp::prepare_span` offers the GPU layer range as one
  span after placement; `forward()` runs it with one call per token and falls
  back to per-matmul routing if the backend declines. Default implementation
  declines, so behaviour is unchanged until the Vulkan backend implements it.
  `FISH_GPU_SPAN=0` disables.
- Output projection routed through `matmul_tensor` and uploaded first in
  `place_layers` (it was the largest per-token CPU read with a GPU present).
- GPU layers' file pages are evicted from RAM after upload (dense models).
- `specs.cpp` takes VRAM from the compute backend and the planner's rationale
  names the GPU layer count.
- Measured (loaded machine, paired): 1B all layers on GPU 13–16.5 tok/s vs
  8.7 tok/s CPU in the same run; see BENCHMARKS.md for the GPU agent's table.

## 2026-10-31 (evening) — Vulkan GPU backend, stage 1

Details, knobs and limits: `docs/engine/GPU.md`. Numbers: `BENCHMARKS.md`.

- New `src/gpu/vk_loader.{h,cpp}`: the Vulkan loader is opened at runtime
  (`vulkan-1.dll` from the app dir/System32 only, `libvulkan.so.1`,
  `libvulkan.1.dylib`/MoltenVK) and every entry point is fetched through
  `vkGetInstanceProcAddr`; `fish.dll` keeps no link-time Vulkan dependency and
  loads on machines without a driver.
- New `src/gpu/vulkan_backend.{h,cpp}`: `ComputeBackend` implementation.
  Registered by a static initializer (skipped with `FISH_GPU=0`), device opened
  lazily on the first query (never under the DLL loader lock); unregisters
  itself when there is no usable device. Discrete > integrated GPU, software
  devices only with `FISH_GPU=cpu`, compute-only queue family preferred.
  `memory_free()` via `VK_EXT_memory_budget`. Weights uploaded as packed GGUF
  bytes through a 32 MB staging buffer (freed at the first matmul); own VRAM
  ceiling for weights + scratch, plus live headroom of max(256 MB, 10% of heap).
  `matmul_group` overridden: one activation upload, one dispatch per tensor,
  one fence. Blocking fence waits with timeouts (no spinning); a device that
  stops responding disables the backend and the CPU takes over. Backend object
  leaked on purpose (no driver calls at process exit).
- New shaders `shaders/{dequant.glsl,matvec.comp,matmul_tiled.comp}`: in-shader
  dequantisation of Q4_K, Q5_K, Q6_K, Q8_0, Q4_0, F16, F32 using 32-bit
  word reads only (portable; handles 18/34/210-byte blocks); 4-row matvec for
  decode, 64x64 shared-memory tile for prefill. Compiled with glslc and
  embedded (`fish_vk_shaders.inc`); a committed copy in `shaders/prebuilt/`
  is used when glslc is missing or does not run (`ninja fish_vk_prebuilt`
  refreshes it).
- `CMakeLists.txt`: `option(FISH_VULKAN ON)`; finds headers/glslc under
  `VULKAN_SDK` or `C:/VulkanSDK/*`; probes that glslc actually runs; disables
  the backend with a message when there are no headers. Builds and m1/m2 pass
  with ON (build-time shaders), ON (prebuilt) and OFF.
- New `test/test_gpu.cpp` (`fish_test_gpu`): all 7 types (synthetic tensors,
  unaligned rows) + the model's `blk.0` tensors vs the CPU f32 path for
  n_tok 1/3/37/256, `matmul_group`, auto placement end to end (Paris), VRAM
  freed on unload, CPU comparison with `FISH_GPU_LAYERS=0`.
- Measured on RTX 3050 Laptop, machine shared with other GPU/CPU load:
  1B fully offloaded 24.3 vs 20.1 tok/s decode (39–40 in an idle-GPU window),
  prefill 2.8x; 8B with 19/32 layers 4.54 vs 3.18 tok/s, prefill 2.3x. The
  decode targets need the stage-2 requests in `GPU.md`.
- Found while bringing it up: Vulkan SDK 1.4.363's glslc/glslangValidator
  crash on Windows with the older system `MSVCP140.dll` (14.36); CMake now
  detects that and falls back to the prebuilt SPIR-V.

## 2026-10-31 (later still) — tool role, token counting, grouped matmul

- Chat templates render role `tool` with each family's convention: Llama 3 →
  `ipython` header; ChatML (Qwen) → user turn wrapping `<tool_response>`;
  Phi-3 / Gemma / others → user turn (`tokenizer.cpp::chat_segments`). Requested
  by the agentic loop (`docs/agentic/REQUESTS.md`).
- `fish_count_chat_tokens(ctx, roles, contents, n)` — exact token count of a
  conversation after templating, for context budgeting. Python:
  `FishNativeClient.count_tokens(messages)` (falls back to chars/4 on old DLLs).
- `ComputeBackend::matmul_group` (several tensors, one input) with a looping
  default; `ops.cpp::matmul_tensors` uses it for q/k/v and gate/up so a GPU
  backend can submit once per group. `LlamaModel::~LlamaModel` releases every
  resident device tensor.

## 2026-10-31 (later) — three-tier placement, resident window, backend hook

- `include/fish/backend.h`: `ComputeBackend` interface (upload/resident/matmul)
  and `gpu_backend()` registration. `ops.cpp::matmul_tensor` routes resident
  tensors to the device with CPU fallback; every projection in `model.cpp`
  (attention, dense FFN, shared expert) goes through it. MoE expert slices stay
  on the CPU for now. A Vulkan implementation is in progress (`docs/engine/GPU.md`).
- `model.cpp::place_layers()`: GPU tier (last layers first, within
  `FISH_VRAM_BUDGET_MB` / 85% of free VRAM, capped by `FISH_GPU_LAYERS`), RAM
  tier (within `FISH_RAM_BUDGET_MB` / 80% of free RAM), disk tier (streamed).
  `fish_stats` reports `gpu_layers / ram_layers / disk_layers / gpu_bytes / gpu_name`.
- Resident window for the disk tier: the prefetch thread fetches layer *i+1*
  and evicts layer *i−3* from the working set (`VirtualUnlock` / `MADV_DONTNEED`),
  so an 8B under a 3 GB RAM budget decodes at 4.3–4.9 tok/s instead of pushing
  the user's other programs to swap.
- Worker threads run below normal priority (`FISH_LOW_PRIORITY=0` disables)
  so the UI stays responsive while generating.
- RAM bandwidth probe now measures aggregate multi-thread read bandwidth
  (28.8 GB/s here vs 14 GB/s single-thread); planner predictions use it.
- Measured: DeepSeek-R1-Distill-Llama-8B Q4_K_M = 5.6 tok/s decode on CPU,
  ~89% of the memory ceiling (see BENCHMARKS.md).

## 2026-10-31 — FISH 2.0.0 (ABI 2)

### Performance
- Int8 activation quantisation (`Q8Act` per 32, `Q8ActK` per 256 with block sums)
  and AVX2 integer dot kernels for Q4_0, Q8_0, Q4_K, Q5_K, Q6_K
  (`quant_x86.cpp`). Scalar integer references in `quant.cpp` are the oracle.
- Branch-free K-quant scale unpacking, byte-shuffle scale broadcasts, F16C
  half-to-float (`quant_x86.cpp`).
- Prefill tiles weight rows into L1 with tokens as the outer loop (`ops.cpp::matmul_batch`).
- RoPE cos/sin precomputed per position (`ops.cpp::rope_build`); supports normal/NeoX
  pairing, partial rotary, linear, YaRN, longrope factors, Llama-3 factors.
- Attention threaded over (token, head) (`ops.cpp::attention_batch`).
- Thread pool: chunked scheduling, default = physical cores (`threadpool.cpp`).
- Layer prefetch thread when weights exceed ~75% of free RAM
  (`model.cpp::prefetch_loop`, `PrefetchVirtualMemory` / `madvise`).

### Model support
- Architectures: llama, qwen2/qwen2moe, qwen3/qwen3moe, phi3, gemma2 (`model.cpp`).
- MoE with the real GGUF layout (`ffn_gate_inp`, 3-D `ffn_{gate,up,down}_exps`,
  `expert_used_count`), experts stay quantised in place; shared experts.
- SentencePiece tokenizer (scores, byte fallback, `▁` handling) alongside byte-level BPE.
- Pre-tokenizer flavours (llama-bpe 3-digit groups, qwen2 single digit, gpt2 unlimited).
- Chat formats: llama3, chatml, phi3, gemma, mistral, deepseek; detection from
  `tokenizer.chat_template`.
- Quant types added: IQ4_NL, IQ4_XS. Vocab size falls back to the token table.

### API
- `fish_generate_chat` (structural chat), `fish_abi_version`, `fish_supported_archs`.
- `fish_stats` += truncated, cached_tokens, n_threads, streaming, prefill tok/s.
- `fish_model_info` += n_experts, n_experts_per_tok, n_ctx_train, chat_format, tokenizer.
- `fish_destroy` waits for a generation in flight.
- Standalone server: Host check, optional `FISH_TOKEN` bearer auth, `\uXXXX`
  decoding incl. surrogate pairs, 413 on oversize bodies, cancels generation when
  the client disconnects, `SIGPIPE` ignored, accepts `{"messages": [...]}`.

### Bugs fixed (symptom → cause → fix)
1. **Garbage output for Mistral/Llama-2/TinyLlama/Phi/Gemma GGUFs.** Only GPT-2
   byte-level BPE existed; SPM vocabularies were tokenised as BPE. → SentencePiece
   implementation (`tokenizer.cpp::encode_spm`), `tokenizer.ggml.model` honoured.
2. **Prompt injection via special tokens.** `<|eot_id|>` typed by a user became a
   control token. → `encode(parse_special=false)` for content; `encode_chat`
   applies templates structurally (`tokenizer.cpp`, `c_api.cpp::fish_generate_chat`).
3. **Double BOS.** Llama-3 template contained `<|begin_of_text|>` and the Python
   layer also passed `add_bos=1`. → BOS added once from `tokenizer.ggml.add_bos_token`.
4. **Whitespace tokenised differently from the reference.** A run of spaces before
   a word swallowed the word's leading space; newline runs lost trailing spaces.
   → `\s+(?!\S)` and `\s*[\r\n]+` implemented (`tokenizer.cpp::split_pre_tokens`).
5. **Emoji and `×÷«»` treated as letters.** → `is_letter_cp` excludes emoji,
   Latin-1 symbols, fullwidth punctuation, private use.
6. **MXFP4 out-of-bounds read.** 4-bit value indexed an 8-entry table (sign bit
   ignored). → sign handled (`quant.cpp::deq_mxfp4`).
7. **MoE unloadable and memory-exploding.** Looked for `gate.weight` /
   `ffn_gate_exps.N.weight` (wrong names), required dense FFN tensors, read
   `experts_per_token` (wrong key), dequantised every expert to f32 (>100 GB for
   Mixtral), dereferenced missing experts. → real layout, in-place quantised
   experts (`model.cpp::bind_layer`, `moe_ffn`).
8. **Models without `vocab_size` metadata failed to load.** → fallback to token
   table / embedding shape.
9. **Over-long prompts dropped the system prompt and generated ~1 token.** Kept
   only the tail with one free slot, and reported "truncated" as an error while
   returning success. → keep head + tail, reserve room for the answer,
   `stats.truncated` flag (`c_api.cpp::generate_ids_locked`).
10. **RoPE recomputed `pow/sin/cos` per head per token** despite a comment
    claiming otherwise. → table.
11. **Attention single-threaded.** → threaded over (token, head).
12. **RAM bandwidth probe reported ~600 TB/s.** Compiler removed a memcpy whose
    result was unused. → checksummed streaming loop (`specs.cpp`).
13. **`head_dim × n_head ≠ n_embd` rejected** (Gemma 2, Qwen 3). → `q_dim()` used
    for projections.
14. **Standalone server** had no Host/auth check, truncated >1 MB bodies silently,
    could not decode `\u` escapes (aiohttp emits them for every non-ASCII
    character), kept generating after the client left, and died on SIGPIPE on
    Linux. → all fixed (`server.cpp`).
15. **Thread count used logical cores capped at 8.** → physical cores.

### Known limits
- CPU only. No GPU.
- qwen2/qwen3/phi3/gemma2 paths are implemented from the GGUF conventions but
  have not yet been run against a real file on this machine.
- Prefill ≈ 100 tok/s on a 1B model; decode is memory-bound (see BENCHMARKS.md).
