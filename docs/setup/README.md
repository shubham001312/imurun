# IMURUN setup and run guide

IMURUN is a Python app (loopback HTTP server + web UI) on top of the FISH engine, a
C++ shared library (`fish.dll` / `libfish.so` / `libfish.dylib`).

- Python 3.11 to 3.14 (3.12 recommended). Newer versions such as 3.15 may have no
  pre-built wheels for `aiohttp` yet.
- Runtime packages: `requirements.txt` (aiohttp, httpx, huggingface_hub, pyyaml).
- Optional: `pywebview` (native window), `keyring`, `psutil`, `fastapi` + `pydantic`.
  These are commented lines in `requirements.txt` and extras in `pyproject.toml`:
  `pip install .[window]`, `.[connectors]`, `.[metrics]`, `.[api]`, `.[all]`.
- Dev/test: `requirements-dev.txt` (includes `build` and `twine` for packaging).

## Install as a package (pip or npm)

These are the commands on https://imutechz.in/imu (checked 2026-11-03). They are the
only ones a user needs; each ends with the single `imurun` command on PATH.

| OS | pip | npm | Source checkout |
|---|---|---|---|
| Windows | `pip install imurun`, then `imurun` | `npx imurun` or `npm i -g imurun && imurun` | `run.bat` |
| macOS | `pip3 install imurun`, then `imurun` | same | `./run.command` |
| Linux | `pipx install imurun` (or `pip install imurun` inside a venv), then `imurun` | same | `./run.sh` |

```
pip install imurun                 # the `imurun` command
pip install "imurun[window]"       # + native window (pywebview); the first run adds it on Win/macOS anyway
pipx install imurun                # same, in its own venv (recommended on Linux / PEP 668 distros)
npx imurun                         # npm launcher (or: npm i -g imurun && imurun)
pip install -U imurun              # upgrade (also from the old 0.1.x CLI, see below)
```

> **Status 2026-11-09:** this app is published on PyPI and npm (0.3.0, 0.3.2, 0.3.3).
> 0.3.0 and 0.3.2 have no updater: upgrade them once with `pip install -U imurun` (or
> `pipx upgrade imurun`, `npm i -g imurun@latest`). From 0.3.3 on the app checks for new
> releases itself ("Updates" below; `imurun update`). The old 0.1.x CLI's own
> `imurun update` also reaches 0.3.x through npm.

| Command | What it does |
|---|---|
| `imurun` | start. The first run installs whatever is missing (next section), then opens the window or the browser |
| `imurun --no-open --port 8800` | server only, on another port (every `start.py` flag works) |
| `imurun --version` | prints `imurun <version>` (e.g. `imurun 0.3.3`) |
| `imurun doctor [--port N]` | Python, core packages, frontend, engine (loads it and prints the ABI), Vulkan loader + GPU driver, home dir, models dir, install state, port free |
| `imurun install [--force] [--no-voice] [--tuning] [--no-build]` | runs the installer into this interpreter (= `python install.py --here ...`) |
| `imurun --get-engine` | downloads the prebuilt engine for this OS from the matching GitHub release |
| `imurun update [--check] [--yes]` | checks PyPI/GitHub and installs a newer version the way this copy was installed (see "Updates") |
| `python -m imurun ...` | the same, when the `Scripts`/`bin` folder is not on PATH |

`imurun` takes the same flags as `start.py` (table below) plus `--version` and
`--get-engine`. It works from any directory.

**What the wheel contains.** `imurun/` is the launcher package (`cli.py`, version).
At build time `setup.py` copies the app *into* it: `start.py`, `install.py`,
`window.py`, `services/`, `model/*.py`, `frontend/design-lab/`, `docs/connectors/`,
`requirements*.txt` and, for platform wheels, the engine as `imurun/bin/fish.dll`
(`libfish.so`, `libfish.dylib`). The layout is the same as a checkout, so every
`__file__`-relative path in `services/` resolves unchanged, and site-packages gets a
single `imurun` folder. The command puts that folder on `sys.path` and `PYTHONPATH`
(so `python -m services...` child processes work too) and calls `start.main`.

| Wheel | Engine |
|---|---|
| `py3-none-win_amd64` | `fish.dll`, MinGW-w64 GCC, static (needs only the Windows UCRT) |
| `py3-none-manylinux_2_28_x86_64` | `libfish.so`, GCC, glibc 2.28+ |
| `py3-none-macosx_11_0_arm64` | `libfish.dylib`, Apple clang, no Vulkan |
| `py3-none-any` (every other platform) | none: the app starts and explains how to get one |

