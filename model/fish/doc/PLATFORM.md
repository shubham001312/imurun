# Platform Abstraction Layer — `platform.h` / `platform.cpp`

## Purpose
**Zero-overhead OS/hardware primitives.** Every function maps to 1-2 CPU instructions or a single syscall. No heap allocation, no locks, no exceptions. This is the *ground floor* — everything above depends on it.

## Design Principles
1. **No abstraction penalty** — `FISH_FORCE_INLINE` on all hot functions
2. **Explicit control** — caller chooses huge pages, NUMA node, pinning, zeroing
3. **Lock-free by default** — MPSC/SPSC rings for all cross-thread communication
4. **Hardware counters built-in** — PMU sampling with zero overhead

## Memory Allocation — `alloc_aligned` / `free_aligned`

### Signature
```cpp
void* alloc_aligned(size_t size, size_t alignment,
                    AllocFlags flags = AllocFlags::NONE, int numa_node = -1) noexcept;
void  free_aligned(void* ptr, size_t size) noexcept;
```

### AllocFlags (bitmask)
| Flag | Effect | When to Use |
|------|--------|-------------|
| `HUGE_2M` | 2 MB pages (TLB reach 512×) | VRAM arena, staging ring, prefetch buffers |
| `HUGE_1G` | 1 GB pages (Linux only) | Massive models (>100 GB) on hugetlbfs |
| `NUMA_LOCAL` | Allocate on current thread's NUMA node | All allocations in NUMA systems |
| `NUMA_SPECIFIC` | Use `numa_node` parameter | Cross-node placement (rare) |
| `PINNED` | `mlock` / `VirtualLock` — never swapped | Staging ring, hot buffers |
| `UNINITIALIZED` | `MADV_FREE` / `MEM_RESET` — skip zeroing | Temporary buffers, known-overwritten |
| `EXECUTABLE` | `PROT_EXEC` / `PAGE_EXECUTE_READWRITE` | JIT code (not used in FISH) |

### Alignment Rules
- Minimum alignment = page size (4 KB, 2 MB, or 1 GB depending on flags)
- Returned pointer guaranteed aligned to `alignment` (power of 2)
- Size rounded up to page boundary internally

### NUMA Awareness
```cpp
// Correct pattern: pin thread first, then allocate
pin_thread_to_core(core_id);
void* buf = alloc_aligned(256*MB, 2*MB, HUGE_2M | PINNED | NUMA_LOCAL);
```
**Never allocate before pinning** — otherwise pages land on wrong node.

### Windows vs Linux Implementation
| Feature | Windows | Linux |
|---------|---------|-------|
| Huge pages | `MEM_LARGE_PAGES` (needs SeLockMemoryPrivilege) | `MAP_HUGETLB` + `MAP_HUGE_SHIFT` |
| NUMA | `VirtualAllocExNuma` (Win10 1803+) | `mbind` + `numa_allocate_nodemask` |
| Pinning | `VirtualLock` | `mlock` |
| Uninitialized | `MEM_RESET` | `MADV_FREE` |

## Timing — `rdtsc` / `monotonic_ns` / `tscto_ns`

### `rdtsc()` → raw TSC (x86) or CNTVCT (ARM64)
- **No serialization** — caller must `lfence`/`dsb` if ordering matters
- Used for: intra-function latency, PMU correlation

### `monotonic_ns()` → calibrated nanoseconds
- Windows: `QueryPerformanceCounter`
- Linux: `clock_gettime(CLOCK_MONOTONIC_RAW)`
- **Monotonic, not wall-clock** — safe for intervals

### `tscto_ns(tsc_delta)` → convert TSC → ns
- Calibrates once at first call (10 ms sleep)
- Thread-safe (atomic init)
- Used by `PmuSession` to timestamp counter snapshots

## PMU (Performance Monitoring Unit) — `PmuSession`

### Purpose
Zero-overhead hardware counter sampling. No `perf` tool, no kernel module — pure `perf_event_open` (Linux) or stub (Windows).

### Counters Collected
```cpp
struct PmuCounters {
    uint64_t cycles;              // CPU cycles
    uint64_t instructions;        // Retired instructions
    uint64_t l1_misses;           // L1 data cache misses
    uint64_t llc_misses;          // Last-level cache misses
    uint64_t branch_mispredicts;  // Branch mispredictions
    uint64_t memory_bytes;        // Estimated: LLC misses × 64 B
};
```

### Usage
```cpp
PmuSession pmu;                    // Opens perf_event fds (Linux)
auto c0 = pmu.snapshot();
// ... hot code ...
auto c1 = pmu.snapshot();
double ipc = double(c1.instructions - c0.instructions) / (c1.cycles - c0.cycles);
double llc_miss_rate = double(c1.llc_misses - c0.llc_misses) / (c1.instructions - c0.instructions);
```

### Overhead
- **~50 ns per snapshot** (single `read` syscall on Linux)
- Safe to call at 100 Hz from stats thread

## Lock-Free Rings — `MpscRing` / `SpscRing`

### Template Parameters
```cpp
template<typename T, size_t CAPACITY>
requires ((CAPACITY & (CAPACITY - 1)) == 0)  // power of 2
```

### MPSC — Multi-Producer, Single-Consumer
- **Producers**: `try_push()` — returns `false` if full
- **Consumer**: `try_pop()` — returns `false` if empty
- **Memory order**: `release` on push, `acquire` on pop
- **Cache-line padded** head/tail to avoid false sharing

### SPSC — Single-Producer, Single-Consumer
- Same API, lower latency (no atomic RMW on producer side)
- Used for: Prefetcher → Compute hot path

### Capacity Sizing
| Ring | Capacity | Rationale |
|------|----------|-----------|
| Prefetch jobs | 64 | Max in-flight layers |
| Prefetch completions | 64 | One per job |
| Token stream | 1024 | Burst tolerance |
| Stats samples | 256 | 10 Hz × 25 s window |

## Connection to Other Modules

```
platform.h
    │
    ├─▶ hardware_probe.cpp     uses: alloc_aligned (for temp buffers), pin_thread_to_core
    ├─▶ gguf_inspect.cpp       uses: alloc_aligned (mmap alternative)
    ├─▶ resident_window.cpp    uses: alloc_aligned (VRAM arena, staging ring)
    ├─▶ prefetcher.cpp         uses: SpscRing<PrefetchJob>, SpscRing<Completion>, rdtsc
    ├─▶ expert_streamer.cpp    uses: alloc_aligned (expert buffers)
    ├─▶ engine.cpp             uses: MpscRing<Token>, PmuSession, monotonic_ns
    └─▶ server routes          uses: MpscRing<HttpRequest>
```

## Invariants (must hold, verified in tests)
1. `alloc_aligned` + `free_aligned` with same `size` → no leak
2. `HUGE_2M` allocation address aligned to 2 MB
3. `PINNED` memory never appears in swap (checked via `/proc/PID/smaps` or `GetProcessMemoryInfo`)
4. `MpscRing::try_push` + `try_pop` → FIFO order, no ABA (single consumer)
5. `PmuSession::snapshot()` delta cycles > 0 for any non-empty interval

## Testing
```bash
# Unit tests in test/test_platform.cpp
./build/fish_test --gtest_filter=Platform*
```

Tests cover:
- Allocation alignment & flags
- NUMA placement verification
- Ring buffer correctness under contention
- PMU counter monotonicity
- TSC ↔ ns calibration accuracy