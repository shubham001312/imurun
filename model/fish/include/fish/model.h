#pragma once
/*
 * FISH — decoder-only transformer over GGUF weights.
 *
 * One forward pass serves every supported architecture; the differences are
 * data (which tensors exist, which norm/activation/RoPE variant) rather than
 * separate code paths:
 *
 *   llama      Llama 2/3, Mistral, TinyLlama, SmolLM, DeepSeek-R1-Distill-Llama,
 *              Mixtral (MoE)
 *   qwen2      Qwen 2/2.5 (q/k/v biases), Qwen2-MoE, DeepSeek-R1-Distill-Qwen
 *   qwen3      Qwen 3 (per-head q/k RMSNorm), Qwen3-MoE
 *   phi3       Phi-3 / 3.5 (fused qkv, fused gate+up, longrope factors)
 *   gemma2     Gemma 2 (post-norms, GELU, soft-capping, sliding window)
 *
 * Weights stay quantised in the memory-mapped file. Activations are quantised
 * to int8 once per matmul. When the file is larger than free RAM the next
 * layer's pages are prefetched while the current one computes.
 */
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "backend.h"
#include "calib.h"
#include "gguf.h"
#include "lora.h"
#include "ops.h"
#include "quant.h"
#include "specs.h"

namespace fish {

// Read the metadata and tensor sizes of a GGUF without allocating the KV
// cache, so the planner can decide how big a context this device can hold
// before anything large is committed.
bool probe_model(const std::string& path, ModelProfile* out, std::string* err);

// Architectures FISH can run. Unknown names fail at load with a clear message.
enum class Arch { Llama, Qwen2, Qwen3, Qwen35, Phi3, Gemma2 };
const char* arch_name(Arch a);
bool arch_supported(const std::string& gguf_arch);

struct LlamaConfig {
    std::string arch = "llama";
    Arch kind = Arch::Llama;
    int n_layers = 0;
    int n_embd = 0;
    int n_ff = 0;
    int n_head = 0;
    int n_head_kv = 0;
    int head_dim = 0;
    int n_ctx = 4096;
    int n_ctx_train = 4096;
    int vocab = 0;
    float eps = 1e-5f;
    float freq_base = 10000.0f;
    int bos = 1;
    int eos = 2;
    bool tied_output = false;

    // RoPE
    RopeParams rope;

    // Mixture of experts (0 experts = dense)
    int n_experts = 0;
    int n_experts_per_tok = 0;
    int n_ff_exp = 0;              // per-expert FFN width
    int n_ff_shexp = 0;            // shared-expert FFN width (0 = none)
    bool expert_weights_norm = true;

    // Variant knobs
    bool norm_plus_one = false;    // Gemma: RMSNorm weight is (1 + w)
    bool gelu = false;             // Gemma: GELU instead of SiLU
    float embd_scale = 1.0f;       // Gemma: sqrt(n_embd)
    float attn_scale = 0.0f;       // 0 = 1/sqrt(head_dim)
    float attn_softcap = 0.0f;     // Gemma2: 50
    float final_softcap = 0.0f;    // Gemma2: 30
    int sliding_window = 0;        // Gemma2: 4096 on even layers
    bool qk_norm = false;          // Qwen3

    // Qwen3.5 hybrid: DeltaNet linear-attention layers interleaved with full
    // attention. full_attention_interval = N means layer i attends fully iff
    // (i+1) % N == 0; every other layer runs the Gulf DeltaNet mixer instead
    // (causal depthwise conv -> delta-rule recurrence over a per-head state).
    int full_attn_interval = 0;    // 0 = plain transformer
    int ssm_state = 0;             // delta-rule state/head dim (2B: 128)
    int ssm_heads = 0;             // delta-rule heads/groups (2B: 16)
    int ssm_conv_kernel = 0;       // depthwise conv taps (2B: 4)
    int ssm_inner = 0;             // q|k|v branch width (2B: 2048)