**Nothing is written into site-packages.** An installed copy never creates `.venv`
and never writes logs or models next to its files. `--bootstrap` is ignored, and
model downloads are pinned to `~/.imurun/models`. Data, sessions, logs, runtime info
and a downloaded engine all go under `~/.imurun` (or `IMURUN_DATA_ROOT`). The CI
smoke test (`.github/scripts/smoke_install.py`) fails if a file appears in the
package directory.

**No engine for this platform.** The start-up log says so and lists the options:
- `imurun --get-engine` downloads `fish-windows-x64.dll` / `libfish-linux-x64.so` /
  `libfish-macos-arm64.dylib` from the GitHub release that matches the installed
  version, checks it against that release's `SHA256SUMS.txt`, and saves it to
  `~/.imurun/bin/`. The launcher then hands it to the bindings through `FISH_LIBRARY`.
- Or set `FISH_LIBRARY=<path>` to a library you built (see "FISH engine" below).

**npm launcher** (`npm/imurun.js`, no dependencies, no telemetry):
1. finds Python 3.11+ (`py -3.12`/`-3.13`/`-3.11`/`-3.14`/`-3`, `python`, `python3` on
   Windows; `python3.12` … `python3`, `python` elsewhere). The Microsoft Store stub is
   never picked, because it does not run. If none is found, it prints the install
   command (`winget install -e --id Python.Python.3.12`, `brew install python@3.12`,
   `apt install python3 python3-venv`) and exits;
2. creates `~/.imurun/venv`, or recreates it if it is broken;
3. installs `imurun==<npm package version>` there (with `[window]` on Windows/macOS,
   retrying without it if that fails), only when nothing is installed or the installed
   version is **older** (from 0.3.3; 0.3.0-0.3.2 reinstalled on any difference, so they
   undid an update made inside the venv). A newer one put there by `imurun update` / the
   in-app update is kept. With `IMURUN_WHEEL` the wheel's version must match exactly.
   Later starts take about a second;
4. runs `python -P -m imurun <args>` with `IMURUN_LAUNCHER=npm`, `IMURUN_VENV=<venv>` and
   `IMURUN_SUPERVISED=1`, and passes on its exit code; exit code 75 (an update was
   applied) runs it again (see "Updates").

Environment variables: `IMURUN_PYTHON` (interpreter to use), `IMURUN_VENV` (venv
location), `IMURUN_WHEEL` (install a local wheel instead of PyPI, used for testing),
`IMURUN_REINSTALL=1`, `IMURUN_NO_WINDOW_EXTRA=1`. `imurun --launcher-info` (npm only)
shows the venv and the Python it would use.

Building the packages yourself and cutting a release: `docs/setup/RELEASE.md`.

## Updates

From **0.3.3** IMURUN checks for a new version and offers it; nothing is installed
until you choose to. 0.3.0 and 0.3.2 have no updater, so getting to 0.3.3 is a
one-time manual step (below).

- **When it checks:** once per launch, in a background thread after the server is up
  (never on the start-up path), against PyPI and the GitHub "latest release", 3 s
  timeout each. The result is cached 6 h in `~/.imurun/update-state.json`. Offline,
  the check just fails quietly. `IMURUN_UPDATE_CHECK=0` turns the automatic check off;
  it is also off under `CI` and pytest. Pre-releases are offered only if you run one.
- **The pop-up:** **Update now** or **Not now**. "Not now" comes back after 3 more
  launches, or at once when an even newer version is published. Closing it (Esc)
  hides it for this launch only. Settings > Profile > "About and updates" and the
  `/update` command check on demand.
- **From a terminal:** `imurun update` (check, show the command, ask y/N),
  `imurun update --check` (only check), `imurun update --yes` (no question).
  `imurun --version` adds "update available: X" from the cache (no network).
- **What runs** depends on how IMURUN was installed:

  | Installed with | Update runs |
  |---|---|
  | `pip install imurun` | `python -m pip install --upgrade imurun==<new>` (same interpreter) |
  | `pipx install imurun` | `pipx upgrade imurun` |
  | `uv tool install imurun` | `uv tool upgrade imurun` |
  | `npx imurun` / `npm i -g imurun` | pip into `~/.imurun/venv` (`imurun==<new>`); the npm launcher package itself is not changed |
  | source checkout | `git pull --ff-only`, then `python install.py --auto`; refused while `git status` shows local changes |

