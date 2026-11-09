# Setup / packaging changelog

## 0.3.4 (2026-11-09)

- **Web search with no API key, across the whole web.** A hidden, private embedded
  browser (on by default; `IMURUN_BROWSER_SEARCH=0` turns it off) reads DuckDuckGo result
  pages, with Bing only as a fallback; a page with a bot check is reported as `blocked`
  and skipped, never solved. Eight keyless APIs are chosen by the kind of question: Mwmbl
  and DuckDuckGo Instant Answers (general), Stack Exchange, GitHub and Hacker News (code),
  arXiv and OpenAlex (science), endoflife.date (releases). Every source pauses itself on
  429/403/quota. The web-search key panel is gone from Settings: there is nothing to set up
  (`docs/research/CHANGELOG.md`, `docs/research/README.md` "Privacy" lists who receives
  queries).
- **Apple Silicon / arm64:** NEON kernels (dot-product and plain) for every quant type the
  AVX2 path covers; on GitHub's Apple Silicon runner the kernels run 14-29x faster than the
  scalar code Macs used before (Q4_K 28.7x). Whole-model Mac tok/s not measured yet; the Mac
  build still has no GPU path (`docs/engine/CHANGELOG.md`).
- First release that 0.3.3 installs receive through the in-app updater.

## 0.3.3 (2026-11-09)

- **Engine:** Qwen3.5 (`qwen35`) gives correct answers (four bugs fixed) and runs its
  matmuls on the GPU: Qwen3.5-2B on an RTX 3050 4 GB, prefill 28 → 149 tok/s, decode
  ~12 → ~32-36 tok/s (`docs/engine/CHANGELOG.md`).
