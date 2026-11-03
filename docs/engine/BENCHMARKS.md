# Engine benchmarks

Machine: AMD Ryzen 7 7435HS (8 cores / 16 threads, AVX2), 16 GB DDR5, NVMe
(1.7 GB/s measured sequential), Windows 11. Model: Llama-3.2-1B-Instruct
Q4_K_M (0.75 GB weights, 96 Q4_K + 17 Q6_K tensors). 8 engine threads.

**Caveat:** the numbers below were taken while another IMURUN server process
held a second copy of the model in RAM and other agents were running, so
decode (memory-bound) is depressed by roughly a third versus an idle machine
(an earlier idle run gave 30 tok/s decode). Re-run on an idle machine before
quoting.

## Kernel (one 2048-wide Q4_K row, single thread)

| Path | Time per row | Relative |
|---|---|---|
| f32 path (dequantise nibbles to float, FMA) | 0.23 µs | 1.0× |
| int8 path, per-32 activation scale (first version) | 0.12 µs | 1.9× |
| int8 path, per-256 scale + branch-free scales + F16C | **0.11 µs** | **2.1×** |

Accuracy: AVX2 int8 kernel equals the scalar int8 reference to 1.7e-8 of
Σ|w·x|; int8 vs f32 differs by 0.03% of Σ|w·x| (activation quantisation).

## End to end (1B Q4_K_M, 8 threads, C ABI)

| Stage | Before this pass | After |
|---|---|---|
| Prefill, 28-token prompt | 34 tok/s | 96 tok/s |
| Prefill, 109-token prompt | ~70 tok/s | 112 tok/s |
| Prefill, 406-token prompt | 81 tok/s | 101 tok/s |
| Decode | 25–30 tok/s | 23–30 tok/s (noise-dominated, see caveat) |
| Second turn with shared system prompt | full prefill | 30 of 44 tokens served from KV prefix cache; prefill 0.13 s vs 0.83 s |

Where the time goes now:

- **Decode** is memory-bandwidth bound: 0.75 GB of weights per token at the
  ~20–25 GB/s this laptop sustains ≈ 30–35 ms/token. Only a smaller model file
  (lower-bit quant) or more bandwidth changes this. 16 threads (SMT) made decode
  *slower* (17.9 tok/s) — keep one thread per physical core.
- **Prefill** is compute-bound on the kernel: ~10–14 G multiply-adds/s per core.
  AVX2 peak with `vpmaddubsw` is far higher; the remaining overhead is per-group
  scale broadcasts and the per-(row, token) function-pointer dispatch.
  Next steps (roadmap): process 2–4 rows per call sharing the activation loads,
  and an AVX-512 VNNI variant (`vpdpbusd`) on CPUs that have it.

## 8B model: DeepSeek-R1-Distill-Llama-8B Q4_K_M (4.58 GB), 2026-10-31

Measured with the new kernels, 8 threads, idle machine, planner context 4096:

| Metric | Value |
|---|---|
| Load (mmap + metadata) | 2.9 s |
| First prefill (cold page cache, 13 tokens) | 3.16 s — dominated by reading 4.6 GB from NVMe at 1.9 GB/s |
| Second turn prefill (13 cached of 26 tokens) | 0.17 s |
| **Decode** | **5.6 tok/s** |
| Aggregate RAM read bandwidth (all cores, measured by the planner) | 28.8 GB/s |
| Bytes moved per token | 4.58 GB → memory ceiling ≈ 6.3 tok/s |

Decode is at ~89% of this laptop's measured memory bandwidth: the CPU path is
essentially at its physical limit for this file size. Reaching 20 tok/s with
a 4.6 GB model would need ~92 GB/s, which no laptop CPU memory system provides.
What moves the number (see `docs/ROADMAP.md`):