- **Restart:** after a successful update the server stops (closing the model) and the
  new version starts on the same port; the open page reloads itself, a native window
  is closed and reopened. The `imurun` command written by setup and the npm launcher
  restart the app themselves (exit code 75); `python start.py`, `run.bat` and pip's own
  `imurun` start a fresh process. On Windows the loaded `fish.dll` is first renamed to
  `fish.dll.old-<pid>` (a loaded DLL cannot be overwritten, but it can be renamed) and
  the next start deletes it. If the update fails, the old version keeps running and the
  pop-up shows pip's or git's last lines.

**Getting to 0.3.3 the first time** (one manual step; after that the app offers updates):

| You have | Run |
|---|---|
| 0.3.0 / 0.3.2 via pip | `python -m pip install --upgrade imurun` (the same Python you installed it with; `pip install -U imurun` if `pip` is that one) |
| 0.3.0 / 0.3.2 via pipx | `pipx upgrade imurun` |
| 0.3.0 / 0.3.2 via npm | `npm i -g imurun@latest` (global), or `npx imurun@latest` once. The new launcher installs 0.3.3 into `~/.imurun/venv`. Updating only the venv does not stick with a 0.3.0-0.3.2 launcher: it reinstalls its own version on the next start |
| a source checkout | `git pull`, then `run.bat` / `./run.sh` (or `python install.py`) |
| the old 0.1.x CLI (Bun/TypeScript) | npm-installed: its own `imurun update` runs `npm install -g imurun@latest`, which brings the 0.3.x launcher (needs Python 3.11+). pip-installed: its `imurun update` runs `pip install --upgrade imurun`, which only reaches 0.3.x on Python 3.11+ (otherwise pip silently stays on 0.1.11, see Troubleshooting). From `npx`: it tells you to run `npx imurun@latest`. A 0.1.x source checkout: its `git pull --ff-only` brings this app, then use `run.bat` / `./run.sh`. The standalone `imurun-<os>-<arch>` binary cannot update itself to 0.3.x (0.3.x releases have no such asset, the download 404s): install with pip or npm instead |

## What gets installed, and when (automatic setup)

`install.py` installs everything IMURUN needs into **the interpreter that runs it**:
the project `.venv` in a checkout, or the venv / pipx venv / npm venv of an installed
copy. It is idempotent: when you run it again, finished steps are skipped.

| Step | What | If it fails |
|---|---|---|
| 1. Core packages | `requirements.txt` (aiohttp, httpx, huggingface_hub, pyyaml) | **fatal**: setup stops and names step 1 |
| 2. Native window | `pywebview` on Windows/macOS (Linux: opt-in with `--window`, needs GTK/Qt) | browser UI instead |
| 3. Voice | `requirements-voice.txt` (faster-whisper, av, piper-tts, numpy; ~250 MB) | `/api/voice/*` answers 503 with the reason |
| 4. FISH engine | found (bundled `imurun/bin/`, `bin/`, `model/fish/build/`, `~/.imurun/bin/`); else downloaded from release `v<version>` (SHA256-checked); else, in a checkout with cmake + ninja + g++/clang++, **built** from `model/fish` into `bin/`; else build instructions. The library is then loaded with ctypes and its ABI is read | the app starts in "provisioning" state and says what to do |
| 5. GPU runtime check | Vulkan loader (`System32\vulkan-1.dll` / `libvulkan.so.1` / `libvulkan.1.dylib` or MoltenVK) and a vendor driver (Windows: Khronos registry ICDs + NVIDIA/AMD/Intel driver DLLs; Linux: `/usr/share/vulkan/icd.d/*.json`, `/proc/driver/nvidia`; macOS: MoltenVK in the Homebrew/SDK paths). Report only: it never loads a driver | CPU only; it prints what to install |
| 6. Tuning (opt-in, `--tuning`) | `python -m services.tuning.setup`: torch + transformers in a **separate** venv `~/.imurun/tuning/venv` (2-5 GB) | chat is unaffected |

