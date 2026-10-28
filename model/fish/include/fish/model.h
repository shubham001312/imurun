#pragma once
/*
 * FISH — Llama-family transformer.
 *
 * Forward pass over GGUF weights, with a KV cache and batched prefill.
 * No framework, no BLAS: every projection is a FISH kernel call.
 */
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "gguf.h"
#include "quant.h"
#include "specs.h"

namespace fish {

// Read the metadata and tensor sizes of a GGUF without allocating the KV
// cache, so the planner can decide how big a context this device can hold
// before anything large is committed.
bool probe_model(const std::string& path, ModelProfile* out, std::string* err);

struct LlamaConfig {
    std::string arch = "llama";
    int n_layers = 0;
    int n_embd = 0;
    int n_ff = 0;
    int n_head = 0;
    int n_head_kv = 0;
    int head_dim = 0;
    int n_ctx = 4096;
    int vocab = 0;
    float eps = 1e-5f;
    float freq_base = 10000.0f;
    int bos = 1;
    int eos = 2;
    bool tied_output = false;

    int kv_dim() const { return n_head_kv * head_dim; }
};

struct LayerTensors {
    const TensorInfo* attn_norm = nullptr;
    const TensorInfo* ffn_norm = nullptr;
    const TensorInfo* wq = nullptr;
    const TensorInfo* wk = nullptr;
    const TensorInfo* wv = nullptr;
    const TensorInfo* wo = nullptr;
    const TensorInfo* wg = nullptr;
    const TensorInfo* wu = nullptr;
    const TensorInfo* wd = nullptr;

    std::vector<float> attn_norm_w;
    std::vector<float> ffn_norm_w;
    std::vector<float> bias_q, bias_k, bias_v, bias_o;
};

class LlamaModel {
public:
    // n_ctx is the KV budget FISH is allowed to use, not the model maximum.
    static std::unique_ptr<LlamaModel> load(const std::string& path, int n_ctx,
                                            std::string* err);
    ~LlamaModel();
    LlamaModel(const LlamaModel&) = delete;
    LlamaModel& operator=(const LlamaModel&) = delete;

    const LlamaConfig& config() const { return cfg_; }
    const GgufFile& gguf() const { return *gguf_; }

    // Process n_tok tokens starting at absolute position pos0.
    // After the call, logits() holds the distribution for the last token.
    bool forward(const int32_t* tokens, int n_tok, int pos0);

    const float* logits() const { return logits_.data(); }
    int argmax() const;

    void reset_kv() { kv_len_ = 0; }
    int  kv_len() const { return kv_len_; }
    void set_kv_len(int n) { kv_len_ = n; }

    uint64_t weights_bytes() const { return weights_bytes_; }
    uint64_t kv_bytes() const { return kv_bytes_; }

private:
    LlamaModel() = default;

    bool bind_layer(int i, std::string* err);
    bool load_vector(const TensorInfo* t, std::vector<float>& out);

    LlamaConfig cfg_;
    std::unique_ptr<GgufFile> gguf_;
    std::vector<LayerTensors> layers_;

    const TensorInfo* embd_ = nullptr;
    const TensorInfo* out_norm_ = nullptr;
    const TensorInfo* out_w_ = nullptr;
    std::vector<float> out_norm_w_;
    std::vector<float> rope_freqs_;

    // KV cache: [n_layers][n_ctx][kv_dim]
    std::vector<float> k_cache_;
    std::vector<float> v_cache_;

    // Work buffers, sized for the largest batch FISH will prefill at once.
    int max_batch_ = 256;
    std::vector<float> x_;      // [max_batch][n_embd]
    std::vector<float> xb_;     // [max_batch][n_embd]
    std::vector<float> q_;      // [max_batch][n_embd]
    std::vector<float> k_;      // [max_batch][kv_dim]
    std::vector<float> v_;      // [max_batch][kv_dim]
    std::vector<float> attn_;   // [max_batch][n_embd]
    std::vector<float> proj_;   // [max_batch][n_embd]
    std::vector<float> gate_;   // [max_batch][n_ff]
    std::vector<float> up_;     // [max_batch][n_ff]
    std::vector<float> logits_;

    uint64_t weights_bytes_ = 0;
    uint64_t kv_bytes_ = 0;
    int kv_len_ = 0;
};

} // namespace fish