| Lever | Expected decode on this machine | Effort |
|---|---|---|
| Smaller file: Q3_K_M (3.7 GB) / Q2_K-IQ2 (~3 GB) with int8 kernels for those types | ~7.5 / ~9 tok/s | days (kernels) |
| Speculative decoding with Llama-3.2-1B as draft (same Llama-3 vocabulary) | ~1.5–2.5× → 9–13 tok/s | ~1 week |
| **GPU offload (RTX 3050 Laptop, 4 GB, ~190 GB/s)** via Vulkan/CUDA: Q3_K_S 8B fits entirely; Q4_K_M needs ~24 of 32 layers on GPU | 25–35 tok/s fully resident; 10–14 tok/s partial | weeks |

## GPU offload (Vulkan stage 1), 2026-10-31

RTX 3050 Laptop (4 GB, driver 610.62, ~192 GB/s peak), same laptop. Only the
layer projections run on the GPU (see `GPU.md`). **Caveat:** the session was
shared: another process kept the GPU at 40–60% utilisation / ~50 W for most
runs, and other agents loaded the CPU (CPU-only 8B decode measured 3.2–3.8
tok/s here vs 5.6 idle). The *paired* columns were measured back to back in
one process, so their ratio is meaningful; absolute values are depressed.

### Kernels (`fish_test_gpu`, `FISH_GPU_PROFILE=1`, GPU idle window, 8B tensors)

| Tensor (type, size) | n_tok=1 kernel | Effective | n_tok=256 kernel | CPU int8, 256 tok |
|---|---|---|---|---|
| ffn_gate (Q4_K, 33 MB) | 0.34 ms | 98 GB/s | 20.7 ms (~1.45 TFLOPS) | 173–210 ms |
| ffn_up (Q4_K, 33 MB) | 0.46 ms | 72 GB/s | (same shape as ffn_gate; not captured) | 173–285 ms |
| ffn_down (Q6_K, 48 MB) | 0.71 ms | 68 GB/s | 21.0 ms | 154–239 ms |
| attn_q / attn_output (Q4_K, 9.4 MB) | 0.13 ms | 75 GB/s | 7.0 ms | 46–60 ms |
| attn_v (Q6_K, 3.4 MB) | 0.07 ms | 51 GB/s | 1.8 ms | 10–13 ms |

Fixed cost per synchronous submit (record + submit + fence wait + copies):
~0.08 ms on an idle GPU, 0.3–0.5 ms while another process shares the GPU.
`matmul_group` (q/k/v in one submit) measured 1.05 ms vs 1.89 ms as three
calls under contention. Accuracy vs the CPU f32 path: worst element < 1% of
the 1e-3·Σ|w||x|/n_out bound for all 7 types.

### End to end (`fish_test_gpu <gguf> --no-kernels`, paired runs, loaded machine)

| Model | Placement | Prefill 128 tok | Decode | Decode, CPU only (same run) |
|---|---|---|---|---|
| Llama-3.2-1B Q4_K_M | 16/16 layers on GPU (557 MB) | 251 tok/s (CPU 89) | 24.3 tok/s | 20.1 tok/s |
| Llama-3.2-1B Q4_K_M | same, GPU idle window (earlier build, before the 4-row matvec) | — | 39–40 tok/s | — (idle CPU reference: ~30) |
| DeepSeek-R1-Distill-Llama-8B Q4_K_M | auto: 19/32 layers (2.36 GB) | 23.2 tok/s (CPU 9.9) | 4.54 tok/s | 3.18 tok/s |
| same | 22/32 layers (before the headroom fix) | 26.9 tok/s (CPU 11.0) | 4.45 tok/s | 3.56 tok/s |

8B decode per token with 19 GPU layers: 76 submits, 39 ms GPU kernels
(~60 GB/s), ~39 ms submit/wait overhead (contended), ~142 ms CPU (13 layers +
the 525 MB output projection + attention). With 22 layers the GPU kernels
dropped to ~35 GB/s: the heap was full while another process grew its VRAM,
and WDDM demoted part of the weights to system memory. The backend now keeps
max(256 MB, 10% of the heap) free, live.

**Targets not reached yet** (8B ≥ 12 tok/s with ~24 layers; 1B ≥ 80 tok/s).
What limits decode now, in order: CPU work that is still per token
(output projection, attention, the CPU layers), then 4 synchronous submits per
layer. The levers are in `GPU.md` → "Requests to the engine owner" (output
projection on the GPU, whole-layer spans with the KV cache in VRAM).
Re-measure on an idle machine before quoting absolute numbers.

