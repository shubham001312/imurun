/*
 * FISH — LoRA adapter validation and merge (see include/fish/lora.h).
 */
#include <fish/lora.h>
#include <fish/threadpool.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <map>
#include <mutex>
#include <new>

namespace fish {

namespace {

bool ends_with(const std::string& s, const char* suf) {
    const size_t n = std::strlen(suf);
    return s.size() >= n && s.compare(s.size() - n, n, suf) == 0;
}

bool is_2d(const TensorInfo* t) {
    if (!t || t->ne.size() < 2) return false;
    for (size_t d = 2; d < t->ne.size(); ++d) if (t->ne[d] != 1) return false;
    return true;
}

std::string shape(const TensorInfo* t) {
    std::string s = "[";
    for (size_t d = 0; d < t->ne.size(); ++d) s += (d ? ", " : "") + std::to_string(t->ne[d]);
    return s + "]";
}

// Whole 2-D adapter tensor to f32, row by row (rows are whole blocks).
bool load_f32(const TensorInfo* t, std::vector<float>& out) {
    const uint64_t n0 = t->ne[0], n1 = t->ne[1];
    const size_t rb = ggml_row_size(t->type, n0);
    if (rb == 0 || rb * n1 > t->nbytes) return false;
    try { out.resize(static_cast<size_t>(n0 * n1)); } catch (const std::bad_alloc&) { return false; }
    for (uint64_t r = 0; r < n1; ++r)
        if (!dequant_row(t->type, t->data + r * rb, out.data() + r * n0, n0)) return false;
    return true;
}

} // namespace

bool lora_plan(const GgufFile& model, const GgufFile& ad,
               std::vector<LoraTarget>* targets, float* alpha, std::string* err) {
    auto fail = [&](const std::string& why) { if (err) *err = why; return false; };
    targets->clear();

    const std::string gtype = ad.str("general.type", "");
    if (gtype != "adapter")
        return fail("not a LoRA adapter GGUF (general.type = \"" + gtype + "\", expected \"adapter\")");
    const std::string atype = ad.str("adapter.type", "");
    if (atype != "lora")
        return fail("unsupported adapter type \"" + atype + "\" (only \"lora\" is supported)");
    const std::string march = model.str("general.architecture", "llama");
    const std::string aarch = ad.str("general.architecture", "");
    if (!aarch.empty() && aarch != march)
        return fail("adapter is for architecture " + aarch + ", the model is " + march);
    if (ad.has("adapter.alora.invocation_tokens"))
        return fail("activated LoRA (aLoRA) adapters are not supported: they apply only after an "
                    "invocation sequence, which a merge at load cannot express");
    *alpha = static_cast<float>(ad.f64("adapter.lora.alpha", 0.0));

    // Pair .lora_a / .lora_b by base name; anything else is refused rather
    // than skipped, so a partial merge can never look like success.
    std::map<std::string, std::pair<const TensorInfo*, const TensorInfo*>> pairs;
    for (const TensorInfo& t : ad.tensors()) {
        if (ends_with(t.name, ".lora_a")) pairs[t.name.substr(0, t.name.size() - 7)].first = &t;
        else if (ends_with(t.name, ".lora_b")) pairs[t.name.substr(0, t.name.size() - 7)].second = &t;
        else return fail("adapter tensor " + t.name + " is not a LoRA factor (.lora_a/.lora_b); "
                         "adapters that replace whole tensors (norms, embeddings) are not supported");
    }
    if (pairs.empty()) return fail("adapter has no LoRA tensors");

    for (const auto& kv : pairs) {
        const std::string& name = kv.first;
        const TensorInfo* a = kv.second.first;
        const TensorInfo* b = kv.second.second;
        if (!a || !b) return fail("adapter tensor " + name + " lacks its " + (a ? ".lora_b" : ".lora_a") + " half");
        const TensorInfo* w = model.find(name);
        if (!w) return fail("adapter targets " + name + ", which the model does not have (wrong base model?)");
        if (!is_2d(w))
            return fail("adapter targets " + name + " " + shape(w) +
                        ": LoRA on 3-D (MoE expert) tensors is not supported");
        if (!is_2d(a) || !is_2d(b)) return fail("LoRA factors of " + name + " are not 2-D");
        for (const TensorInfo* f : {a, b}) {
            if (!quant_supported(f->type) || !f->data)
                return fail(std::string("LoRA factor ") + f->name + " has unsupported type " + ggml_type_name(f->type));
        }
        if (!quantize_supported(w->type))
            return fail(std::string("cannot merge into ") + name + ": its type " + ggml_type_name(w->type) +
                        " has no requantiser (supported: " + quantize_supported_list() + ")");
        const uint64_t n_in = w->ne[0], n_out = w->ne[1];
        if (n_in % static_cast<uint64_t>(ggml_blck_size(w->type)) != 0)
            return fail("tensor " + name + " row is not a whole number of blocks");
        LoraTarget t;
        t.base = w; t.a = a; t.b = b;
        t.embd = name == "token_embd.weight";
        const uint64_t r = b->ne[0];
        if (r == 0 || r > 4096) return fail("LoRA rank of " + name + " is " + std::to_string(r));
        if (t.embd) {
            if (a->ne[0] != r || a->ne[1] != n_out || b->ne[1] != n_in)
                return fail("tensor " + name + " has incorrect LoRA shapes: a " + shape(a) + ", b " + shape(b) +
                            " for weight " + shape(w) + " (expected a [r, " + std::to_string(n_out) +
                            "], b [r, " + std::to_string(n_in) + "]; wrong base model?)");
        } else {
            if (a->ne[0] != n_in || b->ne[1] != n_out)
                return fail("tensor " + name + " has incorrect LoRA shapes: a " + shape(a) + ", b " + shape(b) +
                            " for weight " + shape(w) + " (wrong base model?)");
            if (a->ne[1] != r)
                return fail("lora_a of " + name + " is not transposed (a " + shape(a) + ", b " + shape(b) + ")");
        }
        t.rank = static_cast<int>(r);
        targets->push_back(t);
    }
    return true;
}

uint64_t lora_merged_bytes(const LoraTarget& t, const LoraMergeOptions& o) {
    if (o.keep_type) return t.base->nbytes;
    return static_cast<uint64_t>(ggml_row_size(o.out_type, t.base->ne[0])) * t.base->ne[1];
}

bool lora_merge(const LoraTarget& t, const LoraMergeOptions& opt, uint8_t* dst,
                MergeStats* stats, std::string* err) {
    const float scale = opt.scale, alpha = opt.alpha;
    auto fail = [&](const std::string& why) { if (err) *err = why; return false; };
    const TensorInfo* w = t.base;
    const int n_in = static_cast<int>(w->ne[0]);
    const int n_out = static_cast<int>(w->ne[1]);
    const int r = t.rank;
    const float eff = lora_effective_scale(scale, alpha, r);

    // P [n_out][r] (scaled) and Q [r][n_in]: delta[o][:] = sum_k P[o][k] Q[k][:].
    std::vector<float> fa, fb, P, Q;
    if (!load_f32(t.a, fa) || !load_f32(t.b, fb)) return fail("cannot read LoRA factors of " + w->name);
    try {
        P.resize(static_cast<size_t>(n_out) * r);
        Q.resize(static_cast<size_t>(r) * n_in);
    } catch (const std::bad_alloc&) {
        return fail("out of memory for the LoRA factors of " + w->name);
    }
    if (t.embd) {
        // a [n_vocab][r] is already P; b [n_embd][r] transposes into Q.
        for (size_t i = 0; i < P.size(); ++i) P[i] = fa[i] * eff;
        for (int e = 0; e < n_in; ++e)
            for (int k = 0; k < r; ++k) Q[static_cast<size_t>(k) * n_in + e] = fb[static_cast<size_t>(e) * r + k];
    } else {
        // b [n_out][r] is P; a [r][n_in] is Q.
        for (size_t i = 0; i < P.size(); ++i) P[i] = fb[i] * eff;
        Q.swap(fa);
    }
    fa.clear(); fa.shrink_to_fit();
    fb.clear(); fb.shrink_to_fit();

    const size_t rb = ggml_row_size(w->type, static_cast<uint64_t>(n_in));
    if (rb * static_cast<size_t>(n_out) > w->nbytes || !w->data) return fail("bad tensor size for " + w->name);
    const GgmlType out_type = opt.keep_type ? w->type : opt.out_type;
    const size_t orb = ggml_row_size(out_type, static_cast<uint64_t>(n_in));
    if (!opt.keep_type && (!quantize_supported(out_type) ||
                           n_in % ggml_blck_size(out_type) != 0))
        return fail(std::string("cannot write ") + w->name + " as " + ggml_type_name(out_type));
    // Per-tensor seed for the stochastic rounding: reproducible merges.
    uint64_t seed = 1469598103934665603ull;
    for (char ch : w->name) seed = (seed ^ static_cast<unsigned char>(ch)) * 1099511628211ull;

    std::atomic<bool> ok{true};
    std::mutex mu;
    MergeStats total;
    ThreadPool::instance().parallel_range(n_out, [&](int r0, int r1) {
        thread_local std::vector<float> base, delta;
        base.resize(static_cast<size_t>(n_in));
        delta.resize(static_cast<size_t>(n_in));
        MergeStats local;
        for (int o = r0; o < r1 && ok.load(std::memory_order_relaxed); ++o) {
            const uint8_t* src = w->data + static_cast<size_t>(o) * rb;
            if (!dequant_row(w->type, src, base.data(), static_cast<uint64_t>(n_in))) { ok = false; break; }
            std::fill(delta.begin(), delta.end(), 0.0f);
            const float* p = P.data() + static_cast<size_t>(o) * r;
            for (int k = 0; k < r; ++k) {
                const float c = p[k];
                if (c == 0.0f) continue;
                const float* q = Q.data() + static_cast<size_t>(k) * n_in;
                float* d = delta.data();
                for (int i = 0; i < n_in; ++i) d[i] += c * q[i];
            }
            uint8_t* out = dst + static_cast<size_t>(o) * orb;
            if (opt.keep_type) {
                if (!merge_row(w->type, src, base.data(), delta.data(), out, static_cast<uint64_t>(n_in), &local,
                               opt.rounding, seed + static_cast<uint64_t>(o) * 0x9E3779B97F4A7C15ull)) { ok = false; break; }
            } else {
                // Other output type: a fresh element-wise / block quantisation of base + delta.
                thread_local std::vector<float> tgt, back;
                tgt.resize(static_cast<size_t>(n_in));
                back.resize(static_cast<size_t>(n_in));
                for (int i = 0; i < n_in; ++i) tgt[static_cast<size_t>(i)] = base[static_cast<size_t>(i)] + delta[static_cast<size_t>(i)];
                if (!quantize_row(out_type, tgt.data(), out, static_cast<uint64_t>(n_in)) ||
                    !dequant_row(out_type, out, back.data(), static_cast<uint64_t>(n_in))) { ok = false; break; }
                for (int i = 0; i < n_in; ++i) {
                    const double dv = delta[static_cast<size_t>(i)];
                    local.delta_sq += dv * dv;
                    local.moved_dot += (static_cast<double>(back[static_cast<size_t>(i)]) - base[static_cast<size_t>(i)]) * dv;
                    const double e = static_cast<double>(back[static_cast<size_t>(i)]) - tgt[static_cast<size_t>(i)];
                    local.err_sq += e * e;
                }
                ++local.blocks_refit;
                ++local.blocks;
            }
        }
        std::lock_guard<std::mutex> lk(mu);
        total.add(local);
    }, 16);
    if (!ok) return fail(std::string("merge failed for ") + w->name + " (" + ggml_type_name(w->type) + ")");
    if (stats) stats->add(total);
    return true;
}

} // namespace fish
