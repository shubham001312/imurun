# Prefetcher — `prefetcher.cpp` / `prefetcher.h`

## Purpose
**Asynchronous Cold→Warm→Hot promotion at `process_ubatch` boundary.** Runs in dedicated background thread. Overlaps disk I/O and H2D copies with compute.

## Architecture
```
Main Thread (Compute)                    Prefetcher Thread
────────────────────────────            ────────────────────
process_ubatch(n)                        run()
    │
    ├─ schedule_prefetch({n+1, n+2}) ───► push to job queue
    │                                       │
    ├─ llama_decode()                     ├─ pop job
    │                                       ├─ for each layer:
    │                                       │   acquire staging slot
    │                                       │   pread(tensor ranges) → slot
    │                                       │   mark_ready(layer, slot)
    │                                       │
    ├─ wait_ready(n+1) ◄──────────────────┤ (blocks if not ready)
    │
    ▼
process_ubatch(n+1)
```

## Job Queue: Lock-Free MPSC Ring
```cpp
// In Prefetcher::Impl
MpscRing<PrefetchJob, 64> job_queue;      // Main → Prefetcher
SpscRing<Completion, 64> completion_queue; // Prefetcher → Main (per layer)

struct PrefetchJob {
    std::vector<int> layer_indices;  // Layers to prefetch
    uint64_t deadline_tsc;           // Optional: priority by deadline
};

struct Completion {
    int layer_idx;
    int staging_slot;
    bool success;
};
```

## Prefetch Algorithm
```cpp
void Prefetcher::run() {
    while (!stop) {
        PrefetchJob job;
        if (!job_queue.try_pop(job)) {
            std::this_thread::sleep_for(100us);  // Backoff
            continue;
        }

        for (int layer_idx : job.layer_indices) {
            // 1. Check if already hot (skip)
            if (window.is_hot(layer_idx)) continue;

            // 2. Acquire staging slot (spin-wait, typically instant)
            int slot = window.acquire_staging_slot();

            // 3. Read tensor data from disk/mmap into staging slot
            //    Uses tensor offset/size from TensorTable
            for (const TensorDesc& t : tensors_for_layer(layer_idx)) {
                if (t.is_expert) continue;  // Handled by ExpertStreamer
                pread(model_fd, staging_ptr(slot) + t.offset_in_layer,
                      t.size, t.offset_in_file);
            }

            // 4. Signal completion
            window.mark_staging_ready(layer_idx, slot);
            completion_queue.try_push({layer_idx, slot, true});
        }
    }
}
```

## Disk I/O Path
| Tier | Read Path |
|------|-----------|
| **Cold (disk)** | `pread` / `ReadFile` with `FILE_FLAG_NO_BUFFERING` (direct I/O) |
| **Warm (mmap)** | `memcpy` from mmap region (OS page cache) |
| **Direct I/O** | Aligned to 4 KB, size multiple of 4 KB |

**Windows**: `ReadFile` with `FILE_FLAG_NO_BUFFERING` + `OVERLAPPED` for true async
**Linux**: `io_uring` (preferred) or `pread` + thread pool

## Integration with `process_ubatch`
```cpp
// In Engine::next_token() / generation loop:
void Engine::generate_loop() {
    while (!done) {
        // 1. Tell prefetcher what we need NEXT
        prefetcher.schedule_prefetch(next_layers_for_ubatch());

        // 2. Ensure current layers are hot (blocks if promotion needed)
        for (int idx : current_layers) window.ensure_hot(idx);

        // 3. Compute
        llama_decode(ctx, batch);

        // 4. Before next ubatch, wait for at least one next layer
        if (!next_layers.empty()) {
            prefetcher.wait_ready(next_layers[0]);
        }
    }
}
```

## Prefetch Horizon
| Model Size | Horizon (layers ahead) | Rationale |
|------------|------------------------|-----------|
| 7B (32 layers) | 2 | 2 layers fit in staging ring |
| 70B (80 layers) | 4 | More layers, more time to prefetch |
| MoE | 2 + experts | ExpertStreamer handles experts |

**Adaptive horizon**: Increase if `wait_ready` frequently blocks; decrease if staging slots starved.

## Cancellation
```cpp
void Prefetcher::cancel_all() {
    job_queue.clear();           // Drop pending jobs
    window.release_all_staging_slots();  // Return slots to free pool
}
```
Called on `Engine::cancel(handle)` or model unload.

## Metrics (Exported via EngineStats)
```cpp
struct PrefetchStats {
    uint64_t jobs_submitted = 0;
    uint64_t jobs_completed = 0;
    uint64_t jobs_failed = 0;
    uint64_t bytes_prefetched = 0;
    uint64_t wait_ready_blocked_us = 0;  // Time main thread blocked
    double avg_prefetch_latency_us = 0;
};
```

## Invariants
1. **Never blocks main thread** except in `wait_ready()` (explicit sync point)
2. **Staging slots bounded** — max `plan.staging_slots` in flight
3. **No duplicate work** — `is_hot()` check before issuing I/O
4. **Graceful shutdown** — `cancel_all()` + thread join in destructor
5. **Priority by deadline** — urgent layers (next ubatch) first

## Testing
```cpp
TEST(Prefetcher, OverlapsIOWithCompute) {
    // Mock slow disk (10 ms pread)
    // Measure: compute time + prefetch time < compute time + 10 ms
    // i.e., prefetch fully overlapped
}

TEST(Prefetcher, WaitReadyBlocksOnlyWhenNeeded) {
    // Prefetch layer 5
    // wait_ready(5) returns immediately (already in staging)
    // wait_ready(6) blocks until prefetch completes
}

TEST(Prefetcher, Cancellation) {
    // Submit 100 jobs, cancel_all(), verify thread exits in < 10 ms
}
```

## Future: Predictive Prefetch
```cpp
// Use attention pattern / router logits to predict next layers
void Prefetcher::hint_next_layers(const float* router_logits, int top_k) {
    // Convert logits → layer indices → schedule_prefetch
}
```
Requires exposure of router logits from MoE models.