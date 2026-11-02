# Code change requests from setup/packaging

The setup work changed only `requirements*.txt`, `pyproject.toml`, `start.py`,
`install.py`, `run.*` and `docs/setup/`. None of the items below block startup:
with `requirements.txt` installed, `start.py --no-open` serves `/health` = 200.
They are cleanups for the owners of the named modules.

## 1. `model/fish_client.py`: make `aiohttp` a lazy import (owner: model/)

`import aiohttp` at module level (line 12) makes the whole server unimportable
without aiohttp, even though the default compute path is the in-process native
engine (`model/fish_native.py`, ctypes only). This is the import behind the reported
crash. Request: import aiohttp inside the HTTP-client code path, or
`try: import aiohttp except ImportError: aiohttp = None`, and raise a clear error
only when the HTTP engine path is actually used.

## 2. `services/server/app.py` and `services/server/api.py`: do not mask real import errors (owner: services/)

```python
try:
    from ..model.fish_client import SyncFishModelClient
except ImportError:
    from model.fish_client import SyncFishModelClient
```
`except ImportError` also catches a missing *third-party* module raised inside
`fish_client`. The user then sees a chained `No module named 'services.model'`
before the real error. Request: catch only the relative-import failure, for example
`except ImportError as exc: if exc.name not in ("services.model", None): raise`, or
pick the import with `if __package__ and __package__.count(".") >= 2`.

## 3. `services/model_manager.py`: lazy `httpx` / `huggingface_hub` (owner: services/)

These are imported at module level. `api.py` and `config.resolve_model_path` already
tolerate an `ImportError`, but importing them only inside the download/catalogue
functions would let the server start (and list local models) without them.

## 4. `pyyaml` is declared but not imported anywhere (owner: services/)

A search of `services/`, `model/*.py` and `start.py` finds no `import yaml`. It stays in
`requirements.txt` because the old `pyproject.toml` declared it and config files
may use it. If nothing will read YAML, tell setup and it will be removed.

## 5. `--host` has no effect on the bind address (owner: services/server)

`build_server()` does not accept or forward a `host`, and `ApiServer` binds
`(host, port)` with its default (loopback). `start.py --host` therefore only changes
`cfg.host` and the printed message. That is probably intentional (loopback-only
security model). If so, consider removing the flag or documenting it as display-only.
If not, add a `host=` parameter to `build_server`.

## 6. Optional extras used through `try: import` (informational)

- `services/server/models_api.py` needs `fastapi` + `pydantic`. It is optional, and
  `_HAS_MODELS_API` falls back to `services.model_manager`. Extra: `imurun[api]`.
- `services/connectors/tokens.py` uses `keyring`. Extra: `imurun[connectors]`.
- `services/server/resources.py` uses `psutil`. Extra: `imurun[metrics]`.

If any of these should become mandatory, say so and setup will move them into
`requirements.txt`.

## 7. `services/server/app.py`: `_start_idle_watchdog(cfg: ServerConfig)` (minor)

`ServerConfig` is not imported in `app.py`. It works only because of
`from __future__ import annotations`. Import it, or drop the annotation.

## 8. `install.py --voice` (voice owner, 2026-11-02) — DONE 2026-11-02

Done by setup: `python install.py --voice` adds `-r requirements-voice.txt`, and
`pyproject.toml` has a `voice` extra (`pip install "imurun[voice]"`) with the same
four packages. It is deliberately not part of `all` (it is large). The `run.*` doctor
summary is not done.

Original request:


`requirements-voice.txt` exists (faster-whisper, av, piper-tts, numpy;
edge-tts commented out because it sends text to Microsoft). `install.py` has
no extras mechanism, so please add a `--voice` flag that also runs
`pip install -r requirements-voice.txt` (same pattern as `--dev` at
`install.py` `args += ["-r", str(REQUIREMENTS_DEV)]`), and a `voice` extra in
`pyproject.toml` with the same four packages. After install, `python -m
services.voice doctor` prints what works. Optional: `run.*` could print the
doctor's one-line summary (`/api/status.voice`).

## 9. ~~`services/connectors/apps.py`: `apps.local.json` is written next to the module~~ (done 2026-11-02: `services/paths.local_file`; the data root when installed) (owner: services/connectors) — 2026-11-02

`LOCAL_FILE = Path(__file__).with_name("apps.local.json")` (line 31), and
`write_local` (`apps import-google`) writes there. In a pip install that path is
`site-packages/imurun/services/connectors/`. That breaks the "nothing is written
into site-packages" rule, the file is lost on every upgrade, and the write fails on a
read-only or system install. Request: when `Path(__file__).parents[2] / "__init__.py"`
exists (the installed layout, see `install.INSTALLED`), read and write
`<data root>/apps.local.json` (`IMURUN_DATA_ROOT` or `~/.imurun`). Keep the
checkout path for development. The wheel build already excludes `*.local.json`
(`setup.py` EXCLUDE, `MANIFEST.in`), so a developer's registered secrets never ship.

## 10. ~~Model directories default to `<app root>/models`~~ (done 2026-11-02: `services/paths.models_dir()` = `IMURUN_MODELS_DIR` > `<data root>/models` when installed > `<app root>/models`; used by `model_manager.get_model_manager()` and `tuning/paths.models_dir()`, so the `start.py` workaround can go) (owners: services/, services/tuning) — 2026-11-02

- `services/model_manager.py:931` `get_model_manager()` with no argument uses
  `Path(__file__).parents[1] / "models"`. `api.py` calls it that way, so in an installed
  wheel downloads would land in site-packages. **Worked around in `start.py`:** in the
  installed layout it calls `get_model_manager(<data root>/models)` before the server
  is built, so the singleton is pinned to `~/.imurun/models`. Request: make the
  default `IMURUN_MODELS_DIR` → `<data root>/models` for installed copies, so the
  workaround can be removed.
- `services/tuning/paths.py:116` `models_dir()` = `repo_root() / "models"`. Tuning
  exports would also write GGUFs into site-packages when installed. Same fix
  requested. Not worked around.

## 11. ~~`model/fish_native.py` `default_library_path`: also look in `~/.imurun/bin`~~ (done 2026-11-02, after `bin/` and `model/fish/build`) (owner: model/) — 2026-11-02

`imurun --get-engine` saves the prebuilt library to `<data root>/bin/`. `start.py`
passes it on through `FISH_LIBRARY`, so this works today. Searching
`<data root>/bin` after `REPO_ROOT/bin` and `model/fish/build` (the order is in
`install.find_engine_library`) would make the bindings work the same way when
something other than `start.py` uses them.

