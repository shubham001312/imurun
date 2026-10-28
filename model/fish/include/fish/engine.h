#pragma once
/*
 * FISH Engine — Public C++17 API
 * Links llama.cpp (llama-server-impl) and adds the streaming scheduler.
 * No Python, no transformers, no AirLLM runtime.
 */

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>
#include <optional>
#include <functional>
#include <chrono>
#include <memory>
#include <thread>
#include <unordered_map>
#include <atomic>

namespace fish {

// ============================================================================
// 1. Hardware Probe
// ============================================================================
struct DiskSpeed {
    double seq_read_mb_s = 0;
    double seq_write_mb_s = 0;
    double random_read_iops = 0;
};

struct HardwareCaps {
    size_t ram_bytes = 0;
    size_t vram_bytes = 0;          // 0 if no GPU
    int cpu_cores = 0;
    bool has_mmap = false;
    bool has_direct_io = false;
    DiskSpeed disk{};
    std::string gpu_name;
    std::string gpu_driver;
};

HardwareCaps probe_hardware();

// ============================================================================
// 2. GGUF Inspector (read-only, no model load)
// ============================================================================
enum class QuantType : uint8_t {
    UNKNOWN = 0,
    F32, F16, BF16,
    Q4_0, Q4_1, Q5_0, Q5_1, Q8_0,
    Q2_K, Q3_K, Q4_K, Q5_K, Q6_K, Q8_K,
    IQ2_XXS, IQ2_XS, IQ3_XXS, IQ3_S, IQ4_NL, IQ4_XS,
    // add more as needed
};

struct TensorDesc {
    std::string name;
    size_t offset = 0;        // byte offset in GGUF file
    size_t size = 0;          // byte size on disk
    QuantType qtype = QuantType::UNKNOWN;
    int layer_idx = -1;       // -1 = non-layer (embedding, output, norms)
    bool is_expert = false;   // true for MoE expert tensors
    int expert_id = -1;       // if is_expert
};

using TensorTable = std::vector<TensorDesc>;

TensorTable inspect_gguf(const std::string& model_path);

// ============================================================================
// 3. Layer Planner (residency policy)
// ============================================================================
struct LayerPlan {
    std::vector<int> hot_layers;    // resident in VRAM (or CPU RAM if no GPU)
    std::vector<int> warm_layers;   // mmap-backed CPU RAM
    std::vector<int> cold_layers;   // disk only, streamed on demand
    size_t vram_budget_bytes = 0;
    size_t staging_ring_bytes = 0;  // pinned host memory for async H2D
    int staging_slots = 4;
};

LayerPlan make_layer_plan(const HardwareCaps& caps,
                          const TensorTable& tensors,
                          size_t user_vram_limit_bytes = 0); // 0 = auto

// ============================================================================
// 4. Resident Window (fixed VRAM arena + staging ring)
// ============================================================================
class ResidentWindow {
public:
    explicit ResidentWindow(const LayerPlan& plan);
    ~ResidentWindow();

    // Ensure a layer is in Hot tier (blocking if promotion needed)
    void ensure_hot(int layer_idx);

    // Demote a layer to Warm (LRU eviction)
    void demote(int layer_idx);

    // Get ggml backend buffer for a layer's tensors (valid after ensure_hot)
    void* get_layer_buffer(int layer_idx) const;

    // Current residency snapshot for stats/debug
    struct Residency {
        int layer_idx;
        enum Tier { COLD, WARM, HOT } tier;
        size_t vram_bytes = 0;
        size_t ram_bytes = 0;
    };
    std::vector<Residency> snapshot() const;

private:
    struct Impl;
    std::unique_ptr<Impl> pimpl_;
};

// ============================================================================
// 5. Prefetcher (async Cold→Warm→Hot at ubatch boundary)
// ============================================================================
class Prefetcher {
public:
    explicit Prefetcher(const TensorTable& tensors, ResidentWindow& window);
    ~Prefetcher();

    // Call at the start of each ubatch with the *next* layers needed
    void schedule_prefetch(const std::vector<int>& next_layer_indices);

    // Block until a specific layer is ready in at least Warm tier
    void wait_ready(int layer_idx);

