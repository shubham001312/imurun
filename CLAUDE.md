# IMURUN — guide for engineers and CLI agents

Read this first. It tells you what the product is, where each piece lives,
how to build and test it, and the rules that keep several people (or agents)
from stepping on each other.

## What this is

IMURUN is a local-first AI chat app. Three layers, one process:

| Layer | Path | Language | Talks to |
|---|---|---|---|
| **FISH engine** — GGUF inference (tokenizer, int8 kernels, KV cache, planner) | `model/fish/` | C++17 | loaded in-process as `bin/fish.dll` (`libfish.so` / `.dylib`) |
| **Python bindings + server** — ctypes bindings, turn runner, sessions, tools, connectors, HTTP/SSE API | `model/*.py`, `services/` | Python 3.11–3.14 | engine via C ABI; frontend via loopback HTTP |
| **Frontend** — "Liquid Paper" UI, themes, voice, models tab | `frontend/design-lab/` | vanilla HTML/CSS/JS (no build step, strict CSP) | server via `/api/*` + `/api/events` SSE |

Entry points: `python start.py` (native window if pywebview is installed, else browser),
`run.bat` / `run.sh` / `run.command` (install + start), `python install.py`; installed: `imurun` (`pip install imurun`, `npx imurun`; `imurun/cli.py` → `start.main`, see `docs/setup/RELEASE.md`).

## Documentation map

| Topic | Where |
|---|---|
| Setup, run, troubleshooting (Win/mac/Linux) | `docs/setup/README.md` |
| Engine: how it works, supported models, kernels, ABI | `docs/engine/ARCHITECTURE.md`, `docs/engine/ARCHITECTURES.md` |
| Engine: measured performance | `docs/engine/BENCHMARKS.md` |
| Engine: what changed and bugs fixed | `docs/engine/CHANGELOG.md` |
| Server API contract (routes, events, auth) | `services/server/doc/README.md` |
| Server/tools changes and bugs fixed | `docs/server/CHANGELOG.md` |
| Agentic layer: tool-call loop, tools, permissions/workspace, approvals, skills, sub-agents | `docs/agentic/` (README, CHANGELOG, BUGS-FIXED, REQUESTS) |
| Research engine (`web_search` / `web_fetch`): model-planned queries, keyless sources, page cleaning, ranking, browser-search helper protocol | `docs/research/` (README, CHANGELOG, REQUESTS, BROWSER-SEARCH) |
| Connectors (OAuth for Gmail, GitHub, Slack, Vercel, Render, Cloudflare, Supabase, Canva; `<service>_<verb>` tools) and the MCP client | `docs/connectors/` (README, one page per service, MCP, CHANGELOG, REQUESTS) |
| Tuning channel: consented local LoRA/QLoRA fine-tuning from thumbs/teach feedback, feasibility gate, separate training venv, GGUF export (`services/tuning/`, `/api/tuning/*`) | `docs/tuning/` (README, CHANGELOG, REQUESTS) |
| Voice: server STT/TTS, languages, measured round trips, install per OS (`services/voice/`, `/api/voice/*`) | `docs/voice/` (README, CHANGELOG, BUGS-FIXED, REQUESTS) |
| Resources: CPU/RAM/VRAM/disk budget with headroom per device class, dynamic thread target, process priority, GPU detection (`services/resources/`, `/api/resources`) | `docs/resources/` (README, CHANGELOG, BUGS-FIXED) |
| Frontend architecture, bugs fixed, voice, API requests | `docs/frontend/*.md` |
| Cross-module requests between owners | `docs/setup/CODE-REQUESTS.md`, `docs/frontend/API-REQUESTS.md` |
| Roadmap (GPU, voice server, streaming beyond prefetch…) | `docs/ROADMAP.md` |
| Brand: the Aperture logo, its math, motion, landing intro, icons | `brand/README.md` |
| Module-level docs | each `services/<module>/doc/README.md` |

Historical design notes for engine modules that were **never compiled**
(`model/fish/doc/*.md` other than `README.md`) describe a planned design, not
the code. `docs/engine/` is the truth.

## Build and test

Engine (needs CMake ≥ 3.20, Ninja, and GCC/Clang — MSVC is not supported, the
kernels use GCC target attributes):

