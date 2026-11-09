# About the files in this folder

`ARCHITECTURE.md`, `ADAPTER_MGR.md`, `COMPRESSION_POLICY.md`, `ENGINE_API.md`,
`EXPERT_STREAMER.md`, `GGUF_INSPECT.md`, `HARDWARE_PROBE.md`, `LAYER_PLANNER.md`,
`PLATFORM.md`, `PREFETCHER.md` and `RESIDENT_WINDOW.md` are **design notes for a
planned tiered-residency engine** (VRAM arena, staging ring, expert cache,
LoRA hot-swap). The matching `src/*.cpp` skeletons are deliberately **not
compiled** (see `CMakeLists.txt`). Nothing in them describes the engine that
ships.

The engine as built is documented in:

- `docs/engine/ARCHITECTURE.md` — source map, data flow, kernels, streaming, ABI
- `docs/engine/ARCHITECTURES.md` — supported model families and quant types
- `docs/engine/BENCHMARKS.md` — measured numbers
- `docs/engine/CHANGELOG.md` — changes and bugs fixed

Keep these notes if the tiered design is pursued (see `docs/ROADMAP.md`),
otherwise they can be deleted.
