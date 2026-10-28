#include <fish/engine.h>
#include <algorithm>
#include <numeric>
#include <unordered_map>

namespace fish {

LayerPlan make_layer_plan(const HardwareCaps& caps,
                          const TensorTable& tensors,
                          size_t user_vram_limit_bytes) {
    LayerPlan plan;

    // Determine VRAM budget
    size_t vram_budget = user_vram_limit_bytes;
    if (vram_budget == 0) {
        if (caps.vram_bytes > 0) {
            // Leave 512 MB headroom for OS / display
            vram_budget = (caps.vram_bytes > 512 * 1024 * 1024) ?
                          caps.vram_bytes - 512 * 1024 * 1024 : caps.vram_bytes / 2;
        } else {
            // No GPU: use CPU RAM as "VRAM" (hot tier = RAM)
            vram_budget = caps.ram_bytes / 4; // Conservative
        }
    }
    plan.vram_budget_bytes = vram_budget;

    // Staging ring: 256 MB default, 4 slots
    plan.staging_ring_bytes = 256 * 1024 * 1024;
    plan.staging_slots = 4;

    // Group tensors by layer
    std::unordered_map<int, size_t> layer_size;
    std::unordered_map<int, size_t> layer_expert_size;
    for (const auto& t : tensors) {
        if (t.layer_idx >= 0) {
            if (t.is_expert) {
                layer_expert_size[t.layer_idx] += t.size;
            } else {
                layer_size[t.layer_idx] += t.size;
            }
        }
    }

    // Sort layers by index
    std::vector<int> layer_indices;
    for (const auto& kv : layer_size) layer_indices.push_back(kv.first);
    std::sort(layer_indices.begin(), layer_indices.end());

    // Greedy: fill hot from first layers (embedding + early layers are most reused)
    // But also consider: output layer and final norm are always hot
    size_t used = 0;
    for (int idx : layer_indices) {
        size_t sz = layer_size[idx] + layer_expert_size[idx];
        if (used + sz <= vram_budget) {
            plan.hot_layers.push_back(idx);
            used += sz;
        } else {
            // Remaining go to warm (mmap) or cold
            // Heuristic: next 4 layers warm, rest cold
            if (plan.warm_layers.size() < 4) {
                plan.warm_layers.push_back(idx);
            } else {
                plan.cold_layers.push_back(idx);
            }
        }
    }

    // Always keep embedding (layer -1) and output (if detected) in hot
    // This is handled by the caller ensuring layer_idx=-1 tensors are in hot

    return plan;
}

} // namespace fish