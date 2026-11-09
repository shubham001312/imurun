# FISH GPU backend (Vulkan)

One portable backend (Vulkan compute) for NVIDIA, AMD and Intel on
Windows/Linux, later Android and macOS (MoltenVK). Two levels:

- **Stage 1 — per-matmul offload.** The projections of the GPU layers run on
  the device, one synchronous submit per matmul group; attention, norms and the
  KV cache stay on the CPU. Always available.
- **Stage 2 — layer spans.** The contiguous range of GPU layers runs entirely on
  the device: norms, projections, RoPE, attention over a **device-resident f16
  KV cache**, FFN, residuals. One command buffer, one submit, one fence per
  `forward()`; x crosses the bus once each way. Default when the model allows it
  (`FISH_GPU_SPAN=0` turns it off).
- **Online re-quantisation** (default since the residency planner,
  2026-11-02/03; `FISH_GPU_REQUANT=0` turns it off): the device gets a smaller,
  importance-weighted copy of the weights (Q4_K / FQ3 / FQ3S) so more layers fit
  in VRAM; the copies are cached on disk. The GGUF and the CPU path are
  untouched. See "Residency planner and device formats".

Code: `model/fish/src/gpu/` (`vk_loader.*`, `vulkan_backend.*`, `requant.*`),
`model/fish/shaders/` (GLSL), test `model/fish/test/test_gpu.cpp`.
Hook: `include/fish/backend.h` (`ComputeBackend`, incl. the span API).

## How the backend is found and loaded

1. `fish.dll` / `libfish.so` has **no link-time Vulkan dependency**. A static
   initializer in `vulkan_backend.cpp` registers the backend object
   (`set_gpu_backend`) unless `FISH_GPU=0` (`off`/`false`/`no`). It does
   nothing else: DllMain runs under the loader lock, and creating a Vulkan
   instance there (which loads driver DLLs and starts driver threads) can
   deadlock.
2. The device is opened lazily on the first query (`place_layers()` →
   `memory_free()` at model load): the loader is opened at runtime
   (`vulkan-1.dll` from the application directory / System32 only — never
   CWD/PATH —, `libvulkan.so.1`, `libvulkan.1.dylib` / `libMoltenVK.dylib`), all
   entry points come from `vkGetInstanceProcAddr` / `vkGetDeviceProcAddr`, and a
   Vulkan **1.1** instance is created (`VK_KHR_portability_enumeration` when
   present; `FISH_VK_VALIDATE=1` adds the validation layer if installed).
3. If anything fails, the backend **unregisters itself** (`gpu_backend()`
   becomes `nullptr`, engine CPU-only); the reason is in
   `fish::gpu::vulkan_status()` and on stderr with `FISH_GPU_VERBOSE=1`.
4. Nothing is torn down at process exit: the object is deliberately leaked and
   the loader never closed (no driver calls during DLL detach). The destructor
   does release everything, for code that deletes it explicitly.

## Device selection

Discrete GPU > integrated > virtual; CPU/software implementations only with
`FISH_GPU=cpu`; `FISH_GPU_DEVICE=<index>` forces one. API 1.1+ and a compute
queue are required; a **compute-only queue family** is preferred so FISH's
work does not share the compositor's queue (`FISH_VK_QUEUE=universal` forces
the graphics+compute family; measured: no latency difference on the RTX 3050).

## Budgets and knobs

| Variable | Effect |
|---|---|
| `FISH_GPU=0` | No GPU backend at all (not even registered). |
| `FISH_GPU=cpu` | Allow (and prefer) a software Vulkan device. For CI/testing. |
| `FISH_GPU_LAYERS=N` | At most N layers on the GPU (`0` = CPU only). Unset = as many as fit. |
| `FISH_VRAM_BUDGET_MB=M` | VRAM ceiling. Unset = live free VRAM minus the headroom. |
| `FISH_GPU_SPAN=0` | Disable layer spans (stage-1 per-matmul routing only). |
| `FISH_GPU_REQUANT=1/2/3` | Legacy fixed re-quantisation modes (see below); they skip the residency planner. `0` = never re-quantise (the planner may still shorten the context). Unset / `auto` = the residency planner decides ("Residency planner and device formats"). |
| `FISH_GPU_MIN_CTX=n` | Shortest context the planner may fit to keep the whole model on the GPU (default 2048). |
| `FISH_GPU_KV=f16\|q8` | Device KV type; default f16, int8 when the planner is VRAM-bound. |
| `FISH_VRAM_HEADROOM_MB=m` | VRAM kept free (default max(256 MB, 5% of the heap)). Wins over `_PCT`. |
| `FISH_VRAM_HEADROOM_PCT=p` | VRAM kept free as a share of the device heap (0..90); for hosts that do not know the card's size. |
| `FISH_GPU_CACHE=0`, `FISH_CACHE_DIR` | Disable / move the importance + re-quantised-bytes cache (`~/.imurun/cache/fish`). |
| `FISH_VK_COOPMAT=0` | No tensor-core prefill kernels. |
| `FISH_GPU_DEVICE=i` | Use physical device i. |
| `FISH_GPU_VERBOSE=1` | Init line (device, heap, budget), span sizes, or why the backend / span declined. |
| `FISH_GPU_PROFILE=1` | GPU timestamps around every submit (`fish::gpu::vulkan_profile`). |
| `FISH_GPU_PROFILE=2` | Also per-stage GPU time inside each decode span (printed at span release). |
| `FISH_VK_MATVEC_MAX=n` | Largest token count that uses the matvec kernel (default 4). |
| `FISH_VK_VALIDATE=1` | Enable `VK_LAYER_KHRONOS_validation` if installed. |