## GPU stage 2: layer spans + online re-quantisation, 2026-11-01

Same laptop, same caveat (shared machine: other agents on the CPU, other
programs holding 0.7–1.8 GB VRAM). Every row pairs the GPU run with a CPU-only
run from the same process. Spans: the GPU layers run as one device command
buffer per token with the KV cache in VRAM (f16); see `GPU.md`.

### Kernels after the word-wise dequant (1B shapes, `FISH_GPU_PROFILE=1`)

| Tensor | Before | After |
|---|---|---|
| ffn_down Q6_K 8192×2048 (13.8 MB) | 0.223 ms (62 GB/s) | 0.161 ms (86 GB/s) |
| ffn_gate / ffn_up Q4_K 2048×8192 (9.4 MB) | 0.102 ms (93 GB/s) | 0.096 ms (99 GB/s) |
| attn_q / attn_output Q4_K (2.4 MB) | 0.032 ms | 0.032 ms (74 GB/s) |
| attn_v Q6_K (0.9 MB) | 0.017 ms | 0.015 ms (59 GB/s) |

### Llama-3.2-1B Q4_K_M, all 16 layers + output on the GPU

| | Decode | Prefill 128 | Per token |
|---|---|---|---|
| span | **65.1 tok/s** | 408 tok/s | 2 submits; GPU 14.0 ms (matvec 92.6%), CPU 0.7 ms |
| per-matmul (`FISH_GPU_SPAN=0`) | 29.9 tok/s | 274 tok/s | 65 submits |
| CPU | 19.6 tok/s | 92 tok/s | |

| final regression run (less contended), same build | **79.1 tok/s** span / 48.5 per-matmul / 25.9 CPU | | |

Equivalence (teacher-forced, 33 steps): span vs per-matmul 33/33 argmax,
max |Δlogit| 0.0056; span vs CPU 32/33, 3.7%. Target ≥ 80 tok/s: at the edge
(65–79 measured depending on machine load).

### DeepSeek-R1-Distill-Llama-8B Q4_K_M (4.58 GB file)

| `FISH_GPU_REQUANT` | GPU layers | Device weights | Decode GPU | Decode CPU (same run) | Top-1 vs CPU, 200 steps | KL mean / max |
|---|---|---|---|---|---|---|
| 0 | 15 | 2290 MB | 7.81 tok/s | 5.28 | | |
| 0 (another program held 1.8 GB VRAM) | 7 | 1307 MB | 3.97 tok/s | 3.36 | 98.5% (the f32-vs-int8 floor) | 0.0004 / 0.004 |
| 2: Q6_K→Q4_K | 18 | 2388 MB | 8.68 tok/s | 3.41 | 96.5% | 0.009 / 0.088 |
| 2 (VRAM taken by another program mid-run) | 8 | 1218 MB | 5.03 tok/s | 4.64 | 97.0% | 0.006 / 0.055 |
| 3: + gate/up → FQ3 | 21 | 2445 MB | **10.35 tok/s** | 4.81 | 95.0% | 0.044 / 0.372 |
| 1: all Q4_K → FQ3 | 21 | 2268 MB | 10.04 tok/s | 5.11 | 88.1% ✗ | 0.112 / 2.01 |
| 1 | 22 | 2359 MB | 12.14 tok/s | 5.28 | (not run at 22) | |

Per token, mode 3 / 21 layers (96.6 ms): GPU 32.9 ms (2 submits), CPU 63.1 ms
(11 layers). Load with re-quantisation: 6–30 s depending on mode and CPU load.
Re-quantisation error per weight (relative RMS vs the GGUF): Q6_K→Q4_K ~7.4%,
Q4_K→FQ3 ~14%.

**20 tok/s for this file on this card is not reached** (best measured 12.1
tok/s with failing quality, 10.35 with quality at the bar). It needs all 32
layers on the GPU (~3.35–3.9 GB of weights vs ~2.3–2.8 GB usable here) *and*
~110 GB/s sustained matvec throughput, or speculative decoding on top. See
`GPU.md` → Measured → "What remains".

