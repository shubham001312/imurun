/*
 * M5 verification — LoRA adapters merged at load (include/fish/lora.h).
 *
 *   fish_test_m5 <model.gguf> [real-adapter.gguf [merged-reference.gguf]]
 *
 * 1. Builds a synthetic adapter (random A/B, rank 4, alpha 8) for five
 *    tensors of the model: Q4_K and Q6_K projections in three layers, and
 *    token_embd (the transposed orientation), one factor stored as F16.
 * 2. Merged weights: for sampled rows, merged vs dequant(W) + scale*alpha/r*BA
 *    must be within quantisation error AND never farther than the unmerged
 *    row (the grid guarantee of merge_row).
 * 3. scale = 0 goes through the whole merge path and must reproduce every
 *    tensor byte for byte and the logits exactly.
 * 4. scale = 1 changes the logits; the merged model also runs through the
 *    normal GPU placement (when a GPU backend is active) and agrees with the
 *    CPU path.
 * 5. Bad adapters (wrong shape, non-LoRA tensor, other architecture, missing
 *    target) are refused with a clear error.
 * 6. Optional: a real adapter from the tuning channel — merge time, retention,
 *    greedy sanity, and (with a merged reference GGUF) teacher-forced KL /
 *    top-1 agreement of base and base+adapter against the reference.
 */
#include <fish/backend.h>
#include <fish/lora.h>
#include <fish/model.h>
#include <fish/quant.h>
#include <fish/tokenizer.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

using namespace fish;

static int failures = 0;
static void check(bool cond, const std::string& what) {
    std::printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what.c_str());
    if (!cond) ++failures;
}

// ---------------------------------------------------------------- GGUF writer

struct WTensor {
    std::string name;
    GgmlType type;
    std::vector<uint64_t> ne;
    std::vector<uint8_t> bytes;
};
struct WKv {
    std::string key;
    bool is_float;
    std::string s;
    float f;
};

static void put_u32(std::vector<uint8_t>& o, uint32_t v) { for (int i = 0; i < 4; ++i) o.push_back(static_cast<uint8_t>(v >> (8 * i))); }
static void put_u64(std::vector<uint8_t>& o, uint64_t v) { for (int i = 0; i < 8; ++i) o.push_back(static_cast<uint8_t>(v >> (8 * i))); }
static void put_str(std::vector<uint8_t>& o, const std::string& s) { put_u64(o, s.size()); o.insert(o.end(), s.begin(), s.end()); }

static bool write_gguf(const std::string& path, const std::vector<WKv>& kvs, const std::vector<WTensor>& ts) {
    std::vector<uint8_t> o;
    o.insert(o.end(), {'G', 'G', 'U', 'F'});
    put_u32(o, 3);
    put_u64(o, ts.size());
    put_u64(o, kvs.size());
    for (const WKv& kv : kvs) {
        put_str(o, kv.key);
        if (kv.is_float) {
            put_u32(o, 6);   // FLOAT32
            uint32_t b; std::memcpy(&b, &kv.f, 4); put_u32(o, b);
        } else {
            put_u32(o, 8);   // STRING
            put_str(o, kv.s);
        }
    }
    uint64_t off = 0;
    for (const WTensor& t : ts) {
        put_str(o, t.name);
        put_u32(o, static_cast<uint32_t>(t.ne.size()));
        for (uint64_t n : t.ne) put_u64(o, n);
        put_u32(o, static_cast<uint32_t>(t.type));
        put_u64(o, off);
        off += (t.bytes.size() + 31) / 32 * 32;
    }
    while (o.size() % 32) o.push_back(0);
    for (const WTensor& t : ts) {
        o.insert(o.end(), t.bytes.begin(), t.bytes.end());
        while (o.size() % 32) o.push_back(0);
    }
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    const bool ok = std::fwrite(o.data(), 1, o.size(), f) == o.size();
    std::fclose(f);
    return ok;
}

