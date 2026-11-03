#pragma once
/*
 * FISH — compute backend hook.
 *
 * The CPU path is built in. A GPU backend (Vulkan first; CUDA/Metal later)
 * implements this interface, registers itself at library load, and the model
 * then places as many weight tensors on it as the memory budget allows. Every
 * matmul on a resident tensor goes to the device; everything else stays on
 * the CPU. Activations cross the bus as a small f32/int8 batch per call.
 *
 * Contract:
 *   - upload() is called at model load (never during decode); it must not
 *     exceed the budget it was given and must return false if the tensor does
 *     not fit, leaving earlier uploads intact.
 *   - matmul() is synchronous: when it returns, `y` holds the result.
 *   - The backend may be called from any thread, one call at a time.
 */
#include <cstdint>
#include <cstdlib>
#include <string>
#include "gguf.h"
#include "ops.h"

namespace fish {

class ComputeBackend {
public:
    virtual ~ComputeBackend() = default;

    virtual const char* name() const = 0;          // "vulkan", "cuda", ...
    virtual const char* device_name() const = 0;   // "NVIDIA GeForce RTX 3050 Laptop GPU"
    // One human-readable line of what is resident: layer count, re-quantised
    // mix, span KV size. Shown in stats and the UI's model card.
    virtual std::string describe() const { return name(); }
    virtual uint64_t memory_total() const = 0;
    virtual uint64_t memory_free() const = 0;

    // Weight types this backend can multiply. Others stay on the CPU.
    virtual bool supports(GgmlType type) const = 0;

    // Make a weight tensor resident on the device.
    virtual bool upload(const TensorInfo* t) = 0;
    virtual bool resident(const TensorInfo* t) const = 0;

    // ------------------------------------------------------ device formats
    //
    // Optional: device copies in another layout than the file's, chosen per
    // tensor by the planner (LlamaModel::place_layers, docs/engine/GPU.md
    // "Device formats"). kDevFile is the file's own bytes (upload()).
    // device_bytes() is what upload_as() would occupy, 0 = not possible.
    // importance: n_in non-negative column weights (calibration E[x^2]) for
    // the re-quantiser, or null. cache_file: where the re-quantised bytes
    // are read from / written to so the next load skips the work ("" = none).
    enum DeviceFormat : int { kDevFile = 0, kDevQ4K = 1, kDevFQ3 = 2, kDevFQ3S = 3 };
    // Hint before a batch of uploads totalling `bytes`: a backend may
    // pre-allocate that much so per-allocation rounding does not eat VRAM.
    virtual void reserve_device(uint64_t bytes) { (void)bytes; }
    // Frees reserved blocks that hold no tensor yet (planner re-plans).
    virtual void release_reserve() {}
    // Load-time upload profile (cache hits, re-quantisation, copy seconds); reset clears it.
    virtual std::string upload_profile(bool reset) { (void)reset; return std::string(); }
    virtual uint64_t device_bytes(const TensorInfo* t, int fmt) const {
        return fmt == kDevFile && t ? t->nbytes : 0;
    }
    virtual bool upload_as(const TensorInfo* t, int fmt, const float* importance,
                           const std::string& cache_file) {
        (void)importance; (void)cache_file;
        return fmt == kDevFile && upload(t);
    }
    virtual void release(const TensorInfo* t) = 0;
    virtual uint64_t bytes_resident() const = 0;

    // y[n_tok][n_out] = W . x for a resident tensor. `acts` carries x quantised
    // to int8 (may be null); `x` is always valid f32.
    virtual bool matmul(const TensorInfo* t, const float* x, const ActBatch* acts,
                        int n_tok, int n_in, int n_out, float* y) = 0;

    // Several resident tensors applied to the *same* input (q/k/v projections,
    // gate/up). One submit for the whole group is what keeps decode latency
    // low; the default just loops, so a backend may start without it.
    virtual bool matmul_group(const TensorInfo* const* ts, int n, const float* x,
                              const ActBatch* acts, int n_tok, int n_in,
                              const int* n_outs, float* const* ys) {
        for (int i = 0; i < n; ++i)
            if (!matmul(ts[i], x, acts, n_tok, n_in, n_outs[i], ys[i])) return false;
        return true;
    }

