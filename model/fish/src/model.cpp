/*
 * FISH â€” decoder-only transformer implementation.
 *
 * See model.h for the list of architectures. The forward pass is one
 * function; every architectural difference is a flag in LlamaConfig that was
 * decided once at load time from the GGUF metadata.
 */
#ifdef _WIN32
// PrefetchVirtualMemory is a Windows 8 API; the SDK headers hide it below
// that level, and the C++ standard headers pin the level first, so this must
// be the very first thing in the translation unit.
#  undef _WIN32_WINNT
#  define _WIN32_WINNT 0x0A00
#endif

#include <fish/model.h>
#include <fish/backend.h>
#include <fish/ops.h>
#include <fish/threadpool.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <sys/mman.h>
#  include <unistd.h>
#endif

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

void ensure(std::vector<float>& v, size_t n) { if (v.size() < n) v.resize(n); }

// Byte offset of expert `e` inside a 3-D [n_in][n_out][n_expert] tensor.
const uint8_t* expert_ptr(const TensorInfo* t, int e) {
    const size_t per_expert = ggml_row_size(t->type, t->ne[0]) * static_cast<size_t>(t->ne[1]);
    return t->data + per_expert * static_cast<size_t>(e);
}

// Prefetch hint for a byte range of the mapped file. Advisory: failure is
// harmless, the page fault path still works.
void os_prefetch(const uint8_t* begin, size_t len) {
    if (!begin || len == 0) return;
    const uintptr_t page = 4096;
    const uintptr_t b = reinterpret_cast<uintptr_t>(begin) & ~(page - 1);
    const uintptr_t e = (reinterpret_cast<uintptr_t>(begin) + len + page - 1) & ~(page - 1);
#ifdef _WIN32
    WIN32_MEMORY_RANGE_ENTRY entry;
    entry.VirtualAddress = reinterpret_cast<PVOID>(b);
    entry.NumberOfBytes = static_cast<SIZE_T>(e - b);
    PrefetchVirtualMemory(GetCurrentProcess(), 1, &entry, 0);
#else
    madvise(reinterpret_cast<void*>(b), static_cast<size_t>(e - b), MADV_WILLNEED);
#endif
}

// Drop a byte range of the mapped file from this process's working set. The
// data stays on disk (the mapping is read-only), so it is purely a hint that
// these pages may be reclaimed first. On Windows, VirtualUnlock on pages that
// were never locked is the documented way to trim them from the working set.
void os_evict(const uint8_t* begin, size_t len) {
    if (!begin || len == 0) return;
    const uintptr_t page = 4096;
    const uintptr_t b = reinterpret_cast<uintptr_t>(begin) & ~(page - 1);
    const uintptr_t e = (reinterpret_cast<uintptr_t>(begin) + len + page - 1) & ~(page - 1);
#ifdef _WIN32
    VirtualUnlock(reinterpret_cast<LPVOID>(b), static_cast<SIZE_T>(e - b));
#else
    madvise(reinterpret_cast<void*>(b), static_cast<size_t>(e - b), MADV_DONTNEED);
#endif
}

// Budget knobs. The defaults leave the machine usable: never more than 80% of
// the RAM that is free at load, never more than 85% of free VRAM.
uint64_t env_mb(const char* name, uint64_t def) {
    if (const char* v = std::getenv(name)) {
        const long long mb = std::atoll(v);
        if (mb > 0) return static_cast<uint64_t>(mb) << 20;
    }
    return def;
}

struct ArchEntry { const char* name; Arch kind; };
const ArchEntry kArchs[] = {
    {"llama", Arch::Llama},   {"qwen2", Arch::Qwen2}, {"qwen2moe", Arch::Qwen2},
    {"qwen3", Arch::Qwen3},   {"qwen3moe", Arch::Qwen3},
    {"phi3", Arch::Phi3},     {"gemma2", Arch::Gemma2},
};

bool arch_lookup(const std::string& s, Arch* out) {
    for (const auto& a : kArchs) if (s == a.name) { *out = a.kind; return true; }
    return false;
}

} // namespace

const char* arch_name(Arch a) {
    switch (a) {
        case Arch::Llama:  return "llama";
        case Arch::Qwen2:  return "qwen2";
        case Arch::Qwen3:  return "qwen3";
        case Arch::Phi3:   return "phi3";
        case Arch::Gemma2: return "gemma2";
    }
    return "unknown";
}

bool arch_supported(const std::string& gguf_arch) {
    Arch a;
    return arch_lookup(gguf_arch, &a);
}

LlamaModel::~LlamaModel() {
    {
        std::lock_guard<std::mutex> lk(prefetch_mu_);
        prefetch_stop_ = true;
    }
    prefetch_cv_.notify_all();
    if (prefetch_thread_.joinable()) prefetch_thread_.join();

    // Give the device memory back: the TensorInfo pointers die with gguf_, so
    // the backend must not be left holding them.
    if (ComputeBackend* gpu = gpu_backend()) {
        if (gpu_span_) gpu->span_release();
        for (const LayerTensors& L : layers_) {
            for (const TensorInfo* t : {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd,
                                        L.shexp_gate, L.shexp_up, L.shexp_down}) {
                if (t && gpu->resident(t)) gpu->release(t);
            }
        }
        for (const TensorInfo* t : {out_w_, embd_}) {
            if (t && gpu->resident(t)) gpu->release(t);
        }
    }
}

// ---------------------------------------------------------------- probe

bool probe_model(const std::string& path, ModelProfile* out, std::string* err) {
    if (!out) return false;
    std::string e;
    auto g = GgufFile::open(path, &e);
    if (!g) {
        if (err) *err = e;
        return false;
    }

    const std::string a = g->str("general.architecture", "llama");
    auto key = [&](const char* k) { return a + "." + k; };

    const int n_layers = static_cast<int>(g->u64(key("block_count"), 0));
    const int n_embd = static_cast<int>(g->u64(key("embedding_length"), 0));
    const int n_ff = static_cast<int>(g->u64(key("feed_forward_length"), 0));
    const int n_head = static_cast<int>(g->u64(key("attention.head_count"), 0));
    const int n_head_kv = static_cast<int>(g->u64(key("attention.head_count_kv"), n_head));
    int head_dim = static_cast<int>(g->u64(key("attention.key_length"), 0));
    if (head_dim <= 0 && n_head > 0) head_dim = n_embd / n_head;

    ModelProfile p;
    p.arch = a;
    p.file_bytes = g->file_size();
    p.n_layers = n_layers;
    p.n_embd = n_embd;
    p.n_ff = n_ff;
    p.n_ctx_max = static_cast<int>(g->u64(key("context_length"), 4096));
    p.n_experts = static_cast<int>(g->u64(key("expert_count"), 0));
    p.experts_per_token = static_cast<int>(g->u64(key("expert_used_count"), 0));

    uint64_t blk_bytes = 0, expert_bytes = 0;
    for (const auto& t : g->tensors()) {
        p.weights_bytes += t.nbytes;
        if (t.name.rfind("blk.", 0) == 0) blk_bytes += t.nbytes;
        if (t.is_expert && t.ne.size() == 3 && t.ne[2] > 0) expert_bytes += t.nbytes / t.ne[2];
    }
    p.per_layer_bytes = n_layers > 0 ? blk_bytes / static_cast<uint64_t>(n_layers) : 0;
    p.expert_bytes = n_layers > 0 ? expert_bytes / static_cast<uint64_t>(n_layers) : 0;
    // K and V, f32, per token across every layer.
    p.kv_per_token_bytes = static_cast<uint64_t>(n_layers) * 2ull *
                           static_cast<uint64_t>(n_head_kv) *
                           static_cast<uint64_t>(head_dim) * sizeof(float);

    *out = std::move(p);
    return true;
}

