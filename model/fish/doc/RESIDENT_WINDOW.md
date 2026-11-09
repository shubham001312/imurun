# Resident Window — `resident_window.cpp` / `resident_window.h`

## Purpose
**Owns the fixed VRAM arena + pinned staging ring.** Zero allocations after construction. Buffer reuse via LRU eviction. Single writer (Prefetcher), multiple readers (compute threads).

## Internal Structure
```
ResidentWindow
├── VRAM Arena (ggml_backend_buffer_t per hot layer)
│   ├── Layer 0: [attn_q, attn_k, attn_v, attn_out, ffn_gate, ffn_up, ffn_down, norms...]
│   ├── Layer 1: [...]
│   └── ...
├── Staging Ring (pinned host memory)
│   ├── Slot 0: 64 MB (async H2D source)
│   ├── Slot 1: 64 MB
│   ├── Slot 2: 64 MB
│   └── Slot 3: 64 MB
└── LRU List (hot layer indices, front = most recent)
```

## Construction
```cpp
ResidentWindow::ResidentWindow(const LayerPlan& plan) {
    // 1. Allocate VRAM arena buffers for each hot layer
    for (int idx : plan.hot_layers) {
        size_t sz = layer_size[idx];  // from TensorTable
        void* buf = ggml_backend_alloc_buffer(backend, sz);
        layer_buffers[idx] = buf;
    }
    // 2. Allocate staging ring (pinned, huge pages)
    for (int i = 0; i < plan.staging_slots; ++i) {
        staging_slots[i] = alloc_aligned(plan.staging_ring_bytes / plan.staging_slots,
                                         2_MB, HUGE_2M | PINNED | NUMA_LOCAL);
    }
    // 3. Initialize LRU with hot_layers (front = most recent = last layer)
    lru_list = plan.hot_layers;  // reverse order: last layer first
}
```

## Hot Path: `ensure_hot(layer_idx)`
```cpp
void ResidentWindow::ensure_hot(int layer_idx) {
    // Fast path: already hot
    if (layer_buffers.contains(layer_idx)) {
        promote_lru(layer_idx);  // Move to front
        return;
    }
    // Slow path: need promotion from Warm or Cold
    // 1. Acquire staging slot (spin-wait on atomic index)
    // 2. Prefetcher has already filled slot with layer data
    // 3. H2D copy: ggml_backend_buffer_set_tensor(dst, src, slot_ptr)
    // 4. Register buffer, update LRU
    // 5. Release staging slot
}
```
**Blocking only if promotion needed.** Prefetcher runs ahead to minimize this.

## Eviction: `demote(layer_idx)`
```cpp
void ResidentWindow::demote(int layer_idx) {
    // Called by Prefetcher when VRAM pressure (not in current impl)
    // 1. D2H copy if Warm tier needs update (rare)
    // 2. ggml_backend_buffer_free(buf)
    // 3. layer_buffers.erase(layer_idx)
    // 4. lru_list.remove(layer_idx)
}
```

## Staging Ring Protocol
```
Producer (Prefetcher)                    Consumer (ensure_hot)
────────────────────                    ────────────────────
1. slot = acquire_slot()                 4. wait for slot_ready[slot]
2. pread(layer_data, staging[slot])  ──► 5. H2D copy from staging[slot]
3. slot_ready[slot].store(true)          6. release_slot(slot)
```
- **Lock-free** via atomic slot index + per-slot `atomic<bool> ready`
- **4 slots** = 3 in-flight + 1 buffer
- **Slot size** = max layer size (rounded to 64 MB)

## Buffer Reuse Guarantees
1. **Same `ggml_backend_buffer_t` reused** for a layer across promotions
2. **No `ggml_backend_buffer_free`/`alloc` on hot path** — only on explicit demote
3. **Staging slots recycled** — no allocation after startup
4. **Tensor pointers stable** while layer is Hot (ggml requires this)

## Thread Safety
| Operation | Thread | Synchronization |
|-----------|--------|-----------------|
| `ensure_hot` | Compute (main) | Mutex per layer (fine-grained) |
| `demote` | Prefetcher | Same mutex |
| `get_layer_buffer` | Compute | Atomic load (pointer stable once hot) |
| Staging slot acquire/release | Both | Atomic ring index |

## Connection to Prefetcher
```cpp
// Prefetcher runs in background thread:
void Prefetcher::run() {
    while (true) {
        auto layers = pop_prefetch_job();
        for (int idx : layers) {
            // Fill staging slot with layer data from disk/mmap
            void* slot = window.acquire_staging_slot();
            pread(tensor_offsets[idx], slot, tensor_sizes[idx]);
            window.mark_staging_ready(idx, slot);
        }
    }
}

// Compute thread:
void compute_ubatch() {
    window.ensure_hot(current_layer);  // Blocks only if not prefetched
    window.ensure_hot(next_layer);     // Overlapped with current compute
    llama_decode(...);
}
```

## Invariants
1. `layer_buffers.size() ≤ plan.hot_layers.size()` at all times
2. `staging_slots` all allocated at construction, never reallocated
3. `get_layer_buffer(idx)` returns non-null **iff** `ensure_hot(idx)` completed
4. LRU order reflects actual access pattern (updated on every `ensure_hot`)
4. No memory leaks: destructor frees all `ggml_backend_buffer_t` + staging slots

## Testing
```cpp
TEST(ResidentWindow, HotPathZeroAlloc) {
    ResidentWindow w(plan);
    // Warm up
    for (int idx : plan.hot_layers) w.ensure_hot(idx);
    // Measure allocations during 1000 ensure_hot calls
    AllocTracker tracker;
    for (int i = 0; i < 1000; ++i) {
        w.ensure_hot(plan.hot_layers[i % plan.hot_layers.size()]);
    }
    EXPECT_EQ(tracker.alloc_count, 0);  // Zero allocations
}

TEST(ResidentWindow, PromotionFromCold) {
    ResidentWindow w(plan_with_cold_layers);
    // Prefetch layer 30 (cold)
    w.prefetch_layer(30);  // Fills staging slot
    // ensure_hot should promote without disk I/O
    auto start = rdtsc();
    w.ensure_hot(30);
    auto cycles = rdtsc() - start;
    EXPECT_LT(cycles, 100000);  // < 100k cycles (H2D only)
}
```

## Future: Async H2D with CUDA Graphs
```cpp
// Instead of synchronous ggml_backend_buffer_set_tensor:
cudaStream_t stream = get_compute_stream();
cudaMemcpyAsync(dst, src, size, cudaMemcpyHostToDevice, stream);
cudaEventRecord(ready_event, stream);
// compute waits on event
```
Requires `ggml_backend_cuda` integration.