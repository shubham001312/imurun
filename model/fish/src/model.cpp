/*
 * FISH — Llama-family transformer implementation.
 */
#include <fish/model.h>
#include <fish/ops.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <new>

namespace fish {

namespace {

inline float* row_ptr(std::vector<float>& v, int i, int stride) {
    return v.data() + static_cast<size_t>(i) * static_cast<size_t>(stride);
}
inline const float* row_ptr(const std::vector<float>& v, int i, int stride) {
    return v.data() + static_cast<size_t>(i) * static_cast<size_t>(stride);
}

// Add an optional bias vector to every row of a [n_tok][n_out] matrix.
void add_bias(float* y, int n_tok, int n_out, const std::vector<float>& bias) {
    if (bias.empty()) return;
    for (int t = 0; t < n_tok; ++t) {
        float* r = y + static_cast<size_t>(t) * n_out;
        for (int i = 0; i < n_out; ++i) r[i] += bias[i];
    }
}

} // namespace

LlamaModel::~LlamaModel() = default;

bool probe_model(const std::string& path, ModelProfile* out, std::string* err) {
    if (!out) return false;
    std::string e;
    auto g = GgufFile::open(path, &e);
    if (!g) {
        if (err) *err = e;
        return false;
    }

    const int n_layers = static_cast<int>(g->u64("llama.block_count", 0));
    const int n_embd = static_cast<int>(g->u64("llama.embedding_length", 0));
    const int n_ff = static_cast<int>(g->u64("llama.feed_forward_length", 0));
    const int n_head = static_cast<int>(g->u64("llama.attention.head_count", 0));
    const int n_head_kv = static_cast<int>(g->u64("llama.attention.head_count_kv", n_head));
    int head_dim = static_cast<int>(g->u64("llama.attention.key_length", 0));
    if (head_dim <= 0 && n_head > 0) head_dim = n_embd / n_head;

    ModelProfile p;
    p.arch = g->str("general.architecture", "llama");
    p.file_bytes = g->file_size();
    p.n_layers = n_layers;
    p.n_embd = n_embd;
    p.n_ff = n_ff;
    p.n_ctx_max = static_cast<int>(g->u64("llama.context_length", 4096));

    uint64_t blk_bytes = 0;
    for (const auto& t : g->tensors()) {
        p.weights_bytes += t.nbytes;
        if (t.name.rfind("blk.", 0) == 0) blk_bytes += t.nbytes;
    }
    p.per_layer_bytes = n_layers > 0 ? blk_bytes / static_cast<uint64_t>(n_layers) : 0;
    // K and V, f32, per token across every layer.
    p.kv_per_token_bytes = static_cast<uint64_t>(n_layers) * 2ull *
                           static_cast<uint64_t>(n_head_kv) *
                           static_cast<uint64_t>(head_dim) * sizeof(float);

    *out = std::move(p);
    return true;
}

bool LlamaModel::load_vector(const TensorInfo* t, std::vector<float>& out) {
    if (!t) return false;
    const uint64_t n = t->n_elements();
    if (n == 0 || n > (1ull << 30)) return false;
    out.resize(static_cast<size_t>(n));
    return dequant_row(t->type, t->data, out.data(), n);
}

bool LlamaModel::bind_layer(int i, std::string* err) {
    const std::string p = "blk." + std::to_string(i) + ".";
    LayerTensors& L = layers_[static_cast<size_t>(i)];

    L.attn_norm = gguf_->find(p + "attn_norm.weight");
    L.ffn_norm  = gguf_->find(p + "ffn_norm.weight");
    L.wq = gguf_->find(p + "attn_q.weight");
    L.wk = gguf_->find(p + "attn_k.weight");
    L.wv = gguf_->find(p + "attn_v.weight");
    L.wo = gguf_->find(p + "attn_output.weight");
    L.wg = gguf_->find(p + "ffn_gate.weight");
    L.wu = gguf_->find(p + "ffn_up.weight");
    L.wd = gguf_->find(p + "ffn_down.weight");

    const char* required[] = {"attn_norm.weight", "ffn_norm.weight", "attn_q.weight",
                              "attn_k.weight", "attn_v.weight", "attn_output.weight",
                              "ffn_gate.weight", "ffn_up.weight", "ffn_down.weight"};
    const TensorInfo* have[] = {L.attn_norm, L.ffn_norm, L.wq, L.wk, L.wv, L.wo,
                                L.wg, L.wu, L.wd};
    for (int k = 0; k < 9; ++k) {
        if (!have[k]) {
            if (err) *err = "missing tensor " + p + required[k];
            return false;
        }
    }

    if (!load_vector(L.attn_norm, L.attn_norm_w)) { if (err) *err = "bad attn_norm " + std::to_string(i); return false; }
    if (!load_vector(L.ffn_norm, L.ffn_norm_w))   { if (err) *err = "bad ffn_norm " + std::to_string(i); return false; }

    // Optional biases (Qwen and friends); Llama 3 has none.
    if (const TensorInfo* b = gguf_->find(p + "attn_q.bias")) load_vector(b, L.bias_q);
    if (const TensorInfo* b = gguf_->find(p + "attn_k.bias")) load_vector(b, L.bias_k);
    if (const TensorInfo* b = gguf_->find(p + "attn_v.bias")) load_vector(b, L.bias_v);
    if (const TensorInfo* b = gguf_->find(p + "attn_output.bias")) load_vector(b, L.bias_o);
    return true;
}

std::unique_ptr<LlamaModel> LlamaModel::load(const std::string& path, int n_ctx,
                                             std::string* err) {
    auto fail = [&](const std::string& why) -> std::unique_ptr<LlamaModel> {
        if (err) *err = why;
        return nullptr;
    };

    std::unique_ptr<LlamaModel> m(new (std::nothrow) LlamaModel());
    if (!m) return fail("out of memory");

    std::string gerr;
    m->gguf_ = GgufFile::open(path, &gerr);
    if (!m->gguf_) return fail(gerr);

    const GgufFile& g = *m->gguf_;
    LlamaConfig& c = m->cfg_;
    c.arch = g.str("general.architecture", "llama");
    if (c.arch != "llama") return fail("unsupported architecture: " + c.arch);

    c.n_layers  = static_cast<int>(g.u64("llama.block_count", 0));
    c.n_embd    = static_cast<int>(g.u64("llama.embedding_length", 0));
    c.n_ff      = static_cast<int>(g.u64("llama.feed_forward_length", 0));
    c.n_head    = static_cast<int>(g.u64("llama.attention.head_count", 0));
    c.n_head_kv = static_cast<int>(g.u64("llama.attention.head_count_kv", c.n_head));
    c.vocab     = static_cast<int>(g.u64("llama.vocab_size", 0));
    c.eps       = static_cast<float>(g.f64("llama.attention.layer_norm_rms_epsilon", 1e-5));
    c.freq_base = static_cast<float>(g.f64("llama.rope.freq_base", 10000.0));
    c.bos       = static_cast<int>(g.i64("tokenizer.ggml.bos_token_id", 1));
    c.eos       = static_cast<int>(g.i64("tokenizer.ggml.eos_token_id", 2));

    if (c.n_layers <= 0 || c.n_embd <= 0 || c.n_head <= 0 || c.n_ff <= 0 || c.vocab <= 0)
        return fail("incomplete model metadata");

    c.head_dim = static_cast<int>(g.u64("llama.attention.key_length", 0));
    if (c.head_dim <= 0) c.head_dim = c.n_embd / c.n_head;
    if (c.head_dim * c.n_head != c.n_embd) return fail("head_dim * n_head != n_embd");
    if (c.n_head % c.n_head_kv != 0) return fail("n_head is not a multiple of n_head_kv");

    const int model_ctx = static_cast<int>(g.u64("llama.context_length", 4096));
    c.n_ctx = n_ctx > 0 ? std::min(n_ctx, model_ctx > 0 ? model_ctx : n_ctx) : 4096;
    if (c.n_ctx < 32) return fail("context too small");

    m->embd_     = g.find("token_embd.weight");
    m->out_norm_ = g.find("output_norm.weight");
    m->out_w_    = g.find("output.weight");
    if (!m->embd_) return fail("missing token_embd.weight");
    if (!m->out_norm_) return fail("missing output_norm.weight");
    if (!m->out_w_) { m->out_w_ = m->embd_; c.tied_output = true; }

    if (!m->load_vector(m->out_norm_, m->out_norm_w_)) return fail("bad output_norm");

    // Llama-3 style RoPE scaling is stored as one factor per dimension pair.
    if (const TensorInfo* rf = g.find("rope_freqs.weight")) {
        if (!m->load_vector(rf, m->rope_freqs_)) return fail("bad rope_freqs");
        if (static_cast<int>(m->rope_freqs_.size()) != c.head_dim / 2)
            return fail("rope_freqs has the wrong length");
    }

    m->layers_.resize(static_cast<size_t>(c.n_layers));
    for (int i = 0; i < c.n_layers; ++i) {
        if (!m->bind_layer(i, err)) return nullptr;
    }

    // KV cache. Two caches, one per K and V, laid out [layer][pos][kv_dim].
    const uint64_t kv_elems = static_cast<uint64_t>(c.n_layers) * c.n_ctx * c.kv_dim();
    m->kv_bytes_ = kv_elems * sizeof(float) * 2;
    if (m->kv_bytes_ > (8ull << 30)) return fail("KV cache would exceed 8 GB");
    try {
        m->k_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);
        m->v_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);

