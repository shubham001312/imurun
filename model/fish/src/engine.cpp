#include <fish/engine.h>
#include <memory>
#include <atomic>

namespace fish {

struct Engine::Impl {
    std::string model_path;
    LayerPlan plan;
    HardwareCaps caps;
    std::unique_ptr<ResidentWindow> window;
    std::unique_ptr<Prefetcher> prefetcher;
    std::unique_ptr<ExpertStreamer> expert_streamer;
    AdapterMgr adapter_mgr;
    EngineStats stats;
    std::atomic<uint64_t> next_handle{1};
    std::atomic<bool> cancelled{false};

    Impl(const std::string& mp, const LayerPlan& p, const HardwareCaps& c)
        : model_path(mp), plan(p), caps(c),
          window(std::make_unique<ResidentWindow>(p)),
          prefetcher(std::make_unique<Prefetcher>(inspect_gguf(mp), *window)),
          expert_streamer(std::make_unique<ExpertStreamer>(inspect_gguf(mp), *window)) {
        stats.start_time = std::chrono::steady_clock::now();
    }
};

class EngineReal : public Engine {
    std::unique_ptr<Impl> pimpl_;
public:
    EngineReal(const std::string& model_path, const LayerPlan& plan, const HardwareCaps& caps)
        : pimpl_(std::make_unique<Impl>(model_path, plan, caps)) {}

    GenerateHandle start_gen(const Prompt& prompt, const SampleParams& params) override {
        // Stub: in real impl, tokenize, build batch, start llama_decode loop in background
        return pimpl_->next_handle++;
    }

    int next_token(GenerateHandle handle, int32_t& out_token) override {
        // Stub: return 0 (end of stream) immediately
        return 0;
    }

    void cancel(GenerateHandle handle) override {
        pimpl_->cancelled = true;
    }

    EngineStats stats() const override {
        return pimpl_->stats;
    }

    void set_adapters(const AdapterSet& adapters) override {
        pimpl_->adapter_mgr.activate(adapters);
    }

    void clear_adapters() override {
        pimpl_->adapter_mgr.deactivate();
    }

    int n_layers() const override { return 32; } // from plan
    int n_ctx() const override { return 8192; }
    size_t vocab_size() const override { return 128000; }
};

std::unique_ptr<Engine> Engine::create(const std::string& model_path,
                                       const LayerPlan& plan,
                                       const HardwareCaps& caps) {
    return std::make_unique<EngineReal>(model_path, plan, caps);
}

} // namespace fish