static WTensor f32_tensor(const std::string& name, uint64_t ne0, uint64_t ne1, const std::vector<float>& v) {
    WTensor t{name, GgmlType::F32, {ne0, ne1}, {}};
    t.bytes.resize(v.size() * 4);
    std::memcpy(t.bytes.data(), v.data(), t.bytes.size());
    return t;
}
// Rounds v through f16 in place, so the test's reference uses what was stored.
static WTensor f16_tensor(const std::string& name, uint64_t ne0, uint64_t ne1, std::vector<float>& v) {
    WTensor t{name, GgmlType::F16, {ne0, ne1}, {}};
    t.bytes.resize(v.size() * 2);
    for (size_t i = 0; i < v.size(); ++i) {
        const uint16_t h = fp32_to_fp16(v[i]);
        std::memcpy(t.bytes.data() + 2 * i, &h, 2);
        v[i] = fp16_to_fp32(h);
    }
    return t;
}

// ---------------------------------------------------------------- synthetic adapter

struct Factor {
    std::string name;      // base tensor
    bool embd = false;
    int n_in = 0, n_out = 0, rank = 0;
    std::vector<float> P;  // [n_out][r]  (unscaled)
    std::vector<float> Q;  // [r][n_in]
};

static double row_rms(const TensorInfo* t, int row) {
    const uint64_t n = t->ne[0];
    std::vector<float> v(n);
    dequant_row(t->type, t->data + static_cast<size_t>(row) * ggml_row_size(t->type, n), v.data(), n);
    double s = 0;
    for (float x : v) s += static_cast<double>(x) * x;
    return std::sqrt(s / static_cast<double>(n));
}

// delta row o (unscaled by eff)
static void delta_row(const Factor& f, int o, float eff, std::vector<float>& d) {
    d.assign(static_cast<size_t>(f.n_in), 0.0f);
    for (int k = 0; k < f.rank; ++k) {
        const float c = f.P[static_cast<size_t>(o) * f.rank + k] * eff;
        const float* q = f.Q.data() + static_cast<size_t>(k) * f.n_in;
        for (int i = 0; i < f.n_in; ++i) d[static_cast<size_t>(i)] += c * q[i];
    }
}

static std::string tmp_path(const char* tag) {
    const char* d = std::getenv("TEMP");
    if (!d) d = std::getenv("TMPDIR");
    if (!d) d = ".";
    return std::string(d) + "/fish_m5_" + tag + ".gguf";
}

// ---------------------------------------------------------------- helpers

static std::vector<float> prompt_logits(LlamaModel& m, const std::vector<int32_t>& ids) {
    m.reset_kv();
    if (!m.forward(ids.data(), static_cast<int>(ids.size()), 0)) return {};
    return std::vector<float>(m.logits(), m.logits() + m.config().vocab);
}

static int argmax_of(const std::vector<float>& v) {
    return static_cast<int>(std::max_element(v.begin(), v.end()) - v.begin());
}

static std::string greedy(LlamaModel& m, Tokenizer& tk, const std::vector<int32_t>& ids, int n) {
    m.reset_kv();
    if (!m.forward(ids.data(), static_cast<int>(ids.size()), 0)) return "<forward failed>";
    std::vector<int32_t> out;
    int pos = static_cast<int>(ids.size());
    for (int i = 0; i < n; ++i) {
        int32_t t = m.argmax();
        out.push_back(t);
        if (tk.is_eog(t) || !m.forward(&t, 1, pos++)) break;
    }
    return tk.decode(out);
}

static std::vector<float> softmax(const float* l, int n) {
    std::vector<float> p(l, l + n);
    const float mx = *std::max_element(p.begin(), p.end());
    double s = 0;
    for (float& x : p) { x = std::exp(x - mx); s += x; }
    for (float& x : p) x = static_cast<float>(x / s);
    return p;
}

// Teacher-forced logits for every position of `ids` (batched, all_logits).
static std::vector<std::vector<float>> all_logits(LlamaModel& m, const std::vector<int32_t>& ids) {
    m.reset_kv();
    std::vector<std::vector<float>> out;
    if (!m.forward(ids.data(), static_cast<int>(ids.size()), 0, true)) return out;
    for (size_t t = 0; t < ids.size(); ++t)
        out.emplace_back(m.logits_at(static_cast<int>(t)), m.logits_at(static_cast<int>(t)) + m.config().vocab);
    return out;
}