Tuning is never automatic, because it is large and the tuning channel requires the
user's consent. On Windows only, `python install.py --build-tools` runs
`winget install -e --id BrechtSanders.WinLibs.POSIX.UCRT` / `Kitware.CMake` /
`Ninja-build.Ninja` and then builds the engine. The WinGet `Links` folder and the
WinLibs `mingw64\bin` folder are added to PATH for the build, so no new terminal is
needed.

**State file.** The result goes to `~/.imurun/install-state.json` (or
`$IMURUN_DATA_ROOT/install-state.json`), with one entry per interpreter path: IMURUN
version, Python version, core package versions, the window, voice and tuning results,
the engine (path, size, mtime, ABI, fish version, source: `found`, `downloaded`,
`built` or `missing`) and the GPU report.

**Every start** (`imurun`, `start.py`, `run.*`) first runs `install.quick_check()`. It
checks that the core modules are importable (`find_spec`), that
`frontend/design-lab/index.html` exists, that this interpreter has a state entry for
the current version, and that the engine file is unchanged (path + size + mtime). It
took 20 ms in a fresh venv on Windows. It never loads the engine, never imports
torch/whisper, and changes no thread or GPU setting. If any check fails (first run,
upgrade, deleted package, new engine build), start runs `install.py --here --auto`
with the same interpreter, shows the progress on the console, and then continues.
Only a step-1 failure stops it. A missing engine that was already reported does not
re-run setup on every start.

**Automatic runs are leaner than manual ones.** With `--auto`, the engine is built
only after a yes in a terminal (or with `IMURUN_AUTO_BUILD=1`). With `CI` set or
`IMURUN_SKIP_OPTIONAL=1`, voice and the engine build are skipped. Other switches:
`IMURUN_NO_VOICE=1`, and `IMURUN_SKIP_SETUP=1` / `--skip-setup` (no automatic step
at all).

**Force a reinstall:** `imurun install --force` (installed copy) or
`python install.py --force` (checkout). This redoes every step and rewrites the state
entry. `imurun install --tuning` adds the tuning venv later.

Tested on Windows 11 x64 (Python 3.12, NVIDIA RTX 3050): a fresh venv from the wheel
(with and without a bundled engine), pipx, the npm launcher, and an unpacked sdist
used as a checkout. The macOS and Linux branches (Vulkan paths, `python3.x` lookup,
`libfish.so`/`.dylib`) were written from the docs and the code. Only the CI
`install-smoke` jobs exercise them; nobody has run them by hand.

### Installed layout

```
<venv>/Scripts/imurun(.exe)  (bin/imurun)   console script -> imurun.cli:main
<venv>/Lib/site-packages/imurun/            never written after install
    __init__.py cli.py __main__.py          launcher
    start.py install.py window.py requirements*.txt
    services/**  model/*.py  frontend/design-lab/**  docs/connectors/**
    bin/fish.dll | libfish.so | libfish.dylib    (platform wheels only)
~/.imurun/                                  (or IMURUN_DATA_ROOT) - created by install.py,
    install-state.json                      re-checked on every start (ensure_home_layout)
    settings/   secrets/*  sessions/*  runtime/*   (* = 0700 on POSIX)
    models/  models/.cache/                 model downloads for an installed copy
    bin/                                    engine from --get-engine
    cache/fish/                             engine importance + re-quantised bytes
    logs/install.log                        pip and build output
    skills/  tools/  workspace/  feedback/  agent skills, tool state, files, tuning signals
    voice/                                  speech models and voices
    tuning/{base,built,datasets,runs}/      tuning venv (tuning/venv), inputs, outputs
    tmp/
    venv/                                   only for the npm launcher
```

## Quick start

| OS      | First run and every later run           |
|---------|-----------------------------------------|
| Windows | double-click `run.bat`, or `run.bat` in a terminal |
| macOS   | double-click `run.command`, or `./run.command` |
| Linux   | `./run.sh`                              |

On the first run the launcher:
1. looks for Python 3.11 to 3.14. If there is none, it installs Python 3.12
   (Windows: `winget`; macOS: Homebrew; Linux: apt/dnf/pacman after asking you);
2. runs `install.py`, which creates `.venv` and installs everything listed in
   "What gets installed" above (packages, pywebview on Windows/macOS, voice, the
   engine: found, downloaded or built, and the GPU check);
3. starts `start.py` from `.venv`.

Later runs go straight to step 3. Launcher arguments are passed to `start.py`, for
example `run.bat --browser --port 8800`.

