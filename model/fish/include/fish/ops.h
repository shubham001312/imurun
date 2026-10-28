#pragma once
/*
 * FISH — numerical operations.
 *
 * Small, allocation-free primitives. Everything takes raw pointers and
 * lengths so the scheduler can call them on arena slices without copying.
 */
#include <cstdint>
#include <cstddef>
#include "gguf.h"

namespace fish {

void rmsnorm(float* out, const float* x, const float* w, int n, float eps);

void silu_inplace(float* x, int n);
void softmax_inplace(float* x, int n);

// Rotary position embedding for one token.
//   q: n_head    * head_dim
//   k: n_head_kv * head_dim
//   freq_factors may be null; when present theta is divided by it per pair.
void rope_apply(float* q, float* k,
                int n_head, int n_head_kv, int head_dim,
                int pos, float freq_base, const float* freq_factors);

// Y[t * n_out + i] = dot(W_row_i, X[t * n_in]) for every output row.
// The weight row is read once and reused for all tokens, which is what makes
// prompt prefill cheap: one pass over the weights instead of one per token.
void matmul_batch(GgmlType type, const void* w,
                  const float* x, int n_tok,
                  int n_in, int n_out, float* y);

// Causal multi-head attention for one token against a KV cache.
//   q:      n_head * head_dim
//   k_cache/v_cache: [n_ctx][n_head_kv * head_dim]
//   out:    n_head * head_dim
void attention_one(const float* q,
                   const float* k_cache, const float* v_cache,
                   int n_ctx, int n_head, int n_head_kv, int head_dim,
                   int n_past, float* out);

} // namespace fish