## LoRA adapter merged at load, 2026-11-02 (1B Q4_K_M, real r=16 adapter, 112 tensors)

Paired, alternating, best of 3 per round, 3 rounds, through `FishNativeClient`:

| | Plain | With adapter |
|---|---|---|
| Decode, GPU span (16 layers) | 104.1 tok/s | 103.4 tok/s |
| Decode, CPU (`FISH_GPU_LAYERS=0`) | 24.9 tok/s | 25.6 tok/s |
| Load | ~0.7 s | ~2.4 s (merge 1.4–2.1 s) |
| Extra RAM | — | 557 MB (merged copies) |

Merge quality and the rounding comparison are in `CHANGELOG.md` (2026-11-02).
Reproduce: `model\fish\build\fish_test_m5.exe <base.gguf> <adapter.gguf> [<merged reference.gguf>]`.

## Default placement, cold vs warm load, 2026-11-03

RTX 3050 Laptop 4 GB (the desktop held 0.63–0.80 GB of VRAM during every
run), Ryzen 7 7435HS, **`FISH_THREADS=4`**, below-normal priority, one
benchmark at a time. Driver: the C ABI through `ctypes`, two chat turns,
greedy, 64 new tokens:
- turn 1: system prompt + "Explain in two sentences why the sky is blue.";
- turn 2: shares the system prompt via the prefix cache.

TTFT = wall time from `fish_generate_chat` to the first streamed token
(prefill + the first decode step). "CPU only" = `FISH_GPU_LAYERS=0` in the
same session, right after the GPU run. "Before" = `bin/fish.dll` of
2026-11-01 (no residency planner), same session. No environment knobs other
than the ones named.

### Decode and first token

| Model | Build | Placement (`stats().placement.planner`) | Decode | TTFT turn 1 / turn 2 | Load |
|---|---|---|---|---|---|
| DeepSeek-R1-Distill-Llama-8B Q4_K_M | before | 14/32 layers at file precision | 7.3 tok/s | 3.96 s / 1.11 s | 4.1 s |
| | **after** | partial: 31/32 planned, span 28 (FQ3S mostly), ctx 2048, KV int8 | **18.7 tok/s** | **0.67 s / 0.29 s** | **2.75 s** (warm) |
| | after, second cache dir | 29 planned, span 28 | 16.2 tok/s | 0.84 s / 0.27 s | 3.71 s (warm) |
| | after, `fish_test_gpu --ppl` (ctx 512) | 30/32, span 30 | 21.3 tok/s | — | 1.6 s |
| | CPU only | — | 5.3 tok/s | 3.87 s / 1.28 s | 0.8 s |
| same, `FISH_VRAM_HEADROOM_MB=1024` (server balanced preset) | before | 13/32 at file precision | 6.9 tok/s | 4.21 s / 1.03 s | 3.8 s |
| | after | partial: 21/32 | 11.5 tok/s | 1.49 s / 0.55 s | 2.5 s (warm) |
| Phi-3-mini-4k Q4_K_M | before | 29/32, f16 KV (ctx 2048) | 32.3 tok/s | 0.76 s | 3.8 s |
| | before, n_ctx 4096 requested | 23/32 | 21.4 tok/s | 1.68 s | 3.5 s |
| | **after** | resident 32/32 at file precision, KV int8 | **39.7 tok/s** | 0.31 s / 0.12 s | 3.3 s |
| | after, n_ctx 4096 requested | resident, ctx fitted to 2816 | 40.2 tok/s | 0.14 s | 1.4 s |
| | after, headroom 1024 | resident, re-quantised (Q4_K/FQ3) | 47.9 tok/s | 0.25 s | 190 s cold (see below) |
| | CPU only | — | 9.7 tok/s | 2.46 s | 0.7 s |
| Llama-3.2-1B-Instruct Q4_K_M | after | resident 16/16 at file precision, ctx 8192, KV f16 | 84.2 tok/s (100.3 turn 2) | 0.17 s / 0.04 s | 2.5 s |
| | CPU only | — | 17.1 tok/s | 1.04 s | 1.1 s |