### Manual steps (any OS)

```
python install.py            # create/repair .venv + install requirements (+ pywebview on Win/macOS)
python install.py --check    # report only: venv, packages, engine, build tools
python start.py              # start (re-runs itself inside .venv if needed)
```

Other ways to install:

```
python start.py --bootstrap                     # create .venv, install, start
python start.py --install                       # pip install into the current Python, then start
python -m pip install -r requirements.txt       # plain pip
python -m pip install -r requirements-dev.txt   # + test tools
```

`install.py` options: `--here` (use the current interpreter, no venv), `--python PATH`
(base interpreter for the venv), `--window` / `--no-window`, `--no-voice`, `--tuning`,
`--no-build`, `--build-tools`, `--force`, `--dev`, `--check`, `--doctor`, `--verbose`
(raw pip/build output instead of the animated progress). The pip and build log is
`~/.imurun/logs/install.log`.

## start.py options

| Flag | Meaning |
|------|---------|
| `--port N` | port (default 8732) |
| `--model PATH` | GGUF model path (sets `FISH_MODEL`) |
| `--host H` | host setting (the server currently always binds 127.0.0.1) |
| `--window` | native window through pywebview (the default when pywebview is installed) |
| `--browser` | open the default browser |
| `--no-open` | server only; open `http://127.0.0.1:<port>/` yourself |
| `--bootstrap` / `--install` | install the dependencies first (see above; `--bootstrap` is source-checkout only) |
| `--skip-setup` | skip the automatic install step (also `IMURUN_SKIP_SETUP=1`) |
| `--version` | print `imurun <version>` and exit |
| `--get-engine` | download the prebuilt engine for this OS into `~/.imurun/bin` and exit |

The UI is served at `http://127.0.0.1:<port>/`. The bearer token is injected
server-side, so the root URL is all you need. Closing the window stops the server.
In browser or `--no-open` mode, press Ctrl+C. `IMURUN_NO_BROWSER=1` acts like `--no-open`.

Health check: `GET http://127.0.0.1:<port>/health` returns `{"ok": true}`.

## FISH engine

The server and UI start without the engine. Chat stays unavailable until the library
exists. The library is found through `FISH_LIBRARY`, then `bin/` (`imurun/bin/` in an
installed wheel), then `model/fish/build/`, then `~/.imurun/bin/` (where `--get-engine`
puts it).
Build it like this:

```
cd model/fish
cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release
ninja -C build
# copy build/fish.dll (or libfish.so / libfish.dylib) to ../../bin/
```

`python install.py --check` lists any missing build tools and how to install them:
- Windows: `winget install -e --id Kitware.CMake`, `Ninja-build.Ninja` and MinGW-w64 GCC
  (`BrechtSanders.WinLibs.POSIX.UCRT`, or MSYS2 UCRT64 as in CI). **MSVC is not
  supported** because the kernels use GCC target attributes. MinGW links the DLL
  statically, so `fish.dll` needs nothing but the Windows UCRT.
- macOS: `xcode-select --install`, `brew install cmake ninja`.
- Linux: `sudo apt install build-essential cmake ninja-build`.

## Use hosted models (NVIDIA, OpenRouter, DeepSeek, …)

Besides the models on this computer, IMURUN can answer with a model that runs
on a provider's servers: NVIDIA (build.nvidia.com, free developer keys),
OpenRouter, DeepSeek, OpenAI, Groq, Together, Mistral, or any server with an
OpenAI-compatible API (vLLM, LM Studio, Ollama, LiteLLM, a company gateway).
Replies then need an internet connection, and the provider sees what you send.

1. Get a key from the provider's site (the connect dialog links to the page).
2. Type `/connect` in the message box (or `/connect nvidia`, `/connect openrouter`,
   `/connect deepseek` … to go straight to one), or press **Connect a provider**
   at the top of **Settings > Models**, or pick **Add a provider** in the model
   picker.
3. Paste the key and press **Verify and save**. IMURUN checks it with one
   request to that provider and stores it in the system keychain (else an
   encrypted file under the data folder). It is only ever sent to that
   provider's own address and is never shown again; the app shows `****abcd`.
4. **Choose models** opens next: tick the models you want, press **Save and pick
   a model**, and pick one to switch to it.
