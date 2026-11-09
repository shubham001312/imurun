# FISH Engine — Architecture Overview

## Vision
**One binary that scales from 4 GB RAM phone to 8×H100 node.** Same model file, same API, zero configuration. The engine *invents* a new residency model by fusing:
- **llama.cpp** — battle-tested ggml backend, GGUF format, quantization, LoRA, MoE expert cache
- **AirLLM ideas** — layer streaming, one-layer-ahead prefetch, CPU-resident tables — but reimagined for C++/GGUF without Python/transformers overhead

## Core Innovation: Three-Tier Residency Arena
```
┌─────────────────────────────────────────────────────────────────┐
│                        VRAM ARENA (Fixed)                       │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌───────────┐ │
│  │ Layer 0     │ │ Layer 1     │ │ Layer 2     │ │ Staging   │ │  ← Pinned host memory ring
│  │ (embedding) │ │ (attn)      │ │ (ffn)       │ │ Slot 0-3  │ │     64-256 MB each
│  └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘ │
│  LRU eviction → demote to Warm tier                            │
└─────────────────────────────────────────────────────────────────┘
                              │ async H2D via staging ring
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      WARM TIER (CPU RAM, mmap)                  │
│  GGUF file memory-mapped; OS page cache manages residency       │
│  Next 4-8 layers pre-positioned here by Prefetcher              │
└─────────────────────────────────────────────────────────────────┘
                              │ async pread / io_uring
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       COLD TIER (NVMe / Disk)                   │
│  Original GGUF file — never fully loaded                        │
│  Tensor offset table (from GGUF header) enables byte-accurate   │
│  random access to any layer/expert without scanning             │
└─────────────────────────────────────────────────────────────────┘
```

**Key properties:**
- **Zero per-layer GC** — buffers allocated once at startup, reused forever
- **Fixed VRAM budget** — user sets (e.g. 3 GB on 4 GB card); engine never exceeds
- **Staging ring** — 4 slots, 64-256 MB each, pinned host memory, overlaps H2D with compute
- **Tensor-granular** — not layer-granular; attention vs FFN can live in different tiers

## Module Dependency Graph
```
platform.h/cpp          ← Ground floor: alloc, timing, PMU, lock-free rings
      │
      ├─▶ hardware_probe.cpp     → HardwareCaps (RAM, VRAM, CPU, disk, mmap, NUMA)
      ├─▶ gguf_inspect.cpp       → TensorTable (name, offset, size, quant, layer_idx, expert_id)
      ├─▶ layer_planner.cpp      → LayerPlan (hot/warm/cold split, VRAM budget, staging config)
      ├─▶ compression_policy.cpp → QuantPolicy per tensor, online re-quant
      │
      ▼
ResidentWindow          ← Owns VRAM arena + staging ring; buffer reuse, LRU
      │
      ├─▶ Prefetcher        ← Background thread: Cold→Warm (pread) + Warm→Hot (H2D)
      └─▶ ExpertStreamer    ← MoE experts as first-class residents
      │
      ▼
AdapterMgr              ← LoRA + Control Vectors; serializes per-adapter batches
      │
      ▼
Engine (API)            ← start_gen / next_token / cancel / stats / set_adapters
      │
      ▼
Embedded Server Routes  ← /fish/load, /fish/stream, /fish/health, /fish/stats
                        (links llama-server-impl, replicates server_run_instance)
```

## Data Flow: Single Token Generation
```
User Prompt
    │
    ▼
Engine::start_gen() → tokenize → build first ubatch
    │
    ▼
for each ubatch:
    ├─ Prefetcher.schedule_prefetch(next_layers)  ← async, non-blocking
    │
    ├─ ResidentWindow.ensure_hot(current_layers)  ← blocks only if promotion needed
    │
    ├─ AdapterMgr.activate(request_adapters)      ← llama_set_adapters_lora + cvec
    │
    ├─ llama_decode() / llama_encode()            ← ggml graph compute
    │       │
    │       ├─ cb_eval hook (if needed for custom ops)
    │       └─ ggml_backend_sched runs on CPU/GPU/Metal/CUDA/Vulkan
    │
    ├─ Prefetcher.wait_ready(next_layer)          ← sync point before next ubatch
    │
    ▼
sample next token → yield to caller
```

## Concurrency Model
| Thread | Role | Communication |
|--------|------|---------------|
| **Main (API)** | HTTP request handling, `start_gen`, `next_token` | Lock-free SPSC rings to workers |
| **Prefetcher** | Background Cold→Warm→Hot promotions | `MpscRing<PrefetchJob>` from main; `SpscRing<Completion>` back |
| **Compute** | `llama_decode` loop (can be multiple for batch) | Direct ggml_backend calls; no queue |
| **Stats Sampler** | 10 Hz PMU snapshot, memory pressure | Atomic counters, no locks |