// ---------------------------------------------------------------- load

bool LlamaModel::load_vector(const TensorInfo* t, std::vector<float>& out) {
    if (!t) return false;
    const uint64_t n = t->n_elements();
    if (n == 0 || n > (1ull << 30)) return false;
    out.resize(static_cast<size_t>(n));
    if (!dequant_row(t->type, t->data, out.data(), n)) return false;
    return true;
}

bool LlamaModel::bind_layer(int i, std::string* err) {
    const std::string p = "blk." + std::to_string(i) + ".";
    LayerTensors& L = layers_[static_cast<size_t>(i)];
    const GgufFile& g = *gguf_;
    auto fail = [&](const std::string& why) { if (err) *err = why; return false; };
    auto need = [&](const char* name) -> const TensorInfo* {
        const TensorInfo* t = g.find(p + name);
        return t;
    };
    auto norm_vec = [&](const TensorInfo* t, std::vector<float>& v, const char* what) {
        if (!load_vector(t, v)) return fail(std::string("bad ") + what + " in layer " + std::to_string(i));
        if (cfg_.norm_plus_one) for (float& x : v) x += 1.0f;
        return true;
    };

    L.attn_norm = need("attn_norm.weight");
    L.ffn_norm  = need("ffn_norm.weight");
    L.wq = need("attn_q.weight");
    L.wk = need("attn_k.weight");
    L.wv = need("attn_v.weight");
    L.wo = need("attn_output.weight");
    L.wqkv = need("attn_qkv.weight");
    L.wg = need("ffn_gate.weight");
    L.wu = need("ffn_up.weight");
    L.wd = need("ffn_down.weight");

    if (!L.attn_norm) return fail("missing tensor " + p + "attn_norm.weight");
    if (!L.ffn_norm)  return fail("missing tensor " + p + "ffn_norm.weight");
    if (!L.wo)        return fail("missing tensor " + p + "attn_output.weight");
    if (!L.wqkv && !(L.wq && L.wk && L.wv))
        return fail("missing attention projections in layer " + std::to_string(i));
    if (L.wqkv) {
        const uint64_t want = static_cast<uint64_t>(cfg_.q_dim() + 2 * cfg_.kv_dim());
        if (L.wqkv->ne.size() < 2 || L.wqkv->ne[1] != want)
            return fail("fused attn_qkv has an unexpected shape in layer " + std::to_string(i));
    }

    if (cfg_.is_moe()) {
        L.gate_inp  = need("ffn_gate_inp.weight");
        L.gate_exps = need("ffn_gate_exps.weight");
        L.up_exps   = need("ffn_up_exps.weight");
        L.down_exps = need("ffn_down_exps.weight");
        if (!L.gate_inp || !L.gate_exps || !L.up_exps || !L.down_exps)
            return fail("missing expert tensors in layer " + std::to_string(i) +
                        " (FISH expects ffn_gate_inp / ffn_*_exps)");
        for (const TensorInfo* t : {L.gate_exps, L.up_exps, L.down_exps}) {
            if (t->ne.size() != 3 || static_cast<int>(t->ne[2]) != cfg_.n_experts)
                return fail("expert tensor " + t->name + " is not [n][m][" +
                            std::to_string(cfg_.n_experts) + "]");
        }
        if (static_cast<int>(L.gate_exps->ne[1]) != cfg_.n_ff_exp)
            cfg_.n_ff_exp = static_cast<int>(L.gate_exps->ne[1]);
        L.shexp_gate = need("ffn_gate_shexp.weight");
        L.shexp_up   = need("ffn_up_shexp.weight");
        L.shexp_down = need("ffn_down_shexp.weight");
        L.shexp_gate_inp = need("ffn_gate_inp_shexp.weight");
        if (L.shexp_gate && L.shexp_up && L.shexp_down)
            cfg_.n_ff_shexp = static_cast<int>(L.shexp_gate->ne[1]);
    } else {
        if (!L.wu || !L.wd) return fail("missing FFN tensors in layer " + std::to_string(i));
        if (!L.wg) {
            // Phi-3 style fused gate|up: ffn_up holds 2 * n_ff rows.
            if (L.wu->ne.size() < 2 || L.wu->ne[1] != static_cast<uint64_t>(2 * cfg_.n_ff))
                return fail("missing tensor " + p + "ffn_gate.weight");
        }
    }

    if (!norm_vec(L.attn_norm, L.attn_norm_w, "attn_norm")) return false;
    if (!norm_vec(L.ffn_norm, L.ffn_norm_w, "ffn_norm")) return false;

    if (cfg_.kind == Arch::Gemma2) {
        const TensorInfo* pa = need("post_attention_norm.weight");
        const TensorInfo* pf = need("post_ffw_norm.weight");
        if (!pa || !pf) return fail("gemma2 layer " + std::to_string(i) + " lacks post norms");
        if (!norm_vec(pa, L.post_attn_norm_w, "post_attention_norm")) return false;
        if (!norm_vec(pf, L.post_ffn_norm_w, "post_ffw_norm")) return false;
    }
    if (const TensorInfo* qn = need("attn_q_norm.weight")) {
        const TensorInfo* kn = need("attn_k_norm.weight");
        if (!kn) return fail("attn_q_norm without attn_k_norm in layer " + std::to_string(i));
        if (!norm_vec(qn, L.q_norm_w, "attn_q_norm")) return false;
        if (!norm_vec(kn, L.k_norm_w, "attn_k_norm")) return false;
        cfg_.qk_norm = true;
    }

    // Optional biases (Qwen 2 and friends); Llama 3 has none.
    if (const TensorInfo* b = need("attn_q.bias")) load_vector(b, L.bias_q);
    if (const TensorInfo* b = need("attn_k.bias")) load_vector(b, L.bias_k);
    if (const TensorInfo* b = need("attn_v.bias")) load_vector(b, L.bias_v);
    if (const TensorInfo* b = need("attn_output.bias")) load_vector(b, L.bias_o);
    if (const TensorInfo* b = need("attn_qkv.bias")) load_vector(b, L.bias_qkv);

    // Byte range for prefetch.
    L.file_begin = std::numeric_limits<uint64_t>::max();
    L.file_end = 0;
    for (const auto& t : g.tensors()) {
        if (t.layer != i) continue;
        L.file_begin = std::min(L.file_begin, t.offset);
        L.file_end = std::max(L.file_end, t.offset + t.nbytes);
    }
    if (L.file_end == 0) L.file_begin = 0;
    return true;
}

std::unique_ptr<LlamaModel> LlamaModel::load(const std::string& path, int n_ctx,
                                             std::string* err) {
    return load(path, n_ctx, LoadOptions{}, err);
}

