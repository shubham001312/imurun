# Layer Planner — `layer_planner.cpp`

## Purpose
**Converts `HardwareCaps` + `TensorTable` → `LayerPlan`**. Decides which layers live in VRAM (Hot), CPU RAM (Warm), or disk (Cold). Single source of truth for residency policy.

## Input
```cpp
HardwareCaps caps;          // From probe_hardware()
TensorTable tensors;        // From inspect_gguf()
size_t user_vram_limit;     // 0 = auto (from caps.vram_bytes)
```

## Output: `LayerPlan`
```cpp
struct LayerPlan {
    std::vector<int> hot_layers;       // Resident in VRAM (or CPU RAM if no GPU)
    std::vector<int> warm_layers;      // mmap-backed, next-in-line
    std::vector<int> cold_layers;      // Disk only, streamed on demand
    size_t vram_budget_bytes = 0;      // Hard limit for Hot tier
    size_t staging_ring_bytes = 0;     // Pinned host memory for async H2D
    int staging_slots = 4;             // Ring buffer depth
};
```

## Algorithm (Current: Greedy Front-Loading)

### 1. VRAM Budget Calculation
```cpp
size_t vram_budget = user_vram_limit;
if (vram_budget == 0) {
    if (caps.vram_bytes > 0) {
        vram_budget = (caps.vram_bytes > 512_MB) ?
                      caps.vram_bytes - 512_MB : caps.vram_bytes / 2;
    } else {
        vram_budget = caps.ram_bytes / 4;  // CPU-only: 25% RAM
    }
}
plan.vram_budget_bytes = vram_budget;
```

### 2. Per-Layer Size Aggregation
```cpp
unordered_map<int, size_t> layer_size, layer_expert_size;
for (auto& t : tensors) if (t.layer_idx >= 0) {
    if (t.is_expert) layer_expert_size[t.layer_idx] += t.size;
    else layer_size[t.layer_idx] += t.size;
}
```

### 3. Greedy Fill (First Layers First)
```cpp
size_t used = 0;
for (int idx : sorted_layer_indices) {
    size_t sz = layer_size[idx] + layer_expert_size[idx];
    if (used + sz <= vram_budget) {
        plan.hot_layers.push_back(idx);
        used += sz;
    } else if (plan.warm_layers.size() < 4) {
        plan.warm_layers.push_back(idx);
    } else {
        plan.cold_layers.push_back(idx);
    }
}
```

### 4. Staging Ring
```cpp
plan.staging_ring_bytes = 256_MB;  // Fixed, tunable via env
plan.staging_slots = 4;
```

## Why Front-Loading?
- **Embedding + early layers** reused across all tokens (prompt + generation)
- **Attention in early layers** has highest reuse (KV cache)
- **KV cache** for Hot layers stays in VRAM → no recompute
- **Cold layers** only touched during prefill of deep layers

## Alternative Policies (Future)
| Policy | When Better |
|--------|-------------|
| **Sliding Window** | Long context, streaming generation (keep last N hot) |
| **Importance-Weighted** | With imatrix: keep high-salience layers hot |
| **Profile-Guided** | Runtime access counters → dynamic promotion |
| **MoE-Aware** | Keep router + top-K experts hot |

## Connection to ResidentWindow
```cpp
ResidentWindow window(plan);
// window internally:
//   - Allocates VRAM buffers for hot_layers
//   - Maps warm_layers via mmap (handled by Prefetcher)
//   - Tracks LRU within hot_layers for eviction
```

## Invariants
1. `hot ∪ warm ∪ cold = all layer indices` (partition)
2. `Σ hot_layer_sizes ≤ vram_budget_bytes`
3. `warm_layers.size() ≤ 8` (configurable)
4. Layer -1 (embedding) and output layer **always hot** (enforced by caller)
5. Deterministic for same inputs (no randomness)

## Testing
```cpp
TEST(LayerPlanner, VRAMBudgetRespected) {
    HardwareCaps caps{.vram_bytes = 4_GB, .ram_bytes = 16_GB};
    TensorTable t = make_32_layer_model(1_MB_per_layer); // 32 MB total
    LayerPlan plan = make_layer_plan(caps, t, 2_GB);     // 2 GB limit
    size_t hot_bytes = 0;
    for (int idx : plan.hot_layers) hot_bytes += layer_size[idx];
    EXPECT_LE(hot_bytes, 2_GB);
}

TEST(LayerPlanner, CPUMode) {
    HardwareCaps caps{.vram_bytes = 0, .ram_bytes = 8_GB};
    LayerPlan plan = make_layer_plan(caps, tensors, 0);
    // Hot tier uses CPU RAM; staging ring still allocated
    EXPECT_GT(plan.vram_budget_bytes, 0);
}
```

## Tuning Knobs (Environment Variables)
| Variable | Default | Effect |
|----------|---------|--------|
| `FISH_VRAM_HEADROOM_MB` | 512 | VRAM reserved for OS/display |
| `FISH_WARM_LAYER_COUNT` | 4 | Max layers in Warm tier |
| `FISH_STAGING_MB` | 256 | Staging ring total size |
| `FISH_STAGING_SLOTS` | 4 | Ring depth |

## Future: Dynamic Re-Planning
```cpp
// At runtime, if access pattern changes:
LayerPlan new_plan = make_layer_plan(caps, tensors, vram_budget, access_counters);
window.replan(new_plan);  // Moves buffers, updates LRU
```
Requires `ResidentWindow::replan()` implementation.