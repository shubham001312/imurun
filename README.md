# IMURUN

Local-first AI chat with its own inference engine. One process, no cloud: a C++
engine (**FISH**) runs GGUF models on the CPU, a Python server hosts sessions,
tools and connectors, and a vanilla-JS "Liquid Paper" UI opens in a native
window or your browser. Windows, macOS and Linux.

## Install

Needs Python 3.11–3.14 (3.12 recommended).

```
pip install imurun            # then:  imurun
npx imurun                    # or:    npm i -g imurun && imurun
```

- **pip** installs the `imurun` command. Wheels for Windows x64, Linux x64
  (glibc 2.28+) and macOS arm64 include the FISH engine; on other platforms
  pip installs the app without it, and `imurun --get-engine` or a source build
  supplies it. `pip install "imurun[window]"` adds the native window.
- **npm** is a thin launcher with no dependencies: it finds Python, creates
  `~/.imurun/venv`, installs the matching `imurun` version from PyPI there,
  and runs it. If Python is missing it tells you how to install it.
- Everything IMURUN writes (sessions, models, logs, settings) goes to
  `~/.imurun` (override with `IMURUN_DATA_ROOT`).

Flags: `--window` (native window via pywebview, default when installed),
`--browser`, `--no-open`, `--port N`, `--model path.gguf`, `--version`,
`--get-engine`.

### From source

```
# Windows            macOS                 Linux
run.bat              ./run.command         ./run.sh
```
Each script creates `.venv`, installs `requirements.txt`, and starts the server
(`python start.py`, which takes the same flags as `imurun`).
Full setup and troubleshooting: `docs/setup/README.md`. Releasing:
`docs/setup/RELEASE.md`.

Models are downloaded from the **Models** tab (Llama 3.x, Mistral, Qwen 2.5/3,
Phi-3, Gemma 2, DeepSeek-R1 distills, Mixtral…) or placed in `models/`.

## Engine in one paragraph

FISH memory-maps a GGUF, keeps weights quantised, quantises activations to int8
once per matmul and runs AVX2 integer dot products (scalar fallback elsewhere).
Chat templates are applied structurally inside the engine (user text can never
inject control tokens), a KV prefix cache makes repeated system prompts free, and
when a model is larger than free RAM the next layer's pages are prefetched while
the current one computes. Supported architectures: llama (incl. Mistral,
DeepSeek distills, Mixtral MoE), qwen2/qwen3 (+MoE), phi3, gemma2. Details, limits
and numbers: `docs/engine/`.

What it is **not** (yet): a GPU engine, or a VRAM/NVMe tiered streamer — see
`docs/ROADMAP.md`.

## Build the engine

```
cd model/fish
cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release   # GCC/Clang; MSVC not supported
ninja -C build
./build/fish_test_m1 ../../models/<model>.gguf
./build/fish_test_m2 ../../models/<model>.gguf
```
Copy `build/fish.dll` (`libfish.so`, `libfish.dylib`) to `bin/`.

## Layout

```
imurun/                          the `imurun` command (pip); setup.py bundles the app into it
npm/imurun.js, package.json      the npm launcher
start.py, install.py, run.*      launchers and installer
model/fish/                      FISH engine (C++), tests in model/fish/test
model/*.py                       ctypes bindings and model client
services/                        server, turns, sessions, tools, connectors, safety
frontend/design-lab/             UI (no build step)
docs/                            documentation index: docs/README.md
tests/                           runnable engine/server scripts
archive/                         retired one-off scripts
```

Working on it? Read `CLAUDE.md` (applies to humans and CLI agents alike).

## Security

Loopback-only bind, per-run bearer token injected into the page (never in URLs
or logs), 1 MB body cap, Host-header check, strict CSP (`default-src 'self'`),
OAuth tokens in the OS keychain, approval cards for write actions.

MIT License — see `LICENSE`.
