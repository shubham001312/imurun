#pragma once
/*
 * FISH — specs engine and memory budget.
 *
 * FISH does not ask the model how much of it to load. It measures the machine
 * (cores, RAM, free RAM, VRAM, real sequential disk bandwidth) and then
 * decides, for this model on this device:
 *
 *   - how much context is safe,
 *   - how many transformer layers stay resident in RAM,
 *   - how many layers form the streaming window,
 *   - how many hot experts to cache (MoE),
 *   - and, honestly, how fast that will be.
 *
 * The window is FISH's construct. For a dense model the granularity is whole
 * layers; for an MoE it is experts. Same planner, same arena, two granularities.
 */
#include <cstdint>
#include <string>
#include <vector>

namespace fish {

struct HardwareSpecs {
    std::string cpu_name;
    int physical_cores = 0;
    int logical_cores = 0;
    bool avx2 = false;
    bool avx512 = false;
    bool neon = false;

    uint64_t ram_total_bytes = 0;
    uint64_t ram_available_bytes = 0;
    uint64_t swap_total_bytes = 0;

    std::string gpu_name;
    uint64_t vram_total_bytes = 0;
    uint64_t vram_free_bytes = 0;
    bool unified_memory = false;

    // Measured, not assumed. 0 means "not measured on this device".
    double disk_read_mb_s = 0.0;
    double ram_bandwidth_mb_s = 0.0;
};

struct ModelProfile {
    uint64_t file_bytes = 0;
    uint64_t weights_bytes = 0;
    uint64_t per_layer_bytes = 0;      // dense: one transformer block
    uint64_t kv_per_token_bytes = 0;
    int n_layers = 0;
    int n_embd = 0;
    int n_ff = 0;
    int n_ctx_max = 0;
    int n_experts = 0;                 // 0 for a dense model
    int experts_per_token = 0;
    uint64_t expert_bytes = 0;         // one expert, when n_experts > 0
    std::string arch;
    std::string quant_mix;
};

struct PlanRequest {
    int n_ctx = 0;                     // 0 = FISH decides
    bool allow_vram = true;
    uint64_t ram_reserve_bytes = 0;    // 0 = auto
};

struct ExecutionPlan {
    std::string mode;                  // "resident" | "windowed" | "streamed"
    int n_ctx = 0;
    int n_threads = 0;
    int resident_layers = 0;
    int window_layers = 0;
    int expert_cache_slots = 0;

    uint64_t ram_budget_bytes = 0;
    uint64_t arena_bytes = 0;
    uint64_t vram_bytes = 0;

    double bytes_per_token = 0.0;
    double predicted_tok_s = 0.0;
    double predicted_ttft_s = 0.0;
    std::string rationale;
};

class SpecsEngine {
public:
    static SpecsEngine& instance();

    // Cached after the first call.
    const HardwareSpecs& hardware();

    // Re-measure. `probe_path` (usually the GGUF being loaded) is used for the
    // sequential-read benchmark, so the number reflects the real model file on
    // the real disk rather than a synthetic temporary file.
    void refresh(const std::string& probe_path);

    ExecutionPlan plan(const ModelProfile& model, const PlanRequest& req) const;

private:
    SpecsEngine() = default;
    HardwareSpecs hw_;
    bool measured_ = false;
};

std::string hardware_json(const HardwareSpecs& hw);
std::string plan_json(const ExecutionPlan& plan);

// ---------------------------------------------------------------------------
// Arena — a hard-capped bump allocator.
//
// The engine reserves one block at startup and never asks the OS for more.
// If a request would exceed the cap it returns nullptr and the caller shrinks
// the window instead of letting the machine swap itself to death.
// ---------------------------------------------------------------------------
class Arena {
public:
    explicit Arena(uint64_t capacity);
    ~Arena();
    Arena(const Arena&) = delete;
    Arena& operator=(const Arena&) = delete;

    void* alloc(uint64_t bytes, size_t align = 64);
    void reset();

    uint64_t used() const { return used_; }
    uint64_t capacity() const { return capacity_; }
    uint64_t remaining() const { return capacity_ - used_; }

private:
    uint8_t* base_ = nullptr;
    uint64_t capacity_ = 0;
    uint64_t used_ = 0;
};

} // namespace fish
