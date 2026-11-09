/*
 * FISH — numerical operations.
 */
#include <fish/ops.h>
#include <fish/backend.h>
#include <fish/quant.h>
#include <fish/threadpool.h>

#include <algorithm>
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

void gelu_inplace(float* x, int n) {
    const float k = 0.7978845608028654f;  // sqrt(2/pi)
    for (int i = 0; i < n; ++i) {
        const float v = x[i];
        x[i] = 0.5f * v * (1.0f + std::tanh(k * (v + 0.044715f * v * v * v)));
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

// ---------------------------------------------------------------- RoPE

namespace {

// YaRN correction dimension, as in the reference implementation.
float yarn_corr_dim(int n_dims, int n_orig_ctx, float n_rot, float base) {
    return static_cast<float>(n_dims) *
           std::log(static_cast<float>(n_orig_ctx) / (n_rot * 2.0f * 3.14159265358979f)) /
           (2.0f * std::log(base));
}

float yarn_ramp(float low, float high, int i0) {
    const float y = (static_cast<float>(i0) / 2.0f - low) / std::max(0.001f, high - low);
    return 1.0f - std::min(1.0f, std::max(0.0f, y));
}

} // namespace

void rope_build(RopeCache& cache, int n_ctx, const RopeParams& p) {
    cache.n_ctx = n_ctx;
    cache.head_dim = p.head_dim;
    cache.rot_dim = p.rot_dim > 0 ? std::min(p.rot_dim, p.head_dim) : p.head_dim;
    cache.neox = p.neox;
    const int half = cache.rot_dim / 2;
    cache.cos_.assign(static_cast<size_t>(n_ctx) * half, 0.0f);
    cache.sin_.assign(static_cast<size_t>(n_ctx) * half, 0.0f);

    const bool yarn = p.yarn_factor > 0.0f && p.yarn_orig_ctx > 0;
    float corr_low = 0, corr_high = 0, mscale = 1.0f;
    if (yarn) {
        corr_low = std::floor(yarn_corr_dim(cache.rot_dim, p.yarn_orig_ctx, p.yarn_beta_fast, p.freq_base));
        corr_high = std::ceil(yarn_corr_dim(cache.rot_dim, p.yarn_orig_ctx, p.yarn_beta_slow, p.freq_base));
        corr_low = std::max(0.0f, corr_low);
        corr_high = std::min(static_cast<float>(cache.rot_dim - 1), corr_high);
        mscale = p.yarn_attn_factor * (1.0f + 0.1f * std::log(p.yarn_factor));
    }

    std::vector<double> inv_freq(static_cast<size_t>(half));
    for (int i = 0; i < half; ++i) {
        inv_freq[static_cast<size_t>(i)] =
            std::pow(static_cast<double>(p.freq_base),
                     -2.0 * static_cast<double>(i) / static_cast<double>(cache.rot_dim));
    }

    for (int pos = 0; pos < n_ctx; ++pos) {
        float* c = cache.cos_.data() + static_cast<size_t>(pos) * half;
        float* s = cache.sin_.data() + static_cast<size_t>(pos) * half;
        for (int i = 0; i < half; ++i) {
            double theta_extrap = static_cast<double>(pos) * inv_freq[static_cast<size_t>(i)];
            if (p.freq_factors) {
                const float ff = p.freq_factors[i];
                theta_extrap /= (ff != 0.0f ? ff : 1.0f);
            }
            double theta;
            float m = p.mscale;
            if (yarn) {
                const double theta_interp = theta_extrap / p.yarn_factor;
                const float ramp = yarn_ramp(corr_low, corr_high, 2 * i);
                theta = theta_interp * (1.0 - ramp) + theta_extrap * ramp;
                m *= mscale;
            } else {
                theta = theta_extrap * p.freq_scale;
            }
            c[i] = static_cast<float>(std::cos(theta)) * m;
            s[i] = static_cast<float>(std::sin(theta)) * m;
        }
    }
}

void rope_apply(float* q, float* k, int n_head, int n_head_kv, int pos,
                const RopeCache& cache) {
    const int D = cache.head_dim;
    const int half = cache.rot_dim / 2;
    if (pos < 0 || pos >= cache.n_ctx || half <= 0) return;
    const float* c = cache.cos_.data() + static_cast<size_t>(pos) * half;
    const float* s = cache.sin_.data() + static_cast<size_t>(pos) * half;

    auto rotate = [&](float* vec, int n_heads) {
        for (int h = 0; h < n_heads; ++h) {
            float* v = vec + static_cast<size_t>(h) * D;
            if (cache.neox) {
                for (int i = 0; i < half; ++i) {
                    const float x0 = v[i], x1 = v[i + half];
                    v[i]        = x0 * c[i] - x1 * s[i];
                    v[i + half] = x0 * s[i] + x1 * c[i];
                }
            } else {
                for (int i = 0; i < half; ++i) {
                    const float x0 = v[2 * i], x1 = v[2 * i + 1];
                    v[2 * i]     = x0 * c[i] - x1 * s[i];
                    v[2 * i + 1] = x0 * s[i] + x1 * c[i];
                }
            }
        }
    };
    if (q) rotate(q, n_head);
    if (k) rotate(k, n_head_kv);
}

// ---------------------------------------------------------------- matmul

void ActBatch::quantize(const float* x, int n_tok_, int n_in_) {
    n_tok = n_tok_;
    n_in = n_in_;
    const size_t need32 = static_cast<size_t>(n_tok) * (n_in / kQ8ActBlock);
    if (q32.size() < need32) q32.resize(need32);
    for (int t = 0; t < n_tok; ++t)
        quantize_acts(x + static_cast<size_t>(t) * n_in, static_cast<uint64_t>(n_in),
                      q32.data() + static_cast<size_t>(t) * (n_in / kQ8ActBlock));
    if (n_in % kQ8ActKBlock == 0) {
        const size_t need256 = static_cast<size_t>(n_tok) * (n_in / kQ8ActKBlock);
        if (q256.size() < need256) q256.resize(need256);
        for (int t = 0; t < n_tok; ++t)
            quantize_acts_k(x + static_cast<size_t>(t) * n_in, static_cast<uint64_t>(n_in),
                            q256.data() + static_cast<size_t>(t) * (n_in / kQ8ActKBlock));
    } else {
        q256.clear();
    }
}

void matmul_batch(GgmlType type, const void* w,
                  const float* x, const ActBatch* acts, int n_tok,
                  int n_in, int n_out, float* y) {
    const size_t row_bytes = ggml_row_size(type, static_cast<uint64_t>(n_in));
    const uint8_t* base = static_cast<const uint8_t*>(w);
    ThreadPool& pool = ThreadPool::instance();

    const bool k_path = acts && acts->has256() && has_q8k_kernel(type) && (n_in % kQ8ActKBlock) == 0;
    const bool q_path = acts && has_q8_kernel(type) && (n_in % kQ8ActBlock) == 0;

    if (k_path || q_path) {
        // Every (row, token) pair is one packed-byte dot product. A tile of
        // rows stays in L1 while the tokens are the outer loop, so each
        // activation row is read once per tile and each weight byte once.
        const int rows_per_tile = std::max(1, static_cast<int>((24u * 1024u) / std::max<size_t>(1, row_bytes)));
        pool.parallel_range(n_out, [&](int r0, int r1) {
            for (int tile = r0; tile < r1; tile += rows_per_tile) {
                const int tile_end = std::min(r1, tile + rows_per_tile);
                for (int t = 0; t < n_tok; ++t) {
                    float* yt = y + static_cast<size_t>(t) * n_out;
                    if (k_path) {
                        const Q8ActK* xt = acts->row256(t);
                        for (int i = tile; i < tile_end; ++i) {
                            float v = 0.0f;
                            vec_dot_q8k(type, base + static_cast<size_t>(i) * row_bytes, xt,
                                        static_cast<uint64_t>(n_in), &v);
                            yt[i] = v;
                        }
                    } else {
                        const Q8Act* xt = acts->row32(t);
                        for (int i = tile; i < tile_end; ++i) {
                            float v = 0.0f;
                            vec_dot_q8(type, base + static_cast<size_t>(i) * row_bytes, xt,
                                       static_cast<uint64_t>(n_in), &v);
                            yt[i] = v;
                        }
                    }
                }
            }
        }, /*grain=*/std::max(rows_per_tile, n_out / (pool.active_threads() * 8)));
        return;
    }

    if (n_tok == 1) {
        pool.parallel_range(n_out, [&](int r0, int r1) {
            for (int i = r0; i < r1; ++i) {
                const void* row = base + static_cast<size_t>(i) * row_bytes;
                y[i] = vec_dot(type, row, x, static_cast<uint64_t>(n_in));
            }
        });
        return;
    }

    // f32 fallback for formats without an integer kernel: dequantise each
    // weight row once and reuse it for every token.
    pool.parallel_range(n_out, [&](int r0, int r1) {
        thread_local std::vector<float> row_buf;
        if (static_cast<int>(row_buf.size()) < n_in) row_buf.resize(static_cast<size_t>(n_in));
        for (int i = r0; i < r1; ++i) {
            const void* row = base + static_cast<size_t>(i) * row_bytes;
            if (!dequant_row(type, row, row_buf.data(), static_cast<uint64_t>(n_in))) {
                for (int t = 0; t < n_tok; ++t) {
                    y[static_cast<size_t>(t) * n_out + i] =
                        vec_dot(type, row, x + static_cast<size_t>(t) * n_in,
                                static_cast<uint64_t>(n_in));
                }
                continue;
            }
            for (int t = 0; t < n_tok; ++t) {
                y[static_cast<size_t>(t) * n_out + i] =
                    dot_f32_fast(row_buf.data(), x + static_cast<size_t>(t) * n_in,
                                 static_cast<uint64_t>(n_in));
            }
        }
    });
}

void matmul_batch(GgmlType type, const void* w,
                  const float* x, int n_tok,
                  int n_in, int n_out, float* y) {
    if ((has_q8_kernel(type) || has_q8k_kernel(type)) && (n_in % kQ8ActBlock) == 0) {
        thread_local ActBatch acts;
        acts.quantize(x, n_tok, n_in);
        matmul_batch(type, w, x, &acts, n_tok, n_in, n_out, y);
        return;
    }
    matmul_batch(type, w, x, nullptr, n_tok, n_in, n_out, y);
}

namespace {
ComputeBackend* g_gpu = nullptr;
}

ComputeBackend* gpu_backend() { return g_gpu; }
void set_gpu_backend(ComputeBackend* backend) { g_gpu = backend; }

namespace {
MatmulObserver g_obs = nullptr;
void* g_obs_user = nullptr;
}

void set_matmul_observer(MatmulObserver fn, void* user) {
    g_obs = fn;
    g_obs_user = user;
}

void matmul_tensor(const TensorInfo* t, const float* x, const ActBatch* acts,
                   int n_tok, int n_in, int n_out, float* y) {
    if (g_obs) g_obs(g_obs_user, t, x, n_tok, n_in);
    if (g_gpu && g_gpu->resident(t)) {
        if (g_gpu->matmul(t, x, acts, n_tok, n_in, n_out, y)) return;
        // A failed device call falls back to the CPU rather than losing the token.
    }
    matmul_batch(t->type, t->data, x, acts, n_tok, n_in, n_out, y);
}

void matmul_tensors(const TensorInfo* const* ts, int n, const float* x, const ActBatch* acts,
                    int n_tok, int n_in, const int* n_outs, float* const* ys) {
    bool all_gpu = g_gpu != nullptr && n > 0 && g_obs == nullptr;
    for (int i = 0; i < n && all_gpu; ++i) all_gpu = g_gpu->resident(ts[i]);
    if (all_gpu && g_gpu->matmul_group(ts, n, x, acts, n_tok, n_in, n_outs, ys)) return;
    for (int i = 0; i < n; ++i) matmul_tensor(ts[i], x, acts, n_tok, n_in, n_outs[i], ys[i]);
}

// ---------------------------------------------------------------- attention

void attention_batch(const float* q,
                     const float* k_cache, const float* v_cache,
                     int n_ctx, int n_head, int n_head_kv, int head_dim,
                     int pos0, int n_tok, float* out,
                     float scale, float softcap, int window) {
    const int group = n_head / n_head_kv;
    const int kv_stride = n_head_kv * head_dim;
    const int E = n_head * head_dim;
    const int items = n_tok * n_head;

    // One (token, head) pair per work item; heads of one token are
    // independent, so decode parallelises across heads.
    ThreadPool::instance().parallel_range(items, [&](int i0, int i1) {
        thread_local std::vector<float> scores;
        if (static_cast<int>(scores.size()) < n_ctx) scores.resize(static_cast<size_t>(n_ctx));

        for (int item = i0; item < i1; ++item) {
            const int t = item / n_head;
            const int h = item % n_head;
            const int pos = pos0 + t;
            const int kvh = h / group;
            const float* qh = q + static_cast<size_t>(t) * E + static_cast<size_t>(h) * head_dim;
            float* oh = out + static_cast<size_t>(t) * E + static_cast<size_t>(h) * head_dim;

            int t_begin = 0;
            if (window > 0 && pos - window + 1 > 0) t_begin = pos - window + 1;

            float max_score = -INFINITY;
            for (int j = t_begin; j <= pos; ++j) {
                const float* kt = k_cache + static_cast<size_t>(j) * kv_stride +
                                  static_cast<size_t>(kvh) * head_dim;
                float dot = dot_f32_fast(qh, kt, static_cast<uint64_t>(head_dim)) * scale;
                if (softcap > 0.0f) dot = softcap * std::tanh(dot / softcap);
                scores[static_cast<size_t>(j)] = dot;
                if (dot > max_score) max_score = dot;
            }
            float sum = 0.0f;
            for (int j = t_begin; j <= pos; ++j) {
                const float e = std::exp(scores[static_cast<size_t>(j)] - max_score);
                scores[static_cast<size_t>(j)] = e;
                sum += e;
            }
            const float inv = sum > 0.0f ? 1.0f / sum : 0.0f;
            for (int d = 0; d < head_dim; ++d) oh[d] = 0.0f;
            for (int j = t_begin; j <= pos; ++j) {
                const float p = scores[static_cast<size_t>(j)] * inv;
                const float* vt = v_cache + static_cast<size_t>(j) * kv_stride +
                                  static_cast<size_t>(kvh) * head_dim;
                for (int d = 0; d < head_dim; ++d) oh[d] += p * vt[d];
            }
        }
    }, /*grain=*/1);
    (void)n_ctx;
}

} // namespace fish