    // ---------------------------------------------------------- layer spans
    //
    // Optional. A backend that implements spans runs a contiguous range of
    // dense transformer layers entirely on the device — norms, projections,
    // RoPE, attention over a device-resident KV cache, FFN, residuals — so a
    // decode token costs one submit for the whole range instead of four per
    // layer plus CPU attention in between. This is what the decode targets
    // need (see docs/engine/GPU.md).
    //
    // Contract:
    //   - span_prepare() is called once after placement with every layer of
    //     the range already resident (upload() succeeded for its projections).
    //     It may allocate the KV cache for those layers (n_ctx positions) and
    //     upload the small vectors it is handed. Returning false means "no
    //     span": the model keeps routing per matmul and never calls
    //     span_forward.
    //   - span_forward() reads x[n_tok][n_embd] (f32, the residual stream at
    //     the start of layer_begin), writes positions pos0..pos0+n_tok-1 into
    //     its KV cache, and returns the residual stream after layer_end-1 in
    //     place. On failure it must return false WITHOUT having modified x;
    //     the model then falls back for that token and disables the span.
    //   - span_kv_truncate(n_past) drops cached positions >= n_past (prefix
    //     cache reuse / reset). Called with 0 on reset_kv().
    //   - MoE layers are never put in a span; Gemma-2 soft-caps, post-norms
    //     and sliding window, Qwen-3 q/k norms, Phi-3 fused qkv / gate|up and
    //     per-tensor biases all arrive in the descriptors and must be honoured
    //     or refused in span_prepare().
    struct SpanLayer {
        const TensorInfo* wq = nullptr;
        const TensorInfo* wk = nullptr;
        const TensorInfo* wv = nullptr;
        const TensorInfo* wqkv = nullptr;        // fused q|k|v (Phi-3), else null
        const TensorInfo* wo = nullptr;
        const TensorInfo* wg = nullptr;          // null when wu is fused gate|up
        const TensorInfo* wu = nullptr;
        const TensorInfo* wd = nullptr;
        const float* attn_norm = nullptr;        // [n_embd]
        const float* ffn_norm = nullptr;         // [n_embd]
        const float* post_attn_norm = nullptr;   // Gemma-2, else null
        const float* post_ffn_norm = nullptr;    // Gemma-2, else null
        const float* q_norm = nullptr;           // Qwen-3 per-head [head_dim], else null
        const float* k_norm = nullptr;
        const float* bias_q = nullptr;           // [q_dim] or null
        const float* bias_k = nullptr;           // [kv_dim]
        const float* bias_v = nullptr;
        const float* bias_o = nullptr;           // [n_embd]
        const float* bias_qkv = nullptr;         // [q_dim + 2 kv_dim]
        bool sliding_window = false;             // this layer uses the window
    };

    struct SpanParams {
        int layer_begin = 0, layer_end = 0;      // [begin, end)
        int n_embd = 0, n_head = 0, n_head_kv = 0, head_dim = 0, n_ff = 0, n_ctx = 0;
        float eps = 1e-5f;
        float attn_scale = 0.0f;                 // 0 = 1/sqrt(head_dim)
        float attn_softcap = 0.0f;               // Gemma-2: 50, else 0
        int window = 0;                          // sliding window size, 0 = none
        bool gelu = false;                       // GELU (Gemma) instead of SiLU
        // RoPE table: cos/sin [n_ctx][rot_dim/2]; neox pairs (i, i+half).
        const float* rope_cos = nullptr;
        const float* rope_sin = nullptr;
        int rot_dim = 0;
        bool rope_neox = false;
        const SpanLayer* layers = nullptr;       // layer_end - layer_begin entries
        // int8 KV cache (one f32 absmax per 32 values, 2.25 B per K+V element
        // instead of 4) when the planner is VRAM-bound; a backend without it
        // may keep f16. Requires kv_dim and head_dim multiples of 32.
        bool kv_q8 = false;
    };

    virtual bool span_prepare(const SpanParams&) { return false; }
    virtual bool span_forward(float* x, int n_tok, int pos0) { (void)x; (void)n_tok; (void)pos0; return false; }
    virtual void span_kv_truncate(int n_past) { (void)n_past; }
    virtual void span_release() {}
};

// VRAM kept free for other programs, applied by the backend to every
// allocation and by the planner to its budget. Default max(256 MB, 5% of the
// heap); FISH_VRAM_HEADROOM_PCT=p (0..90) sets a share of the heap, which a
// host can pass without knowing the card's size; FISH_VRAM_HEADROOM_MB=m
// (wins when both are set) sets it in MB. 256 MB is the measured floor below
// which WDDM starts demoting weights to system memory (GPU.md).
inline uint64_t vram_headroom_bytes(uint64_t heap_bytes) {
    uint64_t h = heap_bytes / 20;
    if (h < (256ull << 20)) h = 256ull << 20;
    if (const char* e = std::getenv("FISH_VRAM_HEADROOM_PCT")) {
        if (*e) {
            const double pct = std::atof(e);
            if (pct >= 0.0 && pct <= 90.0) h = static_cast<uint64_t>(static_cast<double>(heap_bytes) * pct / 100.0);
        }
    }
    if (const char* e = std::getenv("FISH_VRAM_HEADROOM_MB")) {
        if (*e) {
            const long long mb = std::atoll(e);
            if (mb >= 0) h = static_cast<uint64_t>(mb) << 20;
        }
    }
    return h;
}

// The registered GPU backend, or nullptr (no GPU build, no device, or
// FISH_GPU=0). Set once at startup; read freely afterwards.
ComputeBackend* gpu_backend();
void set_gpu_backend(ComputeBackend* backend);

} // namespace fish
