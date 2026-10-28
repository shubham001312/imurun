/*
 * FISH — numerical operations, scalar reference.
 */
#include <fish/ops.h>
#include <fish/quant.h>
#include <fish/threadpool.h>

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <vector>

namespace fish {

void rmsnorm(float* out, const float* x, const float* w, int n, float eps) {
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += static_cast<double>(x[i]) * x[i];
    const float scale = 1.0f / std::sqrt(static_cast<float>(sum / n) + eps);
    for (int i = 0; i < n; ++i) out[i] = x[i] * scale * w[i];
}

void silu_inplace(float* x, int n) {
    for (int i = 0; i < n; ++i) {
        const float v = x[i];
        x[i] = v / (1.0f + std::exp(-v));
    }
}

void softmax_inplace(float* x, int n) {
    if (n <= 0) return;
    float m = x[0];
    for (int i = 1; i < n; ++i) if (x[i] > m) m = x[i];
    float sum = 0.0f;
    for (int i = 0; i < n; ++i) {
        x[i] = std::exp(x[i] - m);
        sum += x[i];
    }
    if (sum <= 0.0f) {  // degenerate: uniform
        for (int i = 0; i < n; ++i) x[i] = 1.0f / static_cast<float>(n);
        return;
    }
    const float inv = 1.0f / sum;
    for (int i = 0; i < n; ++i) x[i] *= inv;
}

void rope_apply(float* q, float* k,
                int n_head, int n_head_kv, int head_dim,
                int pos, float freq_base, const float* freq_factors) {
    const int half = head_dim / 2;

    // Precompute the angle for each dimension pair once, reuse for q and k.
    // The frequency is identical for every head, which is why this is cheap.
    auto rotate = [&](float* vec, int n_heads) {
        for (int h = 0; h < n_heads; ++h) {
            float* v = vec + static_cast<size_t>(h) * head_dim;
            for (int i = 0; i < half; ++i) {
                // GGUF stores Q/K permuted for the adjacent-pair convention:
                // pair (2i, 2i+1), theta = pos * base^(-2i/d) / freq_factor.
                const float base = std::pow(freq_base, -2.0f * static_cast<float>(i) / static_cast<float>(head_dim));
                const float ff = freq_factors ? freq_factors[i] : 1.0f;
                const float theta = static_cast<float>(pos) * base / (ff != 0.0f ? ff : 1.0f);
                const float c = std::cos(theta);
                const float s = std::sin(theta);
                const float x0 = v[2 * i];
                const float x1 = v[2 * i + 1];
                v[2 * i]     = x0 * c - x1 * s;
                v[2 * i + 1] = x0 * s + x1 * c;
            }
        }
    };

    rotate(q, n_head);
    rotate(k, n_head_kv);
}

void matmul_batch(GgmlType type, const void* w,
                  const float* x, int n_tok,
                  int n_in, int n_out, float* y) {
    const size_t row_bytes = ggml_row_size(type, static_cast<uint64_t>(n_in));
    const uint8_t* base = static_cast<const uint8_t*>(w);

    // Decode: one token. The fused kernel is fastest because the row is
    // streamed from memory exactly once.
    if (n_tok == 1) {
        ThreadPool::instance().parallel_for(n_out, [&](int i) {
            const void* row = base + static_cast<size_t>(i) * row_bytes;
            y[i] = vec_dot(type, row, x, static_cast<uint64_t>(n_in));
        });
        return;
    }

    // Prefill: dequantise each weight row once and reuse it for every prompt
    // token. This is what makes a long prompt cost one pass over the weights
    // instead of one per token.
    ThreadPool::instance().parallel_for(n_out, [&](int i) {
        thread_local std::vector<float> row_buf;
        if (static_cast<int>(row_buf.size()) < n_in) row_buf.resize(static_cast<size_t>(n_in));

        const void* row = base + static_cast<size_t>(i) * row_bytes;
        if (!dequant_row(type, row, row_buf.data(), static_cast<uint64_t>(n_in))) {
            for (int t = 0; t < n_tok; ++t) {
                y[static_cast<size_t>(t) * n_out + i] =
                    vec_dot(type, row, x + static_cast<size_t>(t) * n_in,
                            static_cast<uint64_t>(n_in));
            }
            return;
        }
        for (int t = 0; t < n_tok; ++t) {
            y[static_cast<size_t>(t) * n_out + i] =
                dot_f32_fast(row_buf.data(), x + static_cast<size_t>(t) * n_in,
                             static_cast<uint64_t>(n_in));
        }
    });
}

void attention_one(const float* q,
                   const float* k_cache, const float* v_cache,
                   int n_ctx, int n_head, int n_head_kv, int head_dim,
                   int n_past, float* out) {
    const int group = n_head / n_head_kv;
    const int kv_stride = n_head_kv * head_dim;
    const float inv_sqrt = 1.0f / std::sqrt(static_cast<float>(head_dim));

    // Scores for one head are kept on the stack when the context is short and
    // in a thread-local scratch otherwise; attention_one is never re-entrant
    // on the same buffer, so a static scratch is safe under the model mutex.
    static thread_local float* scores = nullptr;
    static thread_local int scores_cap = 0;
    if (n_ctx > scores_cap) {
        std::free(scores);
        scores = static_cast<float*>(std::malloc(sizeof(float) * static_cast<size_t>(n_ctx)));
        scores_cap = scores ? n_ctx : 0;
    }
    if (!scores) return;

    for (int h = 0; h < n_head; ++h) {
        const int kvh = h / group;
        const float* qh = q + static_cast<size_t>(h) * head_dim;
        float max_score = -INFINITY;
        for (int t = 0; t <= n_past; ++t) {
            const float* kt = k_cache + static_cast<size_t>(t) * kv_stride +
                              static_cast<size_t>(kvh) * head_dim;
            float dot = 0.0f;
            for (int d = 0; d < head_dim; ++d) dot += qh[d] * kt[d];
            dot *= inv_sqrt;
            scores[t] = dot;
            if (dot > max_score) max_score = dot;
        }
        float sum = 0.0f;
        for (int t = 0; t <= n_past; ++t) {
            const float e = std::exp(scores[t] - max_score);
            scores[t] = e;
            sum += e;
        }
        const float inv = sum > 0.0f ? 1.0f / sum : 0.0f;
        float* oh = out + static_cast<size_t>(h) * head_dim;
        for (int d = 0; d < head_dim; ++d) oh[d] = 0.0f;
        for (int t = 0; t <= n_past; ++t) {
            const float p = scores[t] * inv;
            const float* vt = v_cache + static_cast<size_t>(t) * kv_stride +
                              static_cast<size_t>(kvh) * head_dim;
            for (int d = 0; d < head_dim; ++d) oh[d] += p * vt[d];
        }
    }
    (void)n_ctx;
}

} // namespace fish