`memory_free()` uses **`VK_EXT_memory_budget`** (heap budget − heap usage, so
other programs' VRAM use is seen live; without the extension 80% of the heap
minus FISH's own allocations). The backend applies its own ceiling (85% of
free at init or `FISH_VRAM_BUDGET_MB`) to weights + scratch + span buffers, and
refuses any allocation that would leave less than **max(256 MB, 10% of the
heap)** free on the live budget. That headroom is measured, not cosmetic: with
the heap filled to the last ~64 MB while another program grew its VRAM, WDDM
demoted part of the weights to system memory and the kernels fell from ~60 to
~35 GB/s. `upload()` fails cleanly when a check fails; `place_layers()` then
stops adding layers (since 2026-11-01 it trusts `upload()` and
`bytes_resident()` deltas rather than file sizes, and reserves the span's KV +
arena before placing layers).

`fish::gpu::vulkan_describe()` returns one line for stats, e.g.
`vulkan NVIDIA GeForce RTX 3050 Laptop GPU: 2359 MB weights, 134 x Q4_K->FQ3
1740 MB (from 2236), 21 x Q6_K->Q4_K 619 MB (from 903); span 22 layers, KV 44 MB f16`.

## What runs where

| | GPU | CPU |
|---|---|---|
| Stage 2 (span, default) | for the GPU layers: RMSNorm (+ Gemma post-norms, Qwen-3 q/k norms), biases, q/k/v, RoPE, KV append + attention over the device KV cache (GQA, soft-cap, sliding window), wo, residual, gate/up (fused or not), SiLU/GELU·mul, down, residual; and the output projection | embeddings, the CPU layers, final norm, sampling |
| Stage 1 (`FISH_GPU_SPAN=0`, MoE models, or a declined span) | projections of the GPU layers + output projection | everything else, incl. attention and KV for every layer |

Placement (`LlamaModel::place_layers()`): output projection first, then the
*last* layers backwards while they fit. Decode per token with a span: 1 submit
for the span + 1 for the output projection; stage 1: 4 per GPU layer + 1.

### Span execution (`span_forward`)

One command buffer: copy x (n_tok × n_embd f32) into the device arena →
per layer, per chunk of ≤ 64 tokens: rmsnorm → q/k/v matvecs (or tiled matmuls
for prefill) → bias → q/k norm → RoPE (cos/sin table uploaded once) → KV store
(f16) → attention → wo → residual add → rmsnorm → gate/up → act·mul → down →
residual add, with a compute→compute barrier between dependent steps → copy x
back. Prefill chunks see the KV written by earlier chunks, so causality holds
inside one submit. On any failure x is left untouched and the model falls back.
`span_kv_truncate(n)` keeps positions < n valid; a `span_forward` that would
leave a gap in the device KV is refused rather than guessed.

Device memory for a span: arena (x for 256 tokens + per-chunk work buffers:
~11 MB for 1B, ~20 MB for 8B), params (norm vectors, biases, RoPE table) and the
KV cache: `n_layers_in_span × n_ctx × kv_dim × 4` bytes (f16 K and V) — 128 KB
per position for the 8B, i.e. 64 MB at n_ctx 512, 512 MB at 4096.

## Online re-quantisation (`FISH_GPU_REQUANT`)

At upload, rows are dequantised with the engine's `dequant_row()` and
re-quantised on the engine thread pool; the device copy is smaller, nothing
else changes.

