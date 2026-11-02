#pragma once
/*
 * FISH — numerical operations.
 *
 * Small, allocation-free primitives. Everything takes raw pointers and
 * lengths so the scheduler can call them on arena slices without copying.
 * The heavy ones (matmul, attention) spread their rows over the thread pool.
 */
#include <cstdint>
#include <cstddef>
#include <vector>
#include "gguf.h"
#include "quant.h"

namespace fish {

void rmsnorm(float* out, const float* x, const float* w, int n, float eps);

void silu_inplace(float* x, int n);
void gelu_inplace(float* x, int n);      // tanh approximation (Gemma)
void softmax_inplace(float* x, int n);

// ---------------------------------------------------------------- RoPE
//
// Angles are precomputed once per model for every (position, pair): decode
// then costs two multiplies per pair instead of a pow, a sin and a cos.
struct RopeParams {
    int   head_dim = 0;
    int   rot_dim = 0;             // rotated dims (<= head_dim); 0 = head_dim
    float freq_base = 10000.0f;
    float freq_scale = 1.0f;       // linear scaling: theta /= factor
    bool  neox = false;            // pairs (i, i+half) instead of (2i, 2i+1)
    float mscale = 1.0f;           // multiplies cos/sin (longrope attention factor)
    // YaRN. yarn_factor == 0 disables.
    float yarn_factor = 0.0f;
    float yarn_attn_factor = 1.0f;
    float yarn_beta_fast = 32.0f;
    float yarn_beta_slow = 1.0f;
    int   yarn_orig_ctx = 0;
    // Llama-3 per-pair frequency factors (rope_freqs.weight), rot_dim/2 long.
    const float* freq_factors = nullptr;
};

struct RopeCache {
    int n_ctx = 0;
    int head_dim = 0;
    int rot_dim = 0;
    bool neox = false;
    std::vector<float> cos_;   // [n_ctx][rot_dim/2]
    std::vector<float> sin_;
};

void rope_build(RopeCache& cache, int n_ctx, const RopeParams& p);

// Rotate q (n_head heads) and k (n_head_kv heads) for one token at `pos`.
void rope_apply(float* q, float* k, int n_head, int n_head_kv, int pos,
                const RopeCache& cache);

// ---------------------------------------------------------------- matmul
//
// A batch of activations quantised once in both int8 layouts, so the same
// batch can feed Q4_K, Q6_K (256-block) and Q4_0/Q8_0 (32-block) weights.
struct ActBatch {
    std::vector<Q8Act>  q32;
    std::vector<Q8ActK> q256;
    int n_tok = 0;
    int n_in = 0;

    // Quantise n_tok rows of n_in floats. The 256 layout is only built when
    // n_in is a multiple of 256 (a K-quant row always is).
    void quantize(const float* x, int n_tok, int n_in);

    const Q8Act*  row32(int t) const  { return q32.data() + static_cast<size_t>(t) * (n_in / kQ8ActBlock); }
    const Q8ActK* row256(int t) const { return q256.data() + static_cast<size_t>(t) * (n_in / kQ8ActKBlock); }
    bool has256() const { return !q256.empty(); }
};

// Y[t * n_out + i] = dot(W_row_i, X[t * n_in]) for every output row.
//
// `acts` holds the activations in int8 (may be null: then `x` is used with
// the f32 kernels). Every weight byte is read from memory exactly once.
void matmul_batch(GgmlType type, const void* w,
                  const float* x, const ActBatch* acts, int n_tok,
                  int n_in, int n_out, float* y);

// Convenience: quantise x then call the above.
void matmul_batch(GgmlType type, const void* w,
                  const float* x, int n_tok,
                  int n_in, int n_out, float* y);

// Same, addressed by tensor: routes to the GPU backend when the tensor is
// resident there, else to the CPU kernels on t->data.
void matmul_tensor(const TensorInfo* t, const float* x, const ActBatch* acts,
                   int n_tok, int n_in, int n_out, float* y);

// Several tensors on one input. All on the GPU -> one grouped device call;
// otherwise each tensor is routed individually.
void matmul_tensors(const TensorInfo* const* ts, int n, const float* x, const ActBatch* acts,
                    int n_tok, int n_in, const int* n_outs, float* const* ys);

// Calibration hook: when set, every matmul_tensor() call reports its f32
// input first (the model's load-time calibration pass collects per-column
// E[x^2] with it to weight the re-quantisation of device copies). Not
// thread-safe to change while a forward runs; null = off (the default).
using MatmulObserver = void (*)(void* user, const TensorInfo* t, const float* x, int n_tok, int n_in);
void set_matmul_observer(MatmulObserver fn, void* user);

// ---------------------------------------------------------------- attention
//
// Causal multi-head attention for a batch of tokens against a KV cache that
// already holds positions [0, pos0 + n_tok).
//   q:      [n_tok][n_head * head_dim]
//   k_cache/v_cache: [n_ctx][n_head_kv * head_dim]
//   out:    [n_tok][n_head * head_dim]
//   scale:  usually 1/sqrt(head_dim)
//   softcap: 0 = none; otherwise scores = softcap * tanh(scores / softcap)
//   window: 0 = full causal; otherwise only the last `window` positions
void attention_batch(const float* q,
                     const float* k_cache, const float* v_cache,
                     int n_ctx, int n_head, int n_head_kv, int head_dim,
                     int pos0, int n_tok, float* out,
                     float scale, float softcap = 0.0f, int window = 0);

} // namespace fish