    int kv_dim() const { return n_head_kv * head_dim; }
    int q_dim() const { return n_head * head_dim; }
    bool is_moe() const { return n_experts > 0; }
    bool delta_layer(int i) const {
        return full_attn_interval > 0 && (((i + 1) % full_attn_interval) != 0);
    }
    // Hybrid archs keep KV slots for the full-attention layers only; a delta
    // layer i owns slot i / full_attn_interval.
    int kv_slots() const {
        return full_attn_interval > 0 ? (n_layers + full_attn_interval - 1) / full_attn_interval
                                      : n_layers;
    }
    int kv_slot(int i) const { return full_attn_interval > 0 ? i / full_attn_interval : i; }
    int conv_dim() const { return ssm_inner > 0 ? 3 * ssm_inner : 0; }
};

struct LayerTensors {
    const TensorInfo* attn_norm = nullptr;
    const TensorInfo* ffn_norm = nullptr;
    const TensorInfo* wq = nullptr;
    const TensorInfo* wk = nullptr;
    const TensorInfo* wv = nullptr;
    const TensorInfo* wo = nullptr;
    const TensorInfo* wqkv = nullptr;      // Phi-3 fused q|k|v
    const TensorInfo* wg = nullptr;
    const TensorInfo* wu = nullptr;        // Phi-3: fused gate|up (2 * n_ff rows)
    const TensorInfo* wd = nullptr;

    // Qwen3.5 DeltaNet layers (q|k|v fused pre-conv lives in wqkv)
    const TensorInfo* wgate = nullptr;     // attn_gate: x -> output gate z (n_embd)
    const TensorInfo* ssm_alpha = nullptr; // x -> a_t raw per DeltaNet head
    const TensorInfo* ssm_beta = nullptr;  // x -> beta raw per DeltaNet head
    const TensorInfo* ssm_out = nullptr;   // o_proj for the DeltaNet branch

    // MoE (3-D tensors: [n_in][n_out][n_expert])
    const TensorInfo* gate_inp = nullptr;
    const TensorInfo* gate_exps = nullptr;
    const TensorInfo* up_exps = nullptr;
    const TensorInfo* down_exps = nullptr;
    const TensorInfo* shexp_gate = nullptr;
    const TensorInfo* shexp_up = nullptr;
    const TensorInfo* shexp_down = nullptr;
    const TensorInfo* shexp_gate_inp = nullptr;   // Qwen2-MoE: sigmoid gate on the shared expert

    // Dequantised small vectors
    std::vector<float> attn_norm_w;
    std::vector<float> ffn_norm_w;
    std::vector<float> post_attn_norm_w;   // Gemma2
    std::vector<float> post_ffn_norm_w;    // Gemma2
    std::vector<float> q_norm_w, k_norm_w; // Qwen3
    std::vector<float> bias_q, bias_k, bias_v, bias_o;
    std::vector<float> bias_qkv;           // Phi-3 variants with a fused bias

    // Qwen3.5 DeltaNet small vectors (dequantised; fp32 state lives in the model)
    std::vector<float> conv_w;             // [conv_dim][kernel], channel-major
    std::vector<float> ssm_a;              // [heads]: negative decay per head
    std::vector<float> dt_bias;            // [heads]
    std::vector<float> ssm_norm_w;         // [state]: gated-RMSNorm weight, raw (no +1)

    // Byte range of this layer's weights in the file, for prefetch.
    uint64_t file_begin = 0, file_end = 0;
};

class LlamaModel {
public:
    struct LoadOptions {
        int gpu_layers = -1;      // -1 = automatic placement; 0 = CPU only (draft models)
        bool allow_span = true;
        // LoRA adapter GGUF merged into the weights before placement
        // (include/fish/lora.h). Empty = none. A merge failure fails the load.
        std::string adapter_path;
        float adapter_scale = 1.0f;
        // Defaults: re-quantise to each tensor's own type with stochastic
        // rounding (quant.h). Env FISH_ADAPTER_ROUNDING=nearest and
        // FISH_ADAPTER_MERGE_TYPE=f16|bf16|f32|q8_0 override them (quality
        // studies; a wider type costs RAM/VRAM and decode speed).
        MergeRounding adapter_rounding = MergeRounding::Stochastic;
        std::string adapter_merge_type;   // "" = keep each tensor's type
    };

    // n_ctx is the KV budget FISH is allowed to use, not the model maximum.
    static std::unique_ptr<LlamaModel> load(const std::string& path, int n_ctx,
                                            std::string* err);
    static std::unique_ptr<LlamaModel> load(const std::string& path, int n_ctx,
                                            const LoadOptions& opts, std::string* err);
    ~LlamaModel();
    LlamaModel(const LlamaModel&) = delete;
    LlamaModel& operator=(const LlamaModel&) = delete;