5. Later, open the model picker with **Ctrl M** (macOS ⌘ M, when the system does
   not keep it for "minimise"), `/model`, or the model button under the message
   box. It lists local and hosted models together; `/model <name>` switches
   straight to a model when the name matches only one.

For **Custom (OpenAI-compatible)** enter the base URL, the part before
`/chat/completions` (for example `https://llm.example.com/v1` or
`http://localhost:11434/v1`). It must be `https`; plain `http` is only accepted
for this computer (`localhost`, `127.0.0.1`). Tick **This server needs no key**
for local servers without one. If the server has no model list, type the model
ids in **Choose models**.

Keys can also come from the environment: `NVIDIA_API_KEY`, or
`IMURUN_<PROVIDER>_API_KEY` (`IMURUN_OPENROUTER_API_KEY`, `IMURUN_DEEPSEEK_API_KEY`,
`IMURUN_OPENAI_API_KEY`, `IMURUN_GROQ_API_KEY`, `IMURUN_TOGETHER_API_KEY`,
`IMURUN_MISTRAL_API_KEY`). The selected model is restored on the next start;
picking a local model switches back to FISH. Routes: `services/server/doc/README.md`.

## Use IMURUN from other apps (OpenAI-compatible API, OpenClaw)

IMURUN serves an OpenAI-compatible API on the same loopback server, so any app
that takes "a base URL plus an API key" can use the local model. Open
**Settings > Use by API**: it shows the three values, each with a Copy button,
plus ready-to-paste curl, Python and OpenClaw snippets.

| Value | Example |
|---|---|
| Base URL | `http://127.0.0.1:8732/v1` (the port is `--port`, default 8732) |
| API key | `sk-imurun-...` (masked until "Show"; Copy copies the real key) |
| Model id | the chosen model's file name without `.gguf`, e.g. `Llama-3.2-1B-Instruct-Q4_K_M` (`GET /v1/models`) |

- The key is **persistent**: it lives in `<data root>/secrets/openai_api_key`
  (`~/.imurun` unless `IMURUN_DATA_ROOT` is set; file `0600`, folder `0700` on
  macOS/Linux) and survives restarts. **Regenerate key** replaces it at once; apps
  using the old key get `401` until you paste the new one. The per-run shell token
  (Settings > API) is a different thing and changes every start.
- Endpoints: `GET /v1/models`, `POST /v1/chat/completions` (`messages`, `stream`,
  `max_tokens`, `temperature`, `top_p`, `stop`). Text only; there is no tool
  calling, embeddings or `/v1/responses`. The `model` field in a request is
  accepted as given; the loaded model answers.
- No model chosen yet: completions answer `503 model_not_available`. Choose one in
  Models first. The first call after a start or an idle unload also loads the weights.
- Loopback only: apps on this computer can connect, other machines cannot.
  Browser pages on other origins cannot call it either (no CORS).

```
curl http://127.0.0.1:8732/v1/chat/completions \
  -H "Authorization: Bearer $IMURUN_KEY" -H "Content-Type: application/json" \
  -d '{"model": "Llama-3.2-1B-Instruct-Q4_K_M", "messages": [{"role": "user", "content": "Hello"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8732/v1", api_key="sk-imurun-...")
print(client.chat.completions.create(model="Llama-3.2-1B-Instruct-Q4_K_M",
      messages=[{"role": "user", "content": "Hello"}]).choices[0].message.content)
```

**OpenClaw** reads `~/.openclaw/openclaw.json` (JSON5). Add IMURUN as a custom
OpenAI-compatible provider (`api: "openai-completions"`, which is OpenClaw's mode
for `/v1/chat/completions` servers) and make it the default model:

```json5
{
  models: {
    mode: "merge",
    providers: {
      imurun: {
        baseUrl: "http://127.0.0.1:8732/v1",   // keep the /v1
        apiKey: "sk-imurun-...",
        api: "openai-completions",
        models: [{ id: "Llama-3.2-1B-Instruct-Q4_K_M", name: "IMURUN", reasoning: false,
                   input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
                   contextWindow: 8192, maxTokens: 2048 }],
      },
    },
  },
  agents: { defaults: { model: { primary: "imurun/Llama-3.2-1B-Instruct-Q4_K_M" } } },
}
```

