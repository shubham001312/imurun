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
#include <fish/tokenizer.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
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
    {"qwen35", Arch::Qwen35},
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
        case Arch::Qwen35: return "qwen35";
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
    if (warm_thread_.joinable()) warm_thread_.join();

    // Give the device memory back: the TensorInfo pointers die with gguf_, so
    // the backend must not be left holding them.
    if (ComputeBackend* gpu = gpu_backend()) {
        if (gpu_span_) gpu->span_release();
        for (const LayerTensors& L : layers_) {
            for (const TensorInfo* t : {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd,
                                        L.wgate, L.ssm_alpha, L.ssm_beta, L.ssm_out,
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
    L.ffn_norm  = need("ffn_norm.weight");   // absent by design on qwen35 (post_attention_norm feeds the FFN)
    L.wq = need("attn_q.weight");
    L.wk = need("attn_k.weight");
    L.wv = need("attn_v.weight");
    L.wo = need("attn_output.weight");
    L.wqkv = need("attn_qkv.weight");
    L.wg = need("ffn_gate.weight");
    L.wu = need("ffn_up.weight");
    L.wd = need("ffn_down.weight");

    if (!L.attn_norm) return fail("missing tensor " + p + "attn_norm.weight");
    if (!L.ffn_norm && cfg_.kind != Arch::Qwen35)
        return fail("missing tensor " + p + "ffn_norm.weight");

    if (cfg_.delta_layer(i)) {
        // Qwen3.5 DeltaNet layer: fused q|k|v projected pre-conv, a z output
        // gate, and the ssm tensors. No o_proj / per-head norms here.
        if (!L.wqkv) return fail("missing tensor " + p + "attn_qkv.weight (DeltaNet layer)");
        L.wgate = need("attn_gate.weight");
        L.ssm_alpha = need("ssm_alpha.weight");
        L.ssm_beta = need("ssm_beta.weight");
        L.ssm_out = need("ssm_out.weight");
        if (!L.wgate || !L.ssm_alpha || !L.ssm_beta || !L.ssm_out)
            return fail("missing DeltaNet tensors in layer " + std::to_string(i) +
                        " (attn_gate / ssm_alpha / ssm_beta / ssm_out)");
        if (L.wqkv->ne.size() < 2 ||
            L.wqkv->ne[1] != static_cast<uint64_t>(3 * cfg_.ssm_inner))
            return fail("fused attn_qkv has an unexpected shape in DeltaNet layer " +
                        std::to_string(i));
    } else {
        if (!L.wo) return fail("missing tensor " + p + "attn_output.weight");
        if (!L.wqkv && !(L.wq && L.wk && L.wv))
            return fail("missing attention projections in layer " + std::to_string(i));
        if (L.wqkv) {
            const uint64_t want = static_cast<uint64_t>(cfg_.q_dim() + 2 * cfg_.kv_dim());
            if (L.wqkv->ne.size() < 2 || L.wqkv->ne[1] != want)
                return fail("fused attn_qkv has an unexpected shape in layer " + std::to_string(i));
        }
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
    if (L.ffn_norm && !norm_vec(L.ffn_norm, L.ffn_norm_w, "ffn_norm")) return false;

    if (cfg_.kind == Arch::Gemma2) {
        const TensorInfo* pa = need("post_attention_norm.weight");
        const TensorInfo* pf = need("post_ffw_norm.weight");
        if (!pa || !pf) return fail("gemma2 layer " + std::to_string(i) + " lacks post norms");
        if (!norm_vec(pa, L.post_attn_norm_w, "post_attention_norm")) return false;
        if (!norm_vec(pf, L.post_ffn_norm_w, "post_ffw_norm")) return false;
    }
    if (cfg_.kind == Arch::Qwen35) {
        // post_attention_norm is stored folded (1+w); it guards the FFN path
        // in front of the MLP. Used directly, no +1 — same for every norm here.
        const TensorInfo* pa = need("post_attention_norm.weight");
        if (!pa) return fail("qwen35 layer " + std::to_string(i) + " lacks post_attention_norm");
        if (!norm_vec(pa, L.post_attn_norm_w, "post_attention_norm")) return false;
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

    // Qwen3.5 DeltaNet small vectors. conv_w is f32 [conv_dim][kernel],
    // channel-major (ne[0] = kernel runs fastest).
    if (cfg_.delta_layer(i)) {
        const TensorInfo* cv = need("ssm_conv1d.weight");
        if (!cv) return fail("missing tensor " + p + "ssm_conv1d.weight (DeltaNet)");
        if (!load_vector(cv, L.conv_w)) return fail("bad ssm_conv1d in layer " + std::to_string(i));
        const TensorInfo* av = need("ssm_a");
        if (!av || !load_vector(av, L.ssm_a))
            return fail("bad ssm_a in layer " + std::to_string(i));
        const TensorInfo* db = need("ssm_dt.bias");
        if (!db || !load_vector(db, L.dt_bias))
            return fail("bad ssm_dt.bias in layer " + std::to_string(i));
        const TensorInfo* sn = need("ssm_norm.weight");
        if (!sn || !load_vector(sn, L.ssm_norm_w))
            return fail("bad ssm_norm in layer " + std::to_string(i));
        if (L.ssm_a.size() != static_cast<size_t>(cfg_.ssm_heads) ||
            L.dt_bias.size() != static_cast<size_t>(cfg_.ssm_heads) ||
            L.ssm_norm_w.size() != static_cast<size_t>(cfg_.ssm_state) ||
            L.conv_w.size() != static_cast<size_t>(cfg_.conv_dim()) *
                               static_cast<size_t>(cfg_.ssm_conv_kernel))
            return fail("DeltaNet vector shapes disagree with metadata in layer " +
                        std::to_string(i));
        // ssm_a arrives already negative (-exp(A_log)); it is used as a plain
        // multiplier on the softplus decay, never exponentiated again.
    }

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
    using lclk = std::chrono::steady_clock;
    auto since = [](lclk::time_point a) { return std::chrono::duration<double>(lclk::now() - a).count(); };
    const auto lt0 = lclk::now();
    m->gguf_ = GgufFile::open(path, &gerr);
    const double t_open = since(lt0);
    if (!m->gguf_) return fail(gerr);

    const GgufFile& g = *m->gguf_;
    LlamaConfig& c = m->cfg_;
    c.arch = g.str("general.architecture", "llama");
    if (!arch_lookup(c.arch, &c.kind))
        return fail("unsupported architecture: " + c.arch +
                    " (FISH runs llama, qwen2, qwen3, qwen35, phi3, gemma2 and their MoE variants)");
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

    // ---- Qwen3.5 hybrid (Gated DeltaNet + gated full attention)
    // Full attention on layers with (i+1) % interval == 0; DeltaNet elsewhere.
    // Norm weights in the file are already folded (converter stored 1+w) and
    // ssm_norm is raw accordingly; nothing gets a +1 here either way.
    if (c.kind == Arch::Qwen35) {
        c.full_attn_interval = static_cast<int>(g.u64(key("full_attention_interval"), 4));
        c.ssm_state = static_cast<int>(g.u64(key("ssm.state_size"), 128));
        c.ssm_heads = static_cast<int>(g.u64(key("ssm.group_count"), 16));
        c.ssm_conv_kernel = static_cast<int>(g.u64(key("ssm.conv_kernel"), 4));
        c.ssm_inner = static_cast<int>(g.u64(key("ssm.inner_size"), c.n_embd));
        if (c.full_attn_interval <= 0 || c.ssm_state <= 0 || c.ssm_heads <= 0 ||
            c.ssm_conv_kernel <= 1 || c.ssm_inner <= 0 || c.ssm_inner % c.ssm_heads != 0)
            return fail("qwen35: bad ssm.* / full_attention_interval metadata");
        // v1 runs the whole graph on the CPU: the mixer recurrence and the
        // fused per-head q|gate path have no device-code path yet.
        m->opts_.gpu_layers = 0;
    }

    // ---- architecture knobs
    c.rope.head_dim = c.head_dim;
    c.rope.rot_dim = static_cast<int>(g.u64(key("rope.dimension_count"), c.head_dim));
    c.rope.freq_base = c.freq_base;
    c.rope.neox = (c.kind == Arch::Qwen2 || c.kind == Arch::Qwen3 ||
                   c.kind == Arch::Qwen35 || c.kind == Arch::Phi3 || c.kind == Arch::Gemma2);
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

    // ---- DeltaNet bookkeeping: slot map + fp32 recurring state
    if (c.full_attn_interval > 0) {
        m->delta_slot_.assign(static_cast<size_t>(c.n_layers), -1);
        int nd = 0;
        for (int i = 0; i < c.n_layers; ++i)
            if (c.delta_layer(i)) m->delta_slot_[static_cast<size_t>(i)] = nd++;
        try {
            m->conv_hist_.assign(static_cast<size_t>(nd) * (c.ssm_conv_kernel - 1) *
                                 static_cast<size_t>(c.conv_dim()), 0.0f);
            m->ssm_S_.assign(static_cast<size_t>(nd) * c.ssm_heads * c.ssm_state *
                             static_cast<size_t>(c.ssm_state), 0.0f);
        } catch (const std::bad_alloc&) {
            return fail("not enough memory for the DeltaNet state");
        }
    }

    // ---- KV cache and work buffers
    const int E = c.n_embd, Q = c.q_dim(), KV = c.kv_dim();
    const uint64_t kv_elems = static_cast<uint64_t>(c.kv_slots()) * c.n_ctx * KV;
    m->kv_bytes_ = kv_elems * sizeof(float) * 2;
    if (m->kv_bytes_ > (16ull << 30)) return fail("KV cache would exceed 16 GB");
    // The CPU KV cache is allocated after placement (alloc_cpu_kv): layers
    // in a GPU span keep theirs in VRAM, and the planner may shorten n_ctx.
    // Allocating all of it here first committed n_layers x n_ctx x kv_dim x 8
    // bytes (3.2 GB for Phi-3-mini at 4096) that placement then had to count.
    try {
        const int mb = m->max_batch_;
        const int ff = std::max(c.n_ff, std::max(c.n_ff_exp, c.n_ff_shexp));
        m->x_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->xb_.assign(static_cast<size_t>(mb) * E, 0.0f);
        m->qkv_.assign(static_cast<size_t>(mb) *
                       std::max<size_t>(static_cast<size_t>(Q + 2 * KV),
                                        c.ssm_inner > 0 ? 3 * static_cast<size_t>(c.ssm_inner) : 0), 0.0f);
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

    const double t_meta = since(lt0) - t_open;
    if (ComputeBackend* gb = gpu_backend()) gb->upload_profile(true);
    const auto lt1 = lclk::now();
    m->place_layers();
    const double t_place = since(lt1);
    const auto lt2 = lclk::now();
    m->prepare_span();
    const double t_span = since(lt2);
    const auto lt3 = lclk::now();
    if (!m->alloc_cpu_kv()) return fail("not enough memory for the KV cache");
    const double t_kv = since(lt3);
    if (m->stream_) m->prefetch_thread_ = std::thread([mp = m.get()] { mp->prefetch_loop(); });
    // CPU layers of a resident (non-streamed) placement: ask the OS to read
    // their file pages now, in large requests, while the caller tokenises
    // and starts the first prefill, instead of faulting them in 4-64 KB at a
    // time inside the first forward pass (cold first token on the 8B).
    if (!m->stream_ && m->gpu_layers_ < c.n_layers) {
        m->warm_thread_ = std::thread([mp = m.get()] {
            const int cpu = mp->cfg_.n_layers - mp->gpu_layers_;
            for (int li = 0; li < cpu; ++li) {
                const LayerTensors& L = mp->layers_[static_cast<size_t>(li)];
                if (L.file_end > L.file_begin)
                    os_prefetch(mp->gguf_->data_base() + L.file_begin, static_cast<size_t>(L.file_end - L.file_begin));
            }
        });
    }
    {
        // Wall-clock per load phase (fish_stats.load_profile, FISH_GPU_VERBOSE).
        // "place" contains calibration and the GPU upload; the upload part is
        // broken down by the backend (cache hits / re-quantised / file bytes).
        char b[400];
        const std::string up = gpu_backend() ? gpu_backend()->upload_profile(false) : std::string();
        std::snprintf(b, sizeof(b), "open %.2f s, metadata+bind %.2f s, place %.2f s (calibration %.1f s, upload %.1f s%s%s%s), span %.2f s, cpu kv %.2f s",
                      t_open, t_meta, t_place, m->calib_seconds_, m->upload_seconds_,
                      up.empty() ? "" : " [", up.c_str(), up.empty() ? "" : "]", t_span, t_kv);
        m->load_profile_ = b;
        if (std::getenv("FISH_GPU_VERBOSE") && std::atoi(std::getenv("FISH_GPU_VERBOSE")) != 0)
            std::fprintf(stderr, "[fish/load] %s\n", b);
    }
    return m;
}

// ---------------------------------------------------------------- adapter

const TensorInfo* LlamaModel::weight(const std::string& name) const {
    if (embd_ && embd_->name == name) return embd_;
    if (out_w_ && out_w_->name == name) return out_w_;
    for (const LayerTensors& L : layers_)
        for (const TensorInfo* t : {L.wq, L.wk, L.wv, L.wo, L.wqkv, L.wg, L.wu, L.wd,
                                    L.wgate, L.ssm_alpha, L.ssm_beta, L.ssm_out,
                                    L.gate_inp, L.gate_exps, L.up_exps, L.down_exps,
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
    // DeltaNet state is rebuilt exactly like the KV cache: from zero.
    if (!conv_hist_.empty()) std::fill(conv_hist_.begin(), conv_hist_.end(), 0.0f);
    if (!ssm_S_.empty()) std::fill(ssm_S_.begin(), ssm_S_.end(), 0.0f);
}

void LlamaModel::set_kv_len(int n) {
    if (n != kv_len_ && !conv_hist_.empty()) {
        // A rewind invalidates the recurring state; generation re-prefills
        // from zero like any context reset.
        std::fill(conv_hist_.begin(), conv_hist_.end(), 0.0f);
        std::fill(ssm_S_.begin(), ssm_S_.end(), 0.0f);
    }
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
    p.kv_q8 = gpu_kv_q8_;
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
        // The released layer runs on the CPU from RAM (it was evicted after
        // upload; the OS pages it back in). It is not a disk-streamed layer.
        if (!stream_) ram_layers_ = cfg_.n_layers - gpu_layers_;
        span_layers_.erase(span_layers_.begin());
        p.layer_begin = cfg_.n_layers - gpu_layers_;
        p.layers = span_layers_.data();
        gpu_span_ = gpu->span_prepare(p);
    }
    gpu_bytes_ = gpu->bytes_resident();
}

// The span keeps its own KV cache in VRAM; the CPU copy for those layers
// would only waste RAM (512 MB on an 8B at 4096). The CPU cache covers the
// CPU layers only: they are the prefix [0, n_layers - gpu_layers).
bool LlamaModel::alloc_cpu_kv() {
    const size_t n = static_cast<size_t>(cfg_.kv_slots()) * cfg_.n_ctx * cfg_.kv_dim();
    try {
        k_cache_.assign(n, 0.0f);
        v_cache_.assign(n, 0.0f);
    } catch (const std::bad_alloc&) {
        return false;
    }
    kv_bytes_ = static_cast<uint64_t>(n) * sizeof(float) * 2;
    return true;
}

// ---------------------------------------------------------------- calibration

bool LlamaModel::calibrate(Importance* out, int max_tokens, std::string* err) {
    auto fail = [&](const std::string& why) { if (err) *err = "calibration: " + why; return false; };
    if (gpu_layers_ > 0 || gpu_span_) return fail("must run before placement");
    std::string e;
    auto tok = Tokenizer::create(*gguf_, &e);
    if (!tok) return fail(e);
    std::vector<int32_t> ids = tok->encode(calibration_text(), true, false);
    if (static_cast<int>(ids.size()) > max_tokens) ids.resize(static_cast<size_t>(max_tokens));
    if (ids.size() < 16) return fail("calibration text too short for this tokenizer");

    struct Acc { std::vector<double> s; long n = 0; };
    std::unordered_map<const TensorInfo*, Acc> acc;
    MatmulObserver obs = [](void* u, const TensorInfo* t, const float* x, int n_tok, int n_in) {
        Acc& a = (*static_cast<std::unordered_map<const TensorInfo*, Acc>*>(u))[t];
        if (a.s.size() != static_cast<size_t>(n_in)) { a.s.assign(static_cast<size_t>(n_in), 0.0); a.n = 0; }
        for (int r = 0; r < n_tok; ++r) {
            const float* xr = x + static_cast<size_t>(r) * n_in;
            for (int j = 0; j < n_in; ++j) a.s[static_cast<size_t>(j)] += static_cast<double>(xr[j]) * xr[j];
        }
        a.n += n_tok;
    };

    // A temporary KV cache for the calibration sequence only.
    const int saved_ctx = cfg_.n_ctx;
    const int len = static_cast<int>(ids.size());
    cfg_.n_ctx = len;
    try {
        const size_t n = static_cast<size_t>(cfg_.kv_slots()) * len * cfg_.kv_dim();
        k_cache_.assign(n, 0.0f);
        v_cache_.assign(n, 0.0f);
    } catch (const std::bad_alloc&) {
        cfg_.n_ctx = saved_ctx;
        return fail("no memory for the calibration KV cache");
    }
    set_matmul_observer(obs, &acc);
    bool ok = true;
    for (int p = 0; p < len && ok; p += max_batch_) {
        const int n = std::min(max_batch_, len - p);
        ok = forward(ids.data() + p, n, p);
    }
    set_matmul_observer(nullptr, nullptr);
    cfg_.n_ctx = saved_ctx;
    std::vector<float>().swap(k_cache_);
    std::vector<float>().swap(v_cache_);
    kv_len_ = 0;
    if (!ok) return fail("forward pass failed");

    out->clear();
    for (const auto& kv : acc) {
        // The output projection sees one row per forward call (last token
        // only): too few samples to weight by. It gets the plain fit.
        if (kv.second.n < 64) continue;
        std::vector<float> v(kv.second.s.size());
        for (size_t j = 0; j < v.size(); ++j) v[j] = static_cast<float>(kv.second.s[j] / kv.second.n);
        (*out)[kv.first->name] = std::move(v);
    }
    return true;
}

// ---------------------------------------------------------------- residency planner
//
// Goal: as many layers as possible (all of them, ideally) and the output
// projection resident on the GPU as one span: one submit per token, and a
// CPU layer costs ~6 ms per token on an 8B (4 threads) against ~1 ms on the
// GPU. In order:
//   1. file bytes + KV for the requested context fit      -> nothing changes;
//   2. file bytes + KV for a shorter context fit (>= FISH_GPU_MIN_CTX,
//      default 2048, or the request if smaller)           -> context shortened;
//   3. device copies re-quantised (FISH_GPU_REQUANT unset or "auto"): start
//      every tensor at its smallest format, then upgrade the most sensitive
//      tensors while the budget at the floor context allows (k/v first, then
//      output, ffn_down, attention, gate/up; first and last layers first);
//      what is left becomes context;
//   4. not even that fits: the top N layers + output, re-quantised the same
//      way, with N as large as the budget allows at the floor context (the
//      first layers run on the CPU at file precision);
//   5. otherwise false: the caller places partially at file precision.
// placement_note_ records the decision (or why there was none) for
// fish_stats.placement and FISH_GPU_VERBOSE.
bool LlamaModel::plan_resident(ComputeBackend* gpu, uint64_t budget, uint64_t arena) {
    using CB = ComputeBackend;
    placement_note_.clear();
    const bool verbose = std::getenv("FISH_GPU_VERBOSE") && std::atoi(std::getenv("FISH_GPU_VERBOSE")) != 0;
    char why[320];
    auto decline = [&]() {
        placement_note_ = why;
        if (verbose) std::fprintf(stderr, "[fish/plan] %s\n", why);
        return false;
    };
    const char* rq = std::getenv("FISH_GPU_REQUANT");
    const bool allow_requant = !rq || !*rq || std::strcmp(rq, "auto") == 0;
    // FISH_GPU_TIERS: subset of "file,q4k,fq3,fq3s" (format studies).
    std::string tiers = "file,q4k,fq3,fq3s";
    if (const char* e = std::getenv("FISH_GPU_TIERS")) if (*e) tiers = e;
    auto tier_on = [&](const char* n) {
        const std::string s = "," + tiers + ",";
        return s.find(std::string(",") + n + ",") != std::string::npos;
    };
    const int fmts[4] = {CB::kDevFile, CB::kDevQ4K, CB::kDevFQ3, CB::kDevFQ3S};
    const char* fmt_names[4] = {"file", "q4k", "fq3", "fq3s"};
    const int NL = cfg_.n_layers;

    // Roles, in the order the upgrade passes visit them.
    enum Role { kQ, kK, kV, kO, kQKV, kG, kU, kD, kOut };
    struct Item {
        const TensorInfo* t;
        int role, layer;
        std::vector<std::pair<int, uint64_t>> opts;   // best first, strictly shrinking
        size_t cur = 0;
    };
    std::vector<Item> items;
    auto add = [&](const TensorInfo* t, int role, int layer) {
        Item it{t, role, layer, {}, 0};
        uint64_t prev = UINT64_MAX;
        for (int i = 0; i < 4; ++i) {
            if (i > 0 && !allow_requant) break;
            if (!tier_on(fmt_names[i])) continue;
            const uint64_t b = gpu->device_bytes(t, fmts[i]);
            if (b == 0 || b >= prev) continue;
            it.opts.push_back({fmts[i], b});
            prev = b;
        }
        if (it.opts.empty()) return false;
        items.push_back(std::move(it));
        return true;
    };
    if (!out_w_ || !add(out_w_, kOut, NL)) {
        std::snprintf(why, sizeof(why), "no resident plan: the output projection has no device format");
        return decline();
    }
    // Besides the span arena: per-matmul scratch, descriptor pools and the
    // driver's own allocations, measured at ~20-40 MB on the RTX 3050.
    arena += 64ull << 20;
    for (int li = 0; li < NL; ++li) {
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        const std::pair<const TensorInfo*, int> ts[] = {{L.wq, kQ}, {L.wk, kK}, {L.wv, kV}, {L.wo, kO},
            {L.wqkv, kQKV}, {L.wg, kG}, {L.wu, kU}, {L.wd, kD}};
        for (const auto& p : ts) {
            if (p.first && !add(p.first, p.second, li)) {
                std::snprintf(why, sizeof(why), "no resident plan: %s (%s) has no device format",
                              p.first->name.c_str(), ggml_type_name(p.first->type));
                return decline();
            }
        }
    }

    int floor_ctx = 2048;
    if (const char* e = std::getenv("FISH_GPU_MIN_CTX")) floor_ctx = std::max(64, std::atoi(e));
    floor_ctx = std::min(floor_ctx, cfg_.n_ctx);
    // lo = first GPU layer; the layers below it stay on the CPU (inactive).
    int lo = 0;
    auto active = [&](const Item& it) { return it.layer >= lo; };
    auto total = [&] { uint64_t s = 0; for (const Item& it : items) if (active(it)) s += it.opts[it.cur].second; return s; };
    // Device KV per position (GPU layers): f16 K+V = 4 B per kv element;
    // int8 + one f32 scale per 32 = 2.25 B. int8 when VRAM-bound: the file's
    // weights with an f16 cache for the requested context do not fit
    // (FISH_GPU_KV=f16|q8 forces one).
    const bool q8_ok = cfg_.kv_dim() % 32 == 0 && cfg_.head_dim % 32 == 0;
    const char* kvenv = std::getenv("FISH_GPU_KV");
    auto kvpp = [&]() -> uint64_t {
        const uint64_t e = static_cast<uint64_t>(NL - lo) * static_cast<uint64_t>(cfg_.kv_dim());
        return gpu_kv_q8_ ? e * 9ull / 4ull : e * 4ull;
    };
    // + 6% of the weights: measured on the RTX 3050 (8B, 218 tensors), the
    // live budget fell ~8-11% more than the bytes uploaded (lazy residency of
    // the reserved blocks, scratch, driver bookkeeping); without it the span
    // later declined and gave back 3 of 31 planned layers.
    auto fits = [&](uint64_t w, int ctx) { return w + w / 16 + arena + kvpp() * static_cast<uint64_t>(ctx) <= budget; };
    gpu_kv_q8_ = false;
    if (q8_ok && kvenv && std::strcmp(kvenv, "q8") == 0) gpu_kv_q8_ = true;
    else if (q8_ok && !(kvenv && std::strcmp(kvenv, "f16") == 0) && !fits(total(), cfg_.n_ctx)) gpu_kv_q8_ = true;

    // Format studies: FISH_GPU_FORCE=<file|q4k|fq3|fq3s> puts every tensor in
    // that format where it exists (else its smallest), no upgrades.
    const char* force = std::getenv("FISH_GPU_FORCE");
    const bool forced = force && *force && allow_requant;
    if (forced) {
        int ff = -1;
        for (int i = 0; i < 4; ++i) if (std::strcmp(force, fmt_names[i]) == 0) ff = fmts[i];
        for (Item& it : items) {
            it.cur = it.opts.size() - 1;
            for (size_t k = 0; k < it.opts.size(); ++k) if (it.opts[k].first == ff) it.cur = k;
        }
    }
    // The device-bytes cache (calib.h). Computed before the format choice:
    // re-quantising the 8B costs ~100 s at 4 threads, so when a previous load
    // left cached files the upgrade passes only move a tensor to a format
    // that is cached (or the file's own bytes), and a budget that differs by
    // a few MB from the last load does not trigger a new re-quantisation.
    std::string variant = "dev1;fq3s1-c0.35";
    if (adapter_.active) variant += ";adapter=" + opts_.adapter_path + ";" + std::to_string(opts_.adapter_scale);
    const std::string dir = allow_requant ? device_cache_dir(*gguf_, variant) : std::string();
    auto cached = [&](const Item& it, int f) {
        if (f == CB::kDevFile) return true;
        if (dir.empty()) return false;
        std::error_code ec;
        return std::filesystem::exists(dir + "/" + cache_file_name(it.t->name, fmt_names[f]), ec);
    };
    bool have_cache = false;
    if (!dir.empty()) {
        std::error_code ec;
        for (const auto& de : std::filesystem::directory_iterator(dir, ec)) {
            if (de.path().extension() == ".dev") { have_cache = true; break; }
        }
    }
    const uint64_t file_all = total();
    uint64_t W = file_all;
    bool upgrade = false;
    if (!fits(W, floor_ctx)) {
        if (!allow_requant) {
            std::snprintf(why, sizeof(why),
                          "no resident plan: file weights %.0f MB + KV + %.0f MB margin > budget %.0f MB and "
                          "re-quantisation is off (FISH_GPU_REQUANT=0)",
                          W / 1048576.0, arena / 1048576.0, budget / 1048576.0);
            return decline();
        }
        if (!forced) {
            for (Item& it : items) it.cur = it.opts.size() - 1;
            upgrade = true;
        }
        W = total();
        const uint64_t all_min = W;
        // Step 4: fewer layers, all at their smallest format. Each step down
        // also frees that layer's share of the KV cache.
        while (lo < NL && !fits(W, floor_ctx)) { ++lo; W = total(); }
        if (!fits(W, floor_ctx)) {
            std::snprintf(why, sizeof(why), "no resident plan: even the output projection alone does not fit the budget %.0f MB",
                          budget / 1048576.0);
            return decline();
        }
        if (lo > 0 && verbose)
            std::fprintf(stderr, "[fish/plan] all %d layers need %.0f MB at the smallest formats (+ KV + %.0f MB margin); "
                                 "budget %.0f MB -> top %d layers on the GPU\n",
                         NL, all_min / 1048576.0, arena / 1048576.0, budget / 1048576.0, NL - lo);
    }
    if (upgrade) {
        const uint64_t limit = (budget - arena - kvpp() * static_cast<uint64_t>(floor_ctx)) / 17 * 16;
        bool cached_only = have_cache;
        // One step up: the next better format (with a cache, the next better
        // one that is cached or the file's own bytes).
        auto up = [&](Item& it) {
            size_t to = it.cur;
            while (to > 0) {
                --to;
                if (!cached_only || cached(it, it.opts[to].first)) break;
                if (to == 0) return false;
            }
            if (to == it.cur) return false;
            const uint64_t d = it.opts[to].second - it.opts[it.cur].second;
            if (W + d > limit) return false;
            it.cur = to;
            W += d;
            return true;
        };
        // With a cache, a tensor whose smallest format is not cached but a
        // bigger cached one fits goes there first (no re-quantisation).
        if (cached_only) {
            for (Item& it : items) {
                if (!active(it) || cached(it, it.opts[it.cur].first)) continue;
                for (size_t k = it.cur; k-- > 0;) {
                    if (!cached(it, it.opts[k].first)) continue;
                    const uint64_t d = it.opts[k].second - it.opts[it.cur].second;
                    if (W + d <= limit) { it.cur = k; W += d; }
                    break;
                }
            }
        }
        // Layer order: ends first (lo, n-1, lo+1, n-2, ...), the output last.
        auto rank = [&](int layer) {
            if (layer >= NL) return 0;
            const int d = std::min(layer - lo, NL - 1 - layer);
            return 2 * d + (layer > (lo + NL) / 2 ? 1 : 0);
        };
        std::vector<Item*> order;
        for (Item& it : items) if (active(it)) order.push_back(&it);
        std::stable_sort(order.begin(), order.end(), [&](Item* a, Item* b) { return rank(a->layer) < rank(b->layer); });
        auto pass = [&](std::initializer_list<int> roles, bool full) {
            for (Item* it : order) {
                if (std::find(roles.begin(), roles.end(), it->role) == roles.end()) continue;
                if (full) { while (up(*it)) {} } else up(*it);
            }
        };
        pass({kK, kV}, true);
        for (int round = 0; round < 3; ++round) {
            pass({kOut}, false);
            pass({kD}, false);
            pass({kQ, kO, kQKV}, false);
            pass({kG, kU}, false);
        }
    }
    int ctx = cfg_.n_ctx;
    if (!fits(W, ctx)) {
        const uint64_t room = budget > arena + W + W / 16 ? budget - arena - W - W / 16 : 0;
        ctx = static_cast<int>(std::min<uint64_t>(static_cast<uint64_t>(cfg_.n_ctx), room / kvpp()));
        ctx = std::max(floor_ctx, ctx / 256 * 256);
    }

    // Calibration (only when something is re-quantised): cached per model.
    bool any_requant = false;
    for (const Item& it : items) if (active(it)) any_requant |= it.opts[it.cur].first != CB::kDevFile;
    // (Cache key "variant" above: bump it when a re-quantiser or kFQ3SCubic
    // in src/gpu/requant.h changes.)
    Importance imp;
    double calib_s = 0;
    bool calib_cached = false;
    const auto t0 = std::chrono::steady_clock::now();
    if (any_requant) {
        const std::string ifile = dir.empty() ? std::string() : dir + "/importance.v1.bin";
        int ntok = 0;
        calib_cached = importance_load(ifile, &imp, &ntok);
        if (std::getenv("FISH_GPU_NO_IMPORTANCE")) { imp.clear(); calib_cached = true; }
        else if (!calib_cached) {
            int max_tok = 512;
            if (const char* e = std::getenv("FISH_CALIB_TOKENS")) max_tok = std::max(16, std::atoi(e));
            std::string cerr;
            if (verbose) std::fprintf(stderr, "[fish/plan] calibrating on up to %d tokens (CPU, once; cached afterwards)\n", max_tok);
            if (calibrate(&imp, max_tok, &cerr)) importance_save(ifile, imp, max_tok);
            else if (verbose) std::fprintf(stderr, "[fish/plan] %s\n", cerr.c_str());
        }
        calib_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        // Re-quantising without importance is not an option (measured on
        // Llama-3.2-1B: FQ3 +69% perplexity, FQ3S unusable; requant.h). The
        // study switch FISH_GPU_NO_IMPORTANCE is the only way to get it.
        if (imp.empty() && !std::getenv("FISH_GPU_NO_IMPORTANCE")) {
            std::snprintf(why, sizeof(why), "resident placement needs re-quantisation but calibration failed; partial placement");
            return decline();
        }
    }

    // Upload: output first, then the layers from the last one down.
    std::stable_sort(items.begin(), items.end(), [](const Item& a, const Item& b) { return a.layer > b.layer; });
    const uint64_t resident0 = gpu->bytes_resident();
    const uint64_t live0 = gpu->memory_free();
    // Reserve, then check that the span's KV cache + arena still fit next to
    // the reservation: WDDM lowers the reported budget by more than the bytes
    // allocated (measured 8-14% more on the RTX 3050), and a span that does
    // not fit is trimmed layer by layer after every tensor was uploaded (and
    // possibly re-quantised) for nothing. Drop layers before uploading instead.
    {
        const uint64_t hr = vram_headroom_bytes(gpu->memory_total());
        const uint64_t span_need = kvpp() * static_cast<uint64_t>(ctx) + (32ull << 20);
        for (int tries = 0;; ++tries) {
            gpu->reserve_device(W);
            const uint64_t live1 = gpu->memory_free();
            if (live1 >= span_need + hr || tries >= 3 || lo >= NL - 1) break;
            gpu->release_reserve();
            const double ratio = std::max(1.0, static_cast<double>(live0 > live1 ? live0 - live1 : 0) / std::max<uint64_t>(W, 1));
            const int lo0 = lo;
            while (lo < NL - 1 && static_cast<double>(W) * ratio + static_cast<double>(kvpp() * static_cast<uint64_t>(ctx) + (32ull << 20) + hr) >
                                      static_cast<double>(live0)) {
                ++lo;
                W = total();
            }
            if (lo == lo0) { ++lo; W = total(); }
            if (verbose)
                std::fprintf(stderr, "[fish/plan] after reserving, live free %.0f MB < span %.0f MB + headroom %.0f MB "
                                     "(driver overhead x%.2f): top %d layers instead\n",
                             live1 / 1048576.0, (kvpp() * static_cast<double>(ctx) + (32ull << 20)) / 1048576.0, hr / 1048576.0,
                             ratio, NL - lo);
        }
        any_requant = false;
        for (const Item& it : items) if (active(it)) any_requant |= it.opts[it.cur].first != CB::kDevFile;
    }
    if (verbose)
        std::fprintf(stderr, "[fish/plan] budget %.0f MB: weights %.0f MB (%d layers) + KV %.0f MB (ctx %d) + margin %.0f MB; live free %.0f MB\n",
                     budget / 1048576.0, W / 1048576.0, NL - lo, kvpp() * static_cast<double>(ctx) / 1048576.0, ctx,
                     arena / 1048576.0, live0 / 1048576.0);
    const auto t1 = std::chrono::steady_clock::now();
    for (const Item& it : items) {
        if (!active(it)) continue;
        const int f = it.opts[it.cur].first;
        const float* w = nullptr;
        std::string cache;
        if (f != CB::kDevFile) {
            auto im = imp.find(it.t->name);
            if (im != imp.end() && im->second.size() == static_cast<size_t>(it.t->ne[0])) w = im->second.data();
            // Study runs without importance must not read/write the cache.
            if (!dir.empty() && !std::getenv("FISH_GPU_NO_IMPORTANCE"))
                cache = dir + "/" + cache_file_name(it.t->name, fmt_names[f]);
        }
        if (!gpu->upload_as(it.t, f, w, cache)) {
            const double done = (gpu->bytes_resident() - resident0) / 1048576.0;
            const double fell = (live0 > gpu->memory_free() ? live0 - gpu->memory_free() : 0) / 1048576.0;
            for (const Item& u : items) if (gpu->resident(u.t)) gpu->release(u.t);
            std::snprintf(why, sizeof(why), "resident upload of %s failed after %.0f MB (live free fell by %.0f MB); partial placement",
                          it.t->name.c_str(), done, fell);
            return decline();
        }
    }
    const double up_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t1).count();
    calib_seconds_ = calib_s;
    upload_seconds_ = up_s;
    if (verbose)
        std::fprintf(stderr, "[fish/plan] uploaded %.0f MB, live free fell by %.0f MB\n",
                     (gpu->bytes_resident() - resident0) / 1048576.0,
                     (live0 > gpu->memory_free() ? live0 - gpu->memory_free() : 0) / 1048576.0);
    gpu_layers_ = NL - lo;
    gpu_bytes_ = gpu->bytes_resident() - resident0;
    for (int li = lo; li < NL; ++li) evict_layer(li);

    int counts[4] = {0, 0, 0, 0};
    for (const Item& it : items) if (active(it)) ++counts[it.opts[it.cur].first];
    char buf[320];
    std::snprintf(buf, sizeof(buf),
                  "%s: %d/%d layers + output, %.0f MB weights (file %d, q4k %d, fq3 %d, fq3s %d tensors), "
                  "ctx %d -> %d, KV %s, budget %.0f MB, calibration %s %.1f s, upload %.1f s",
                  lo == 0 ? "resident" : "partial", NL - lo, NL, gpu_bytes_ / 1048576.0,
                  counts[0], counts[1], counts[2], counts[3],
                  cfg_.n_ctx, ctx, gpu_kv_q8_ ? "int8" : "f16", budget / 1048576.0,
                  !any_requant ? "none" : calib_cached ? "cached" : imp.empty() ? "failed" : "ran",
                  calib_s, up_s);
    placement_note_ = buf;
    if (verbose) std::fprintf(stderr, "[fish/plan] %s\n", buf);
    cfg_.n_ctx = ctx;
    return true;
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
    bool span_planned = false;
    ComputeBackend* gpu = gpu_backend();
    placement_note_ = gpu ? "" : "no GPU backend (no Vulkan device, or FISH_GPU=0)";
    if (gpu) {
        // Same rule as the backend's own ceiling: live free VRAM minus a
        // headroom of max(256 MB, 5% of the heap) (FISH_VRAM_HEADROOM_MB).
        const uint64_t vfree = gpu->memory_free();
        const uint64_t headroom = vram_headroom_bytes(gpu->memory_total());
        const uint64_t vram_budget = env_mb("FISH_VRAM_BUDGET_MB", vfree > headroom ? vfree - headroom : 0);
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

        // Fully resident if at all possible (see plan_resident); the legacy
        // FISH_GPU_REQUANT=1/2/3 modes and an explicit layer cap skip it.
        const bool legacy_requant = requant && !(std::getenv("FISH_GPU_REQUANT") &&
                                                 std::strcmp(std::getenv("FISH_GPU_REQUANT"), "auto") == 0);
        placement_note_.clear();
        if (span_wanted && opts_.allow_span && want >= cfg_.n_layers && !legacy_requant &&
            plan_resident(gpu, vram_budget, arena)) {
            span_planned = true;
        } else {
            gpu_kv_q8_ = false;   // a failed plan must not leave its KV choice behind
            if (placement_note_.empty()) {
                placement_note_ = cfg_.is_moe() ? "planner skipped: MoE (experts stay on the CPU)"
                                : !span_wanted || !opts_.allow_span ? "planner skipped: spans off"
                                : want < cfg_.n_layers ? "planner skipped: GPU layer cap (FISH_GPU_LAYERS / gpu_layers)"
                                : "planner skipped: fixed FISH_GPU_REQUANT mode";
            }
        }

        const uint64_t resident0 = gpu->bytes_resident();
        // The output projection first: it is read on every token whatever the
        // layer split, and it is the single largest matmul in the model.
        if (!span_planned && want > 0 && out_w_ && gpu->supports(out_w_->type) &&
            static_cast<uint64_t>(out_w_->nbytes * est) + arena <= vram_budget) {
            gpu->upload(out_w_);
        }
        // Offload from the last layer backwards: with a partial offload the
        // CPU then runs the first layers while the GPU layers' weights already
        // sit on the device, and the output projection stays near the end.
        for (int li = cfg_.n_layers - 1; li >= 0 && gpu_layers_ < want && !span_planned; --li) {
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
        if (!span_planned) {
            gpu_bytes_ = gpu->bytes_resident() - resident0;
            char b[200];
            std::snprintf(b, sizeof(b), "; per-layer placement: %d/%d layers at file precision, budget %.0f MB (headroom %.0f MB)",
                          gpu_layers_, cfg_.n_layers, vram_budget / 1048576.0, headroom / 1048576.0);
            placement_note_ += b;
        }
    }

    // ---- RAM tier
    uint64_t cpu_bytes = 0;
    for (int li = 0; li < cfg_.n_layers - gpu_layers_; ++li) {
        const LayerTensors& L = layers_[static_cast<size_t>(li)];
        cpu_bytes += L.file_end > L.file_begin ? L.file_end - L.file_begin : 0;
    }
    const uint64_t other = weights_bytes_ > cpu_bytes + gpu_bytes_ ? weights_bytes_ - cpu_bytes - gpu_bytes_ : 0;
    // Merged adapter embeddings/output are private memory on top of the file.
    // KV: only the CPU layers keep one in RAM when the GPU layers run as a
    // span (alloc_cpu_kv); counting all of it pushed Phi-3-mini's CPU layers
    // onto the disk-streaming tier at n_ctx 4096.
    const bool span_likely = gpu_layers_ > 0 && !cfg_.is_moe() && opts_.allow_span &&
        !(std::getenv("FISH_GPU_SPAN") && std::atoi(std::getenv("FISH_GPU_SPAN")) == 0);
    const uint64_t kv_ram = static_cast<uint64_t>(span_likely ? cfg_.n_layers - gpu_layers_ : cfg_.n_layers) *
                            cfg_.n_ctx * cfg_.kv_dim() * sizeof(float) * 2;
    const uint64_t fixed = other + kv_ram + owned_extra_bytes_;
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

        if (cfg_.delta_layer(li)) {
            // Qwen3.5 DeltaNet: no self-attention here, the mixer replaces the
            // whole attention sublayer (rope, KV, softmax) for these layers.
            // Everything from the projections to the residual add below is
            // full-attention-only; the FFN block still runs for every layer.
            if (!delta_mixer(L, li, pos0, n_tok)) return false;
        } else if (L.wqkv) {
            const int W = Q + 2 * KV;
            matmul_tensor(L.wqkv, xb_.data(), &xq_, n_tok, E, W, qkv_.data());
            add_bias(qkv_.data(), n_tok, W, L.bias_qkv);
            for (int t = 0; t < n_tok; ++t) {
                const float* r = row_ptr(qkv_, t, W);
                std::memcpy(row_ptr(q_, t, Q), r, sizeof(float) * Q);
                std::memcpy(row_ptr(k_, t, KV), r + Q, sizeof(float) * KV);
                std::memcpy(row_ptr(v_, t, KV), r + Q + KV, sizeof(float) * KV);
            }
        } else if (L.wq && L.wq->ne[1] == 2 * static_cast<uint64_t>(Q)) {
            // Qwen3.5 full attention: wq is fused per-head [q(256) | gate(256)]
            // x n_head. The gate is applied to the attention output right
            // before o_proj; park it in up_ rows (the FFN re-ensures them).
            matmul_tensor(L.wq, xb_.data(), &xq_, n_tok, E, 2 * Q, gate_.data());
            for (int t = 0; t < n_tok; ++t) {
                // matmul_tensor packs output rows at 2*Q, not at gate_'s
                // 2*n_ff row capacity (reading at 2*n_ff broke prefill t>0)
                const float* r = gate_.data() + static_cast<size_t>(t) * 2 * Q;
                float* qr = row_ptr(q_, t, Q);
                float* gr = row_ptr(up_, t, cfg_.n_ff);
                if (std::getenv("FISH_QWEN35_GATE_HALVES")) {
                    std::memcpy(qr, r, sizeof(float) * Q);
                    for (int i = 0; i < Q; ++i) gr[i] = r[Q + i];
                } else {
                    for (int h = 0; h < H; ++h)
                        for (int d = 0; d < D; ++d) {
                            qr[h * D + d] = r[h * 2 * D + d];
                            gr[h * D + d] = r[h * 2 * D + D + d];
                        }
                }
            }
            matmul_tensor(L.wk, xb_.data(), &xq_, n_tok, E, KV, k_.data());
            matmul_tensor(L.wv, xb_.data(), &xq_, n_tok, E, KV, v_.data());
            add_bias(k_.data(), n_tok, KV, L.bias_k);
            add_bias(v_.data(), n_tok, KV, L.bias_v);
        } else {
            const TensorInfo* qkv_t[3] = {L.wq, L.wk, L.wv};
            const int qkv_n[3] = {Q, KV, KV};
            float* qkv_y[3] = {q_.data(), k_.data(), v_.data()};
            matmul_tensors(qkv_t, 3, xb_.data(), &xq_, n_tok, E, qkv_n, qkv_y);
            add_bias(q_.data(), n_tok, Q, L.bias_q);
            add_bias(k_.data(), n_tok, KV, L.bias_k);
            add_bias(v_.data(), n_tok, KV, L.bias_v);
        }

        // The rest of the attention path is full-attention-only; skip it on
        // DeltaNet layers (they have no q/k/v norms, no KV, no o_proj).
        if (!cfg_.delta_layer(li)) {
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
            const size_t kv_off = static_cast<size_t>(cfg_.kv_slot(li)) * cfg_.n_ctx * KV;
            rope_apply(row_ptr(q_, t, Q), row_ptr(k_, t, KV), H, HKV, pos, rope_);
            float* kc = k_cache_.data() + kv_off + static_cast<size_t>(pos) * KV;
            float* vc = v_cache_.data() + kv_off + static_cast<size_t>(pos) * KV;
            std::memcpy(kc, row_ptr(k_, t, KV), sizeof(float) * KV);
            std::memcpy(vc, row_ptr(v_, t, KV), sizeof(float) * KV);
        }

        const float* kc_layer = k_cache_.data() +
            static_cast<size_t>(cfg_.kv_slot(li)) * cfg_.n_ctx * KV;
        const float* vc_layer = v_cache_.data() +
            static_cast<size_t>(cfg_.kv_slot(li)) * cfg_.n_ctx * KV;
        const int window = (cfg_.sliding_window > 0 && (li % 2) == 0) ? cfg_.sliding_window : 0;
        attention_batch(q_.data(), kc_layer, vc_layer, cfg_.n_ctx, H, HKV, D, pos0, n_tok,
                        attn_.data(), attn_scale, cfg_.attn_softcap, window);

        // Qwen3.5 applies its per-head sigmoid gate to the attention output
        // (dims parked in up_ by the fused q|gate path), then quantises.
        if (cfg_.kind == Arch::Qwen35) {
            for (int t = 0; t < n_tok; ++t) {
                float* a = row_ptr(attn_, t, Q);
                const float* gr = row_ptr(up_, t, cfg_.n_ff);
                for (int i = 0; i < Q; ++i)
                    a[i] *= 1.0f / (1.0f + std::exp(-gr[i]));
            }
        }
        const ActBatch* aq = nullptr;
        if ((has_q8_kernel(L.wo->type) || has_q8k_kernel(L.wo->type)) && Q % kQ8ActBlock == 0) {
            attnq_.quantize(attn_.data(), n_tok, Q);
            aq = &attnq_;
        }
        matmul_tensor(L.wo, attn_.data(), aq, n_tok, Q, E, proj_.data());
        add_bias(proj_.data(), n_tok, E, L.bias_o);
        if (cfg_.kind == Arch::Gemma2 && !L.post_attn_norm_w.empty())
            for (int t = 0; t < n_tok; ++t)
                rmsnorm(row_ptr(proj_, t, E), row_ptr(proj_, t, E), L.post_attn_norm_w.data(), E, cfg_.eps);
        for (int t = 0; t < n_tok; ++t) {
            float* xr = row_ptr(x_, t, E);
            const float* pr = row_ptr(proj_, t, E);
            for (int i = 0; i < E; ++i) xr[i] += pr[i];
        }
        } // end full-attention-only path

        // ---- feed-forward block
        // Qwen3.5 has no ffn_norm: post_attention_norm guards the FFN path
        // (it runs on the attention output + residual, before the MLP).
        const float* fnw = (cfg_.kind == Arch::Qwen35)
                               ? L.post_attn_norm_w.data() : L.ffn_norm_w.data();
        for (int t = 0; t < n_tok; ++t)
            rmsnorm(row_ptr(xb_, t, E), row_ptr(x_, t, E), fnw, E, cfg_.eps);
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

// ------------------------------------------------------------- DeltaNet mixer
//
// Qwen3.5 linear-attention layer (Gated DeltaNet, arXiv:2412.06464), one token
// at a time, all state in fp32:
//
//   x -> [q|k|v] (attn_qkv), depthwise causal conv (ssm_conv1d) + SiLU,
//   per-head delta-rule recurrence S = decay*S + k delta^T with an input
//   dependent decay, RMSNorm(o, ssm_norm) * silu(z) with z = attn_gate,
//   then the ssm_out projection. Returns false only on internal errors.
bool LlamaModel::delta_mixer(const LayerTensors& L, int li, int pos0, int n_tok) {
    (void)pos0;
    const int E = cfg_.n_embd;
    const int G = cfg_.ssm_inner;
    const int CD = cfg_.conv_dim();
    const int K = cfg_.ssm_conv_kernel;
    const int NS = cfg_.ssm_state;
    const int NH = cfg_.ssm_heads;
    if (NH > 64 || NS > 128 || K > 4) return false;
    if (cfg_.conv_dim() != 3 * E) return false;  // v1: q|k|v branches are n_embd wide
    const size_t slot = static_cast<size_t>(delta_slot_[static_cast<size_t>(li)]);
    if (slot == static_cast<size_t>(-1)) return false;

    // History row j holds the conv input of pos+j, so while computing token t:
    // hist[0] = x[t-1], hist[1] = x[t-2], hist[2] = x[t-3].
    float* hist = conv_hist_.data() + slot * (static_cast<size_t>(K) - 1) * static_cast<size_t>(CD);
    float* S0 = ssm_S_.data() + slot * static_cast<size_t>(NH) * NS * NS;

    std::vector<float> knv(static_cast<size_t>(NS));
    for (int t = 0; t < n_tok; ++t) {
        const float* xr = row_ptr(xb_, t, E);

        // 1. x -> [q|k|v] into the (private, row-wide) conv stream
        xq_.quantize(xr, 1, E);
        matmul_tensor(L.wqkv, xr, &xq_, 1, E, CD, qkv_.data());
        float* cur = qkv_.data();   // [CD]

        // 2. causal depthwise conv (kernel taps, newest tap last) + SiLU, then
        //    rotate the history: hist = [x_t, x[t-1], x[t-2]]
        //    Shift the history BEFORE writing x_t into row 0: writing first and
        //    shifting after copied x_t into row 1 too, so the conv saw
        //    [x_t, x_t, x[t-2]] instead of [x_t, x[t-1], x[t-2]] from token 2 on.
        raw_.assign(cur, cur + CD);
        for (int c = 0; c < CD; ++c) {
            const float* cw = L.conv_w.data() + static_cast<size_t>(c) * static_cast<size_t>(K);
            float acc = cw[K - 1] * cur[c];
            acc += cw[K - 2] * hist[0 * CD + c];
            acc += cw[K - 3] * hist[1 * CD + c];
            acc += cw[K - 4] * hist[2 * CD + c];
            acc /= (1.0f + std::exp(-acc));   // SiLU
            cur[c] = acc;
        }
        std::memmove(hist + 2 * static_cast<size_t>(CD), hist + 1 * static_cast<size_t>(CD),
                     sizeof(float) * static_cast<size_t>(CD));
        std::memmove(hist + 1 * static_cast<size_t>(CD), hist,
                     sizeof(float) * static_cast<size_t>(CD));
        std::memcpy(hist, raw_.data(), sizeof(float) * static_cast<size_t>(CD));

        const float* k = cur + G;
        const float* v = cur + 2 * G;

        // 3. input-dependent decays and write rates from the block input
        float ab[128];
        matmul_tensor(L.ssm_alpha, xr, &xq_, 1, E, NH, ab);
        matmul_tensor(L.ssm_beta, xr, &xq_, 1, E, NH, ab + 64);

        // 4. delta-rule recurrence per head (fp32 scratch in up_[t][0..G))
        float* o = row_ptr(up_, t, cfg_.n_ff);
        const float inv = 1.0f / std::sqrt(static_cast<float>(NS));
        for (int h = 0; h < NH; ++h) {
            const float* qh = cur + static_cast<size_t>(h) * NS;
            const float* kh = k + static_cast<size_t>(h) * NS;
            const float* vh = v + static_cast<size_t>(h) * NS;
            float* S = S0 + static_cast<size_t>(h) * NS * NS;
            float kn = 0.0f, qn = 0.0f;
            for (int d = 0; d < NS; ++d) {
                kn += kh[d] * kh[d];
                qn += qh[d] * qh[d];
            }
            kn = 1.0f / std::sqrt(kn + 1e-6f);
            qn = 1.0f / std::sqrt(qn + 1e-6f) * inv;
            for (int d = 0; d < NS; ++d) {
                knv[static_cast<size_t>(d)] = kh[d] * kn;
            }
            // scaled q on the fly
            const float a_raw = ab[static_cast<size_t>(h)] + L.dt_bias[static_cast<size_t>(h)];
            const float sp = a_raw > 30.0f ? a_raw : std::log1p(std::exp(a_raw));
            const float gdec = L.ssm_a[static_cast<size_t>(h)] * sp;
            const float decay = std::exp(gdec);
            const float beta = 1.0f / (1.0f + std::exp(-ab[64 + static_cast<size_t>(h)]));
            float* oh = o + static_cast<size_t>(h) * NS;
            float delta[128];
            for (int j = 0; j < NS; ++j) {
                float rj = 0.0f;
                for (int i = 0; i < NS; ++i)
                    rj += S[static_cast<size_t>(i) * NS + j] * knv[static_cast<size_t>(i)];
                // the read sees the DECAYED state: S' = decay*S, r = S'^T k
                delta[j] = beta * (vh[j] - decay * rj);
            }
            for (int i = 0; i < NS; ++i) {
                // old state decays; the new rank-1 term does not
                const float ki = knv[static_cast<size_t>(i)];
                float* rowS = S + static_cast<size_t>(i) * NS;
                for (int j = 0; j < NS; ++j) {
                    rowS[j] = rowS[j] * decay + ki * delta[j];
                }
            }
            for (int j = 0; j < NS; ++j) {
                float oj = 0.0f;
                for (int i = 0; i < NS; ++i)
                    oj += S[static_cast<size_t>(i) * NS + j] * (qh[i] * qn);
                oh[j] = oj;
            }
        }

        // 5. gated output norm: RMSNorm(o, ssm_norm) * silu(z), z = attn_gate
        float* z = row_ptr(gate_, t, 2 * cfg_.n_ff);
        matmul_tensor(L.wgate, xr, &xq_, 1, E, E, z);
        for (int d = 0; d < E; ++d) z[d] /= (1.0f + std::exp(-z[d]));   // silu
        for (int h = 0; h < NH; ++h)
            rmsnorm(o + static_cast<size_t>(h) * NS, o + static_cast<size_t>(h) * NS,
                    L.ssm_norm_w.data(), NS, cfg_.eps);
        for (int d = 0; d < E; ++d) o[d] *= z[d];

        // 6. output projection and residual add
        xq_.quantize(o, 1, E);
        float* pr = row_ptr(proj_, t, E);
        matmul_tensor(L.ssm_out, o, &xq_, 1, E, E, pr);
        float* xr_out = row_ptr(x_, t, E);
        for (int d = 0; d < E; ++d) xr_out[d] += pr[d];
    }
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