int main(int argc, char** argv) {
    const std::string model_path = argc > 1 ? argv[1] : "models/Llama-3.2-1B-Instruct-Q4_K_M.gguf";
    std::string real_adapter = argc > 2 ? argv[2] : "";
    if (real_adapter.empty() && std::getenv("FISH_TEST_ADAPTER")) real_adapter = std::getenv("FISH_TEST_ADAPTER");
    const std::string reference = argc > 3 ? argv[3] : "";

    LlamaModel::LoadOptions cpu;
    cpu.gpu_layers = 0;
    cpu.allow_span = false;
    const int n_ctx = 256;

    std::string err;
    auto base = LlamaModel::load(model_path, n_ctx, cpu, &err);
    if (!base) { std::printf("[FAIL] load: %s\n", err.c_str()); return 1; }
    auto tk = Tokenizer::create(base->gguf(), &err);
    if (!tk) { std::printf("[FAIL] tokenizer: %s\n", err.c_str()); return 1; }
    const std::vector<int32_t> prompt = tk->encode("The capital of France is", true, false);
    const std::vector<float> lg_base = prompt_logits(*base, prompt);
    check(!lg_base.empty(), "base forward");

    // ---- 1. synthetic adapter
    const char* names[] = {"blk.0.attn_q.weight", "blk.0.attn_v.weight", "blk.7.ffn_up.weight",
                           "blk.15.ffn_down.weight", "token_embd.weight"};
    const int R = 4;
    const float alpha = 8.0f;
    std::mt19937 rng(1234);
    std::uniform_real_distribution<float> uni(-1.0f, 1.0f);
    std::vector<Factor> factors;
    std::vector<WTensor> wts;
    for (const char* nm : names) {
        const TensorInfo* w = base->gguf().find(nm);
        if (!w) { std::printf("  (model has no %s, skipped)\n", nm); continue; }
        Factor f;
        f.name = nm;
        f.embd = std::string(nm) == "token_embd.weight";
        f.n_in = static_cast<int>(w->ne[0]);
        f.n_out = static_cast<int>(w->ne[1]);
        f.rank = R;
        // delta rms ~ 0.5 x weight rms at scale 1 (eff = alpha / r = 2)
        double wr = 0;
        for (int k = 0; k < 8; ++k) wr += row_rms(w, (k * 997) % f.n_out);
        wr /= 8;
        const float eff = alpha / R;
        const float sq = std::sqrt(3.0f);   // uniform(-1,1) * sqrt3 has unit variance
        const float sp = static_cast<float>(0.5 * wr / (eff * std::sqrt(static_cast<double>(R))));
        f.P.resize(static_cast<size_t>(f.n_out) * R);
        f.Q.resize(static_cast<size_t>(R) * f.n_in);
        for (float& x : f.P) x = uni(rng) * sq * sp;
        for (float& x : f.Q) x = uni(rng) * sq;
        // File layout (lora.h): standard a = Q [r][n_in] -> ne [n_in, r], b = P -> ne [r, n_out];
        // token_embd a = P [n_vocab][r] -> ne [r, n_vocab], b = Q^T [n_embd][r] -> ne [r, n_embd].
        if (f.embd) {
            std::vector<float> bt(static_cast<size_t>(f.n_in) * R);
            for (int e = 0; e < f.n_in; ++e)
                for (int k = 0; k < R; ++k) bt[static_cast<size_t>(e) * R + k] = f.Q[static_cast<size_t>(k) * f.n_in + e];
            wts.push_back(f32_tensor(f.name + ".lora_a", R, static_cast<uint64_t>(f.n_out), f.P));
            wts.push_back(f32_tensor(f.name + ".lora_b", R, static_cast<uint64_t>(f.n_in), bt));
        } else if (f.name == "blk.0.attn_q.weight") {
            wts.push_back(f16_tensor(f.name + ".lora_a", static_cast<uint64_t>(f.n_in), R, f.Q));
            wts.push_back(f16_tensor(f.name + ".lora_b", R, static_cast<uint64_t>(f.n_out), f.P));
        } else {
            wts.push_back(f32_tensor(f.name + ".lora_a", static_cast<uint64_t>(f.n_in), R, f.Q));
            wts.push_back(f32_tensor(f.name + ".lora_b", R, static_cast<uint64_t>(f.n_out), f.P));
        }
        factors.push_back(std::move(f));
    }
    const std::string arch = base->config().arch;
    const std::vector<WKv> good_kv = {{"general.architecture", false, arch, 0}, {"general.type", false, "adapter", 0},
                                      {"adapter.type", false, "lora", 0}, {"adapter.lora.alpha", true, "", alpha}};
    const std::string synth = tmp_path("synthetic");
    check(write_gguf(synth, good_kv, wts), "synthetic adapter written (" + std::to_string(factors.size()) + " tensors, r=4)");

    // ---- 2. merged weights vs the exact target
    LlamaModel::LoadOptions o1 = cpu;
    o1.adapter_path = synth;
    o1.adapter_scale = 1.0f;
    auto t0 = std::chrono::steady_clock::now();
    auto m1 = LlamaModel::load(model_path, n_ctx, o1, &err);
    const double load1 = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    check(m1 != nullptr, "load with adapter at scale 1" + (m1 ? std::string() : ": " + err));
    if (!m1) return 1;
    const LoraReport& rep = m1->adapter();
    std::printf("  adapter: %d tensors, rank %d, alpha %.1f, eff scale %.3f, %.1f MB merged in %.2f s (load %.2f s); "
                "retention %.3f residual %.3f, %llu/%llu blocks refit\n",
                rep.tensors, rep.rank_max, rep.alpha, rep.effective_scale, rep.merged_bytes / 1048576.0,
                rep.seconds, load1, rep.retention(), rep.residual(),
                static_cast<unsigned long long>(rep.stats.blocks_refit), static_cast<unsigned long long>(rep.stats.blocks));
    check(rep.active && rep.tensors == static_cast<int>(factors.size()) && rep.rank_max == R &&
          std::fabs(rep.effective_scale - alpha / R) < 1e-6f, "adapter report (tensors, rank, alpha/r scaling)");
    check(m1->weight("blk.0.attn_k.weight") == base->gguf().find("blk.0.attn_k.weight") ||
          m1->weight("blk.0.attn_k.weight") == m1->gguf().find("blk.0.attn_k.weight"),
          "untargeted tensors stay the file's own");
    if (m1->config().tied_output)
        check(m1->weight("output.weight") == nullptr, "tied model: no output.weight binding added");

    // Per-tensor check of sampled rows against the exact f32 target.
    auto check_rows = [&](LlamaModel& m, const char* mode, bool nearest_mode) {
        for (const Factor& f : factors) {
            const TensorInfo* src = m.gguf().find(f.name);
            const TensorInfo* mt = m.weight(f.name);
            if (!src || !mt) { check(false, "bound " + f.name); continue; }
            if (!nearest_mode)
                check(mt != src && mt->type == src->type && mt->nbytes == src->nbytes,
                      f.name + ": merged copy, same type " + ggml_type_name(src->type));
            const uint64_t n = src->ne[0];
            const size_t rb = ggml_row_size(src->type, n);
            double worst_rel = 0, sum_err = 0, sum_delta = 0, sum_target = 0, moved = 0;
            bool never_worse = true;
            std::vector<float> b(n), mm(n), d;
            for (int k = 0; k < 24; ++k) {
                const int row = static_cast<int>((static_cast<long long>(k) * 7919 + 13) % f.n_out);
                dequant_row(src->type, src->data + row * rb, b.data(), n);
                dequant_row(mt->type, mt->data + row * rb, mm.data(), n);
                delta_row(f, row, m.adapter().effective_scale, d);
                double e = 0, dd = 0, tt = 0;
                for (uint64_t i = 0; i < n; ++i) {
                    const double tg = static_cast<double>(b[i]) + d[i];
                    e += (mm[i] - tg) * (mm[i] - tg);
                    dd += static_cast<double>(d[i]) * d[i];
                    tt += tg * tg;
                    moved += (static_cast<double>(mm[i]) - b[i]) * d[i];
                }
                if (e > dd * (1.0 + 1e-6) + 1e-12) never_worse = false;
                worst_rel = std::max(worst_rel, std::sqrt(e / std::max(tt, 1e-30)));
                sum_err += e; sum_delta += dd; sum_target += tt;
            }
            // Quantisation error bound for the target: Q4_K's level spacing is
            // ~1/15 of a group's range, Q6_K's ~1/63, so the relative RMS error
            // of a freshly placed 4-bit value is at most ~10%, 6-bit ~3%.
            const double bound = src->type == GgmlType::Q4_K ? 0.12 : src->type == GgmlType::Q6_K ? 0.035 : 0.01;
            const double rel = std::sqrt(sum_err / sum_target);
            const double retention = moved / std::max(sum_delta, 1e-30);
            char what[256];
            std::snprintf(what, sizeof(what), "[%s] %s (%s): rel RMS vs W+sBA %.4f (worst row %.4f, bound %.3f), "
                          "retention %.3f", mode, f.name.c_str(), ggml_type_name(src->type), rel, worst_rel, bound,
                          retention);
            check(rel <= bound && worst_rel <= bound * 1.5 && retention > 0.9 && retention < 1.1, what);
            if (nearest_mode)
                check(never_worse, std::string("[") + mode + "] " + f.name +
                      ": no sampled row is farther from W+sBA than the unmerged row");
        }
    };
    check_rows(*m1, "stochastic", false);
    {
        LlamaModel::LoadOptions on = o1;
        on.adapter_rounding = MergeRounding::Nearest;
        auto mn = LlamaModel::load(model_path, n_ctx, on, &err);
        check(mn != nullptr, "load with nearest rounding" + (mn ? std::string() : ": " + err));
        if (mn) check_rows(*mn, "nearest", true);
    }

    // ---- 3. scale 0: exact
    {
        LlamaModel::LoadOptions o0 = cpu;
        o0.adapter_path = synth;
        o0.adapter_scale = 0.0f;
        auto m0 = LlamaModel::load(model_path, n_ctx, o0, &err);
        check(m0 != nullptr, "load with adapter at scale 0" + (m0 ? std::string() : ": " + err));
        if (m0) {
            bool same = m0->adapter().tensors == static_cast<int>(factors.size());
            for (const Factor& f : factors) {
                const TensorInfo* src = m0->gguf().find(f.name);
                const TensorInfo* mt = m0->weight(f.name);
                same = same && mt && src && mt != src && std::memcmp(mt->data, src->data, src->nbytes) == 0;
            }
            check(same, "scale 0: every merged tensor is byte-identical to the file's");
            const std::vector<float> lg0 = prompt_logits(*m0, prompt);
            check(lg0.size() == lg_base.size() &&
                  std::memcmp(lg0.data(), lg_base.data(), lg0.size() * sizeof(float)) == 0,
                  "scale 0: logits bit-identical to the plain model");
        }
    }

    // ---- 4. scale 1 changes the logits; GPU placement path
    const std::vector<float> lg1 = prompt_logits(*m1, prompt);
    {
        float md = 0;
        bool finite = !lg1.empty();
        for (size_t i = 0; i < lg1.size() && finite; ++i) {
            finite = std::isfinite(lg1[i]);
            md = std::max(md, std::fabs(lg1[i] - lg_base[i]));
        }
        char what[160];
        std::snprintf(what, sizeof(what), "scale 1: logits finite and different (max |dlogit| %.3f, argmax %d vs %d)",
                      md, argmax_of(lg1), argmax_of(lg_base));
        check(finite && md > 1e-2f, what);
    }
    if (ComputeBackend* gpu = gpu_backend()) {
        m1.reset();   // VRAM is not needed by the CPU model, but RAM is shared; keep one merged copy
        LlamaModel::LoadOptions og;
        og.adapter_path = synth;
        og.adapter_scale = 1.0f;
        auto mg = LlamaModel::load(model_path, n_ctx, og, &err);
        check(mg != nullptr, "GPU placement load with adapter" + (mg ? std::string() : ": " + err));
        if (mg) {
            const TensorInfo* t = mg->weight("blk.15.ffn_down.weight");
            std::printf("  placement: %d GPU layers (%s), merged blk.15.ffn_down resident on device: %s\n",
                        mg->gpu_layers(), mg->gpu_span() ? "span" : "per-matmul",
                        (t && gpu->resident(t)) ? "yes" : "no");
            const std::vector<float> lgg = prompt_logits(*mg, prompt);
            float md = 0, mx = 0;
            for (size_t i = 0; i < lgg.size(); ++i) {
                md = std::max(md, std::fabs(lgg[i] - lg1[i]));
                mx = std::max(mx, std::fabs(lg1[i]));
            }
            char what[200];
            std::snprintf(what, sizeof(what), "GPU-placed merged model agrees with the CPU merged model "
                          "(argmax %d vs %d, max |dlogit| %.3f = %.2f%%)", argmax_of(lgg), argmax_of(lg1),
                          md, 100.0 * md / std::max(mx, 1e-6f));
            check(!lgg.empty() && argmax_of(lgg) == argmax_of(lg1) && md <= 0.05f * mx, what);
            if (mg->gpu_layers() > 0) check(t && gpu->resident(t), "merged tensor was uploaded by the normal placement");
        }
    } else {
        std::printf("  (no GPU backend active: placement check skipped)\n");
    }

    // ---- 5. refusals
    auto expect_refusal = [&](const char* tag, const std::vector<WKv>& kv, const std::vector<WTensor>& ts,
                              const char* needle) {
        const std::string p = tmp_path(tag);
        write_gguf(p, kv, ts);
        LlamaModel::LoadOptions o = cpu;
        o.adapter_path = p;
        std::string e;
        auto m = LlamaModel::load(model_path, n_ctx, o, &e);
        check(!m && e.rfind("adapter: ", 0) == 0 && e.find(needle) != std::string::npos,
              std::string("refuses ") + tag + ": " + e);
        std::remove(p.c_str());
    };
    {
        const Factor& f = factors[0];
        std::vector<float> a(f.Q), bb(static_cast<size_t>(R) * 7, 0.1f);
        expect_refusal("wrong_shape", good_kv,
                       {f32_tensor(f.name + ".lora_a", static_cast<uint64_t>(f.n_in), R, a),
                        f32_tensor(f.name + ".lora_b", R, 7, bb)}, "shape");
        expect_refusal("non_lora", good_kv,
                       {f32_tensor("blk.0.attn_norm.weight", static_cast<uint64_t>(f.n_in), 1,
                                   std::vector<float>(static_cast<size_t>(f.n_in), 1.0f))}, "not a LoRA factor");
        std::vector<WKv> other = good_kv;
        other[0].s = "qwen2";
        if (arch == "qwen2") other[0].s = "llama";
        expect_refusal("other_arch", other, {wts[0], wts[1]}, "architecture");
        WTensor a2 = wts[0], b2 = wts[1];
        a2.name = "blk.999.attn_q.weight.lora_a";
        b2.name = "blk.999.attn_q.weight.lora_b";
        expect_refusal("missing_target", good_kv, {a2, b2}, "does not have");
        std::vector<WKv> notad = good_kv;
        notad[1].s = "model";
        expect_refusal("not_adapter", notad, {wts[0], wts[1]}, "not a LoRA adapter");
    }
    std::remove(synth.c_str());
    m1.reset();

    // ---- 6. real adapter (optional)
    if (!real_adapter.empty()) {
        std::printf("\nreal adapter: %s\n", real_adapter.c_str());
        const std::vector<int32_t> text = tk->encode(
            "The quick brown fox jumps over the lazy dog. In 1969, Neil Armstrong became the first "
            "person to walk on the Moon. Water boils at 100 degrees Celsius at sea level, and the "
            "chemical symbol for gold is Au. Paris is the capital of France.", true, false);
        const int V = base->config().vocab;
        auto flat = [](const std::vector<std::vector<float>>& x) {
            std::vector<double> v;
            for (const auto& r : x) v.insert(v.end(), r.begin(), r.end());
            return v;
        };
        const std::vector<double> L_base = flat(all_logits(*base, text));

        struct Variant { const char* label; MergeRounding rounding; const char* type; float scale; };
        const Variant vars[] = {
            {"same type, stochastic (default)", MergeRounding::Stochastic, "", 1.0f},
            {"same type, nearest", MergeRounding::Nearest, "", 1.0f},
            {"f16 oracle, scale 0", MergeRounding::Stochastic, "f16", 0.0f},
            {"f16 oracle, scale 1", MergeRounding::Stochastic, "f16", 1.0f},
            {"q8_0 merge, stochastic", MergeRounding::Stochastic, "q8_0", 1.0f},
        };
        constexpr int NV = 5;
        std::vector<std::vector<double>> L(NV);
        std::unique_ptr<LlamaModel> keep;   // the default merge, for the reference comparison
        for (int vi = 0; vi < NV; ++vi) {
            LlamaModel::LoadOptions o = cpu;
            o.adapter_path = real_adapter;
            o.adapter_scale = vars[vi].scale;
            o.adapter_rounding = vars[vi].rounding;
            o.adapter_merge_type = vars[vi].type;
            t0 = std::chrono::steady_clock::now();
            auto mr = LlamaModel::load(model_path, n_ctx, o, &err);
            const double lr = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            check(mr != nullptr, std::string("real adapter merges: ") + vars[vi].label + (mr ? "" : ": " + err));
            if (!mr) continue;
            const LoraReport& r = mr->adapter();
            std::printf("  %-32s %d tensors r=%d alpha %.0f eff %.2f, %.1f MB, merge %.2f s (load %.2f s), "
                        "retention %.4f residual %.4f\n",
                        vars[vi].label, r.tensors, r.rank_max, r.alpha, r.effective_scale, r.merged_bytes / 1048576.0,
                        r.seconds, lr, r.retention(), r.residual());
            L[vi] = flat(all_logits(*mr, text));
            if (vi == 0) {
                const std::string g = greedy(*mr, *tk, prompt, 12);
                std::printf("  greedy (default merge): 'The capital of France is%s'\n", g.c_str());
                check(g.find("Paris") != std::string::npos, "real adapter: still answers Paris");
                keep = std::move(mr);
            }
        }
        // Output-level effect of the adapter: logit change vs the f16 oracle's
        // change (each measured from its own baseline: plain model / f16 scale 0).
        if (!L[2].empty() && !L[3].empty() && L[0].size() == L_base.size() && L[1].size() == L_base.size()) {
            auto effect = [&](const std::vector<double>& x, const std::vector<double>& x0, double* cosv, double* relv) {
                double dot = 0, nx = 0, no = 0, ne = 0;
                for (size_t i = 0; i < x.size(); ++i) {
                    const double dx = x[i] - x0[i], dor = L[3][i] - L[2][i];
                    dot += dx * dor; nx += dx * dx; no += dor * dor; ne += (dx - dor) * (dx - dor);
                }
                *cosv = dot / std::sqrt(std::max(nx * no, 1e-300));
                *relv = std::sqrt(ne / std::max(no, 1e-300));
            };
            double cs, rs, cn, rn, c8 = 0, r8 = 0;
            effect(L[0], L_base, &cs, &rs);
            effect(L[1], L_base, &cn, &rn);
            if (L[4].size() == L_base.size()) effect(L[4], L_base, &c8, &r8);
            double no = 0;
            for (size_t i = 0; i < L[3].size(); ++i) no += (L[3][i] - L[2][i]) * (L[3][i] - L[2][i]);
            std::printf("  adapter effect on teacher-forced logits (%zu tokens) vs the f16 oracle "
                        "(RMS oracle change %.4f):\n"
                        "    stochastic: cosine %.3f, relative error %.3f\n"
                        "    nearest:    cosine %.3f, relative error %.3f\n"
                        "    q8_0 merge: cosine %.3f, relative error %.3f (FISH_ADAPTER_MERGE_TYPE=q8_0)\n",
                        text.size(), std::sqrt(no / static_cast<double>(L[3].size())), cs, rs, cn, rn, c8, r8);
            if (!reference.empty()) {
                // The tuning channel's own pipeline (f16 merge -> llama-quantize): its
                // effect is reference minus the same pipeline without the adapter.
                // Only meaningful when argv[1] is that no-adapter file.
                auto ref = LlamaModel::load(reference, n_ctx, cpu, &err);
                if (ref) {
                    const std::vector<double> L_ref = flat(all_logits(*ref, text));
                    if (L_ref.size() == L_base.size()) {
                        double cr, rr;
                        effect(L_ref, L_base, &cr, &rr);
                        std::printf("    reference file minus argv[1]: cosine %.3f, relative error %.3f "
                                    "(the channel's merged GGUF, if argv[1] is its no-adapter twin)\n", cr, rr);
                    }
                }
            }
            check(cs > cn, "stochastic rounding keeps more of the adapter's effect than nearest");
        }
        if (!reference.empty() && keep) {
            auto ref = LlamaModel::load(reference, n_ctx, cpu, &err);
            check(ref != nullptr, "reference (merged by the tuning channel) loads" + (ref ? std::string() : ": " + err));
            if (ref) {
                const auto lr_ref = all_logits(*ref, text);
                auto compare = [&](const std::vector<double>& x, double* kl, int* agree) {
                    double s = 0;
                    *agree = 0;
                    for (size_t t = 0; t < lr_ref.size(); ++t) {
                        std::vector<float> xr(x.begin() + static_cast<long long>(t) * V,
                                              x.begin() + static_cast<long long>(t + 1) * V);
                        const auto p = softmax(lr_ref[t].data(), V);
                        const auto q = softmax(xr.data(), V);
                        double k = 0;
                        for (int i = 0; i < V; ++i)
                            if (p[static_cast<size_t>(i)] > 0) k += p[static_cast<size_t>(i)] *
                                (std::log(p[static_cast<size_t>(i)]) - std::log(std::max(q[static_cast<size_t>(i)], 1e-30f)));
                        s += k;
                        if (argmax_of(xr) == argmax_of(lr_ref[t])) ++*agree;
                    }
                    *kl = s / static_cast<double>(lr_ref.size());
                };
                if (lr_ref.size() == text.size()) {
                    double kb, ka, kn;
                    int ab, aa, an;
                    compare(L_base, &kb, &ab);
                    compare(L[0], &ka, &aa);
                    compare(L[1], &kn, &an);
                    std::printf("  teacher-forced vs the tuning channel's merged Q4_K_M (%zu tokens):\n"
                                "    base                     KL %.5f, top-1 %d/%zu\n"
                                "    base + adapter (stoch.)  KL %.5f, top-1 %d/%zu\n"
                                "    base + adapter (nearest) KL %.5f, top-1 %d/%zu\n",
                                text.size(), kb, ab, text.size(), ka, aa, text.size(), kn, an, text.size());
                    std::printf("  [INFO] unless argv[1] was quantised exactly like the reference (same tool, no\n"
                                "         imatrix), most of this KL is the two quantisations, not the adapter\n");
                }
            }
        }
    } else {
        std::printf("\n  (no real adapter given: pass one as argv[2] or FISH_TEST_ADAPTER)\n");
    }

    std::printf("\n%s (%d failure%s)\n", failures ? "M5 FAILED" : "M5 PASSED", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