std::unique_ptr<LlamaModel> LlamaModel::load(const std::string& path, int n_ctx,
                                             const LoadOptions& opts, std::string* err) {
    auto fail = [&](const std::string& why) -> std::unique_ptr<LlamaModel> {
        if (err) *err = why;
        return nullptr;
    };

    std::unique_ptr<LlamaModel> m(new (std::nothrow) LlamaModel());
    if (!m) return fail("out of memory");
    m->opts_ = opts;

    std::string gerr;
    m->gguf_ = GgufFile::open(path, &gerr);
    if (!m->gguf_) return fail(gerr);

    const GgufFile& g = *m->gguf_;
    LlamaConfig& c = m->cfg_;
    c.arch = g.str("general.architecture", "llama");
    if (!arch_lookup(c.arch, &c.kind))
        return fail("unsupported architecture: " + c.arch +
                    " (FISH runs llama, qwen2, qwen3, phi3, gemma2 and their MoE variants)");
    auto key = [&](const char* k) { return c.arch + "." + k; };

    for (const auto& t : g.tensors()) {
        if (!quant_supported(t.type))
            return fail(std::string("tensor type ") + ggml_type_name(t.type) +
                        " is not supported yet (" + t.name + ")");
    }

    c.n_layers  = static_cast<int>(g.u64(key("block_count"), 0));
    c.n_embd    = static_cast<int>(g.u64(key("embedding_length"), 0));
    c.n_ff      = static_cast<int>(g.u64(key("feed_forward_length"), 0));
    c.n_head    = static_cast<int>(g.u64(key("attention.head_count"), 0));
    c.n_head_kv = static_cast<int>(g.u64(key("attention.head_count_kv"), c.n_head));
    c.eps       = static_cast<float>(g.f64(key("attention.layer_norm_rms_epsilon"), 1e-5));
    c.freq_base = static_cast<float>(g.f64(key("rope.freq_base"), 10000.0));
    c.bos       = static_cast<int>(g.i64("tokenizer.ggml.bos_token_id", 1));
    c.eos       = static_cast<int>(g.i64("tokenizer.ggml.eos_token_id", 2));
    c.n_ctx_train = static_cast<int>(g.u64(key("context_length"), 4096));

    // Vocabulary: metadata, else the token table, else the embedding matrix.
    c.vocab = static_cast<int>(g.u64(key("vocab_size"), 0));
    if (c.vocab <= 0) {
        if (const MetaValue* toks = g.meta("tokenizer.ggml.tokens")) c.vocab = static_cast<int>(toks->sarr.size());
    }
    m->embd_ = g.find("token_embd.weight");
    if (!m->embd_) return fail("missing token_embd.weight");
    if (c.vocab <= 0 && m->embd_->ne.size() > 1) c.vocab = static_cast<int>(m->embd_->ne[1]);

    if (c.n_layers <= 0 || c.n_embd <= 0 || c.n_head <= 0 || c.n_ff <= 0 || c.vocab <= 0)
        return fail("incomplete model metadata");
    if (c.n_head_kv <= 0 || c.n_head % c.n_head_kv != 0) return fail("n_head is not a multiple of n_head_kv");

    c.head_dim = static_cast<int>(g.u64(key("attention.key_length"), 0));
    if (c.head_dim <= 0) c.head_dim = c.n_embd / c.n_head;
    if (c.head_dim <= 0 || c.head_dim % 2 != 0) return fail("bad head_dim");

    // ---- mixture of experts
    c.n_experts = static_cast<int>(g.u64(key("expert_count"), 0));
    c.n_experts_per_tok = static_cast<int>(g.u64(key("expert_used_count"), 0));
    c.n_ff_exp = static_cast<int>(g.u64(key("expert_feed_forward_length"), c.n_ff));
    c.n_ff_shexp = static_cast<int>(g.u64(key("expert_shared_feed_forward_length"), 0));
    if (c.n_experts > 0 && c.n_experts_per_tok <= 0) c.n_experts_per_tok = 2;
    if (c.n_experts_per_tok > c.n_experts) c.n_experts_per_tok = c.n_experts;
    const bool default_norm = !(c.kind == Arch::Qwen2 && c.arch == "qwen2moe");
    c.expert_weights_norm = g.flag(key("expert_weights_norm"), default_norm);

    // ---- architecture knobs
    c.rope.head_dim = c.head_dim;
    c.rope.rot_dim = static_cast<int>(g.u64(key("rope.dimension_count"), c.head_dim));
    c.rope.freq_base = c.freq_base;
    c.rope.neox = (c.kind == Arch::Qwen2 || c.kind == Arch::Qwen3 ||
                   c.kind == Arch::Phi3 || c.kind == Arch::Gemma2);
    if (c.kind == Arch::Gemma2) {
        c.norm_plus_one = true;
        c.gelu = true;
        c.embd_scale = std::sqrt(static_cast<float>(c.n_embd));
        c.attn_softcap = static_cast<float>(g.f64(key("attn_logit_softcapping"), 50.0));
        c.final_softcap = static_cast<float>(g.f64(key("final_logit_softcapping"), 30.0));
        c.sliding_window = static_cast<int>(g.u64(key("attention.sliding_window"), 4096));
    }

    // ---- context
    const int model_ctx = c.n_ctx_train;
    c.n_ctx = n_ctx > 0 ? std::min(n_ctx, model_ctx > 0 ? model_ctx : n_ctx) : std::min(4096, model_ctx);
    if (c.n_ctx < 32) return fail("context too small");

    // ---- rope scaling
    {
        const std::string type = g.str(key("rope.scaling.type"), "none");
        const float factor = static_cast<float>(g.f64(key("rope.scaling.factor"), 1.0));
        const int orig = static_cast<int>(g.u64(key("rope.scaling.original_context_length"), 0));
        const float attn_factor = static_cast<float>(g.f64(key("rope.scaling.attn_factor"), 1.0));
        if (type == "linear" && factor > 0.0f) {
            c.rope.freq_scale = 1.0f / factor;
        } else if (type == "yarn" && factor > 0.0f) {
            c.rope.yarn_factor = factor;
            c.rope.yarn_orig_ctx = orig > 0 ? orig : model_ctx;
            c.rope.yarn_attn_factor = attn_factor;
        } else if (type == "longrope") {
            // Phi-3: per-pair factors; short set within the original window,
            // long set beyond it. The attention factor scales the rotation.
            const TensorInfo* f = (orig > 0 && c.n_ctx > orig) ? g.find("rope_factors_long.weight")
                                                                : g.find("rope_factors_short.weight");
            if (!f) f = g.find("rope_factors_short.weight");
            if (f && !m->load_vector(f, m->rope_freqs_)) return fail("bad rope factors");
            c.rope.mscale = attn_factor;
        }
    }
    // Llama-3 style RoPE scaling is stored as one factor per dimension pair.
    if (m->rope_freqs_.empty()) {
        if (const TensorInfo* rf = g.find("rope_freqs.weight")) {
            if (!m->load_vector(rf, m->rope_freqs_)) return fail("bad rope_freqs");
        }
    }
    if (!m->rope_freqs_.empty()) {
        if (static_cast<int>(m->rope_freqs_.size()) != c.rope.rot_dim / 2)
            return fail("rope factors have the wrong length");
        c.rope.freq_factors = m->rope_freqs_.data();
    }
    rope_build(m->rope_, c.n_ctx, c.rope);

    // ---- output
    m->out_norm_ = g.find("output_norm.weight");
    m->out_w_    = g.find("output.weight");
    if (!m->out_norm_) return fail("missing output_norm.weight");
    if (!m->out_w_) { m->out_w_ = m->embd_; c.tied_output = true; }
    if (!m->load_vector(m->out_norm_, m->out_norm_w_)) return fail("bad output_norm");
    if (c.norm_plus_one) for (float& x : m->out_norm_w_) x += 1.0f;

    m->layers_.resize(static_cast<size_t>(c.n_layers));
    for (int i = 0; i < c.n_layers; ++i) {
        if (!m->bind_layer(i, err)) return nullptr;
    }

    // ---- KV cache and work buffers
    const int E = c.n_embd, Q = c.q_dim(), KV = c.kv_dim();
    const uint64_t kv_elems = static_cast<uint64_t>(c.n_layers) * c.n_ctx * KV;
    m->kv_bytes_ = kv_elems * sizeof(float) * 2;
    if (m->kv_bytes_ > (16ull << 30)) return fail("KV cache would exceed 16 GB");
    try {
        m->k_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);
        m->v_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);

        const int mb = m->max_batch_;
        const int ff = std::max(c.n_ff, std::max(c.n_ff_exp, c.n_ff_shexp));
        m->x_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->xb_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->qkv_.assign(static_cast<size_t>(mb) * (Q + 2 * KV), 0.0f);
        m->q_.assign(static_cast<size_t>(mb) * Q, 0.0f);
        m->k_.assign(static_cast<size_t>(mb) * KV, 0.0f);
        m->v_.assign(static_cast<size_t>(mb) * KV, 0.0f);
        m->attn_.assign(static_cast<size_t>(mb) * Q, 0.0f);
        m->proj_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->gate_.assign(static_cast<size_t>(mb) * 2 * ff, 0.0f);
        m->up_.assign(static_cast<size_t>(mb) * ff, 0.0f);
        m->ffn_out_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->logits_.assign(static_cast<size_t>(c.vocab), 0.0f);
        if (c.is_moe()) m->router_.assign(static_cast<size_t>(mb) * c.n_experts, 0.0f);
    } catch (const std::bad_alloc&) {
        return fail("not enough memory for the KV cache");
    }

    for (const auto& t : g.tensors()) m->weights_bytes_ += t.nbytes;

    // The adapter is merged before placement so the GPU upload, the span and
    // the RAM/disk tiers all see the merged weights and nothing else changes.
    if (!opts.adapter_path.empty() && !m->apply_adapter(err)) return nullptr;

    m->place_layers();
    m->prepare_span();
    if (m->stream_) m->prefetch_thread_ = std::thread([mp = m.get()] { mp->prefetch_loop(); });
    return m;
}