- **Hosted models** retry rate limits, 5xx/overloaded and network drops with backoff
  (2-30 s or the provider's Retry-After, up to 5 retries; `docs/server/CHANGELOG.md`).
- **Windows:** a Smart App Control / WDAC block on `fish.dll` (WinError 4551) is explained
  instead of shown raw.
- Docs: README and setup README no longer say the app is unreleased or "not a GPU engine".
- **In-app updates** (below). 0.3.0 / 0.3.2 users upgrade by hand once
  (`pip install -U imurun`, `pipx upgrade imurun`, `npm i -g imurun@latest`); from 0.3.3 on
  the app offers each new release itself.
- Not in this release: NEON kernels for Apple Silicon, the keyless browser web-search helper.

### In-app updates (0.3.3)

0.3.0 and 0.3.2 shipped without an update system: the one written on 2026-11-03 was
never merged. It is now ported onto the current code (`docs/setup/README.md` "Updates",
contract in `services/server/doc/README.md` "Update contract"):

- `services/update/`: per-launch background check of PyPI + GitHub latest release (3 s
  timeout each, 6 h cache in `~/.imurun/update-state.json`, newer one wins, numeric
  compare so 0.3.10 > 0.3.9), the "Not now = ask again after 3 launches" policy, and
  apply per install: pip, pipx, uv tool, the npm launcher's venv, source checkout. Off
  with `IMURUN_UPDATE_CHECK=0`, and always off under `CI` / pytest. `GET/POST
  /api/update` and an `update` SSE event; `imurun update [--check] [--yes]`;
  `imurun --version` adds "update available: X" from the cache.
- `start.py`: after an applied update the server stops and either exits 75 for a
  supervising launcher (`IMURUN_SUPERVISED=1`) or starts a fresh interpreter itself.
  Stale `fish.dll.old-<pid>` copies (the Windows locked-DLL swap) are removed on start.
- `install.py`: the existing `imurun` launcher (`~/.imurun/cmd/imurun.cmd`,
  `~/.local/bin/imurun`) now loops on exit code 75 instead of `exec`-ing / exiting.
- `npm/imurun.js`: keeps an installed version that is newer than the launcher
  (it used to reinstall its own version on any difference, which would have undone
  every update), passes `IMURUN_LAUNCHER=npm`, `IMURUN_VENV` and `IMURUN_SUPERVISED=1`,
  and restarts the app on exit code 75.
- Changed against the 2026-11-03 version while porting: pip/npm updates pin
  `imurun==<latest>` (a bare `-U` "succeeds" with no change when GitHub has the release
  before PyPI does); pre-releases are offered only to pre-release users; `uv tool`
  installs are recognised; its separate PATH shim (`%LOCALAPPDATA%\Programs\imurun\bin`,
  its own registry writer) was dropped in favour of the launcher `main` already has;
  its general slash commands were not ported (frontend CHANGELOG).
- Not verified yet: a real end-to-end update from one published version to the next
  (needs 0.3.3 and a later release on PyPI). Tested with mocked PyPI/GitHub, the
  launcher loop under `cmd.exe`, and a headless server answering `GET /api/update`.

## 2026-11-09 — packaging no longer ships stale files

- `setup.py`: `build_py` now empties the copied part of the package (`services/`,
  `model/`, `frontend/`, `docs/`, `bin/`, `start.py`, `install.py`, `window.py`,
  `requirements*.txt`, `LICENSE`) in `build/lib/imurun/` before copying again.
  setuptools reuses `build/lib` between builds and only adds to it, so after one
  `pip install .` / `python -m build` in a checkout, every later wheel also carried files
  that had since been deleted from the repo. It also carried the engine library from an
  earlier build, even with `IMURUN_BUNDLE_ENGINE=0`. Reproduced: a deleted
  `services/zz_stale.py` and `bin/fish.dll` both showed up in the next pure wheel. Now the
  second build matches a clean one (289 files).
- `start.py`: SIGTERM, SIGHUP and Ctrl+Break now take the same clean path as Ctrl+C. Before
  this, `kill`, systemd or launchd stopped the process without `server.stop()`, and
  `runtime/api.json` was left with a dead port and token. Verified: after `kill -TERM` the log
  ends with "Stopped.", `api.json` is gone and the port is closed.
- **Security review fixes** (independent review of the setup and CI changes; no Critical findings):
  - The `imurun` launcher and the npm launcher run `python -P -m imurun`. Without `-P`, typing
    `imurun` inside an untrusted folder imported that folder's `imurun/` or `json.py`. A test
    reproduces the attack and fails without `-P`.
  - The Windows launcher never leaves an empty `PYTHONPATH` entry, which would put the cwd on
    `sys.path`. Its folder is always under the user profile, never next to a relocated data root.
  - A launcher folder that every user, or a shared group, can write to is refused.
  - The launcher write is atomic and never follows a planted symlink.
  - Paths are shell-quoted (sh, fish), and `%` is escaped for cmd. The `.cmd` file is written
    in the OEM code page, and a non-string registry `Path` value is left alone.
  - Release: tags and manual runs publish and sign only when the commit is on `main`.
    `engine.yml` no longer narrows the token, so the Azure OIDC login can work. Only the signing
    secrets are passed to it (no more `secrets: inherit`). Actions that run with signing
    credentials are pinned by commit SHA, and npm is pinned to an exact version in the job that
    holds `NODE_AUTH_TOKEN`.
- **Certificates.** HTTPS (model downloads, `--get-engine`, connectors) is verified against the
  OS trust store via `truststore` (new dependency, injected at start by `start.py` / `install.py`).
  This fixes "unable to get local issuer certificate" behind corporate proxies and on the
  python.org macOS build. Checked here: the httpx and urllib contexts become truststore contexts
  with `CERT_REQUIRED`, GitHub and Hugging Face requests succeed, and a local self-signed server
  is still rejected. `IMURUN_NO_TRUSTSTORE=1` opts out; `imurun doctor` reports the source.
- **Publisher identity.** The release attests every file with Sigstore build provenance
  (`gh attestation verify`) and uploads PEP 740 attestations to PyPI. `engine.yml` signs
  `fish.dll` (Azure Artifact Signing) and `libfish.dylib` (Developer ID + notarization) when the
  credentials are configured; otherwise the dylib is ad-hoc signed and the run warns. The
  macOS quarantine flag on a browser-downloaded engine is now named in the load error.
- **`imurun` works from any new terminal, however it was installed** (`install.ensure_command`,
  run by the first-run setup and recorded as `command` in `install-state.json`). Before this,
  `pip install` into a venv, `pip install --user` on Windows, `npx imurun` and a checkout
  all left `imurun` off PATH in a new terminal. If `imurun` does not already run this
  interpreter, setup writes a marked launcher (`~/.local/bin/imurun`; Windows:
  `~/.imurun/cmd/imurun.cmd`) and adds its folder to the user PATH (shell rc block / HKCU
  `Path`). A command that is on PATH only because a venv is activated does not count.
  Foreign files are never overwritten. `IMURUN_NO_PATH=1` and CI skip the PATH change;
  `IMURUN_BIN_DIR` moves the launcher. `imurun doctor` reports it. The CI smoke install now
  runs the launcher too. Verified on Linux: after a fresh wheel install and first run,
  a new login shell and a new interactive bash both resolve `imurun` → `imurun 0.3.0`.
  The Windows branch is written but has not been run yet.
- `.github/workflows/workflow.yml`: **releases are automatic.** A push to `main` whose
  `pyproject.toml` version has no `v<version>` tag yet runs the whole pipeline and then
  creates the tag and the GitHub Release, publishes to PyPI (trusted publishing, already
  registered for `workflow.yml`, checked against the provenance of 0.1.11), and publishes
  to npm. Every other push to `main` stops after `check`. The tag lookup fails closed: an
  error stops the run instead of releasing again. The npm job also marks `imurun@<0.2.0`
  deprecated. The 0.1.x yank on PyPI stays manual, because PyPI has no API for it.
- `npm/imurun.js`: with `IMURUN_WHEEL=<wheel>`, the launcher compares the version in the
  wheel's file name with the one in the venv and installs when they differ. Before this, an
  existing install, for example the old 0.1.11 in `~/.imurun/venv`, was kept and run
  (reproduced: `imurun --version` printed `0.1.11`). When the file name gives no version,
  it always installs.

## 2026-11-03 — data folder created up front

- `install.py`: `HOME_DIRS` + `ensure_home_layout()` create the whole `~/.imurun` tree
  (models, sessions, settings, secrets, runtime, cache/fish, logs, skills, tools, workspace,
  feedback, voice, tuning/{base,built,datasets,runs}, tmp) idempotently - existing folders
  are left alone, private ones get 0700 on POSIX. Runs as the first thing in setup, on every
  start inside `quick_check()` (self-heals a deleted folder; only a folder that cannot be
  created is a reason to stop), and `imurun doctor` reports the count. Recorded as `home` in
  `install-state.json`. Owner ask: "it should properly create the folder where all the memory
  and the other parts are stored, and cross-check whether it is already there."

## 0.3.0 (unreleased) — 2026-11-03: one command installs and runs everything

### Added
- **Automatic setup on every start.** `start.py` / `imurun` call `install.quick_check()`
  (state file + `find_spec` + `stat`; 20 ms measured, no engine load, no heavy imports).
  When something is missing (first run, new version, missing package, changed engine),
  it runs `install.py --here --auto` with the same interpreter and then continues. Only a
  core-package failure stops it. `IMURUN_SKIP_SETUP=1` / `--skip-setup` turn it off.
  (`start.py` `ensure_installed`, `run_install_step`)
- **`install.py` installs everything** (`run_setup`): core packages (fatal); pywebview
  on Windows/macOS; voice (`requirements-voice.txt`, best-effort); the engine (found,
  else downloaded from the release, else built from `model/fish` with CMake + Ninja when
  the toolchain is present, else instructions), then loaded with ctypes and its ABI
  read (`probe_engine`); a GPU runtime report (Vulkan loader + vendor driver, file
  lookups only: `gpu_report`); tuning only with `--tuning` (separate venv). New flags:
  `--force`, `--no-voice`, `--tuning`, `--no-build`, `--build-tools` (Windows winget
  toolchain), `--auto`, `--doctor`.
- **`~/.imurun/install-state.json`**: one entry per interpreter, holding the versions,
  the engine path/size/mtime/ABI/source, the voice/window/tuning results and the GPU
  report.
- **`imurun doctor`** and **`imurun install [--force ...]`** subcommands (`imurun/cli.py`).
- CI **`install-smoke`** job on Windows, Linux and macOS (`ci.yml`). It builds a wheel
  with that commit's engine, installs it into a fresh venv outside the checkout, starts
  `imurun --no-open`, checks `/health`, `/`, the state file and `imurun doctor`, then
  installs the npm tarball into a scratch prefix and runs it. The release workflow's
  wheel jobs run the same script with `--npm`.

### Changed
- An installed copy now auto-installs missing core packages instead of exiting with a
  message. A checkout run inside a venv does the same instead of asking. A checkout
  run with a system Python creates `.venv` without asking.
- Voice is installed by default (it was `--voice` opt-in). It is skipped under `CI` /
  `IMURUN_SKIP_OPTIONAL=1` / `IMURUN_NO_VOICE=1`.
- The install log follows `IMURUN_DATA_ROOT` (`<data root>/logs/install.log`).
- npm launcher: when the version is missing from PyPI, it now says so instead of
  "check your internet connection".
- `docs/setup/README.md`: per-OS command table matching imutechz.in/imu, a status
  note (PyPI/npm still serve 0.1.11), what gets installed and when, the state file,
  force reinstall, the installed layout, and new troubleshooting entries.

## 0.3.0 — 2026-11-02: `pip install imurun`, `npx imurun`, CI and release pipeline

### Added
- **`imurun/` launcher package** (`__init__.py` with `__version__`, `cli.py`, `__main__.py`).
  The `imurun` console script (`imurun.cli:main`) finds the app root, puts it on
  `sys.path` and `PYTHONPATH`, and calls `start.main` with the user's arguments.
  `imurun --version` / `python -m imurun --version`.
- **`setup.py` build glue.** `build_py` copies `services/`, `model/*.py`, `start.py`,
  `install.py`, `window.py`, `requirements*.txt`, `frontend/design-lab/`,
  `docs/connectors/` and the engine library into `imurun/` inside the wheel. This
  keeps the checkout layout, so `services/**` needed no change. When an engine is
  bundled, the wheel is tagged `py3-none-<platform>` (`IMURUN_ENGINE_LIB`,
  `IMURUN_WHEEL_PLAT`, `IMURUN_BUNDLE_ENGINE=0`). Without one it is `py3-none-any`.
- **`MANIFEST.in`.** The sdist carries the app plus the engine sources
  (`model/fish/{src,include,shaders,test}`), and excludes binaries, models,
  `*.local.json`, `*_debug.txt` and `archive/`.
- **`start.py --get-engine`** / `imurun --get-engine`
  (`install.download_engine`). It downloads this platform's library from release
  `v<version>`, verifies it against `SHA256SUMS.txt`, and saves it to `~/.imurun/bin`.
  `find_engine_library` now also searches there, and `start.py` passes the result on
  through `FISH_LIBRARY`.
- **npm launcher** (`package.json`, `npm/imurun.js`; the package name `imurun` and
  the command `imurun` are the ones the 0.1.x npm package used). It finds Python
  3.11+, creates `~/.imurun/venv`, pip-installs `imurun==<npm version>` (with
  `[window]` on Windows/macOS), and runs `python -m imurun`. No dependencies, no
  telemetry.
- **Workflows.** `ci.yml` (lint subset, pytest on 3 OSes plus py3.11/3.13 on Linux,
  engine build, package smoke). `engine.yml` (MinGW/MSYS2, manylinux_2_28 GCC, macOS
  clang). `workflow.yml` (release: version check → tests → engines → per-OS wheels,
  each smoke-installed → sdist → GitHub Release with `SHA256SUMS.txt` → PyPI by
  trusted publishing → npm). `.github/scripts/smoke_install.py` installs a wheel into
  a fresh venv, runs `imurun --no-open`, requires `/health` 200, the UI page and a
  loadable engine, and fails if anything is written into site-packages.
- `docs/setup/RELEASE.md`: the one-time PyPI/npm/GitHub setup, the git state of this
  folder, and how to cut a release step by step.
- `install.py --voice` and the `imurun[voice]` extra (CODE-REQUESTS #8).
- `build` and `twine` in `requirements-dev.txt` and in the `dev` extra.

### Changed
- `pyproject.toml`. It now uses setuptools ≥ 77 with SPDX `license = "MIT"` and
  `license-files`, and lists the real package (`imurun`) instead of
  `services*/model*/response*/server*/providers*/scripts*/test*`. Most of those did
  not exist, and the ones that did would have installed `services` and `model` as
  top-level names in site-packages. The console script was `services.server.app:main`
  and is now `imurun.cli:main`. The broken `imurun-fish = model.fish_native:main` was
  removed (there is no such function). Project URLs, Python 3.13/3.14 classifiers and
  the author (ImuTechz) were added. The unused `setuptools-scm` build requirement was
  dropped. The ruff/mypy/coverage paths now point at directories that exist, and the
  ruff rules moved under `[tool.ruff.lint]`.
- **Installed layout (`install.INSTALLED`).** `start.py` never creates `.venv` and
  ignores `--bootstrap`. A missing package gives a `pip install` hint instead of
  venv instructions. Model downloads are pinned to `~/.imurun/models` (workaround,
  CODE-REQUESTS #10). The install log falls back to the temp dir instead of the
  package directory.
- **Engine build guidance in `install.py --check`** now asks for MinGW-w64 `g++`
  (WinLibs) instead of MSVC Build Tools and an "x64 Native Tools" prompt. MSVC cannot
  build the engine.
- `.gitignore` rewritten. See "Bugs fixed".

### Bugs fixed
- **Real sources were git-ignored.** Symptom: `frontend/design-lab/core.js`,
  `frontend/design-lab/styles/tokens.css` and `tests/test_connectors_secrets.py`
  were missing from `git status` and would never have been pushed, while
  `services/connectors/tokens.py` and `model/fish/*/tokenizer.*` survived only
  because they were already tracked. Cause: catch-all patterns in `.gitignore`
  (`core.*`, `*token*`, `*secret*`, `*credential*`, `*password*`). Fix: targeted
  rules (`*.local.json`, `client_secret*.json`, `.env*`, `*_debug.txt`, key files).
  Checked with `git status --ignored`.
- **`pip install .` produced a broken command.** Symptom: the wheel had no frontend,
  no `start.py`/`install.py`/`window.py` and no engine, and the `imurun` script
  pointed at `services.server.app:main`, which `services` could only import as a
  top-level package. Cause: `pyproject.toml` `packages.find` plus an entry point
  written for a checkout. Fix: the `imurun` package, the `setup.py` bundling, and the
  entry point `imurun.cli:main`.

### Verified on this machine (Windows 11, Python 3.12, Node 26.13 / npm 11.20)
- `python -m build` → sdist + `py3-none-any` wheel. `python -m build --wheel` →
  `py3-none-win_amd64` with `imurun/bin/fish.dll`. `twine check` passes all three.
  None of them contains `*.local.json`, debug dumps, `__pycache__` or models.
- Fresh venv (scratchpad), platform wheel, run from outside the repo:
  `imurun --version` → `imurun 0.3.0`. `imurun --no-open --port <free>` →
  `/health` `{"ok": true}`, `/` serves the UI, the engine is found at
  `site-packages/imurun/bin/fish.dll` and loads through `model.fish_native` +
  `ctypes`. No traceback, and no file was added under `site-packages/imurun`.
- Fresh venv, pure wheel: the server starts and prints the "no FISH engine for this
  platform" guidance (no traceback). `--get-engine` reports the 404 cleanly (no
  release exists yet). With a library in `<data root>/bin` it is picked up. The
  download path and the rejection of a checksum mismatch were tested against a local
  HTTP server.
- npm: `npm pack` → 4 files. `npm i -g <tgz>` into a scratch prefix, then
  `IMURUN_WHEEL=<wheel> imurun --version`: it created the venv with `py -3.12`,
  installed the wheel + `[window]`, printed `imurun 0.3.0`, and the second start
  skipped pip (≈1 s). The server started through the Node launcher answered
  `/health`, and killing the tree left no orphaned Python. A wheel path with spaces
  works.
- `python -m pytest -q`: 324 passed, 8 skipped (window/network/voice/tuning opt-ins).
- CI workflows: YAML parses; the lint commands (ruff subset, `node --check` on all
  JS) pass locally. **The workflows have not run on GitHub yet.**
