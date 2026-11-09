# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\`

## Purpose

The `services` package is the Python backend of IMURUN. This top level holds only
two real modules: `config.py` (the dataclass configuration tree, loaded from
environment variables) and `model_manager.py` (the GGUF model catalogue, download
engine and local model registry used by the Models panel). Everything else in the
package lives in sub-packages documented separately (`connectors/`, `safety/`,
`sessions/`, `skills/`, `persona/`, `prompts/`, `response/`, `server/`).
`__init__.py` is a docstring only and exports nothing.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\config.py`

| Symbol | Signature | Notes |
|---|---|---|
| `FishConfig` | `@dataclass FishConfig(server_url="http://127.0.0.1:8731", stream_endpoint="/fish/stream", vram_limit_mb=8192, context_size=4096, temperature=0.7, top_p=0.9, max_tokens=2048, timeout_seconds=60.0, health_endpoint="/fish/health", stats_endpoint="/stats")` | Frozen-by-convention value object; fields are mutable. |
| `FishConfig.stream_url` | `-> str` (property) | `server_url.rstrip("/") + stream_endpoint`. |
| `FishConfig.health_url` | `-> str` (property) | `server_url.rstrip("/") + health_endpoint`. |
| `FishConfig.stats_url` | `-> str` (property) | `server_url.rstrip("/") + stats_endpoint`. |
| `ServerConfig` | `@dataclass ServerConfig(host="127.0.0.1", port=8732, data_root=None, static_root=None, fish=FishConfig(), max_body=1_000_000, version="0.2")` | `__post_init__` sets `data_root = Path.home()/".imurun"` when left `None`. |
| `load_config` | `load_config() -> ServerConfig` | Reads the environment, never writes files. |

`load_config` environment variables:

| Variable | Field | Default |
|---|---|---|
| `IMURUN_HOST` | `host` | `127.0.0.1` |
| `IMURUN_PORT` | `port` | `8732` |
| `IMURUN_DATA_ROOT` | `data_root` | `~/.imurun` |
| `IMURUN_STATIC_ROOT` | `static_root` | `None` |
| `IMURUN_MAX_BODY` | `max_body` | `1000000` |
| `IMURUN_VERSION` | `version` | `0.2` |
| `FISH_SERVER_URL` | `fish.server_url` | `http://127.0.0.1:8731` |
| `FISH_STREAM_ENDPOINT` | `fish.stream_endpoint` | `/fish/stream` |
| `FISH_VRAM_LIMIT_MB` | `fish.vram_limit_mb` | `8192` |
| `FISH_CONTEXT_SIZE` | `fish.context_size` | `4096` |
| `FISH_TEMPERATURE` | `fish.temperature` | `0.7` |
| `FISH_TOP_P` | `fish.top_p` | `0.9` |
| `FISH_MAX_TOKENS` | `fish.max_tokens` | `2048` |
| `FISH_TIMEOUT_SECONDS` | `fish.timeout_seconds` | `60.0` |
| `FISH_HEALTH_ENDPOINT` | `fish.health_endpoint` | `/fish/health` |
| `FISH_STATS_ENDPOINT` | `fish.stats_endpoint` | `/stats` |

