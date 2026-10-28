# Adapter Manager — `adapter_mgr.cpp` / `adapter_mgr.h`

## Purpose
**LoRA + Control Vector hot-swap at request boundaries.** Serializes per-adapter batches (llama.cpp requirement). Adds Control Vector route missing from llama.cpp server.

## Adapter Types
```cpp
struct LoRAAdapter {
    string id;        // User-facing name
    string path;      // .gguf LoRA file (base model + adapters)
    float scale = 1.0f;
};

struct ControlVector {
    string id;
    string path;      // .gguf or .bin (ggml tensor)
    float scale = 1.0f;
    int layer_start = 0;   // Inclusive
    int layer_end = -1;    // -1 = all layers
};

struct AdapterSet {
    vector<LoRAAdapter> loras;
    vector<ControlVector> cvecs;
};
```

## llama.cpp Constraints (Must Obey)
1. **Per-context, not per-sequence** — `llama_set_adapters_lora(ctx, ...)` affects entire context
2. **Scheduler re-reserve on change** — `sched_need_reserve = true` forces graph rebuild
3. **Batch equality required** — `can_split()` refuses to co-batch requests with different adapter sets
4. **LoRA loading** — `llama_adapter_lora_init(model, path)` → returns `llama_adapter_lora*`
5. **Control vectors** — No native support; must inject via `cb_eval` or custom op

## FISH Implementation

### LoRA: Wrapper Around llama.cpp
```cpp
void AdapterMgr::activate(const AdapterSet& adapters) {
    // 1. Load LoRAs if not cached
    for (auto& lora : adapters.loras) {
        if (!lora_cache.contains(lora.id)) {
            lora_cache[lora.id] = llama_adapter_lora_init(model, lora.path.c_str());
        }
    }

    // 2. Build llama adapter array
    vector<llama_adapter_lora*> lptrs;
    vector<float> scales;
    for (auto& lora : adapters.loras) {
        lptrs.push_back(lora_cache[lora.id]);
        scales.push_back(lora.scale);
    }

    // 3. Activate (triggers sched_need_reserve)
    llama_set_adapters_lora(ctx, lptrs.data(), lptrs.size(), scales.data());

    // 4. Control vectors (custom)
    activate_control_vectors(adapters.cvecs);

    active_set = adapters;
}
```

### Control Vectors: `cb_eval` Injection
```cpp
// In Engine::generate_loop(), before llama_decode:
ctx.cb_eval = [](ggml_cgraph*, int node, void* user_data) {
    auto& cvecs = *static_cast<vector<ControlVector>*>(user_data);
    if (ggml_op_is_linear(node)) {
        // Add control vector to input
        ggml_tensor* input = node->src[0];
        for (auto& cv : cvecs) {
            if (node_layer(node) >= cv.layer_start &&
                (cv.layer_end == -1 || node_layer(node) <= cv.layer_end)) {
                // cvec_add(input, cv.tensor, cv.scale);
            }
        }
    }
    return true;  // Continue
};
ctx.cb_eval_user_data = &active_cvecs;
```
**Zero model reload** — injected at graph eval time.

## Request Boundary Serialization
```
Timeline:
Request A (LoRA "style") → activate(LoRA["style"]) → generate batch A1, A2, A3
Request B (no LoRA)       → activate(empty)       → generate batch B1
Request C (LoRA "code")   → activate(LoRA["code"]) → generate batch C1, C2

Scheduler batches:
  Batch 1: A1, A2, A3   (same adapters)
  Batch 2: B1           (different → separate batch)
  Batch 3: C1, C2       (different → separate batch)
```
**Throughput cost**: ~10-20% for frequent adapter switches (scheduler rebuild).

## Adapter Cache
```cpp
unordered_map<string, llama_adapter_lora*> lora_cache;
// LRU eviction when > 32 adapters loaded
// Each LoRA ~10-50 MB VRAM (rank 64, 7B model)
```

## API Surface
```cpp
class AdapterMgr {
    void activate(const AdapterSet&);   // Call before generation
    void deactivate();                  // Clear all
    AdapterSet active() const;          // For stats/debug
};
```

## Connection to Engine
```cpp
// Engine::start_gen():
GenerateHandle Engine::start_gen(Prompt, SampleParams) {
    // AdapterSet passed via Engine::set_adapters() before start_gen
    // or embedded in Prompt (future)
    adapter_mgr.activate(current_adapters);
    // ... build batch, start generation
}

// Engine::set_adapters() — called by server route:
POST /fish/adapters {loras: [...], cvecs: [...]}
```

## Invariants
1. **Single active AdapterSet per context** — no mixing
2. **LoRA cache never exceeds VRAM budget** — evicts LRU
3. **Control vectors applied at `cb_eval`** — no graph rebuild
4. **Deactivate = empty AdapterSet** — not "no change"
5. **Thread-safe** — only called from main generation thread

## Testing
```cpp
TEST(AdapterMgr, LoRAActivateDeactivate) {
    AdapterMgr am;
    am.activate({{lora: {"style", "style.gguf", 1.0}}});
    EXPECT_EQ(am.active().loras.size(), 1);
    am.deactivate();
    EXPECT_EQ(am.active().loras.size(), 0);
}

TEST(AdapterMgr, BatchSerialization) {
    // Simulate server with 3 requests, 2 adapter sets
    // Verify scheduler sees 2 batches, not 1
}

TEST(AdapterMgr, ControlVectorInjection) {
    // Mock cb_eval, verify cvec added to correct layers
}
```

## Future
- **Per-sequence LoRA** — requires llama.cpp modification (context splitting)
- **LoRA merging** — fuse multiple LoRAs into single adapter
- **Quantized LoRA** — Q4_K_M LoRA weights (smaller VRAM)
- **Control vector scheduling** — per-layer enable/disable