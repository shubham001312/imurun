#include <fish/engine.h>
#include <unordered_map>

namespace fish {

struct ExpertStreamer::Impl {
    const TensorTable& tensors;
    ResidentWindow& window;
    std::unordered_map<int, std::vector<int>> layer_experts; // layer_idx -> expert_ids

    Impl(const TensorTable& t, ResidentWindow& w) : tensors(t), window(w) {
        for (const auto& tensor : tensors) {
            if (tensor.is_expert && tensor.layer_idx >= 0) {
                layer_experts[tensor.layer_idx].push_back(tensor.expert_id);
            }
        }
    }
};

ExpertStreamer::ExpertStreamer(const TensorTable& tensors, ResidentWindow& window)
    : pimpl_(std::make_unique<Impl>(tensors, window)) {}
ExpertStreamer::~ExpertStreamer() = default;

void ExpertStreamer::ensure_expert(int layer_idx, int expert_id) {
    // Stub: in real impl, use moe_cache_size mechanism + custom buffer
    window.ensure_hot(layer_idx);
}

void ExpertStreamer::hint_next_experts(int layer_idx, const std::vector<int>& expert_ids) {
    // Stub: prefetch these experts
    for (int eid : expert_ids) ensure_expert(layer_idx, eid);
}

} // namespace fish