The settings panel fills in your real port, key, model id and context size.
Common failures: a base URL without `/v1`, an old key after **Regenerate key**,
IMURUN not running, or the model id changed after switching models in Models
(update `id` and `primary`). The shape follows OpenClaw's custom-provider docs
(docs.openclaw.ai, read 2026-11-09); IMURUN has not been run against a real
OpenClaw install. OpenClaw's agent features that need tool calls will not work
with this API, plain chat does.

## Troubleshooting

### `pip install imurun` gives a different program (0.1.x: `imurun models`, `imurun run`)

That is the old 0.1.x CLI from PyPI/npm (see the status note above). Check with
`pip show imurun`: it shows `Version: 0.1.11` and the summary "local-first AI coding
agent". Once 0.3.0 is published, `pip install -U imurun` (or `npm i -g imurun@latest`)
replaces it. The npm launcher always installs exactly its own version, so a stale 0.1.x
in `~/.imurun/venv` is replaced automatically.

### `pip install imurun` still gives 0.1.11 after 0.3.0 is published

0.3.0 needs Python 3.11+, and 0.1.x accepted 3.9+. On Python 3.9 or 3.10, pip skips
0.3.0 without saying so and installs the newest 0.1.x. Check `python --version` and use
3.11 to 3.14. The project owner can make this fail loudly by yanking 0.1.0 to 0.1.11 on
PyPI (project → Manage → Releases → Options → Yank). After that, a plain
`pip install imurun` on an old Python fails with "requires a different Python" instead.

### `'imurun' is not recognized` / `command not found: imurun`

The first `imurun` run sets the command up by itself. If `imurun` on PATH does not
already start this install, setup writes a small launcher and puts its folder on your
PATH for new terminals:

| OS | Launcher | PATH change |
|---|---|---|
| Linux, macOS | `~/.local/bin/imurun` | a marked block in `~/.profile`, plus `~/.bashrc` / `~/.zshrc` when present or when that is your shell; fish: `~/.config/fish/conf.d/imurun.fish` |
| Windows | `%USERPROFILE%\.imurun\cmd\imurun.cmd` | appended to the user `Path` (`HKCU\Environment`) |

So it works after `pip install` into a venv, `pip install --user`, `npx imurun` and a
source checkout (`run.sh` / `run.bat`). **Open a new terminal** afterwards; the current one
keeps its old PATH. `imurun doctor` shows which `imurun` a terminal finds. Setup never
overwrites a file it did not write (the launcher is marked `imurun-shim`). It never edits
the PATH when `IMURUN_NO_PATH=1` is set or in CI. `IMURUN_BIN_DIR=<dir>` puts the launcher
somewhere else. `imurun install --force` points the launcher at the install you run it from.
To undo it, delete the launcher and the `# >>> imurun >>>` block. Until then, `python -m imurun`
always works.

### `certificate verify failed` / `unable to get local issuer certificate` (unknown CA)

IMURUN verifies HTTPS against the **operating system's trust store** (Windows certificate
store, macOS Keychain, the Linux CA bundle) through `truststore`, the same library pip uses.
So a corporate proxy's CA that IT installed on the machine is trusted, and the python.org
macOS build works without running "Install Certificates.command". Verification is never turned
off: self-signed or expired certificates are still rejected. `imurun doctor` shows
`HTTPS certificates: system trust store`. If it says `python bundle`, run `imurun install --force`
(it installs `truststore`). `IMURUN_NO_TRUSTSTORE=1` goes back to Python's own bundle.

### Windows "unknown publisher" / macOS "unidentified developer" for the engine

Release builds are signed when the project's signing identities are configured
(`docs/setup/RELEASE.md`, "Code signing"). Every release file also carries a build-provenance
attestation: `gh attestation verify fish-windows-x64.dll -R shubham001312/imurun` proves that
the file came from this repository's release workflow. On macOS, a library you downloaded with a
browser is quarantined. Use `imurun --get-engine` instead (checksum-verified, not quarantined),
or remove the flag: `xattr -d com.apple.quarantine <file>`. `imurun doctor` names the file when
that is the problem.

### `externally-managed-environment` (Debian/Ubuntu/Fedora, Homebrew Python)

The system Python refuses `pip install`. Use `pipx install imurun`, or
`python3 -m venv ~/.venvs/imurun && ~/.venvs/imurun/bin/pip install imurun`, or
`npx imurun` (it creates its own venv).

### Works on one machine, not on another