// ---------------------------------------------------------------- adapter

const TensorInfo* LlamaModel::weight(const std::string& name) const {
    if (embd_ && embd_->name == name) return embd_;
    if (out_w_ && out_w_->name == name) return out_w_;
    for (const LayerTensors& L : layers_)
        for (const TensorInfo* t : {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd, L.gate_inp,
                                    L.gate_exps, L.up_exps, L.down_exps,
                                    L.shexp_gate, L.shexp_up, L.shexp_down, L.shexp_gate_inp})
            if (t && t->name == name) return t;
    return nullptr;
}

bool LlamaModel::apply_adapter(std::string* err) {
    auto fail = [&](const std::string& why) { if (err) *err = "adapter: " + why; return false; };
    const auto t0 = std::chrono::steady_clock::now();
    std::string e;
    auto ad = GgufFile::open(opts_.adapter_path, &e);
    if (!ad) return fail(e);
    std::vector<LoraTarget> targets;
    float alpha = 0.0f;
    if (!lora_plan(*gguf_, *ad, &targets, &alpha, &e)) return fail(e);

    // Every target must be a weight the forward pass reads; find the slots
    // that hold it. token_embd only replaces the input lookup: with tied
    // embeddings the output projection keeps the base tensor, which is what
    // llama.cpp and PEFT do at runtime (the LoRA wraps the embedding only).
    LoraMergeOptions mo;
    mo.scale = opts_.adapter_scale;
    mo.alpha = alpha;
    mo.rounding = opts_.adapter_rounding;
    if (const char* r = std::getenv("FISH_ADAPTER_ROUNDING"))
        if (std::string(r) == "nearest") mo.rounding = MergeRounding::Nearest;
    std::string mtype = opts_.adapter_merge_type;
    if (mtype.empty()) if (const char* v = std::getenv("FISH_ADAPTER_MERGE_TYPE")) mtype = v;
    if (!mtype.empty() && mtype != "same") {
        mo.keep_type = false;
        if (mtype == "f16") mo.out_type = GgmlType::F16;
        else if (mtype == "bf16") mo.out_type = GgmlType::BF16;
        else if (mtype == "f32") mo.out_type = GgmlType::F32;
        else if (mtype == "q8_0") mo.out_type = GgmlType::Q8_0;
        else return fail("unknown merge type '" + mtype + "' (same, f16, bf16, f32, q8_0)");
    }

    struct Job { LoraTarget t; std::vector<const TensorInfo**> slots; };
    std::vector<Job> jobs;
    uint64_t need = 0;
    for (const LoraTarget& t : targets) {
        Job j{t, {}};
        if (t.base == embd_) j.slots.push_back(&embd_);
        else if (t.base == out_w_) j.slots.push_back(&out_w_);
        else
            for (LayerTensors& L : layers_)
                for (const TensorInfo** s : {&L.wq, &L.wk, &L.wv, &L.wo, &L.wqkv, &L.wg, &L.wu, &L.wd,
                                             &L.gate_inp, &L.shexp_gate, &L.shexp_up, &L.shexp_down})
                    if (*s == t.base) j.slots.push_back(s);
        if (j.slots.empty())
            return fail("adapter targets " + t.base->name + ", which FISH does not use as a projection weight");
        need += lora_merged_bytes(t, mo);
        jobs.push_back(std::move(j));
    }

    // The merged copies are private memory that cannot be paged back to the
    // file, so they must fit the RAM budget outright (with the KV cache).
    const uint64_t avail = detect_ram_available_bytes();
    const uint64_t budget = env_mb("FISH_RAM_BUDGET_MB", avail - avail / 5);
    if (avail > 0 && need + kv_bytes_ > budget)
        return fail("merged copies of " + std::to_string(jobs.size()) + " tensors need " +
                    std::to_string(need >> 20) + " MB of RAM plus " + std::to_string(kv_bytes_ >> 20) +
                    " MB KV cache; the RAM budget is " + std::to_string(budget >> 20) +
                    " MB (use a merged GGUF, a smaller context, or raise FISH_RAM_BUDGET_MB)");

    LoraReport rep;
    rep.path = opts_.adapter_path;
    const size_t slash = rep.path.find_last_of("/\\");
    rep.name = slash == std::string::npos ? rep.path : rep.path.substr(slash + 1);
    rep.alpha = alpha;
    rep.scale = opts_.adapter_scale;
    rep.stochastic = mo.rounding == MergeRounding::Stochastic;
    rep.merged_type = mo.keep_type ? "same" : ggml_type_name(mo.out_type);
    rep.rank_min = jobs.empty() ? 0 : jobs[0].t.rank;
    for (Job& j : jobs) {
        const TensorInfo* base = j.t.base;
        const uint64_t bytes = lora_merged_bytes(j.t, mo);
        std::unique_ptr<uint8_t[]> raw(new (std::nothrow) uint8_t[bytes + 64]);
        if (!raw) return fail("out of memory for the merged copy of " + base->name);
        uint8_t* data = reinterpret_cast<uint8_t*>((reinterpret_cast<uintptr_t>(raw.get()) + 63) & ~uintptr_t(63));
        if (!lora_merge(j.t, mo, data, &rep.stats, &e)) return fail(e);
        owned_tensors_.push_back(*base);
        TensorInfo& nt = owned_tensors_.back();
        nt.data = data;
        nt.nbytes = bytes;
        if (!mo.keep_type) nt.type = mo.out_type;
        for (const TensorInfo** s : j.slots) *s = &nt;
        owned_bufs_.push_back(std::move(raw));
        if (base->layer < 0) owned_extra_bytes_ += bytes;
        // The file's pages of this tensor are no longer read (unless a tied
        // output still uses the base embedding): let the OS drop them.
        if (out_w_ != base) os_evict(base->data, static_cast<size_t>(base->nbytes));
        rep.rank_min = std::min(rep.rank_min, j.t.rank);
        rep.rank_max = std::max(rep.rank_max, j.t.rank);
        rep.merged_bytes += bytes;
        ++rep.tensors;
    }
    rep.effective_scale = lora_effective_scale(rep.scale, alpha, rep.rank_max);
    rep.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    rep.active = true;
    adapter_ = std::move(rep);
    return true;
}