| Mode | Q4_K tensors | Q5_K / Q6_K tensors | 8B layer size (vs file) |
|---|---|---|---|
| 0 (default) | as is (4.5 bpw) | as is (5.5 / 6.56 bpw) | ~127 MB |
| 2 | as is | → Q4_K | ~123 MB |
| 3 | ffn_gate/ffn_up → FQ3, others as is | → Q4_K | ~108 MB |
| 1 | → FQ3 (3.5 bpw) | → Q4_K | ~95 MB |

**FQ3** is FISH's own 3-bit layout (`src/gpu/requant.h`): 112 B / 256 values —
f16 d, f16 dmin, the Q4_K 12-byte 6-bit scale/min packing, then per 32-value
group three words A, B, C; values 4s..4s+3 are the bytes of
`((s<4 ? A : B) >> 2(s&3)) & 0x03030303 | ((C >> s) & 0x01010101) << 2`, so the
shader builds four values with two masks and one `unpackUnorm4x8`. Values are
`d·sc·q − dmin·m`, q ∈ 0..7. The quantiser fits (scale, min) per group by
least squares, then quantises the scales to 6 bits under f16 super-scales (the
Q4_K scheme); a 21-point importance-weighted grid search was measured as no
better (relative RMS 13.87% either way for Q4_K→FQ3) at 8x the load time.

Measured re-quantisation error (relative RMS vs the GGUF weights, 1B blk.0):
Q6_K→Q4_K 7.3–7.5%, Q4_K→FQ3 13.9–14.6% (the inherent cost of mapping 16
levels onto 8). End-to-end quality: see Measured.

## Kernels

All GLSL 4.50 compute, SPIR-V (Vulkan 1.1) embedded as `uint32_t` arrays
(`fish_vk_shaders.inc`); no shader file is read at runtime.

| Shader | Used for | Work split |
|---|---|---|
| `matvec.comp` | decode, n_tok ≤ 4 | 256 threads = 8 lane groups × 4 rows; a lane takes whole 32-element sub-blocks, loads the 32 activations once for 4 rows; shared-memory reduction |
| `matmul_tiled.comp` | prefill, n_tok > 4 | 64 rows × 64 tokens per workgroup, K in chunks of 32 dequantised once into shared memory; 4×4 outputs per thread |
| `dequant.glsl` | both | `dequant8` / `dequant32` per type; K-quants and FQ3 decode 4 values per word with masks + `unpackUnorm4x8` (scale × 255 folded in) |
| `span_rmsnorm.comp` | span | one workgroup per row; strided rows cover per-token and per-head (q/k) norms |
| `span_add.comp` | span | residual add or bias broadcast |
| `span_rope.comp` | span | (token, head) workgroups, table lookup, normal / NeoX pairs |
| `span_kv_store.comp` | span | k, v → f16 KV cache (two halves per word: no 16-bit storage feature needed) |
| `span_attention.comp` | span | (head, token) workgroups, keys in chunks of 128 with an online softmax (running max/sum, rescaled accumulator): no score buffer, no context limit from shared memory; GQA, soft-cap, sliding window |
| `span_act_mul.comp` | span | SiLU / GELU(tanh) × up, separate or fused gate|up |

Matmul kernels: one SPIR-V per weight type (F32, F16, Q4_0, Q8_0, Q4_K, Q5_K,
Q6_K, FQ3) × {matvec, tiled} = 16 modules, plus the 6 span modules.

### Dequantisation layouts (`shaders/dequant.glsl`)

Weights are bound as `uint[]`; bytes are extracted with shifts (portable: no
8/16-bit storage feature; non-4-aligned 18/34/210-byte blocks handled with
funnel-shifted reads).

| Type | Block | Layout decoded |
|---|---|---|
| Q4_K | 144 B / 256 | f16 d, f16 dmin, 12 B packed 6-bit scale/min × 8, qs[128]; sub-block s uses the low (s even) / high (s odd) nibbles of qs[32·(s/2) …]; `d·sc·q − dmin·m` |
| Q5_K | 176 B / 256 | as Q4_K + qh[32] at 16; 5th bit of element l in sub-block s = bit s of qh[l] |
| Q6_K | 210 B / 256 | ql[128], qh[64] @128, int8 scales[16] @192, f16 d @208; low 4 bits `ql[64n+l+32(q&1)] >> 4(q>>1)`, high 2 bits `qh[32n+l] >> 2q`, scale `scales[8n+l/16+2q]`; `d·sc·(q6−32)` |
| Q8_0 | 34 B / 32 | f16 d, int8 qs[32] |
| Q4_0 | 18 B / 32 | f16 d, qs[16]: low nibbles = elements 0..15, high = 16..31; `d·(q−8)` |
| F16 / F32 | 2 / 4 B | direct |
| FQ3 | 112 B / 256 | FISH re-quantised 3-bit, above |