    const LlamaConfig& config() const { return cfg_; }
    const GgufFile& gguf() const { return *gguf_; }

    // Process n_tok tokens starting at absolute position pos0.
    // After the call, logits() holds the distribution for the last token.
    // With all_logits=true, logits_at(t) holds the distribution after token t
    // for every t in the batch (what speculative verification needs; the
    // output projection then runs on n_tok rows, so keep the batch small).
    bool forward(const int32_t* tokens, int n_tok, int pos0, bool all_logits = false);

    const float* logits() const { return logits_.data(); }
    const float* logits_at(int t) const { return logits_all_.data() + static_cast<size_t>(t) * cfg_.vocab; }
    int argmax() const;

    void reset_kv();
    int  kv_len() const { return kv_len_; }
    // Hybrid (DeltaNet) models carry a recurrent state that cannot be rewound:
    // the only reusable prefix is the exact current length.
    bool recurrent() const { return !conv_hist_.empty(); }
    void set_kv_len(int n);

    uint64_t weights_bytes() const { return weights_bytes_; }
    uint64_t kv_bytes() const { return kv_bytes_; }
    bool streaming() const { return stream_; }
    int max_batch() const { return max_batch_; }

    // Placement after load: how many layers have their projections on the GPU,
    // how many are kept resident in RAM, and how many stream from disk.
    int gpu_layers() const { return gpu_layers_; }
    int ram_layers() const { return ram_layers_; }
    int disk_layers() const { return std::max(0, cfg_.n_layers - gpu_layers_ - ram_layers_); }
    uint64_t gpu_bytes() const { return gpu_bytes_; }
    // True when the GPU layers run as one device-side span per token.
    bool gpu_span() const { return gpu_span_; }

    // The merged adapter, if any (report.active).
    const LoraReport& adapter() const { return adapter_; }

    // Load-time calibration (device formats, include/fish/calib.h): run the
    // CPU forward over the built-in text (at most max_tokens) and return, per
    // projection, E[x_j^2] of its input columns. Only valid before placement
    // (every matmul must run on the CPU at file precision); the planner calls
    // it itself when it re-quantises. Public for fish_test_gpu's studies.
    bool calibrate(Importance* out, int max_tokens, std::string* err);
    // What the residency planner decided (empty when it was not used).
    const std::string& placement_note() const { return placement_note_; }
    // Wall-clock per load phase (open, bind, place = calibration + upload, span, KV).
    const std::string& load_profile() const { return load_profile_; }
    // The tensor the forward pass actually uses for `name`: the merged copy
    // when an adapter touched it, else the file's tensor. nullptr if unbound.
    const TensorInfo* weight(const std::string& name) const;

private:
    LlamaModel() = default;

    bool bind_layer(int i, std::string* err);
    bool apply_adapter(std::string* err);
    bool load_vector(const TensorInfo* t, std::vector<float>& out);
    // Qwen3.5 DeltaNet mixer for tokens [pos0, pos0+n_tok): causal conv over the
    // fused q|k|v stream, per-head delta-rule recurrence, gated RMSNorm + gate,
    // then the output projection (everything fp32; no int8 for the recurrence).
    bool delta_mixer(const LayerTensors& L, int li, int pos0, int n_tok);
    void prefetch_layer(int li);
    void place_layers();
    void evict_layer(int li);
    void prepare_span();
    // Whole model + output on the GPU as one span, re-quantising device
    // copies and/or shortening the context only as far as needed. False =
    // not possible; nothing is left resident and the caller places partially.
    bool plan_resident(ComputeBackend* gpu, uint64_t vram_budget, uint64_t arena);
    bool alloc_cpu_kv();
    void moe_ffn(const LayerTensors& L, int n_tok);
    void dense_ffn(const LayerTensors& L, int n_tok);
    // expert < 0: whole tensors (dense / shared expert); otherwise the slice of
    // expert `expert` inside the 3-D MoE tensors.
    void ffn_block(const TensorInfo* wg, const TensorInfo* wu, const TensorInfo* wd, int expert,
                   const float* x, const ActBatch* xq, int n_tok, int n_in, int n_ff,
                   float* out, bool fused_gate_up);