The decode spread on the 8B at 28 GPU layers (16.2–18.7 tok/s) is
run-to-run: machine load, GPU clocks, and which tensors the upgrade passes
moved to a bigger format. The planner wants 31 layers with the default
headroom, but the span usually ends at 28–29. WDDM's live budget falls
further during the upload than the 6% margin allows, and the span gives back
a layer at a time until its KV + arena fit next to the 256 MB headroom.

**20 tok/s for the 8B** is reached when ≥ 30 layers fit: 21.3 tok/s at 30.
It is missed at 28: 16.2–18.7 tok/s. Each CPU layer costs ~6 ms per token at
4 threads. The server's 1 GB headroom leaves room for only 21 layers
(11.5 tok/s).

### 8B quality of the default placement (`fish_test_gpu --ppl`, held-out text, 291 positions)

| | Perplexity | Top-1 vs CPU | KL mean / max |
|---|---|---|---|
| CPU, file precision | 14.105 | — | — |
| GPU default (30 layers, 147 FQ3S + 3 FQ3 tensors) | 15.120 (+7.2%) | 82.8% | 0.158 / 1.56 nats |

### Load phases (`fish_stats.load_profile`, 4 threads)

| Model, cache state | Total load | Calibration | Re-quantisation | Upload (copy) | Rest |
|---|---|---|---|---|---|
| 8B, empty cache (`FISH_CACHE_DIR` new dir) | **153.2 s** | 69.6 s | 79.8 s (146 tensors) | 2.2 s | 0.2 s |
| 8B, same dir, second load | **3.71 s** | cached | 0 (146 from cache, 1.6 s) | 0.5 s | 0.1 s |
| 8B, every load before the cache fix (cache hits silently rejected) | 106 s | cached | 100.4 s (161 tensors) | 2.4 s | — |
| Phi-3, headroom 1024, empty cache (before the re-quantiser speed-up) | 190 s | 35.4 s | 152.4 s (103 tensors) | 1.5 s | — |

The open, metadata and bind phases take 0.03–0.07 s for every model: the file
is mmapped, not read. The CPU KV cache allocates in 0.0–0.24 s.

Re-quantiser, single thread (micro-benchmark, 4096×256 synthetic rows):

| Kernel | ns/weight before | after | Weighted relative RMS error (unchanged) |
|---|---|---|---|
| Q4_K (weighted) | 135 | 66 | 3.69% |
| FQ3 (weighted) | 141 | 65 | 7.32% |
| FQ3S | 35 | 34 | 10.02% |

## How to reproduce

```
model\fish\build\fish_test_m1.exe models\Llama-3.2-1B-Instruct-Q4_K_M.gguf   # kernel timings
.venv\Scripts\python.exe model\fish\test\test_fish.py models\Llama-3.2-1B-Instruct-Q4_K_M.gguf
```
GPU: `set FISH_GPU_PROFILE=1` then `model\fish\build\fish_test_gpu.exe <gguf>`
(kernels + end to end) or `... --no-kernels` (end to end only);
`FISH_GPU_PROFILE=2` adds the per-stage span profile; quality of a placement:
`set FISH_GPU_REQUANT=3` + `fish_test_gpu.exe <8B gguf> --quality 200`. Check
`nvidia-smi` first: another GPU user changes the numbers by 2x.
For prefill scaling use `fish_generate` with `max_tokens=1` on prompts of 28 /
109 / 406 tokens and read `prefill_tokens_per_second` from `fish_get_stats`.
Set `FISH_LIBRARY` to `model\fish\build\fish.dll` to bench a fresh build while
a server still holds `bin\fish.dll`.
Load phases and the planner decision: FISH_GPU_VERBOSE=1 ([fish/plan] and [fish/load]
lines) or stats()["placement"]["planner" / "load_profile"]; a cold-cache load: point
FISH_CACHE_DIR at an empty directory. test/test_fish.py honours FISH_LIBRARY.