Other GGUF types (Q2_K, Q3_K, IQ*, BF16, MXFP4, …) → `supports() = false`
(that layer stays on the CPU).

### How to add a type

1. `dequant8(ro, k, v)` (and `dequant32` if one scale covers 32 elements) under
   `#elif defined(TYPE_X)` in `dequant.glsl`.
2. `X` in `_vk_types` (CMakeLists.txt) and `{GgmlType::X, "X"}` in `kernels_`.
3. Add it to the synthetic-type loop in `test_gpu.cpp`, run `fish_test_gpu`,
   refresh the prebuilt SPIR-V.

## Build

`option(FISH_VULKAN "Vulkan GPU backend" ON)`. CMake looks for `vulkan/vulkan.h`
under `$VULKAN_SDK` and `C:/VulkanSDK/*` (newest first) and for `glslc` next to
it: headers + working glslc → shaders compiled at build time; headers but no
working glslc → committed `shaders/prebuilt/fish_vk_shaders.inc`; no headers →
backend disabled with a message; `-DFISH_VULKAN=OFF` → not compiled.

**Windows gotcha:** the glslc/glslangValidator of Vulkan SDK 1.4.363 crash in
`MSVCP140.dll` against an older system VC++ runtime (14.36). CMake probes
`glslc --version` and falls back to the prebuilt SPIR-V. To rebuild shaders
anyway, update the VC++ 2015-2022 redistributable, or put `glslc.exe` in a
private folder with a newer `msvcp140.dll`, `vcruntime140.dll`,
`vcruntime140_1.dll` and configure with `-DFISH_GLSLC=<folder>\glslc.exe`.

Regenerate the prebuilt SPIR-V after editing a shader:

```
cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release -DFISH_GLSLC=<working glslc>
ninja -C build fish_vk_prebuilt     # build/vk_gen/fish_vk_shaders.inc -> shaders/prebuilt/
```

## Tests

`build/fish_test_gpu <model.gguf> [--no-kernels] [--no-e2e] [--no-cpu-compare] [--quality N]`

1. **Kernels.** All supported types on synthetic tensors (unaligned rows,
   partial workgroups) and the model's `blk.0` projections, n_tok 1/3/37/256,
   vs the CPU f32 `matmul_batch`; per-element bound
   `1e-3 · (Σ|w||x|) / n_out` (measured < 1% of it). `matmul_group` likewise.
   With `FISH_GPU_REQUANT` set: the re-quantisation error per tensor and the
   GPU kernel vs the CPU decode of the same re-quantised bytes.
2. **End to end, three runs on identical inputs** (teacher-forced on the first
   run's greedy tokens): auto placement (span), `FISH_GPU_SPAN=0` (per-matmul
   GPU), `FISH_GPU_LAYERS=0` (CPU). Requires Paris, all VRAM freed on unload,
   and **span ≡ per-matmul: same argmax at every step, max |Δlogit| ≤ 2% of
   max |logit|**; reports span vs CPU for information (the CPU uses int8
   activations, so it differs slightly).
3. **`--quality N`**: CPU greedy run of N tokens as the reference, GPU placement
   teacher-forced on it; top-1 agreement (must be ≥ 95%) and KL(P_cpu‖P_gpu).

`fish_test_m1` / `fish_test_m2` link the backend too (m2 runs through the span
when a device is present); `test_fish.py` exercises the prefix cache →
`span_kv_truncate`.

## Measured (RTX 3050 Laptop 4 GB, Ryzen 7 7435HS)

The machine was shared throughout (other agents on the CPU, other programs
holding 0.7–1.8 GB of VRAM and up to 40% of the GPU at low clock). **Every GPU
number is paired with a CPU-only number from the same process, back to back**;
ratios are meaningful, absolute values are depressed. Details in
`BENCHMARKS.md`.

### Llama-3.2-1B Q4_K_M (all 16 layers + output on the GPU), 2026-11-01

| Path | Decode | Prefill 128 tok | GPU per token |
|---|---|---|---|
| span (default) | **65.1 tok/s** (15.4 ms); **79.1 tok/s** in the final, less contended run | 408 tok/s | 2 submits, 14.0 ms GPU, 0.7 ms CPU |
| per-matmul (`FISH_GPU_SPAN=0`) | 29.9 tok/s; 48.5 in the final run | 274 tok/s | 65 submits, 9.5 ms GPU, 18 ms waiting |
| CPU only | 19.6 tok/s; 25.9 in the final run | 92 tok/s | — |

