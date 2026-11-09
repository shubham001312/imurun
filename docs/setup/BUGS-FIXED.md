# Setup / packaging: bugs fixed

Symptom → cause → fix → where. Newest first.

## 2026-11-03

### 1. `pip install imurun` / `npx imurun` do not install this app (NOT fixable in the repo)
- **Symptom:** the owner reports that on another machine the pip and npm commands from
  imutechz.in/imu "are not properly installing". `pip install imurun` gives a program
  with `imurun models` / `imurun run` that downloads an `.exe`. The 0.3.0 npm launcher
  fails with `No matching distribution found for imurun==0.3.0 (from versions: 0.1.3 …
  0.1.11)`.
- **Cause:** PyPI and npm serve `imurun` 0.1.11 (2026-10-21), the earlier Bun/TypeScript
  CLI. No `v0.3.0` tag was ever pushed, so the release workflow never published this
  app. The highest GitHub release is v0.1.11, which has no `fish-*.dll` /
  `SHA256SUMS.txt`, so `imurun --get-engine` also 404s.
- **Fix:** none possible from the repo. The owner has to push tag `v0.3.0`
  (`docs/setup/RELEASE.md`). The npm launcher now prints "imurun 0.3.0 is not on PyPI
  (yet)" instead of blaming the network (`npm/imurun.js:186`). The README status note
  says this plainly.

### 2. A fresh checkout / sdist on another machine has no engine and no way to get one
- **Symptom:** `git clone` + `run.bat` starts, but chat never works ("FISH library not
  found"). `--get-engine` 404s (see 1).
- **Cause:** `bin/*.dll` is git-ignored and pruned from the sdist. `install.py` only
  printed build instructions.
- **Fix:** `install.ensure_engine` (`install.py:1030`): when no engine is found, it
  downloads it from the release; otherwise, with cmake + ninja + g++/clang++ present
  (the WinGet paths are added on Windows), it builds `model/fish` into `bin/`
  (`build_engine`, `install.py:970`). `--build-tools` winget-installs the Windows
  toolchain. Automatic runs ask first.

### 3. Installed copy exits on a missing package instead of installing it
- **Symptom:** `imurun` → "IMURUN cannot start: required Python packages are missing",
  exit 2, unless the user knows about `--install`.
- **Cause:** `start.ensure_dependencies` only printed in the installed layout.
- **Fix:** it runs `install.py --here --auto` and continues (`start.py:141`). The same
  happens inside any venv in a checkout.

### 4. `python install.py` from an installed copy would create `.venv` inside site-packages
- **Symptom:** latent. `install.main` without `--here` picked
  `site-packages/imurun/.venv`.
- **Cause:** `target = venv_python() if venv_healthy() else None` ignored `INSTALLED`.
- **Fix:** `here = args.here or INSTALLED or in_venv()` (`install.py:1509`).

### 5. CI engine smoke check tested the source tree, not the installed wheel
- **Symptom:** locally, `smoke_install.py … --expect-engine` failed with "the bundled
  engine library does not load" for a wheel that does contain `imurun/bin/fish.dll`.
- **Cause:** the `python -c "import imurun …"` probes ran with the checkout as cwd, so
  `import imurun` found `./imurun/` (the launcher sources, no `bin/`). The
  "nothing written into site-packages" snapshot also looked at the wrong directory.
- **Fix:** run those probes with `cwd=work`, a temporary directory outside the checkout
  (`.github/scripts/smoke_install.py:59`, `:124`).

### 6. CI never installed the wheel on Windows or macOS outside a release
- **Symptom:** `ci.yml` smoke-installed only the pure wheel on Ubuntu. Per-OS install
  breakage would surface only on the release tag.
- **Fix:** the new `install-smoke` matrix job (`.github/workflows/ci.yml:113`) uses the
  engine built in the same run, plus an npm launcher smoke. The release wheels job runs
  the same script with `--npm` (`.github/workflows/workflow.yml:128`).
