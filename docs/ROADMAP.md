# Roadmap

Ordered by value per effort. Each item names the owner scope and the doc to
update when it lands.

## Engine (`model/fish`)

1. **Bring up the implemented-but-unverified families** with real files:
   Qwen 2.5 1.5B, Qwen 3 0.6B, Phi-3.5 mini, Gemma 2 2B, Mixtral (MoE). Fix
   what breaks, add rows to `docs/engine/ARCHITECTURES.md`.
2. **Prefill kernel throughput** (currently ~100 tok/s on 1B): process 2–4 weight
   rows per activation load; AVX-512 VNNI (`vpdpbusd`) and AVX2-VNNI paths;
   ARM NEON `sdot` kernels for Apple silicon and phones (the scalar path is the
   only ARM path today).
3. **KV cache in f16** (halves memory, lets the planner give longer context).
4. **IQ2/IQ3 and TQ quant kernels** so the smallest files run on 4 GB devices.
5. **True tiered residency**: pinned staging buffers, explicit per-layer load
   queue with a byte budget instead of relying on the OS page cache;
   `MADV_DONTNEED` after use on Linux. Then expert-level prefetch for MoE after
   routing.
6. **GPU**: Vulkan compute (works on NVIDIA/AMD/Intel/Android) for the matmuls,
   keeping the same int8 activation format; CUDA later. This is the real
   "VRAM arena" from the original design.
7. Speculative decoding with a tiny draft model; `deepseek2` (MLA) and `gemma3`.
8. **LoRA adapter at load** (`docs/tuning/REQUESTS.md` E1, in progress
   2026-11-02): merge the tuning channel's 22 MB adapter GGUF into the
   quantised weights at load so a run no longer needs an 800 MB merged file.

## Server and bindings (`services`, `model/*.py`)

1. Frontend API requests: persona `tone`/`prompt`, settings defaults for
   customisation and voice, per-session context tokens, download progress on
   `/api/events`, session search, regenerate (`docs/frontend/API-REQUESTS.md`).
2. **Voice server**: `/api/voice/status`, `/transcribe` (whisper.cpp-class
   model, multilingual: en/hi/bn/de…), `/speak` (Piper-class local TTS with
   comfortable voices per language); CSP `media-src 'self' blob:`. Until then
   the UI uses the browser's speech APIs.
3. `--host` forwarded to the bind address (`docs/setup/CODE-REQUESTS.md` #5).
4. Pytest coverage for the turn runner, tools and bindings (today the `tests/`
   files are runnable scripts).

## Frontend (`frontend/design-lab`)

See `docs/frontend/CHANGELOG.md` for what shipped; open items are in
`docs/frontend/API-REQUESTS.md` (needs server) and the "Not verified" list
(real engine, real speech, pywebview/WebView2, Firefox/Safari, touch).

## Setup

- Linux/macOS run of `run.sh` / `run.command`, pywebview GTK/Qt notes, signed
  builds, and a CI matrix that builds the engine on all three OSes.
