# FISH — the inference engine

FISH is the model runtime that ships with IMURUN. It is a self-contained C++17
engine: it reads GGUF files, dequantises k-quant weights inside its own kernels,
runs the transformer, tokenises, samples, and manages its own memory budget.
Nothing in this directory links llama.cpp, and no llama.cpp binary is required at
runtime. GGUF is used as a file format (it is an open container), not as a
runtime dependency.

## Shape of the product

```
  Python server  ──ctypes──▶  fish.dll / libfish.so / libfish.dylib
  (services/*)                (model/fish)
```

The engine is a **shared library**, not an executable. The host process loads it
in-process, so:

* no separate program is launched, which keeps Windows Application Control and
  macOS Gatekeeper from treating the engine as an untrusted application;
* the weights live in one address space instead of being copied through a pipe;
* streaming is a callback, not an SSE hop over localhost.

A standalone `fish_server` binary is still built for Linux/container
deployments where the engine must run as its own service.

## Modules

| File | Responsibility |
| --- | --- |
| `src/gguf.cpp` | GGUF parser, memory-mapped tensor data, metadata |
| `src/quant.cpp` | Scalar reference kernels for every supported block format |
| `src/quant_x86.cpp` | AVX2/FMA kernels, installed at runtime after a CPU check |
| `src/ops.cpp` | RMSNorm, RoPE, GQA attention, SwiGLU, softmax, batched matmul |
| `src/model.cpp` | Transformer forward pass, KV cache, layer scheduling |
| `src/tokenizer.cpp` | GPT-2 byte alphabet, llama-bpe merges, chat templates |
| `src/sampler.cpp` | Repetition penalty, temperature, top-k, nucleus |
| `src/specs.cpp` | Hardware measurement, execution plan, bounded arena |
| `src/threadpool.cpp` | Persistent worker pool (one fork/join per projection) |
| `src/c_api.cpp` | Stable C ABI consumed by the Python host |
| `src/server.cpp` | Optional standalone HTTP/SSE server |

The scalar kernels are the correctness oracle; AVX2 is validated against them.

## What it actually does on this machine

Measured, not estimated (Ryzen 7 7435HS, 17 GB RAM, NVMe):

| Model | Quant | Context | Decode | Notes |
| --- | --- | --- | --- | --- |
| Llama 3.2 1B | Q4_K_M | 8192 | ~21 tok/s | fully resident |
| Llama 3.2 3B | Q4_K_M | 4096 | ~9 tok/s | fully resident |

The specs engine predicted 19.4 tok/s for the 1B case before generation started;
the measured result was 20.7 tok/s. The prediction is deliberately memory-bound
(the engine is bandwidth-limited at 4 bits), so it stays honest rather than
optimistic.

### Honest limits

Dense 100B at Q4 is roughly 56 GB of weights. This machine has 17 GB of RAM and
~2 GB/s of disk, so a dense 100B model is a physics problem, not a tuning
problem: ~26 s/token with a perfect streaming window. FISH reports that in its
plan instead of pretending otherwise.

The viable 100B-class path is a **sparse MoE** model (~5B active parameters).
There the per-token traffic is ~3.5 GB, and with an expert cache plus
speculative decoding it becomes interactive. The planner already models
expert-granular windows (`ModelProfile::n_experts`, `experts_per_token`,
`expert_bytes`), but the MoE forward pass itself is not implemented yet — the
engine currently runs dense Llama architectures.

## The specs engine

`SpecsEngine` measures the device and derives a plan:

* physical/logical cores, AVX2/AVX-512/NEON;
* total and *available* RAM, swap;
* sequential read bandwidth, measured with unbuffered I/O against the actual
  model file (not a synthetic temp file);
* RAM copy bandwidth, measured.

From that and the model's own profile it decides the context size, thread count,
how many layers stay resident, how many stream per token, and the arena cap. If
the weights do not fit, it reports `windowed` or `streamed` mode and the
predicted tok/s, with a rationale string. `fish_specs_json()` exposes the whole
thing.

Memory is never assumed: the planner subtracts a reserve (1.5 GB or 12% of RAM,
whichever is larger) and the arena is a hard-capped bump allocator. If a request
would exceed the cap it fails and the caller shrinks the window rather than
letting the machine swap.

## Build

```powershell
cmake -S model/fish -B build/fish -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/fish
```

or on Linux/macOS:

```bash
cmake -S model/fish -B build/fish -DCMAKE_BUILD_TYPE=Release
cmake --build build/fish -j
```

Outputs: `fish.dll` / `libfish.so` / `libfish.dylib`, plus `fish_server` and the
C++ acceptance tests.

On Windows the build embeds a version resource. That is required for the library
to look like a product to Windows rather than an anonymous blob.

## Test

```bash
python model/fish/test/test_fish.py          # C ABI: model, tokenizer, template, stream
python model/fish/test/test_native_client.py # product path: in-process, chat template, stats
```

Both print `PASSED` on success.

## Windows trust (Smart App Control)

A freshly built unsigned DLL has no reputation. Smart App Control (SAC) blocks
such files with `WinError 4551` / CodeIntegrity event 3077, and the verdict is
per file hash — two builds of identical source can differ, because adding a
single function changes the hash.

The product fix is a code-signing certificate for ImuTechz.in (OV or EV) and the
existing timestamped signing flow:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sign_windows.ps1 `
    -Path .\bin -PfxPath C:\certs\imutechz.pfx
```

For local development on a machine with SAC enabled, either sign each build or
turn SAC off in Windows Security. This is a machine setting, not something the
engine can or should work around.

## C ABI

See `include/fish/c_api.h`. The contract used by the Python host:

| Call | Purpose |
| --- | --- |
| `fish_create` / `fish_destroy` | context lifetime |
| `fish_load(path, 0)` | load a GGUF; `0` lets the specs engine choose the context |
| `fish_chat_prompt` | render messages with the model's own template |
| `fish_generate` | stream decoded UTF-8 chunks through a callback |
| `fish_cancel` | stop a running generation |
| `fish_get_stats` | prefill/decode timings, tok/s, context use |
| `fish_specs_json` | measured hardware and the chosen plan |
| `fish_kernel_name` | `scalar` or `avx2` |