    // Cancel all pending ops (e.g. on generation cancel)
    void cancel_all();

private:
    struct Impl;
    std::unique_ptr<Impl> pimpl_;
};

// ============================================================================
// 6. Expert Streamer (MoE expert blocks as first-class residents)
// ============================================================================
class ExpertStreamer {
public:
    ExpertStreamer(const TensorTable& tensors, ResidentWindow& window);
    ~ExpertStreamer();

    // Ensure a specific expert of a layer is in Hot tier
    void ensure_expert(int layer_idx, int expert_id);

    // Prefetch next experts based on router logits (optional hint)
    void hint_next_experts(int layer_idx, const std::vector<int>& expert_ids);

private:
    struct Impl;
    std::unique_ptr<Impl> pimpl_;
};

// ============================================================================
// 7. Compression Policy (quant selection + online re-quant)
// ============================================================================
enum class QuantPolicy {
    Q4_K_M,
    Q8_0,
    IQ4_NL,
    // ...
};

QuantPolicy choose_quant(const TensorDesc& tensor, const HardwareCaps& caps);

// Re-quantize a single tensor in-place (blocking, uses llama.cpp quantizer slab loop)
// Returns new QuantPolicy on success, std::nullopt on failure.
std::optional<QuantPolicy> requantize_tensor(const std::string& model_path,
                                             TensorDesc& tensor,
                                             QuantPolicy target);

// ============================================================================
// 8. Adapter Manager (LoRA + Control Vectors)
// ============================================================================
struct LoRAAdapter {
    std::string id;
    std::string path;     // .gguf LoRA file
    float scale = 1.0f;
};

struct ControlVector {
    std::string id;
    std::string path;     // .gguf or .bin control vector
    float scale = 1.0f;
    int layer_start = 0;
    int layer_end = -1;   // -1 = all layers
};

struct AdapterSet {
    std::vector<LoRAAdapter> loras;
    std::vector<ControlVector> cvecs;
};

class AdapterMgr {
public:
    AdapterMgr() = default;
    ~AdapterMgr();

    // Activate adapters for the *next* generation (serializes batches per llama.cpp)
    void activate(const AdapterSet& adapters);

    // Deactivate all
    void deactivate();

    // Current active set (for stats/debug)
    AdapterSet active() const;

private:
    struct Impl;
    std::unique_ptr<Impl> pimpl_;
};

// ============================================================================
// 9. Engine Stats (telemetry)
// ============================================================================
struct EngineStats {
    double ttft_ms = 0;              // time to first token
    double prefill_tok_s = 0;        // prefill throughput
    double decode_tok_s = 0;         // decode throughput
    size_t peak_ram_bytes = 0;       // process RSS peak
    size_t peak_vram_bytes = 0;      // GPU memory peak
    double disk_mb_per_token = 0;    // Cold→Warm bytes per generated token
    size_t hot_layers_count = 0;
    size_t warm_layers_count = 0;
    size_t cold_layers_count = 0;
    std::chrono::steady_clock::time_point start_time;
};

// ============================================================================
// 10. Engine API (used by embedded server routes)
// ============================================================================
struct Prompt {
    std::string text;
    std::vector<int32_t> tokens;    // pre-tokenized if available
};

struct SampleParams {
    float temperature = 0.7f;
    float top_p = 0.9f;
    float repeat_penalty = 1.1f;
    int top_k = 40;
    int64_t seed = -1;
    int max_tokens = 2048;
    // Extend as needed
};

using GenerateHandle = uint64_t;

class Engine {
public:
    // Factory: creates Engine, loads model, builds residency plan, starts background threads
    static std::unique_ptr<Engine> create(const std::string& model_path,
                                          const LayerPlan& plan,
                                          const HardwareCaps& caps);

    virtual ~Engine() = default;

    // Start a generation stream; returns opaque handle
    virtual GenerateHandle start_gen(const Prompt& prompt,
                                     const SampleParams& params) = 0;

    // Pull next token (blocking). Returns 0 on end-of-stream, -1 on error.
    virtual int next_token(GenerateHandle handle, int32_t& out_token) = 0;

    // Cancel a generation
    virtual void cancel(GenerateHandle handle) = 0;

    // Current stats snapshot
    virtual EngineStats stats() const = 0;

    // Adapter control (per-request boundary)
    virtual void set_adapters(const AdapterSet& adapters) = 0;
    virtual void clear_adapters() = 0;

    // Model info
    virtual int n_layers() const = 0;
    virtual int n_ctx() const = 0;
    virtual size_t vocab_size() const = 0;
};

} // namespace fish