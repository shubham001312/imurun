# Resource governor

Code: `services/resources/` (`budget.py` numbers, `counters.py` measurements,
`governor.py` decisions and the view, `share.py` the thread ledger,
`priority.py` process priority) and the update handler in
`services/server/resources.py`. API contract: `services/server/doc/README.md`
"Resource governor contract". Tests: `tests/test_resources_budget.py`,
`tests/test_resources_governor.py`, `tests/test_resources_detect.py`.

## What it does

The owner's rule: **IMURUN must always leave 20-30 % of the machine to the
user**, scaled by the machine. The governor turns that into:

| Lever | How | Where |
|---|---|---|
| Engine threads | `FISH_THREADS` at every model load = physical cores minus the CPU headroom (1..physical) | `governor.load_env` |
| Per-reply threads | dynamic target: lowered after **2** hot samples (other programs use more cores than the headroom), raised after **3** calm ones; applied by the engine once `fish_set_threads` exists (engine REQUESTS R1) | `governor.observe`, `before_generate` |
| Process priority | Windows `SetPriorityClass(BELOW_NORMAL)` while a reply, a voice request or a tuning step runs, saved class restored when the last one ends; POSIX `os.nice(+10)` (sticky: an unprivileged process cannot raise it back). Setting `resources.lowerPriority`, default on | `priority.PriorityGuard` |
| Voice / tuning threads | same ledger as chat: chat first, voice gets the leftover (min 1), tuning gets 0 while chat runs | `share.ThreadLedger` |
| RAM | `FISH_RAM_BUDGET_MB` = min(total - OS reserve - headroom, free now - headroom) | `budget.ram_budget_now` |
| VRAM | `FISH_VRAM_HEADROOM_MB` (see below) | `budget.vram_headroom_bytes` |
| Disk | downloads / exports / tuning refused when less than max(10 GB, 10 %) would stay free | `governor.check_disk` |
| Research | fetch concurrency from the thread budget | `governor.research_concurrency` |
| GPU visibility | detection with fallbacks + the engine's placement report, in `/api/resources` and the `resources` event | `counters.detect_gpu`, `governor.gpu_view` |

### CPU headroom table (balanced)

Device class: a system battery = laptop (Windows `GetSystemPowerStatus`,
Linux `/sys/class/power_supply/*/type`, macOS `pmset -g batt`); unknown is
treated as a laptop. Laptops keep more because CPU and GPU share one cooler
and throttle sooner.

| Physical cores | Laptop | Desktop | Example: threads granted |
|---|---|---|---|
| <= 4 | 30 % | 30 % | 4 cores -> 3 |
| 5 - 8 | 30 % | 25 % | 8 cores -> 6 (laptop and desktop) |
| 9 - 16 | 25 % | 20 % | 12 -> 9 / 9; 16 -> 12 / 12 |
| > 16 | 20 % | 20 % | 24 -> 19 |

Rounded to whole cores, but never under 20 % of the cores (times the preset
factor), at least one core free on 2+ cores, never all cores. Hyper-threads
are not counted: the engine's int8 matmul is memory-bound and SMT siblings
add contention, not bandwidth.

Presets: `gentle` (alias `eco`) doubles every headroom, `performance`
halves it, `custom` takes `{cpu, ram, vram, disk}` percentages (0-90).

### VRAM headroom and why

Before 2026-11-03 the VRAM headroom was a flat 25 % on a <=4 GB card. On the
owner's RTX 3050 Laptop (4 GB) that kept 1 GB free and left the engine ~2.1 GB,
less than the ~3.1 GB an 8B needs fully resident (engine REQUESTS R3). But the
budget is already `free VRAM at load - headroom`: whatever other programs hold
is excluded by measuring *free* memory. The headroom only has to absorb their
growth and the driver's own allocations, so it is now:

```
headroom = clamp( (max(256 MB, 10 % of card) + growth) x preset factor,  256 MB,  50 % of card )
growth   = min(512 MB, 25 % of what other programs hold)   if they hold > 5 % of the card, else 0
```