void LlamaModel::reset_kv() {
    kv_len_ = 0;
    if (gpu_span_) if (ComputeBackend* gpu = gpu_backend()) gpu->span_kv_truncate(0);
}

void LlamaModel::set_kv_len(int n) {
    kv_len_ = n;
    if (gpu_span_) if (ComputeBackend* gpu = gpu_backend()) gpu->span_kv_truncate(n);
}

// Offer the GPU layers to the backend as one span. The backend may decline
// (it then stays on per-matmul routing); nothing changes for the CPU layers.
void LlamaModel::prepare_span() {
    gpu_span_ = false;
    ComputeBackend* gpu = gpu_backend();
    if (!gpu || gpu_layers_ <= 0 || cfg_.is_moe() || !opts_.allow_span) return;
    if (const char* v = std::getenv("FISH_GPU_SPAN")) if (std::atoi(v) == 0) return;

    const int begin = cfg_.n_layers - gpu_layers_;
    span_layers_.clear();
    span_layers_.reserve(static_cast<size_t>(gpu_layers_));
    auto vec = [](const std::vector<float>& v) -> const float* { return v.empty() ? nullptr : v.data(); };
    for (int li = begin; li < cfg_.n_layers; ++li) {
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        ComputeBackend::SpanLayer s;
        s.wq = L.wq; s.wk = L.wk; s.wv = L.wv; s.wqkv = L.wqkv; s.wo = L.wo;
        s.wg = L.wg; s.wu = L.wu; s.wd = L.wd;
        s.attn_norm = vec(L.attn_norm_w);
        s.ffn_norm = vec(L.ffn_norm_w);
        s.post_attn_norm = vec(L.post_attn_norm_w);
        s.post_ffn_norm = vec(L.post_ffn_norm_w);
        s.q_norm = vec(L.q_norm_w);
        s.k_norm = vec(L.k_norm_w);
        s.bias_q = vec(L.bias_q); s.bias_k = vec(L.bias_k); s.bias_v = vec(L.bias_v);
        s.bias_o = vec(L.bias_o); s.bias_qkv = vec(L.bias_qkv);
        s.sliding_window = cfg_.sliding_window > 0 && (li % 2) == 0;
        span_layers_.push_back(s);
    }

    ComputeBackend::SpanParams p;
    p.layer_begin = begin;
    p.layer_end = cfg_.n_layers;
    p.n_embd = cfg_.n_embd;
    p.n_head = cfg_.n_head;
    p.n_head_kv = cfg_.n_head_kv;
    p.head_dim = cfg_.head_dim;
    p.n_ff = cfg_.n_ff;
    p.n_ctx = cfg_.n_ctx;
    p.eps = cfg_.eps;
    p.attn_scale = cfg_.attn_scale;
    p.attn_softcap = cfg_.attn_softcap;
    p.window = cfg_.sliding_window;
    p.gelu = cfg_.gelu;
    p.rope_cos = rope_.cos_.data();
    p.rope_sin = rope_.sin_.data();
    p.rot_dim = rope_.rot_dim;
    p.rope_neox = rope_.neox;
    p.layers = span_layers_.data();
    gpu_span_ = gpu->span_prepare(p);

    // No room for the span's KV cache / arena: give back the lowest GPU
    // layer (the last one placed) and offer the shorter span, a few times.
    // A span over fewer layers beats per-matmul routing over more of them.
    for (int attempt = 0; !gpu_span_ && attempt < 4 && gpu_layers_ > 1; ++attempt) {
        const int li = cfg_.n_layers - gpu_layers_;
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        for (const TensorInfo* t : {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd})
            if (t && gpu->resident(t)) gpu->release(t);
        --gpu_layers_;
        ram_layers_ = std::max(ram_layers_, 0);
        span_layers_.erase(span_layers_.begin());
        p.layer_begin = cfg_.n_layers - gpu_layers_;
        p.layers = span_layers_.data();
        gpu_span_ = gpu->span_prepare(p);
    }
    gpu_bytes_ = gpu->bytes_resident();

    // The span keeps its own KV cache in VRAM; the CPU copy for those layers
    // would only waste RAM (512 MB on an 8B at 4096). Keep the CPU cache for
    // the CPU layers only — they are the prefix [0, layer_begin).
    if (gpu_span_) {
        const int cpu_layers = cfg_.n_layers - gpu_layers_;
        const size_t keep = static_cast<size_t>(cpu_layers) * cfg_.n_ctx * cfg_.kv_dim();
        k_cache_.resize(keep);
        k_cache_.shrink_to_fit();
        v_cache_.resize(keep);
        v_cache_.shrink_to_fit();
        kv_bytes_ = static_cast<uint64_t>(keep) * sizeof(float) * 2;
    }
}

