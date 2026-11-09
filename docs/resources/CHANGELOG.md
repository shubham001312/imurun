# Resource governor changelog

## 2026-11-03 — headroom that holds, priority, GPU detection

- CPU headroom by device class (laptop / desktop from the battery) and core
  count, never under 20 % of the cores; table in `README.md`.
- Process priority: below normal while a reply / voice request / tuning step
  runs, restored after (`priority.py`, setting `resources.lowerPriority`,
  default on). Voice and tuning grants take part in the same ledger and the
  same priority holder set.
- VRAM headroom is now max(256 MB, 10 %) + a growth term for what other
  programs hold, times the preset factor, clamped 256 MB..50 % of the card
  (was a flat 25 % on <= 4 GB: 1 GB kept, ~2.1 GB left for an 8B).
  "Used by others" excludes IMURUN's own model.
- GPU detection fallbacks: nvidia-smi -> amdgpu sysfs / rocm-smi -> Windows
  registry / `Get-CimInstance Win32_VideoController`, macOS system_profiler;
  the engine's placement report is merged after a load (and is the source
  when the OS sees nothing).
- View: `gpu` (with `label`), `engine`, `priority`, `presets`, `custom`,
  `deviceClass`, `lowerPriority`, `machine.gpuInfo`; `imurun.engine` gains
  `gpu`, `totalLayers`, `tokensPerSecond`, `prefillTokensPerSecond`
  (frontend API-REQUESTS #34).
- `resources` event on model load / placement change; heartbeat 10 s (was 5)
  and only while a client listens; no GPU polling without listeners.
- Presets: `eco` accepted as alias of `gentle`; `preset` accepted as alias
  of the stored key `headroom`.
- `services/server/resources.handle_update` for `PUT/POST /api/resources`
  (routing pending in `api.py`).
- Tests: `tests/test_resources_detect.py` (41 tests: device-class table,
  VRAM rule, parsers from captured outputs, engine merge, priority with a
  mocked kernel32, sampler state machine, listener gating, API round trip).