Failure modes: `int()`/`float()` conversion of a malformed variable raises
`ValueError` at startup; no clamping is performed, so `IMURUN_PORT` is not
validated here (the HTTP bind fails later).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\model_manager.py`

| Symbol | Signature | Notes |
|---|---|---|
| `ModelInfo` | `@dataclass ModelInfo(id, name, filename, size_bytes, size_gb, source, repo_id=None, description="", downloaded=False, path=None)` | Mutable dataclass; the instances inside `RECOMMENDED_MODELS` are module-level singletons that `list_models()` mutates in place. |
| `DownloadProgress` | `@dataclass DownloadProgress(model_id, status, downloaded_bytes, total_bytes, speed_bps, eta_seconds, message="", error=None)` | `status` is one of `starting`, `downloading`, `verifying`, `complete`, `error`. |
| `DownloadProgress.percent` | `-> float` (property) | `0.0` when `total_bytes == 0`. |
| `RECOMMENDED_MODELS` | `dict[str, ModelInfo]` | Keys: `llama-3.2-1b-q4`, `llama-3.2-3b-q4`, `llama-3.2-7b-q4`, `mixtral-8x7b-q4`, `phi-3-mini-q4`, `gemma-2-2b-q4`. All `source="hf"`. |
| `ModelManager.__init__` | `(models_dir: Path, progress_callback=None)` | Creates `models_dir` (`parents=True, exist_ok=True`). `progress_callback` is stored but never invoked by this class. |
| `ModelManager.list_models` | `() -> list[ModelInfo]` | Side effect: sets `.downloaded` and `.path` on the shared catalogue objects. |
| `ModelManager.get_model_path` | `(model_id: str) -> Optional[Path]` | `None` for unknown ids and for files that do not exist. |
| `ModelManager.download_model` | `async (model_id: str, token: Optional[str] = None) -> AsyncGenerator[DownloadProgress, None]` | Yields a terminal `complete` or `error` frame; never raises for unknown ids. |
| `ModelManager._download_from_hf_with_progress` | `async (model_info, dest_path, token) -> AsyncGenerator[DownloadProgress, None]` | Private. Queries `HfApi().repo_info`, then streams with `httpx.AsyncClient`. |
| `ModelManager.delete_model` | `(model_id: str) -> bool` | Unlinks the file; `False` for unknown ids or absent files. |
| `get_model_manager` | `(models_dir: str = "./models") -> ModelManager` | Process-wide singleton; the first call fixes `models_dir` for the process lifetime. |

Download behaviour and failure modes:

- Unknown `model_id` → one `error` frame (`Unknown model: ...`), then the
  generator returns.
- File already present → one `complete` frame with `message="Already downloaded"`.
- `repo_info` succeeds but the filename is not a sibling → `error` frame.
- Any exception inside `download_model` (network, disk, auth) is caught and
  turned into an `error` frame carrying `str(e)`.
- After the stream, the size on disk is compared with the Hub-reported size; a
  mismatch yields an `error` frame (`Size mismatch: ...`). There is no checksum
  verification and no resume: a failed transfer leaves the partial file in place.
- `httpx` timeout is 300 s; the write is synchronous (`open(..., "wb")`) inside
  the async generator, so it blocks the event loop for the duration of the copy.

`services/__init__.py` exposes no symbols.

## Connections

| Importer | Uses |
|---|---|
| `services/server/app.py` | `load_config` (builds `ServerConfig`; `cfg.fish` is handed to `SyncFishModelClient`). |
| `services/server/api.py` | `load_config`; falls back to `services.model_manager.get_model_manager` when `services/server/models_api.py` cannot import FastAPI. |
| `services/server/models_api.py` | `get_model_manager`, `ModelInfo`, `DownloadProgress`. |
| `model/fish_client.py` | `FishConfig` (default config for the FISH HTTP client). |
| `scripts/*` | Not imported; the scripts carry their own model tables. |

HTTP routes that reach this module: `GET /api/models`, `GET /api/models/{id}`,
`POST /api/models/download` (SSE progress), `DELETE /api/models/{id}` — all
implemented in `services/server/api.py`. The Models UI view is
`frontend/design-lab/models.js`, which consumes those routes through `api.js`.

## Invariants

- `ModelManager` writes only inside `models_dir`; filenames come from the fixed
  `RECOMMENDED_MODELS` table, never from a request.
- A download must end with exactly one terminal frame (`complete` or `error`);
  the SSE route closes the connection on that frame.
- `get_model_manager` must stay a singleton: two managers on the same directory
  would race on the same `.gguf` paths.
- `ServerConfig` may only bind loopback (`services/server/api.py` re-checks and
  raises `ValueError` otherwise); `load_config` itself does not enforce this.
- `FishConfig.timeout_seconds` is the only HTTP timeout for streaming; lowering
  it below a normal generation time truncates long answers.
- `RECOMMENDED_MODELS` entries are shared mutable objects: do not rely on
  `ModelInfo.downloaded` being per-caller.
