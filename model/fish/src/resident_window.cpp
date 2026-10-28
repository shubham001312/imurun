#include <fish/engine.h>
#include <unordered_map>
#include <vector>

namespace fish {

struct ResidentWindow::Impl {
    LayerPlan plan;
    std::unordered_map<int, void*> layer_buffers; // layer_idx -> ggml_backend_buffer_t
    std::vector<int> lru_list; // hot layers in LRU order (front = most recent)

    Impl(const LayerPlan& p) : plan(p) {}
};

ResidentWindow::ResidentWindow(const LayerPlan& plan) : pimpl_(std::make_unique<Impl>(plan)) {}
ResidentWindow::~ResidentWindow() = default;

void ResidentWindow::ensure_hot(int layer_idx) {
    // Stub: in real impl, allocate ggml buffer, copy from warm/cold, update LRU
    if (pimpl_->layer_buffers.find(layer_idx) == pimpl_->layer_buffers.end()) {
        pimpl_->layer_buffers[layer_idx] = nullptr; // placeholder
    }
    // Move to front of LRU
    auto it = std::find(pimpl_->lru_list.begin(), pimpl_->lru_list.end(), layer_idx);
    if (it != pimpl_->lru_list.end()) pimpl_->lru_list.erase(it);
    pimpl_->lru_list.insert(pimpl_->lru_list.begin(), layer_idx);
}

void ResidentWindow::demote(int layer_idx) {
    // Stub: release VRAM buffer, keep CPU copy
    pimpl_->layer_buffers.erase(layer_idx);
    auto it = std::find(pimpl_->lru_list.begin(), pimpl_->lru_list.end(), layer_idx);
    if (it != pimpl_->lru_list.end()) pimpl_->lru_list.erase(it);
}

void* ResidentWindow::get_layer_buffer(int layer_idx) const {
    auto it = pimpl_->layer_buffers.find(layer_idx);
    return (it != pimpl_->layer_buffers.end()) ? it->second : nullptr;
}

std::vector<ResidentWindow::Residency> ResidentWindow::snapshot() const {
    std::vector<Residency> out;
    for (int idx : pimpl_->hot_layers) {
        out.push_back({idx, Residency::HOT, 0, 0});
    }
    for (int idx : pimpl_->warm_layers) {
        out.push_back({idx, Residency::WARM, 0, 0});
    }
    for (int idx : pimpl_->cold_layers) {
        out.push_back({idx, Residency::COLD, 0, 0});
    }
    return out;
}

} // namespace fish