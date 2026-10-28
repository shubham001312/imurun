#include <fish/engine.h>
#include <vector>

namespace fish {

struct AdapterMgr::Impl {
    AdapterSet active_set;
};

AdapterMgr::AdapterMgr() : pimpl_(std::make_unique<Impl>()) {}
AdapterMgr::~AdapterMgr() = default;

void AdapterMgr::activate(const AdapterSet& adapters) {
    pimpl_->active_set = adapters;
    // In real impl: call llama_set_adapters_lora / llama_set_adapter_cvec
    // and trigger scheduler re-reserve
}
void AdapterMgr::deactivate() {
    pimpl_->active_set = {};
}
AdapterSet AdapterMgr::active() const {
    return pimpl_->active_set;
}

} // namespace fish