// ---------------------------------------------------------------- placement
//
// Three tiers, decided once at load:
//   GPU   the top layers' projections, as many as fit the VRAM budget;
//   RAM   the rest, if the weights fit the RAM budget (mmap keeps them hot);
//   disk  otherwise the remaining layers stream through a small resident
//         window: prefetch ahead, evict behind, so RSS stays inside budget.
void LlamaModel::place_layers() {
    const uint64_t avail = detect_ram_available_bytes();
    const uint64_t ram_budget = env_mb("FISH_RAM_BUDGET_MB", avail - avail / 5);  // 80% of free

    // ---- GPU tier
    gpu_layers_ = 0;
    gpu_bytes_ = 0;
    ComputeBackend* gpu = gpu_backend();
    if (gpu) {
        const uint64_t vfree = gpu->memory_free();
        const uint64_t vram_budget = env_mb("FISH_VRAM_BUDGET_MB", vfree - vfree * 15 / 100);
        int want = cfg_.n_layers;
        if (const char* v = std::getenv("FISH_GPU_LAYERS")) want = std::max(0, std::atoi(v));
        if (opts_.gpu_layers >= 0) want = std::min(want, opts_.gpu_layers);

        // Memory the span will need on top of the weights, reserved up front
        // so placement does not spend the whole budget and then find no room
        // for the device KV cache: f16 K+V per position per layer, plus a
        // small arena for activations.
        const bool span_wanted = !cfg_.is_moe() &&
            !(std::getenv("FISH_GPU_SPAN") && std::atoi(std::getenv("FISH_GPU_SPAN")) == 0);
        const uint64_t kv_per_layer = span_wanted
            ? static_cast<uint64_t>(cfg_.n_ctx) * static_cast<uint64_t>(cfg_.kv_dim()) * 4ull : 0;
        const uint64_t arena = span_wanted ? (32ull << 20) : 0;
        // Re-quantised uploads occupy less than their file bytes; the exact
        // size is the backend's business, so the pre-check is only a coarse
        // guard against pointless attempts and upload() has the final word.
        const bool requant = std::getenv("FISH_GPU_REQUANT") && std::atoi(std::getenv("FISH_GPU_REQUANT")) != 0;
        const double est = requant ? 0.70 : 1.0;

        const uint64_t resident0 = gpu->bytes_resident();
        // The output projection first: it is read on every token whatever the
        // layer split, and it is the single largest matmul in the model.
        if (want > 0 && out_w_ && gpu->supports(out_w_->type) &&
            static_cast<uint64_t>(out_w_->nbytes * est) + arena <= vram_budget) {
            gpu->upload(out_w_);
        }
        // Offload from the last layer backwards: with a partial offload the
        // CPU then runs the first layers while the GPU layers' weights already
        // sit on the device, and the output projection stays near the end.
        for (int li = cfg_.n_layers - 1; li >= 0 && gpu_layers_ < want; --li) {
            const LayerTensors& L = layers_[static_cast<size_t>(li)];
            const TensorInfo* big[] = {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd};
            uint64_t need = 0;
            bool ok = true;
            for (const TensorInfo* t : big) {
                if (!t) continue;
                if (!gpu->supports(t->type)) { ok = false; break; }
                need += t->nbytes;
            }
            if (!ok) break;
            const uint64_t used = gpu->bytes_resident() - resident0;
            const uint64_t with_this = used + static_cast<uint64_t>(need * est) +
                                       kv_per_layer * static_cast<uint64_t>(gpu_layers_ + 1) + arena;
            if (with_this > vram_budget) break;
            bool uploaded = true;
            for (const TensorInfo* t : big) {
                if (t && !gpu->upload(t)) { uploaded = false; break; }
            }
            if (!uploaded) {
                for (const TensorInfo* t : big) if (t && gpu->resident(t)) gpu->release(t);
                break;
            }
            ++gpu_layers_;
            // The weights now live in VRAM; their file pages can leave RAM.
            // (MoE layers keep their experts on the CPU, so those stay.)
            if (!cfg_.is_moe()) evict_layer(li);
        }
        gpu_bytes_ = gpu->bytes_resident() - resident0;
    }

    // ---- RAM tier
    uint64_t cpu_bytes = 0;
    for (int li = 0; li < cfg_.n_layers - gpu_layers_; ++li) {
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        cpu_bytes += L.file_end > L.file_begin ? L.file_end - L.file_begin : 0;
    }
    const uint64_t other = weights_bytes_ > cpu_bytes + gpu_bytes_ ? weights_bytes_ - cpu_bytes - gpu_bytes_ : 0;
    // Merged adapter embeddings/output are private memory on top of the file.
    const uint64_t fixed = other + kv_bytes_ + owned_extra_bytes_;
    if (avail == 0 || cpu_bytes + fixed <= ram_budget) {
        ram_layers_ = cfg_.n_layers - gpu_layers_;
        stream_ = false;
        return;
    }
    // Streaming: how many of the CPU layers can stay resident, the rest window.
    const uint64_t per_layer = cpu_bytes / std::max(1, cfg_.n_layers - gpu_layers_);
    const uint64_t left = ram_budget > fixed ? ram_budget - fixed : 0;
    int resident = static_cast<int>(std::min<uint64_t>(static_cast<uint64_t>(cfg_.n_layers - gpu_layers_), left / std::max<uint64_t>(1, per_layer)));
    stream_window_ = 3;
    resident = std::max(0, resident - stream_window_);
    ram_layers_ = resident;
    stream_ = true;
}

void LlamaModel::evict_layer(int li) {
    if (li < 0 || li >= cfg_.n_layers) return;
    const LayerTensors& L = layers_[static_cast<size_t>(li)];
    if (L.file_end > L.file_begin)
        os_evict(gguf_->data_base() + L.file_begin, static_cast<size_t>(L.file_end - L.file_begin));
}

// ---------------------------------------------------------------- prefetch

void LlamaModel::prefetch_loop() {
    for (;;) {
        int li;
        {
            std::unique_lock<std::mutex> lk(prefetch_mu_);
            prefetch_cv_.wait(lk, [&] { return prefetch_stop_ || prefetch_request_ >= 0; });
            if (prefetch_stop_) return;
            li = prefetch_request_;
            prefetch_request_ = -1;
        }
        if (li < 0 || li >= cfg_.n_layers) continue;
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        if (L.file_end > L.file_begin)
            os_prefetch(gguf_->data_base() + L.file_begin, static_cast<size_t>(L.file_end - L.file_begin));
        // Layers behind the window are streamed ones (index >= ram_layers_);
        // the first ram_layers_ stay resident by design.
        const int behind = li - stream_window_;
        if (behind >= ram_layers_ && behind < cfg_.n_layers - gpu_layers_) evict_layer(behind);
    }
}

void LlamaModel::prefetch_layer(int li) {
    if (!stream_) return;
    {
        std::lock_guard<std::mutex> lk(prefetch_mu_);
        prefetch_request_ = li;
    }
    prefetch_cv_.notify_one();
}

// ---------------------------------------------------------------- FFN

namespace {
// One projection, whole tensor or one expert's slice.
void mm(const TensorInfo* t, int expert, const float* x, const ActBatch* xq,
        int n_tok, int n_in, int n_out, float* y) {
    if (expert < 0) matmul_tensor(t, x, xq, n_tok, n_in, n_out, y);
    else matmul_batch(t->type, expert_ptr(t, expert), x, xq, n_tok, n_in, n_out, y);
}
} // namespace

// out[n_tok][n_in-of-down] = down( act(gate(x)) * up(x) )
void LlamaModel::ffn_block(const TensorInfo* wg, const TensorInfo* wu, const TensorInfo* wd, int expert,
                           const float* x, const ActBatch* xq, int n_tok, int n_in, int n_ff,
                           float* out, bool fused_gate_up) {
    ensure(gate_, static_cast<size_t>(n_tok) * 2 * n_ff);
    ensure(up_, static_cast<size_t>(n_tok) * n_ff);
    const GgmlType td = wd->type;

    if (fused_gate_up) {
        mm(wu, expert, x, xq, n_tok, n_in, 2 * n_ff, gate_.data());
        for (int t = 0; t < n_tok; ++t) {
            float* g = gate_.data() + static_cast<size_t>(t) * 2 * n_ff;
            const float* u = g + n_ff;
            if (cfg_.gelu) gelu_inplace(g, n_ff); else silu_inplace(g, n_ff);
            for (int i = 0; i < n_ff; ++i) g[i] *= u[i];
            // compact to [n_tok][n_ff]
            if (t > 0) std::memmove(gate_.data() + static_cast<size_t>(t) * n_ff, g, sizeof(float) * n_ff);
        }
    } else {
        if (expert < 0) {
            const TensorInfo* gu_t[2] = {wg, wu};
            const int gu_n[2] = {n_ff, n_ff};
            float* gu_y[2] = {gate_.data(), up_.data()};
            matmul_tensors(gu_t, 2, x, xq, n_tok, n_in, gu_n, gu_y);
        } else {
            mm(wg, expert, x, xq, n_tok, n_in, n_ff, gate_.data());
            mm(wu, expert, x, xq, n_tok, n_in, n_ff, up_.data());
        }
        for (int t = 0; t < n_tok; ++t) {
            float* g = row_ptr(gate_, t, n_ff);
            const float* u = row_ptr(up_, t, n_ff);
            if (cfg_.gelu) gelu_inplace(g, n_ff); else silu_inplace(g, n_ff);
            for (int i = 0; i < n_ff; ++i) g[i] *= u[i];
        }
    }

    const ActBatch* gq = nullptr;
    if ((has_q8_kernel(td) || has_q8k_kernel(td)) && n_ff % kQ8ActBlock == 0) {
        gateq_.quantize(gate_.data(), n_tok, n_ff);
        gq = &gateq_;
    }
    mm(wd, expert, gate_.data(), gq, n_tok, n_ff, n_in, out);
}