    LlamaConfig cfg_;
    std::unique_ptr<GgufFile> gguf_;
    std::vector<LayerTensors> layers_;

    const TensorInfo* embd_ = nullptr;
    const TensorInfo* out_norm_ = nullptr;
    const TensorInfo* out_w_ = nullptr;
    std::vector<float> out_norm_w_;
    std::vector<float> rope_freqs_;
    RopeCache rope_;

    // KV cache: [n_layers][n_ctx][kv_dim] (hybrid archs: full-attn slots only)
    std::vector<float> k_cache_;
    std::vector<float> v_cache_;

    // Qwen3.5 recurring state, fp32, persists across decode calls:
    // conv history of the last (kernel-1) conv inputs per delta layer, and the
    // per-head delta-rule state S. Indexed by delta-layer slot (rng order).
    std::vector<int> delta_slot_;      // layer index -> delta slot (or -1)
    std::vector<float> conv_hist_;     // [n_delta][kernel-1][conv_dim]
    std::vector<float> raw_;           // [conv_dim] pre-conv q|k|v of the current token
    std::vector<float> ssm_S_;         // [n_delta][heads][state][state]

    // Work buffers, sized for the largest batch FISH will prefill at once.
    int max_batch_ = 256;
    std::vector<float> x_;      // [max_batch][n_embd]
    std::vector<float> xb_;     // [max_batch][n_embd]
    ActBatch xq_;               // quantised xb_
    std::vector<float> qkv_;    // [max_batch][q_dim + 2 kv_dim] (fused path)
    std::vector<float> q_;      // [max_batch][q_dim]
    std::vector<float> k_;      // [max_batch][kv_dim]
    std::vector<float> v_;      // [max_batch][kv_dim]
    std::vector<float> attn_;   // [max_batch][q_dim]
    ActBatch attnq_;
    std::vector<float> proj_;   // [max_batch][n_embd]
    std::vector<float> gate_;   // [max_batch][2 * n_ff]  (fused gate|up fits too)
    std::vector<float> up_;     // [max_batch][n_ff]
    ActBatch gateq_;
    std::vector<float> ffn_out_;  // [max_batch][n_embd]
    std::vector<float> logits_;
    std::vector<float> logits_all_;  // [n_tok][vocab] when forward(all_logits=true)

    // MoE scratch
    std::vector<float> router_;        // [max_batch][n_experts]
    std::vector<float> exp_in_;        // gathered tokens
    ActBatch exp_inq_;
    std::vector<float> exp_down_;

    // Prefetch thread (layer streaming). In streaming mode the prefetcher
    // keeps a window of `stream_window_` layers resident and drops the pages
    // of layers behind it, so the process stays inside its RAM budget instead
    // of pushing the user's other programs out.
    bool stream_ = false;
    int stream_window_ = 3;
    std::thread prefetch_thread_;
    std::thread warm_thread_;   // one-shot page-in of the CPU layers after load
    std::mutex prefetch_mu_;
    std::condition_variable prefetch_cv_;
    int prefetch_request_ = -1;
    bool prefetch_stop_ = false;
    void prefetch_loop();

    // Placement
    LoadOptions opts_;
    int gpu_layers_ = 0;
    int ram_layers_ = 0;
    uint64_t gpu_bytes_ = 0;
    bool gpu_span_ = false;
    std::vector<ComputeBackend::SpanLayer> span_layers_;

    // Merged adapter tensors: copies of the file's TensorInfo pointing at
    // owned buffers (the read-only mapping is never written). Bound in place
    // of the originals, so placement, GPU upload and spans treat them like
    // any RAM-resident weight. deque: the bindings hold stable pointers.
    LoraReport adapter_;
    std::deque<TensorInfo> owned_tensors_;
    std::vector<std::unique_ptr<uint8_t[]>> owned_bufs_;
    uint64_t owned_extra_bytes_ = 0;   // merged non-layer tensors (embeddings / output)

    uint64_t weights_bytes_ = 0;
    uint64_t kv_bytes_ = 0;
    int kv_len_ = 0;
    std::string placement_note_;
    std::string load_profile_;
    double calib_seconds_ = 0, upload_seconds_ = 0;
    bool gpu_kv_q8_ = false;   // planner chose the int8 device KV cache
};

} // namespace fish
