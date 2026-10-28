# Expert Streamer — `expert_streamer.cpp` / `expert_streamer.h`

## Purpose
**First-class residency for MoE expert tensors.** Extends llama.cpp's `moe_cache_size` mechanism with FISH's three-tier arena, per-expert LRU, and router-logit hints.

## Background: llama.cpp MoE Support
```cpp
// llama.cpp context params
llama_context_params params;
params.moe_cache_size = 512 * 1024 * 1024;  // 512 MB device cache for experts

// Internally: sched_copy_experts() copies expert weights host→device per token
// Limitation: single cache for ALL experts, no layer affinity, no prefetch
```

## FISH Extension: Expert as Resident
```
ExpertStreamer
├── Layer 0: Expert 0..7  (each = separate buffer in ResidentWindow)
├── Layer 1: Expert 0..7
└── ...
```
Each expert tensor = independent promotion/demotion unit.

## Data Structures
```cpp
// Built at construction from TensorTable
unordered_map<int, vector<int>> layer_experts;  // layer_idx → [expert_ids]

// Per-expert residency (mirrors ResidentWindow tiers)
enum ExpertTier { EXPERT_COLD, EXPERT_WARM, EXPERT_HOT };
struct ExpertResidency {
    int layer_idx;
    int expert_id;
    ExpertTier tier = EXPERT_COLD;
    void* device_buffer = nullptr;  // Valid when HOT
    uint64_t last_access_tsc = 0;   // For LRU
};
vector<ExpertResidency> expert_table;
```

## Core Operations

### `ensure_expert(layer_idx, expert_id)`
```cpp
void ExpertStreamer::ensure_expert(int layer_idx, int expert_id) {
    auto& e = expert_table[expert_index(layer_idx, expert_id)];
    if (e.tier == EXPERT_HOT) {
        e.last_access_tsc = rdtsc();
        return;
    }

    // Promote: Cold → Warm (disk/mmap) → Hot (device)
    if (e.tier == EXPERT_COLD) {
        // Async pread into staging (via Prefetcher)
        prefetcher.schedule_expert_prefetch(layer_idx, expert_id);
        // Will callback to mark_warm when done
        return;  // Caller must wait_ready_expert()
    }

    if (e.tier == EXPERT_WARM) {
        // H2D copy via staging ring (same as ResidentWindow)
        window.ensure_expert_hot(layer_idx, expert_id);
        e.tier = EXPERT_HOT;
        e.last_access_tsc = rdtsc();
    }
}
```

### `hint_next_experts(layer_idx, expert_ids)`
```cpp
void ExpertStreamer::hint_next_experts(int layer_idx, const vector<int>& expert_ids) {
    // Called from router logits (if available) or attention pattern
    for (int eid : expert_ids) {
        auto& e = expert_table[expert_index(layer_idx, eid)];
        if (e.tier == EXPERT_COLD) {
            prefetcher.schedule_expert_prefetch(layer_idx, eid);
        }
    }
}
```

## Integration with Prefetcher
```cpp
// Prefetcher job extended:
struct PrefetchJob {
    vector<int> layer_indices;
    vector<pair<int,int>> expert_indices;  // (layer, expert)
    // ...
};

// In Prefetcher::run():
for (auto [layer, expert] : job.expert_indices) {
    // Read expert tensor from disk into staging
    // Mark ready → ExpertStreamer::on_expert_prefetch_done()
}
```

## Router Logit Integration (Future)
```cpp
// In MoE forward pass (ggml):
// router_logits = linear(input)  // [batch, num_experts]
// top_k = argmax(router_logits, k)
// expert_streamer.hint_next_experts(current_layer, top_k);
```
**Currently**: Not hooked — needs `cb_eval` or ggml graph modification to expose logits.

## Eviction Policy
- **LRU per layer** — each layer has its own expert LRU
- **Max hot experts per layer** = `moe_cache_size / expert_size`
- **Demotion** triggered by `ResidentWindow` VRAM pressure callback

## Connection to ResidentWindow
```cpp
// ResidentWindow extended:
void ResidentWindow::ensure_expert_hot(int layer, int expert) {
    // Same staging ring, same H2D path as layer promotion
    // Expert buffer allocated from same VRAM arena
}
```
**Shared VRAM budget** — experts compete with dense layers for Hot tier space.

## Invariants
1. `expert_table` size = total expert tensors in model (fixed at startup)
2. `ensure_expert` **never allocates** — buffers pre-allocated or reused
3. `hint_next_experts` is **best-effort** — no guarantee of completion before use
4. Expert residency **independent** of dense layer residency
5. Total expert VRAM ≤ `moe_cache_size` (configurable)

## Testing
```cpp
TEST(ExpertStreamer, BasicPromotion) {
    ExpertStreamer es(tensors, window);
    // Cold expert
    es.ensure_expert(0, 3);
    // Should trigger prefetch, then H2D
    // Verify buffer populated
}

TEST(ExpertStreamer, HintReducesLatency) {
    // With hint: ensure_expert returns faster (already warm)
    // Without hint: full Cold→Hot path
}

TEST(ExpertStreamer, LRUEviction) {
    // Fill hot experts beyond budget
    // Verify LRU expert demoted
}
```

## Limitations (Current)
1. **No router logit hook** — `hint_next_experts` unused
2. **Single expert size assumption** — all experts same size (mostly true)
3. **No expert merging** — duplicate experts across layers not deduped
4. **Windows GPU path untested** — needs CUDA graph capture for H2D

## Future
- Router logit exposure via `cb_eval` or custom ggml op
- Expert deduplication (shared weights across layers)
- Dynamic `moe_cache_size` adjustment based on VRAM pressure
- Expert quantization (Q4 for cold, Q8 for hot)