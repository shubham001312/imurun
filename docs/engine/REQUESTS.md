# Requests to the engine owner (`model/fish/**`)

Cross-scope asks from the Python/server side. The engine owner decides how;
each item says what the server already does without it. GPU-specific asks that
predate this file stay in `docs/engine/GPU.md` ("Requests to the engine owner").

## From the resources owner (2026-11-03, `docs/resources/README.md`)

### R1. Change the thread count per generation (P1) — DONE 2026-11-03

Done: `fish_set_threads(ctx, n)` (ABI 4) sets how many pool threads take part
in the following jobs (1..pool size; the pool is still created once from
`FISH_THREADS`, workers above the limit wake and return without work).
`fish_stats.n_threads` and `fish_specs_json().threads` report the active
count, `pool_threads` the pool size. Takes the context lock, so it never
changes a running generation.

**Why.** The resource governor lowers IMURUN's CPU threads when the user's own
programs get busy and raises them back when the host is idle (owner: "always
leave 20-30 % headroom"). Today `ThreadPool::instance()` reads `FISH_THREADS`
once, when the pool is first created, and keeps that size for the whole
process (`threadpool.cpp` `default_threads`). The server sets `FISH_THREADS`
to the governed count before the first load, so the *initial* size is
enforced (8 -> 6 threads on this laptop), but a later lowering cannot reach
the engine.

**Ask.** `FISH_API int fish_set_threads(fish_ctx*, int n)` - the number of
pool workers that take part in the next generation (1..pool size; extra
workers park). Return 0 on success. `fish_stats.n_threads` should report the
active count. The binding is already written: `model/fish_native.py` binds
`fish_set_threads` when the symbol exists and calls it before every
generation with the governor's target (`_begin_generation`); `/api/resources`
reports `imurun.engineAppliesTarget` = true once it is there. No ABI struct
change is needed.

### R2. Let the specs planner's context choice honour `FISH_RAM_BUDGET_MB` (P2) — DONE 2026-11-03

Done: `SpecsEngine::plan()` uses `FISH_RAM_BUDGET_MB` as its RAM budget when
it is set (n_ctx "KV fits a quarter of the budget" and the resident/windowed
choice), the same number `place_layers()` uses.

`place_layers()` and the adapter merge use `FISH_RAM_BUDGET_MB`, which the
governor now sets to `min(total - OS reserve - headroom, free now - headroom)`
at every load. The n_ctx choice in `specs.cpp` (`plan()`, "shrink until the KV
cache fits a quarter of the budget") uses `ram_available - reserve` instead,
so on a machine whose RAM is already full it can still pick a context whose
KV cache eats into the user's headroom. Using the same budget there keeps one
number in charge.

### R3. VRAM headroom as a share, and live VRAM in the stats (P2) — DONE 2026-11-03

Done, both: (a) `FISH_VRAM_HEADROOM_PCT=p` (0..90, share of the device heap;
`FISH_VRAM_HEADROOM_MB` wins when both are set), one rule
(`vram_headroom_bytes()`, `backend.h`) for the backend and the planner;
(b) ABI 4 `fish_stats.vram_total` / `vram_free` (live, read at every
`fish_get_stats`) / `vram_headroom`, from Vulkan on any vendor; Python:
`stats()["placement"]["vram_total_bytes"|"vram_free_bytes"|"vram_headroom_bytes"]`.
`fish_specs_json().hardware.vram_*` already came from the Vulkan backend.

The governor keeps 25 % of a <=4 GB card free (20 % up to 8 GB, 15 % above)
and passes it as `FISH_VRAM_HEADROOM_MB`, which needs the card's size. It
reads the size from `nvidia-smi`, so on AMD / Intel / Apple GPUs it cannot
pass a headroom and the engine's default (max(256 MB, 5 %)) applies. Either:
(a) `FISH_VRAM_HEADROOM_PCT`, or (b) the heap size and live budget
(`VK_EXT_memory_budget`) in `fish_stats` / `fish_specs_json`, so the server
needs no vendor tool. (b) would also let the UI meter show VRAM on every
vendor.

**Conflict to note:** the balanced preset's 1 GB headroom on a 4 GB card
leaves ~2.0-2.3 GB usable when other programs hold 0.7-1.0 GB, less than a
fully resident 8B needs. The `performance` preset halves it (512 MB); the
engine's own measured floor (256 MB) is what `FISH_VRAM_HEADROOM_MB` in a
`custom` preset can go down to. The owner chooses the preset; the server does
not silently override it.

**Update 2026-11-03 (resources owner): conflict resolved on the server side.**
The balanced VRAM headroom is now max(256 MB, 10 % of the card) plus
min(512 MB, 25 % of what other programs hold) when they hold > 5 %, x preset
factor, clamped 256 MB..50 % (`services/resources/budget.py`
`vram_headroom_bytes`; reasoning in `docs/resources/README.md`). Free VRAM is
measured before the load, so other programs' memory is already excluded; the
headroom only covers their growth. 4 GB card with 0.3 GB used elsewhere:
`FISH_VRAM_HEADROOM_MB=486`, ~3.2 GB usable. The ask (a) / (b) above still
stands for non-NVIDIA cards: Windows AMD/Intel and Apple are identity-only
(registry / CIM / system_profiler) and get no live free-VRAM figure.

### R4. (observation, not a request yet) prefill failure with little free VRAM

While another agent rebuilt the engine (2026-11-03 ~01:00), the then-current
`model/fish/build/fish.dll` failed with `prefill failed` on the 1B when only
~310 MB of VRAM was free (another program held 3.6 GB). `bin/fish.dll`
(2026-11-01) loaded the same model on the CPU tier in the same situation.
It may be an in-progress build; recorded so it can be re-checked: a full
card should mean fewer GPU layers, never a failed generation.
