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

```
pip install imurun                 # the `imurun` command
pip install "imurun[window]"       # + native window (pywebview)
npx imurun                         # npm launcher (or: npm i -g imurun && imurun)
```

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
   retrying without it if that fails), and only when the installed version differs.
   Later starts take about a second;
4. runs `python -m imurun <args>` and passes on its exit code.

Environment variables: `IMURUN_PYTHON` (interpreter to use), `IMURUN_VENV` (venv
location), `IMURUN_WHEEL` (install a local wheel instead of PyPI, used for testing),
`IMURUN_REINSTALL=1`, `IMURUN_NO_WINDOW_EXTRA=1`. `imurun --launcher-info` (npm only)
shows the venv and the Python it would use.

Building the packages yourself and cutting a release: `docs/setup/RELEASE.md`.

## Quick start

| OS      | First run and every later run           |
|---------|-----------------------------------------|
| Windows | double-click `run.bat`, or `run.bat` in a terminal |
| macOS   | double-click `run.command`, or `./run.command` |
| Linux   | `./run.sh`                              |

On the first run the launcher:
1. looks for Python 3.11 to 3.14. If there is none, it installs Python 3.12
   (Windows: `winget`; macOS: Homebrew; Linux: apt/dnf/pacman after asking you);
2. runs `install.py`, which creates `.venv`, installs the packages (and pywebview on
   Windows/macOS), and checks for the FISH engine;
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
(base interpreter for the venv), `--window` / `--no-window`, `--dev`, `--verbose`
(raw pip output instead of the animated progress). The pip log is written to
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

## Troubleshooting

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
It switches to a healthy `.venv` automatically, or prints these instructions (and
offers to install when run in a terminal) instead of a traceback.

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