        const int mb = m->max_batch_;
        m->x_.assign(static_cast<size_t>(mb) * c.n_embd, 0.0f);
        m->xb_.assign(static_cast<size_t>(mb) * c.n_embd, 0.0f);
        m->q_.assign(static_cast<size_t>(mb) * c.n_embd, 0.0f);
        m->k_.assign(static_cast<size_t>(mb) * c.kv_dim(), 0.0f);
        m->v_.assign(static_cast<size_t>(mb) * c.kv_dim(), 0.0f);
        m->attn_.assign(static_cast<size_t>(mb) * c.n_embd, 0.0f);
        m->proj_.assign(static_cast<size_t>(mb) * c.n_embd, 0.0f);
        m->gate_.assign(static_cast<size_t>(mb) * c.n_ff, 0.0f);
        m->up_.assign(static_cast<size_t>(mb) * c.n_ff, 0.0f);
        m->logits_.assign(static_cast<size_t>(c.vocab), 0.0f);
    } catch (const std::bad_alloc&) {
        return fail("not enough memory for the KV cache");
    }

    for (const auto& t : g.tensors()) m->weights_bytes_ += t.nbytes;
    return m;
}

bool LlamaModel::forward(const int32_t* tokens, int n_tok, int pos0) {
    if (!tokens || n_tok <= 0) return false;
    if (pos0 < 0 || pos0 + n_tok > cfg_.n_ctx) return false;
    if (n_tok > max_batch_) return false;

    const int E = cfg_.n_embd;
    const int FF = cfg_.n_ff;
    const int KV = cfg_.kv_dim();
    const int H = cfg_.n_head;
    const int HKV = cfg_.n_head_kv;
    const int D = cfg_.head_dim;

    // ---- token embedding: one dequantised row per token
    const size_t emb_row = ggml_row_size(embd_->type, static_cast<uint64_t>(E));
    for (int t = 0; t < n_tok; ++t) {
        const int32_t id = tokens[t];
        if (id < 0 || id >= cfg_.vocab) return false;
        const uint8_t* src = embd_->data + static_cast<size_t>(id) * emb_row;
        if (!dequant_row(embd_->type, src, row_ptr(x_, t, E), static_cast<uint64_t>(E)))
            return false;
    }

    for (int li = 0; li < cfg_.n_layers; ++li) {
        const LayerTensors& L = layers_[static_cast<size_t>(li)];

        // ---- attention block
        for (int t = 0; t < n_tok; ++t)
            rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, t, E), L.attn_norm_w.data(), E, cfg_.eps);

        matmul_batch(L.wq->type, L.wq->data, xb_.data(), n_tok, E, E, q_.data());
        matmul_batch(L.wk->type, L.wk->data, xb_.data(), n_tok, E, KV, k_.data());
        matmul_batch(L.wv->type, L.wv->data, xb_.data(), n_tok, E, KV, v_.data());
        add_bias(q_.data(), n_tok, E, L.bias_q);
        add_bias(k_.data(), n_tok, KV, L.bias_k);
        add_bias(v_.data(), n_tok, KV, L.bias_v);

        for (int t = 0; t < n_tok; ++t) {
            const int pos = pos0 + t;
            rope_apply(row_ptr(q_, t, E), row_ptr(k_, t, KV), H, HKV, D, pos,
                       cfg_.freq_base, rope_freqs_.empty() ? nullptr : rope_freqs_.data());
            float* kc = k_cache_.data() + (static_cast<size_t>(li) * cfg_.n_ctx + pos) * KV;
            float* vc = v_cache_.data() + (static_cast<size_t>(li) * cfg_.n_ctx + pos) * KV;
            std::memcpy(kc, row_ptr(k_, t, KV), sizeof(float) * KV);
            std::memcpy(vc, row_ptr(v_, t, KV), sizeof(float) * KV);
        }

        const float* kc_layer = k_cache_.data() + static_cast<size_t>(li) * cfg_.n_ctx * KV;
        const float* vc_layer = v_cache_.data() + static_cast<size_t>(li) * cfg_.n_ctx * KV;
        for (int t = 0; t < n_tok; ++t) {
            attention_one(row_ptr(q_, t, E), kc_layer, vc_layer, cfg_.n_ctx,
                           H, HKV, D, pos0 + t, row_ptr(attn_, t, E));
        }

        matmul_batch(L.wo->type, L.wo->data, attn_.data(), n_tok, E, E, proj_.data());
        add_bias(proj_.data(), n_tok, E, L.bias_o);
        for (int t = 0; t < n_tok; ++t) {
            float* xr = row_ptr(x_, t, E);
            const float* pr = row_ptr(proj_, t, E);
            for (int i = 0; i < E; ++i) xr[i] += pr[i];
        }

        // ---- feed-forward block
        for (int t = 0; t < n_tok; ++t)
            rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, t, E), L.ffn_norm_w.data(), E, cfg_.eps);

        matmul_batch(L.wg->type, L.wg->data, xb_.data(), n_tok, E, FF, gate_.data());
        matmul_batch(L.wu->type, L.wu->data, xb_.data(), n_tok, E, FF, up_.data());
        for (int t = 0; t < n_tok; ++t) {
            float* gr = row_ptr(gate_, t, FF);
            const float* ur = row_ptr(up_, t, FF);
            silu_inplace(gr, FF);
            for (int i = 0; i < FF; ++i) gr[i] *= ur[i];
        }
        matmul_batch(L.wd->type, L.wd->data, gate_.data(), n_tok, FF, E, proj_.data());
        for (int t = 0; t < n_tok; ++t) {
            float* xr = row_ptr(x_, t, E);
            const float* pr = row_ptr(proj_, t, E);
            for (int i = 0; i < E; ++i) xr[i] += pr[i];
        }
    }

    // ---- final norm + output projection for the last token only
    const float* last = row_ptr(x_, n_tok - 1, E);
    std::vector<float> normed(static_cast<size_t>(E));
    rmsnorm(normed.data(), last, out_norm_w_.data(), E, cfg_.eps);
    matmul_batch(out_w_->type, out_w_->data, normed.data(), 1, E, cfg_.vocab,
                 logits_.data());

    kv_len_ = pos0 + n_tok;
    return true;
}

int LlamaModel::argmax() const {
    int best = 0;
    float bv = -std::numeric_limits<float>::infinity();
    for (int i = 0; i < cfg_.vocab; ++i) {
        if (logits_[static_cast<size_t>(i)] > bv) { bv = logits_[static_cast<size_t>(i)]; best = i; }
    }
    return best;
}

} // namespace fish