void LlamaModel::dense_ffn(const LayerTensors& L, int n_tok) {
    const int E = cfg_.n_embd;
    const bool fused = (L.wg == nullptr);
    ffn_block(L.wg, L.wu, L.wd, -1, xb_.data(), &xq_, n_tok, E, cfg_.n_ff, ffn_out_.data(), fused);
}

void LlamaModel::moe_ffn(const LayerTensors& L, int n_tok) {
    const int E = cfg_.n_embd;
    const int NE = cfg_.n_experts;
    const int K = cfg_.n_experts_per_tok;

    // 1. router logits -> probabilities -> top-k per token
    matmul_batch(L.gate_inp->type, L.gate_inp->data, xb_.data(), &xq_, n_tok, E, NE, router_.data());
    std::vector<int> sel(static_cast<size_t>(n_tok) * K);
    std::vector<float> wsel(static_cast<size_t>(n_tok) * K);
    std::vector<std::pair<float, int>> scores(static_cast<size_t>(NE));
    for (int t = 0; t < n_tok; ++t) {
        float* lg = row_ptr(router_, t, NE);
        softmax_inplace(lg, NE);
        for (int e = 0; e < NE; ++e) scores[static_cast<size_t>(e)] = {lg[e], e};
        std::partial_sort(scores.begin(), scores.begin() + K, scores.end(),
                          [](const auto& a, const auto& b) { return a.first > b.first; });
        float sum = 0.0f;
        for (int k = 0; k < K; ++k) {
            sel[static_cast<size_t>(t) * K + k] = scores[static_cast<size_t>(k)].second;
            wsel[static_cast<size_t>(t) * K + k] = scores[static_cast<size_t>(k)].first;
            sum += scores[static_cast<size_t>(k)].first;
        }
        if (cfg_.expert_weights_norm && sum > 0.0f)
            for (int k = 0; k < K; ++k) wsel[static_cast<size_t>(t) * K + k] /= sum;
    }

    std::fill(ffn_out_.begin(), ffn_out_.begin() + static_cast<size_t>(n_tok) * E, 0.0f);

    // 2. each used expert processes its tokens as one small batch
    std::vector<int> toks;
    std::vector<float> wts;
    for (int e = 0; e < NE; ++e) {
        toks.clear();
        wts.clear();
        for (int t = 0; t < n_tok; ++t)
            for (int k = 0; k < K; ++k)
                if (sel[static_cast<size_t>(t) * K + k] == e) {
                    toks.push_back(t);
                    wts.push_back(wsel[static_cast<size_t>(t) * K + k]);
                }
        if (toks.empty()) continue;
        const int cnt = static_cast<int>(toks.size());

        ensure(exp_in_, static_cast<size_t>(cnt) * E);
        ensure(exp_down_, static_cast<size_t>(cnt) * E);
        for (int j = 0; j < cnt; ++j)
            std::memcpy(exp_in_.data() + static_cast<size_t>(j) * E, row_ptr(xb_, toks[static_cast<size_t>(j)], E),
                        sizeof(float) * E);
        exp_inq_.quantize(exp_in_.data(), cnt, E);
        ffn_block(L.gate_exps, L.up_exps, L.down_exps, e,
                  exp_in_.data(), &exp_inq_, cnt, E, cfg_.n_ff_exp, exp_down_.data(), false);
        for (int j = 0; j < cnt; ++j) {
            float* o = row_ptr(ffn_out_, toks[static_cast<size_t>(j)], E);
            const float* d = exp_down_.data() + static_cast<size_t>(j) * E;
            const float w = wts[static_cast<size_t>(j)];
            for (int i = 0; i < E; ++i) o[i] += w * d[i];
        }
    }

    // 3. shared expert (Qwen2-MoE / DeepSeek style), optionally sigmoid-gated
    if (L.shexp_gate && L.shexp_up && L.shexp_down) {
        ensure(exp_down_, static_cast<size_t>(n_tok) * E);
        ffn_block(L.shexp_gate, L.shexp_up, L.shexp_down, -1,
                  xb_.data(), &xq_, n_tok, E, cfg_.n_ff_shexp, exp_down_.data(), false);
        std::vector<float> gate(static_cast<size_t>(n_tok), 1.0f);
        if (L.shexp_gate_inp) {
            matmul_batch(L.shexp_gate_inp->type, L.shexp_gate_inp->data, xb_.data(), &xq_,
                         n_tok, E, 1, gate.data());
            for (float& gv : gate) gv = 1.0f / (1.0f + std::exp(-gv));
        }
        for (int t = 0; t < n_tok; ++t) {
            float* o = row_ptr(ffn_out_, t, E);
            const float* d = exp_down_.data() + static_cast<size_t>(t) * E;
            for (int i = 0; i < E; ++i) o[i] += gate[static_cast<size_t>(t)] * d[i];
        }
    }
}

// ---------------------------------------------------------------- forward