Span ≡ per-matmul: argmax equal at 33/33 teacher-forced steps, max |Δlogit|
0.0056 (0.02% of max |logit|); span vs CPU: 32/33, 3.7% (int8 activations on
the CPU). Span decode stage profile (`FISH_GPU_PROFILE=2`): matvec 92.6%,
rmsnorm 2.3%, attention 2.2%, act·mul 1.0%, add 0.8%, KV store 0.7%, RoPE
0.4%. Isolated kernels for one layer sum to 0.44 ms (7.1 ms for 16) + the
262 MB output projection (~3 ms); the span takes ~4 ms more than that under
sustained load (60 W power cap, GPU shared). Target 80 tok/s: **at the edge**
— 65.1 tok/s in a contended run, 79.1 tok/s in the final regression run (same
build, CPU 25.9 tok/s in that run); ~80 is what the isolated kernel times add
up to.

### DeepSeek-R1-Distill-Llama-8B Q4_K_M (the 4.6 GB file), 2026-11-01

| Placement | GPU layers | Device weights | Decode | CPU only, same run | 200-step quality vs CPU |
|---|---|---|---|---|---|
| no re-quantisation, span | 15 | 2290 MB | 7.81 tok/s | 5.28 | top-1 98.5%, KL 0.0004 (measured with 7 layers: f32-vs-int8 activation floor) |
| `FISH_GPU_REQUANT=2` (Q6_K→Q4_K) | 18 | 2388 MB | 8.68 tok/s | 3.41 | top-1 **96.5%**, KL 0.009 |
| `FISH_GPU_REQUANT=3` (+ gate/up → FQ3) | 21 | 2445 MB | **10.35 tok/s** | 4.81 | top-1 **95.0%**, KL 0.044 |
| `FISH_GPU_REQUANT=1` (all Q4_K → FQ3) | 21–22 | 2268–2359 MB | 10.0–12.1 tok/s | 5.11–5.28 | top-1 **88.1%**, KL 0.112 — fails |

Per-token breakdown, mode 3, 21 GPU layers (96.6 ms): GPU 32.9 ms (span +
output projection, 2 submits, ~74 GB/s over 2.4 GB), CPU 63.1 ms for the
other 11 layers (~5.7 ms each on this loaded CPU). Span ≡ per-matmul on the
8B: 33/33 argmax, 0.03%.

**What remains for 20 tok/s (50 ms/token)** with this file on this card:

1. *VRAM.* All 32 layers + output need ~3.9 GB in mode 3, ~3.35 GB in mode 1
   (which fails quality), vs ~2.3–2.8 GB usable while other programs hold
   0.7–1.8 GB. Each layer left on the CPU costs ~5.5 ms/token.
2. *Bandwidth.* Even fully resident, mode 3 moves ~3.75 GB/token: at the
   measured ~74 GB/s span throughput that is ~51 ms → ~19.6 tok/s; it needs
   ~110+ GB/s matvec throughput (the isolated Q4_K kernel already does ~98).
3. *Speculative decoding* (engine side, in progress) multiplies whatever
   the verify pass achieves; `span_forward` already verifies n draft tokens in
   one submit (tiled kernels for n > 4) and `span_kv_truncate` drops rejected
   ones.

(2026-11-01 conclusion, superseded: re-quantisation was opt-in then. Since
the residency planner it is the default; see the next section and the quality
line in "Known limits".)

## Residency planner and device formats (2026-11-02)

`LlamaModel::plan_resident()` (called from `place_layers()` for dense models
with spans enabled and no `FISH_GPU_LAYERS` cap) tries to put **every layer
and the output projection** on the GPU as one span before the old
"last layers backwards while they fit" placement is used. A layer left on the
CPU costs more per token than the whole span saves on an 8B, so the planner
gives up first file precision, then context, before it gives up residency:

1. File bytes + device KV for the requested context fit → nothing changes.
2. File bytes fit with a shorter context (≥ `FISH_GPU_MIN_CTX`, default 2048,
   or the request if smaller) → the context is shortened to what fits
   (multiple of 256). `fish_model_info.n_ctx` / stats show the new value.
3. Otherwise the device copies are **re-quantised** (`FISH_GPU_REQUANT` unset
   or `auto`): every tensor starts at its smallest format, then the most
   sensitive tensors are upgraded while the budget at the floor context allows
   (k/v to the file's type first, then in three rounds output, ffn_down,
   q/o/qkv, gate/up; first and last layers before middle ones). Whatever is
   left becomes context.