Run `imurun doctor` on the failing machine. Common causes:
- No engine for that OS/CPU. Only the Windows x64, Linux x64 (glibc 2.28+) and macOS
  arm64 wheels carry one. Other platforms get the pure wheel and need
  `imurun --get-engine` or a source build.
- Python 3.15+ (no aiohttp wheels).
- A `.venv` copied from another PC (see below).
- A `git clone`: it has no `bin/fish.dll`. `python install.py` downloads or builds it.
- Port 8732 already in use.

### `ModuleNotFoundError: No module named 'aiohttp'`

```
File "...\model\fish_client.py", line 12, in <module>
    import aiohttp
ModuleNotFoundError: No module named 'aiohttp'
```
(This often comes after a chained `No module named 'services.model'`. That part is
harmless: it is the relative-import fallback in `services/server/app.py`.)

**Cause:** the Python running `start.py` does not have the runtime packages.
Usually either the packages were never installed (the old repo had no
`requirements.txt`), or `.venv` was copied from another machine and points at a
Python that does not exist here.

**Fix:** run `python install.py`, or `run.bat` / `./run.sh`, or
`python -m pip install -r requirements.txt`. `start.py` now detects this itself.
It switches to a healthy `.venv` automatically. Otherwise it runs `install.py`: into
the active venv, or, with a system Python in a checkout, into a new `.venv`. It prints
these instructions only if that fails or `IMURUN_SKIP_SETUP=1` is set.

### Stale `.venv` copied from another computer

`.venv/pyvenv.cfg` contains `home = C:\Program Files\Python314` (or a similar path)
that does not exist on this PC. `install.py` detects this, deletes the broken `.venv`
(only after confirming it is a venv and is broken) and recreates it. `start.py`
ignores a broken `.venv`. Never copy `.venv` between machines.

### "Python was not found; run without arguments to install from the Microsoft Store"

That message comes from the Windows Store alias, not from a real Python. Install
Python with `winget install -e --id Python.Python.3.12`, open a **new** terminal, and
use `py -3.12` or the full path
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. You can turn the alias off in
Settings > Apps > Advanced app settings > App execution aliases. `run.bat` never
uses the Store alias.

### Python 3.15 or newer: pip tries to compile aiohttp

There are no wheels yet. Install 3.12 next to it. `install.py` prefers 3.11 to 3.14
automatically when it builds `.venv`, or you can pass `--python <path-to-3.12>`.

### Native window does not open

- Windows: pywebview needs the Edge WebView2 runtime (preinstalled on Windows 11;
  otherwise `winget install Microsoft.EdgeWebView2Runtime`) and `pythonnet` (installed
  with pywebview).
- macOS: works with the system WebKit, no extra steps.
- Linux: pywebview needs GTK or Qt bindings. Example: `sudo apt install python3-gi
  gir1.2-webkit2-4.1`, or `pip install pywebview[qt]`. Run `python install.py --window`.
- If the window fails, `start.py` falls back to the browser. `--browser` forces it.

How the window is isolated (first-run safety): the UI shell runs in its own
process, `window.py`, started by `start.py`. If that process exits with an
error or dies within the first 6 seconds, the launcher prints "The native
window could not be shown (...)", writes the window's stderr to
`~/.imurun/logs/window.log`, opens the browser, and keeps serving — nothing in
the UI stack (WebView2, GTK/Qt, .NET icon handling, drivers) can take the
server down. On Windows the window icon is applied with the Win32 API because
`System.Drawing.Icon` rejects `.ico` files with PNG-compressed entries (this
crashed the whole process in an earlier build). The window is also tied to
the launcher's lifetime in both directions: `window.py` receives the launcher's
PID and closes itself if that process disappears (console closed, task
killed), and `start.py` terminates the window on every exit path — so a window
is never left open pointing at a dead server. Tests: `tests/test_launcher.py`
covers the server-only start, the failing-window fallback and the happy path.

### Port already in use

`Could not start the server on port 8732`: IMURUN is probably already running.
Close it or use `--port 8800`.

### macOS: "run.command cannot be opened"

Run `chmod +x run.command run.sh` once, then right-click and choose Open. Gatekeeper
asks only the first time.

### pip fails behind a proxy or offline

Set `HTTPS_PROXY=http://proxy:port` (or `PIP_INDEX_URL` for a mirror) and run
`python install.py --verbose` to see pip's full output.