bool LlamaModel::forward(const int32_t* tokens, int n_tok, int pos0, bool all_logits) {
    if (!tokens || n_tok <= 0) return false;
    if (pos0 < 0 || pos0 + n_tok > cfg_.n_ctx) return false;
    if (n_tok > max_batch_) return false;

    const int E = cfg_.n_embd;
    const int Q = cfg_.q_dim();
    const int KV = cfg_.kv_dim();
    const int H = cfg_.n_head;
    const int HKV = cfg_.n_head_kv;
    const int D = cfg_.head_dim;
    const float attn_scale = cfg_.attn_scale > 0.0f ? cfg_.attn_scale : 1.0f / std::sqrt(static_cast<float>(D));

    // ---- token embedding: one dequantised row per token
    const size_t emb_row = ggml_row_size(embd_->type, static_cast<uint64_t>(E));
    for (int t = 0; t < n_tok; ++t) {
        const int32_t id = tokens[t];
        if (id < 0 || id >= cfg_.vocab) return false;
        const uint8_t* src = embd_->data + static_cast<size_t>(id) * emb_row;
        if (!dequant_row(embd_->type, src, row_ptr(x_, t, E), static_cast<uint64_t>(E)))
            return false;
        if (cfg_.embd_scale != 1.0f) {
            float* xr = row_ptr(x_, t, E);
            for (int i = 0; i < E; ++i) xr[i] *= cfg_.embd_scale;
        }
    }

    for (int li = 0; li < cfg_.n_layers; ++li) {
        if (gpu_span_ && li == cfg_.n_layers - gpu_layers_) {
            // The remaining layers run as one device-side span per token.
            ComputeBackend* gpu = gpu_backend();
            if (gpu && gpu->span_forward(x_.data(), n_tok, pos0)) break;
            // The backend refused without touching x. The CPU KV cache holds
            // nothing for the span layers (it was trimmed), so this token
            // cannot be finished on the CPU path: restore the full cache,
            // stop offering spans, and report the failure. The caller resets
            // and the next generation runs on the per-matmul path.
            gpu_span_ = false;
            const uint64_t kv_elems = static_cast<uint64_t>(cfg_.n_layers) * cfg_.n_ctx * cfg_.kv_dim();
            try {
                k_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);
                v_cache_.assign(static_cast<size_t>(kv_elems), 0.0f);
            } catch (const std::bad_alloc&) {
                return false;
            }
            kv_len_ = 0;
            return false;
        }
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        if (stream_) prefetch_layer(li + 1 < cfg_.n_layers ? li + 1 : 0);

        // ---- attention block
        for (int t = 0; t < n_tok; ++t)
            rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, t, E), L.attn_norm_w.data(), E, cfg_.eps);
        xq_.quantize(xb_.data(), n_tok, E);

        if (L.wqkv) {
            const int W = Q + 2 * KV;
            matmul_tensor(L.wqkv, xb_.data(), &xq_, n_tok, E, W, qkv_.data());
            add_bias(qkv_.data(), n_tok, W, L.bias_qkv);
            for (int t = 0; t < n_tok; ++t) {
                const float* r = row_ptr(qkv_, t, W);
                std::memcpy(row_ptr(q_, t, Q), r, sizeof(float) * Q);
                std::memcpy(row_ptr(k_, t, KV), r + Q, sizeof(float) * KV);
                std::memcpy(row_ptr(v_, t, KV), r + Q + KV, sizeof(float) * KV);
            }
        } else {
            const TensorInfo* qkv_t[3] = {L.wq, L.wk, L.wv};
            const int qkv_n[3] = {Q, KV, KV};
            float* qkv_y[3] = {q_.data(), k_.data(), v_.data()};
            matmul_tensors(qkv_t, 3, xb_.data(), &xq_, n_tok, E, qkv_n, qkv_y);
            add_bias(q_.data(), n_tok, Q, L.bias_q);
            add_bias(k_.data(), n_tok, KV, L.bias_k);
            add_bias(v_.data(), n_tok, KV, L.bias_v);
        }

        if (!L.q_norm_w.empty()) {
            for (int t = 0; t < n_tok; ++t) {
                float* qr = row_ptr(q_, t, Q);
                float* kr = row_ptr(k_, t, KV);
                for (int h = 0; h < H; ++h) rmsnorm(qr + h * D, qr + h * D, L.q_norm_w.data(), D, cfg_.eps);
                for (int h = 0; h < HKV; ++h) rmsnorm(kr + h * D, kr + h * D, L.k_norm_w.data(), D, cfg_.eps);
            }
        }

        for (int t = 0; t < n_tok; ++t) {
            const int pos = pos0 + t;
            rope_apply(row_ptr(q_, t, Q), row_ptr(k_, t, KV), H, HKV, pos, rope_);
            float* kc = k_cache_.data() + (static_cast<size_t>(li) * cfg_.n_ctx + pos) * KV;
            float* vc = v_cache_.data() + (static_cast<size_t>(li) * cfg_.n_ctx + pos) * KV;
            std::memcpy(kc, row_ptr(k_, t, KV), sizeof(float) * KV);
            std::memcpy(vc, row_ptr(v_, t, KV), sizeof(float) * KV);
        }

        const float* kc_layer = k_cache_.data() + static_cast<size_t>(li) * cfg_.n_ctx * KV;
        const float* vc_layer = v_cache_.data() + static_cast<size_t>(li) * cfg_.n_ctx * KV;
        const int window = (cfg_.sliding_window > 0 && (li % 2) == 0) ? cfg_.sliding_window : 0;
        attention_batch(q_.data(), kc_layer, vc_layer, cfg_.n_ctx, H, HKV, D, pos0, n_tok,
                        attn_.data(), attn_scale, cfg_.attn_softcap, window);

        const ActBatch* aq = nullptr;
        if ((has_q8_kernel(L.wo->type) || has_q8k_kernel(L.wo->type)) && Q % kQ8ActBlock == 0) {
            attnq_.quantize(attn_.data(), n_tok, Q);
            aq = &attnq_;
        }
        matmul_tensor(L.wo, attn_.data(), aq, n_tok, Q, E, proj_.data());
        add_bias(proj_.data(), n_tok, E, L.bias_o);
        if (!L.post_attn_norm_w.empty())
            for (int t = 0; t < n_tok; ++t)
                rmsnorm(row_ptr(proj_, t, E), row_ptr(proj_, t, E), L.post_attn_norm_w.data(), E, cfg_.eps);
        for (int t = 0; t < n_tok; ++t) {
            float* xr = row_ptr(x_, t, E);
            const float* pr = row_ptr(proj_, t, E);
            for (int i = 0; i < E; ++i) xr[i] += pr[i];
        }

        // ---- feed-forward block
        for (int t = 0; t < n_tok; ++t)
            rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, t, E), L.ffn_norm_w.data(), E, cfg_.eps);
        xq_.quantize(xb_.data(), n_tok, E);
        if (cfg_.is_moe()) moe_ffn(L, n_tok); else dense_ffn(L, n_tok);
        if (!L.post_ffn_norm_w.empty())
            for (int t = 0; t < n_tok; ++t)
                rmsnorm(row_ptr(ffn_out_, t, E), row_ptr(ffn_out_, t, E), L.post_ffn_norm_w.data(), E, cfg_.eps);
        for (int t = 0; t < n_tok; ++t) {
            float* xr = row_ptr(x_, t, E);
            const float* fr = row_ptr(ffn_out_, t, E);
            for (int i = 0; i < E; ++i) xr[i] += fr[i];
        }
    }

    // ---- final norm + output projection: last token only, or every token
    // (speculative verification). The output projection is the single
    // largest per-token read (hundreds of MB on an 8B); it goes through the
    // backend hook like every other matmul.
    const int n_out_rows = all_logits ? n_tok : 1;
    const int first_row = all_logits ? 0 : n_tok - 1;
    for (int t = 0; t < n_out_rows; ++t)
        rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, first_row + t, E), out_norm_w_.data(), E, cfg_.eps);
    xq_.quantize(xb_.data(), n_out_rows, E);
    float* out = logits_.data();
    if (all_logits) {
        ensure(logits_all_, static_cast<size_t>(n_tok) * cfg_.vocab);
        out = logits_all_.data();
    }
    matmul_tensor(out_w_, xb_.data(), &xq_, n_out_rows, E, cfg_.vocab, out);
    if (cfg_.final_softcap > 0.0f) {
        for (size_t i = 0; i < static_cast<size_t>(n_out_rows) * cfg_.vocab; ++i)
            out[i] = cfg_.final_softcap * std::tanh(out[i] / cfg_.final_softcap);
    }
    if (all_logits)
        std::memcpy(logits_.data(), out + static_cast<size_t>(n_tok - 1) * cfg_.vocab, sizeof(float) * cfg_.vocab);

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