4. **(2026-11-03)** Not even that fits → the **top N layers + output**,
   re-quantised the same way, with N as large as the budget allows at the
   floor context; the first 32 − N layers run on the CPU at file precision.
   Before this step existed, an 8B that missed full residency by ~100 MB fell
   all the way back to step 5 (13–16 layers at file precision, 4.6–7.3 tok/s):
   this was the "GPU not used properly" the owner saw.
5. Otherwise (or if calibration fails) → per-layer placement at file
   precision, as before.

**This is the default path**: no environment variable is needed for an 8B to
be re-quantised and placed on the GPU. `FISH_GPU_REQUANT=0` keeps file
precision (steps 1–2, then 5); `FISH_GPU_REQUANT=1/2/3` are the old fixed
modes and skip the planner.

**What it decided** is in `fish_stats.placement` (ABI 4; Python
`stats()["placement"]["planner"]`) and, with `FISH_GPU_VERBOSE=1`, on stderr
as `[fish/plan]` lines, e.g.
`partial: 31/32 layers + output, 2873 MB weights (file 57, q4k 0, fq3 3, fq3s 158 tensors), ctx 4096 -> 2048, KV int8, budget 3109 MB, calibration cached 0.0 s, upload 1.8 s`,
or why it did not run (`planner skipped: GPU layer cap …`, `no resident plan:
… re-quantisation is off`, `resident upload of … failed …`). The load phases
are in `fish_stats.load_profile` / `[fish/load]`.

Budget: live free VRAM (`VK_EXT_memory_budget`) minus a headroom of
max(256 MB, 5% of the heap), `FISH_VRAM_HEADROOM_PCT` (share of the heap) or
`FISH_VRAM_HEADROOM_MB` (wins), minus the span arena, a 64 MB margin for
scratch, span parameters and driver allocations, and **6% of the weights**
(measured: WDDM lowers the live budget by 8–14% more than the bytes uploaded;
without that margin the span declined afterwards and gave back 3 of 31
planned layers). After reserving the weight blocks the planner re-reads the
live budget and drops layers *before* uploading if the span's KV + arena would
no longer fit. The backend applies the same headroom rule
(`vram_headroom_bytes()`, `backend.h`) to every new allocation; tensors that
land in a block the planner reserved are not checked again (they take no new
VRAM; before this fix the planner's own reservation made the first upload
fail). (Before 2026-11-02: 85% of free plus a max(256 MB, 10%) headroom.)

**Headroom matters most.** The server's balanced preset passes
`FISH_VRAM_HEADROOM_MB=1024`. On this 4 GB card with ~0.75 GB held by the
desktop that leaves ~2.3 GB, i.e. ~22–24 of the 8B's 32 layers; with the
engine default (256 MB) ~3.1 GB, i.e. 28–31 layers. See `BENCHMARKS.md`
(2026-11-03) for both.