```
cd model/fish
cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release
ninja -C build
./build/fish_test_m1 ../../models/<model>.gguf      # GGUF + kernels (int8 vs f32 oracle)
./build/fish_test_m2 ../../models/<model>.gguf      # forward pass, tokenizer round trips
python test/test_fish.py ../../models/<model>.gguf  # through the C ABI (chat, prefix cache)
```
Then copy `build/fish.dll` (or `libfish.so`) to `bin/`. On Windows the running
server locks `bin/fish.dll`; stop it first. Windows toolchain used here:
`winget install BrechtSanders.WinLibs.POSIX.UCRT Kitware.CMake Ninja-build.Ninja`
(binaries land under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\...\mingw64\bin`).

Python: `python -m pip install -r requirements-dev.txt`, then `python -m pytest`
(pytest tests live in `tests/`; the `tests/test_engine_*.py` files are also
runnable scripts that need a model). The launcher's native-window tests are opt-in
(`IMURUN_WINDOW_TESTS=1`) because they open a window on the developer's
screen; never run `start.py` with a window from an agent or a script — use
`--no-open --port <free port>` and stop the process afterwards. Compile-check: `python -m py_compile <files>`.

Frontend: no build. Syntax can be checked with a JS parser; see `docs/frontend/README.md`.

## Ownership and scopes (for parallel work)

Work in one scope at a time and stay inside it; if you need a change in another
scope, write it down in the matching `*-REQUESTS.md` file instead of editing.

- `model/fish/**` — engine. Owner rule: every kernel change must keep
  `fish_test_m1` passing (AVX2 == scalar reference), and every tokenizer/model
  change must keep `fish_test_m2` passing.
- `model/*.py`, `services/**` — bindings and server. Never write tokens or
  request headers to disk or logs. Anything touching the engine context
  (generate / select / close / unload) goes through `FishNativeClient._gen_lock`.
- `frontend/design-lab/**` — UI. CSP is `default-src 'self'`: no CDNs, no inline
  scripts, no remote images; fonts are local.
- `start.py`, `install.py`, `requirements*.txt`, `pyproject.toml`, `run.*` — setup.
- `docs/**` — anyone, but update the doc that owns the topic rather than adding a
  parallel one.

## Conventions

- C++: C++17, no exceptions across the C ABI, every GGUF read bounds-checked,
  comments explain *why*. Structs in `c_api.h` only grow at the end; bump
  `FISH_ABI_VERSION` when they do and mirror the change in `model/fish_native.py`.
- Python: dataclasses, `from __future__ import annotations`, no new heavy
  dependencies without adding them to `requirements.txt`. Tool failures must
  become results, never exceptions that kill a turn.
- Model files (`*.gguf`) live in `models/` and are never committed.
- Debug output goes to the logger, never to files in the working directory.

## Where things are not yet real (do not claim them)

- GPU compute: a Vulkan backend runs the layer projections and the output
  projection on the GPU for as many layers as fit the VRAM budget
  (`docs/engine/GPU.md`, tested on NVIDIA only). Attention, norms, RoPE, the KV
  cache and MoE experts are still on the CPU, so decode is bounded by per-layer
  round trips; whole-layer GPU execution is the next step (`docs/ROADMAP.md`).
- "Layer streaming" today = mmap + prefetch of the next layer + eviction of
  layers behind a 3-layer window when the model exceeds the RAM budget. There
  is no expert-level cache yet.
- Architectures: llama, qwen2, qwen3, phi3, gemma2 (+ their MoE variants).
  Others fail at load with a clear message.
- Voice: the server layer (`services/voice/`, `/api/voice/*`: faster-whisper STT, Piper TTS,
  system-voice fallback) exists and was measured with synthetic speech only (Piper -> Whisper
  round trips; `docs/voice/README.md`). Real microphone audio, macOS `say`, Linux espeak-ng and
  the online edge-tts fallback are untested; Bengali STT is unreliable (CER ~0.3, 1 in 6 clips
  fails); no streaming dictation. The shell still uses the browser Web Speech API until the
  frontend adopts `/api/voice/*` (`docs/frontend/API-REQUESTS.md` #33). The reply language
  reaches the model through `POST /api/turns {language}` (done 2026-11-02).
