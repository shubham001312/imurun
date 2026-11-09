# Resource governor: bugs fixed

Symptom -> cause -> fix -> where.

## 2026-11-03

1. **Desktop freezes while IMURUN answers.** -> The engine runs inside the
   server process at normal priority; its threads competed with the user's
   foreground app on equal terms, and the dynamic thread target cannot reach
   the engine's fixed pool yet (engine R1). -> The process drops to
   BELOW_NORMAL for the duration of every reply / voice request / tuning
   step and is restored after. -> `services/resources/priority.py:87`
   (`PriorityGuard`), `services/resources/governor.py:351` (`before_generate`).

2. **Headroom under 20 % on 9-16 core machines.** -> `round(12 x 0.20)` = 2
   free cores = 17 %; the share was also the same for laptops and desktops.
   -> Device-class table plus a floor of ceil(20 % x preset factor) cores.
   -> `services/resources/budget.py:106` (`cpu_share`), `:274`.

3. **8B could not stay resident on a 4 GB card.** -> Flat 25 % VRAM headroom
   (1 GB) on top of a budget that already excludes other programs' memory.
   -> `max(256 MB, 10 %)` + growth term for others' use, clamped.
   -> `services/resources/budget.py:127` (`vram_headroom_bytes`).

4. **"GPU not detected" on machines without nvidia-smi.** -> nvidia-smi was
   the only source; `machine.gpu` was empty for AMD / Intel / NVIDIA without
   the tool on PATH. -> Fallbacks: sysfs / rocm-smi, Windows registry then
   CIM, macOS system_profiler; engine placement merged after load.
   -> `services/resources/counters.py:601` (`detect_gpu`), `:568`
   (`gpu_identity`), `services/resources/governor.py:606` (`gpu_view`).

5. **After a reload, VRAM "used by others" counted IMURUN's own model.**
   -> nvidia-smi `used` includes our process. -> `measure(own_vram=)`
   subtracts the engine's reported `gpu_bytes`; the cache is cleared at
   `load_env` because a load starts unloaded.
   -> `services/resources/governor.py:95`, `:293`.

6. **The panel showed no placement until the next heartbeat after a load.**
   -> Events fired only on governor state changes. -> `note_engine` (called
   after every load) and sampler-detected placement changes publish at once.
   -> `services/resources/governor.py:315`, `:329`.

7. **Sampler cost CPU with nobody watching.** -> The view was built and
   nvidia-smi spawned every 6 s regardless of listeners. -> Listener count
   from the event broker gates the view and GPU polling.
   -> `services/resources/governor.py:477`, `:505`.