**No mutexes on hot path.** All cross-thread communication via lock-free rings (`platform.h`).

## Memory Allocation Strategy
| Tier | Allocation | Flags | Lifetime |
|------|------------|-------|----------|
| VRAM Arena | `ggml_backend_alloc_buffer` (CUDA/Metal/CPU) | `PINNED` equivalent | Process lifetime |
| Staging Ring | `alloc_aligned(256 MB, 2 MB, HUGE_2M \| PINNED)` | Huge pages, pinned, NUMA-local | Process lifetime |
| Warm (mmap) | `mmap` / `MapViewOfFile` GGUF file | `HUGE_2M` if available | Process lifetime |
| Prefetch Buffers | `alloc_aligned(64 MB, 2 MB, HUGE_2M)` | Reused from pool | Per-prefetch job |

**Allocation happens once at `Engine::create()`** — zero allocations during generation.

## Quantization as a Streaming Knob
- **Offline**: `llama_model_quantize` with `max_buf_size` → produces Q4_K_M / Q8_0 / IQ4_NL shards
- **Online**: `CompressionPolicy::requantize_tensor()` — single-tensor re-quant (Q8→Q4) when VRAM pressure spikes
- **Per-tensor policy**: Attention → higher precision (Q8_0), FFN → aggressive (Q4_K_M), norms → F16

## MoE Expert Streaming
- Reuses llama.cpp's `moe_cache_size` + `sched_copy_experts`
- **Extension**: Experts are first-class residents in `ResidentWindow` with their own LRU
- `ExpertStreamer::hint_next_experts()` called from router logits (if exposed)

## Adapter / Control Vector Hot-Swap
- LoRA: `llama_set_adapters_lora` — forces scheduler re-reserve (serialized per llama.cpp)
- Control Vectors: injected at `process_ubatch` via custom `cb_eval` — no model reload
- **Batching rule**: Requests with different adapter sets *never* co-batch (llama.cpp `can_split` equality check)

## Embedded Server (No Fork)
```
FISH binary
    │
    ├─ links llama-server-impl (static)
    ├─ replicates server.cpp:627-680 bootstrap
    │       common_init(), llama_backend_init(), llama_numa_init()
    │       server_stream_session_manager_start()
    │       server_http_context::init/start
    │       server_routes::init_routes()  ← built-in /v1/completions, /health, etc.
    │
    └─ registers /fish/* routes on same http_context
            GET  /fish/health
            POST /fish/load        {model_path, vram_limit_mb}
            POST /fish/stream      {prompt, params} → SSE tokens
            GET  /fish/stats       EngineStats JSON
            POST /fish/adapters    {loras[], cvecs[]}
            POST /fish/cancel      {handle}
```
**Forces `SERVER_ROLE_INSTANCE`** — router code never linked.

## Build & Deploy
```bash
# One-time setup (done)
winget install Kitware.CMake Ninja-build.Ninja BrechtSanders.WinLibs.POSIX.UCRT Git.Git

# Build
cd imurun/model/fish
cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release
ninja -C build

# Test
./build/fish_test

# Run embedded server
./build/fish_server --model /path/to/model.gguf --vram-limb 3072
```

## Benchmark Targets (this laptop: RTX 3050 4 GB, 16 GB RAM, NVMe)
| Metric | Target |
|--------|--------|
| TTFT (7B Q4_K_M) | < 800 ms |
| Prefill throughput | > 500 tok/s |
| Decode throughput | > 40 tok/s |
| Peak VRAM | ≤ 3.5 GB (user limit 3 GB + headroom) |
| Peak RSS | ≤ 6 GB |
| Disk MB/token (32B model) | < 0.5 MB |
| Largest runnable model | 70B Q4_K_M (streaming) |

## Security
- Loopback-only bind (127.0.0.1)
- Per-run bearer token (injected by Python server, never in URL/log)
- Request body cap: 1 MB (configurable)
- Host header validation (reject non-loopback)
- No remote assets, CSP `default-src 'self'`

## Future Extensions (not in Phase 1)
- Model downloader (HF Hub, Ollama, direct URLs) → separate Python service
- Connector OAuth pipeline → Python server layer
- Multi-model residency → FISH scheduler already supports, needs policy
- Speculative decoding / MTP → `llama_context_params.ctx_other` chain
- Distributed inference → RPC backend (`GGML_RPC`)

---

**Every module has its own `doc/` folder** with function-level docs, invariants, and connection diagrams.