"Held by other programs" comes from nvidia-smi (rocm-smi / amdgpu sysfs on
Linux) minus the bytes IMURUN's own loaded model holds (the engine reports
`gpu_bytes`). 256 MB is the engine's measured floor. Examples: 4 GB card,
0.3 GB used elsewhere -> 486 MB kept, ~3.2 GB usable (was 1 GB / ~2.7 GB);
idle 24 GB card -> 2.4 GB kept; identity-only detection (no live counter) ->
10 %, and the engine measures free VRAM itself.

## GPU detection

`counters.detect_gpu()` tries, in order, and never raises:

1. **nvidia-smi** (PATH or `System32`): name, total, used, free, utilisation - live.
2. Linux: **amdgpu sysfs** (`/sys/class/drm/card*/device/mem_info_vram_*`), then **rocm-smi** `--json` - live.
3. OS identity (cached once per process, no live usage):
   Windows **registry** display-class keys (`DriverDesc`, 64-bit
   `HardwareInformation.qwMemorySize`, sub-millisecond), then
   **`Get-CimInstance Win32_VideoController`** (its `AdapterRAM` is uint32, so
   >= 4 GB cards read as ~4 GB); macOS `system_profiler SPDisplaysDataType`;
   Linux sysfs identity for non-AMD cards.

Discrete cards win over integrated ones; "Microsoft Basic Display", remote and
virtual adapters are skipped. After a model load the engine's own report
(`stats()["placement"]`: gpu name, gpu/ram/disk layers, gpu bytes, mix) is
merged in; when the OS sees nothing but the engine computes on a GPU (e.g.
Vulkan without a vendor tool) the engine is the source. View fields:
`governor.gpu` (with `label`, e.g. "GPU: RTX 3050 Laptop GPU - 16/16 layers,
0.8 GB of 3.2 GB"), `governor.engine`, `governor.machine.gpuInfo`;
`machine.gpu` stays a name string for `frontend/design-lab/hardware.js`.

## Events

`resources` is published at once on a settings change, a thread-target
change, a priority lowering/restore and a model load (`note_engine`) or a
placement change seen by the sampler; otherwise every 10 s, **only while an
`/api/events` client is connected** (the governor reads the broker's
subscriber count). With nobody listening it builds no view and does not poll
the GPU; with `dynamic` off and nobody listening the sampler does nothing.

## Settings (`resources` section)

`headroom` (`gentle|balanced|performance|custom`; `preset` and `eco` are
accepted on input), `custom`, `dynamic` (default true), `lowerPriority`
(default true). Canonical write path today: `PUT /api/settings/resources`
(validated by `validate_settings`, applied at once through
`governor.refresh()`). `PUT/POST /api/resources` goes through the same handler
(`services/server/resources.handle_update`, routed in `api.py` do_PUT/do_POST).

## What is NOT done

- **Per-reply threads do not reach the engine yet**: the thread pool size is
  fixed at the first load until the engine adds `fish_set_threads`
  (engine REQUESTS R1). Today the dynamic target changes the next *load* and
  the voice/tuning grants; priority lowering is what protects the desktop
  during a reply.
- POSIX priority lowering is one-way for the process lifetime.
- VRAM on AMD/Intel under Windows and on Apple: identity only (no live free
  memory) until the engine reports its Vulkan heap budget (engine REQUESTS R3b).
- Voice/whisper VRAM on the GPU is not subtracted as "own" (it counts as
  another program's use, which errs on the safe side).
- Not measured on a real desktop, AMD or Linux machine; the parsers are
  tested against captured outputs only.

## Requests

- **api.py owner** (server): route `PUT`/`POST /api/resources` to
  `services/server/resources.handle_update(server, payload)` -> `self._reply(*result)`;
  update `services/server/doc/README.md` row once done.
- **CLAUDE.md**: add `docs/resources/` (README, CHANGELOG, BUGS-FIXED) to the documentation map.
- Engine: R1-R3 in `docs/engine/REQUESTS.md`.