**Format choice prefers the cache.** Re-quantising the 8B costs ~80 s at 4
threads, and the live budget moves by tens of MB between loads. When the
model's cache directory already holds device files, the upgrade passes only
move a tensor to a format that is cached (or to the file's own bytes), so a
slightly different budget does not trigger a new re-quantisation.

Device KV: f16, or **int8** (`span_kv_store_q8` / `span_attention_q8`: four
codes per word, one f32 absmax per 32 values, 2.25 B per K+V element instead
of 4) when the planner is VRAM-bound (the file's weights with an f16 cache
for the requested context do not fit). `FISH_GPU_KV=f16|q8` forces one.
Measured on Llama-3.2-1B (file weights, held-out perplexity vs the CPU):
f16 KV +0.90%, int8 KV +1.30% (top-1 98.3% / 98.6%, KL 0.0018 / 0.0025).

### Device formats

| Format | bpw | Layout (`src/gpu/requant.h`) | From |
|---|---|---|---|
| file | as is | the GGUF bytes | any supported type |
| Q4_K | 4.5 | GGUF Q4_K, importance-weighted fit | Q5_K, Q6_K, Q8_0, F16, … |
| FQ3 | 3.5 | 6-bit scale + min per 32, 3-bit codes, importance-weighted fit | any K/legacy type |
| **FQ3S** | **3.25** | f16 d, 8 × 6-bit scales, 3-bit codes on 8 levels symmetric around 0, cubic warp `L = u(1−c+cu²)`, c = 0.35 | any K/legacy type |

FQ3S keeps the FQ3 code planes (three words per 32 values, decoded with two
masks and one `unpackUnorm4x8`) and replaces scale + min by one scale and a
non-uniform level map, which costs two extra FMAs per value in the kernel.

**Importance.** The re-quantisers minimise Σ w_j (x_j − x̂_j)² per block with
w_j = E[x_j²] · sqrt(σ² + x_j²), where E[x_j²] is input column j's mean square
over a built-in calibration text (`src/calib.cpp`, ~600 tokens: prose,
explanation, code, arithmetic, dialogue, lists, three other languages), run
once through the CPU forward at file precision with a matmul observer
(`set_matmul_observer`, `ops.h`). The output projection gets the plain fit
(it sees one row per forward). The planner never re-quantises without
importance. Unweighted, FQ3S has no zero level and every near-zero weight
moves by ±0.11 s; the few massive-activation channels of Llama models
multiply exactly those errors (1B perplexity 287 vs 11.9).

**Cache.** `~/.imurun/cache/fish/<file stem>-<size>-<hash>/`:
`importance.v1.bin` and one `<tensor>.<fmt>.dev` per re-quantised tensor
(header with types/shape, then the device bytes). The key hashes the file
size, the first and last MiB, the adapter (path + scale) and the format
version. `FISH_CACHE_DIR` moves the root, `FISH_GPU_CACHE=0` disables it.
Second load reads the bytes instead of calibrating and re-quantising.
**Since 2026-11-03 the warm path really is one:** a cache hit is read with a
4 MB stdio buffer straight into a two-buffer staging ring (`stream_upload`:
chunk k is read from disk while chunk k−1 is DMA'd), with no host copy of the
tensor and no GGUF page touched. Before, every hit was silently rejected
(`setvbuf` was called after the header read, which drops buffered bytes, so
the payload read came up short) and the whole model was re-quantised on every
load: 8B load 106 s → **2.75 s** warm (2873 MB from cache in 1.4 s).
File-precision tensors go through the same ring (memcpy from the mapping
overlapped with the copy).

**Allocation.** Weight buffers are bound into shared device-memory blocks;
the planner reserves its whole total up front (`reserve_device`, ≤ 256 MB
blocks, first-fit). One `vkAllocateMemory` per tensor cost ~1.2 MB each
under WDDM. The per-matmul scratch is capped at 32 MB of output (tokens are
chunked): sized for 256 tokens it was 262 MB of VRAM for a 128k vocabulary.

Study switches (tests only): `FISH_GPU_FORCE=file|q4k|fq3|fq3s` (every tensor
in one format), `FISH_GPU_TIERS=<list>` (formats the planner may use),
`FISH_GPU_NO_IMPORTANCE=1`, `FISH_CALIB_TOKENS=n`, `fish_test_gpu --ppl`
(held-out perplexity, top-1, KL vs the CPU file path; `FISH_TEST_CTX` sets the
load context), `--fq3s-scan` (per-tensor FQ3S error).

### Prefill on the tensor cores (`matmul_coop.comp`, 2026-11-03)

For n_tok > `FISH_VK_MATVEC_MAX` (prefill, verification) every weight type
now has a cooperative-matrix kernel: 64 rows × 64 tokens per workgroup, K in
steps of 32. Half the threads dequantise the weight slab into f16 shared
memory (the same `dequant32` as every other kernel) while the other half
convert the activations to f16. Four subgroups then do 16×16×16
`coopMatMulAdd` with f32 accumulators. It is enabled when the device has
`VK_KHR_cooperative_matrix` with a 16×16×16 f16×f16→f32 subgroup
configuration, `shaderFloat16`, the Vulkan memory model and 32-wide subgroups
(the RTX 3050 has all of them); otherwise the f32 tiled kernel is used.
`FISH_VK_COOPMAT=0` turns it off. f16 activations change kernel outputs by up
to ~4e-4 × max|y|; held-out perplexity on the 1B went from +0.90% to +0.92%
vs the CPU (no measurable change).

Measured, 1B fully on the GPU: prefill of 128 tokens **983 tok/s** vs 670
(tiled f32), 256 tokens 626 tok/s, on a GPU that other programs were using.
A 64 × 128 tile with 128-token span chunks measured no better under the same
contention and was reverted. The kernel reaches only ~1 TFLOPS on the 1B's
shapes, far below the tensor cores' peak. The next step is the load side:
coalesced activation loads, double-buffered slabs, and a wider K per barrier.

### Prefix cache across requests

`fish_generate_chat` keeps the longest common token prefix with the previous
request, including the tokens it generated (`cache_ids`). Measured on the 8B
on the CPU: a repeated 1,936-token prompt costs 0.42 s instead of 157 s.
Since 2026-11-03 a **truncated** prompt keeps its cut point (`trunc_head`,
`trunc_drop_end` in `c_api.cpp`). Before, every new turn moved the cut and
the cache matched only the head: 8B follow-up turn 222 s with 497 of 1,984
tokens cached; 1B at ctx 1024: 240/960 cached, 4.35 s → 720/739 cached,
0.08 s. A new cut drops an extra quarter of the budget, so the next turns fit
under the same cut. There is one cached sequence per context: a side
generation with another prefix (planner, title, voice clean-up) evicts the
conversation. A multi-slot cache is on the roadmap.

## Known limits

- **VRAM is the binding constraint for the 8B on a 4 GB card.** Other programs
  (desktop, browser) hold ~0.7–1.8 GB of it. The 32 layers + output need
  2.9 GB at the smallest formats (FQ3S) + KV + margins ≈ 3.2 GB, so with
  0.75 GB taken and the default 256 MB headroom 28–30 layers fit, with the
  server's 1024 MB headroom ~22. Each layer left on the CPU costs ~6 ms per
  token at 4 threads; 28 GPU layers measured 18.7 tok/s, 30 measured 21.3.
- **Quality of 3-bit re-quantisation**: see Measured; mode 1 does not meet the
  ≥ 95% top-1 bar, so re-quantisation stays opt-in.
- Decode matvec reaches ~75–100 GB/s (Q4_K) and ~60–86 GB/s (Q6_K) of ~192;
  under sustained load the laptop GPU hits its 60 W power cap and, with other
  GPU users, long span submissions are time-sliced (the span then measures up to
  ~1.5× its isolated kernel time).
- The tiled prefill kernel is a plain f32 tile (~1.4 TFLOPS); no
  cooperative-matrix path.
- MoE models use stage 1 only (experts on the CPU).
- Cold load (first load of a model, or a format the cache does not hold yet)
  costs calibration (~512 tokens on the CPU) plus the re-quantisation:
  153 s for the 8B at 4 threads (calibration 69.6 s, re-quantisation 79.8 s,
  2026-11-03, after the 2.1x re-quantiser speed-up). Warm loads read the
  cache: 2.75–3.7 s for the 8B.
- **Quality of the 8B's default placement** (mostly FQ3S, 3.25 bpw): held-out
  perplexity +7.2% vs the CPU at file precision, top-1 agreement 82.8% over
  every position, KL 0.158 nats (`fish_test_gpu --ppl`, 2026-11-03). This is
  roughly what a Q3_K file costs; it is the price of residency on a 4 GB card.
  `FISH_GPU_REQUANT=0` keeps file precision (and ~7 tok/s).
- Tested on NVIDIA only (RTX 3050 Laptop, driver 610.62). AMD/Intel/Android
  paths are portable SPIR-V without vendor extensions but **not run**.

## Requests to the engine owner

Done: `~LlamaModel` releases resident tensors; `matmul_group`; output
projection through `matmul_tensor` and uploaded first; GPU layers' pages
evicted after upload; `specs.cpp` reads VRAM from `gpu_backend()`; the span API;
`place_layers` reserves span KV + arena, trusts `upload()` /
`bytes_resident()`, and retries a declined span with fewer layers.

Open:

1. **Surface `fish::gpu::vulkan_describe()` in `fish_stats`** (e.g. a
   `gpu_detail` string at the end of the struct) so users see the placement and
   the re-quantisation mix.
2. **KV for the span is f16 at full `n_ctx`.** For the 8B that is 512 MB at
   n_ctx 4096, i.e. ~4 layers' worth of VRAM. Options on the engine side: a
   smaller default n_ctx when the GPU is the bottleneck, or let the span start
   with a short KV and grow it (would need a `span_kv_reserve(n)` call before
   long prompts). Also: the CPU still allocates f32 KV for the span layers
   (wasted RAM: n_ctx × kv_dim × 8 bytes per layer).
3. **A failed span after successful ones**: the model then runs those layers
   on the CPU with a CPU KV cache that lacks the earlier positions. Today the
   backend only fails on a lost device (after which every GPU path is off), but
   the fallback should also `reset_kv()` (or replay) rather than continue.
4. For speculative decoding: `span_forward` already handles n_tok > 1 at any
   pos0 ≤ the device KV length and returns the residual stream for every token;
   rejected draft tokens are dropped with `span_kv_truncate(n_accepted)`.
