/*
 * GPU backend acceptance test.
 *
 *   fish_test_gpu <model.gguf> [--no-kernels] [--no-e2e] [--no-cpu-compare]
 *
 * 1. Kernels: for every weight type the backend supports that occurs in the
 *    model, upload the blk.0 tensors of that type (or the first 2-D tensor of
 *    that type elsewhere) and compare matmul() with the CPU f32 matmul_batch
 *    for n_tok = 1, 3 (matvec path), 37 and 256 (tiled path). Per element
 *    the error must stay within 1e-3 x (sum_i sum_k |w_ik||x_k|) / n_out.
 *    Times GPU vs the CPU int8 path the engine uses.
 * 2. End to end: LlamaModel::load with automatic placement must put layers on
 *    the GPU and greedy decoding of "The capital of France is" must still say
 *    Paris. Then the same with FISH_GPU_LAYERS=0 for the CPU number.
 *
 * Without a backend (FISH_VULKAN=OFF build, no device, FISH_GPU=0) the kernel
 * part is skipped and the end-to-end part runs CPU-only.
 */
#include <fish/backend.h>
#include <fish/model.h>
#include <fish/ops.h>
#include <fish/quant.h>
#include <fish/tokenizer.h>
#if FISH_HAVE_VULKAN
#include "gpu/vulkan_backend.h"
#include "gpu/requant.h"
#endif

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <string>
#include <vector>

using namespace fish;
using Clock = std::chrono::steady_clock;

static int failures = 0;
static void check(bool cond, const char* what) {
    std::printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) ++failures;
}

static double secs(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration<double>(b - a).count();
}

static void set_env(const char* k, const char* v) {
#ifdef _WIN32
    _putenv_s(k, v ? v : "");
#else
    if (v) setenv(k, v, 1); else unsetenv(k);
#endif
}

// ------------------------------------------------------------------ kernels

static void test_tensor(ComputeBackend* gpu, const TensorInfo& t) {
    const int n_in = static_cast<int>(t.ne[0]);
    const int n_out = static_cast<int>(t.ne[1]);
    std::printf("\n%s  %s  [%d x %d]  %.1f MB\n", t.name.c_str(), ggml_type_name(t.type),
                n_in, n_out, t.nbytes / 1e6);

    auto u0 = Clock::now();
    const bool up = gpu->upload(&t);
    auto u1 = Clock::now();
    char what[200];
    std::snprintf(what, sizeof(what), "upload %s (%.0f ms, %.2f GB/s)", t.name.c_str(),
                  secs(u0, u1) * 1e3, t.nbytes / secs(u0, u1) / 1e9);
    check(up && gpu->resident(&t), what);
    if (!up) return;

    // Column sums of |w| for the error bound: S_t = sum_k A_k |x_tk|.
    std::vector<double> colabs(static_cast<size_t>(n_in), 0.0);
    {
        std::vector<float> row(static_cast<size_t>(n_in));
        const size_t rb = t.row_bytes();
        for (int i = 0; i < n_out; ++i) {
            dequant_row(t.type, t.data + static_cast<size_t>(i) * rb, row.data(), static_cast<uint64_t>(n_in));
            for (int k = 0; k < n_in; ++k) colabs[static_cast<size_t>(k)] += std::fabs(row[static_cast<size_t>(k)]);
        }
    }

    std::mt19937 rng(1234);
    std::normal_distribution<float> nd(0.0f, 1.0f);
    const int toks[] = {1, 3, 37, 256};
    for (int n_tok : toks) {
        std::vector<float> x(static_cast<size_t>(n_tok) * n_in);
        for (float& v : x) v = nd(rng);
        std::vector<float> ref(static_cast<size_t>(n_tok) * n_out), got(ref.size(), NAN);
        matmul_batch(t.type, t.data, x.data(), nullptr, n_tok, n_in, n_out, ref.data());   // f32 oracle

        const bool ok = gpu->matmul(&t, x.data(), nullptr, n_tok, n_in, n_out, got.data());
        double worst_ratio = 0.0, worst_err = 0.0, worst_bound = 0.0;
        bool finite = true;
        for (int tk = 0; tk < n_tok; ++tk) {
            double s = 0.0;
            for (int k = 0; k < n_in; ++k) s += colabs[static_cast<size_t>(k)] * std::fabs(x[static_cast<size_t>(tk) * n_in + k]);
            const double bound = 1e-3 * s / n_out;
            for (int i = 0; i < n_out; ++i) {
                const size_t idx = static_cast<size_t>(tk) * n_out + i;
                if (!std::isfinite(got[idx])) { finite = false; continue; }
                const double e = std::fabs(static_cast<double>(got[idx]) - ref[idx]);
                if (e / bound > worst_ratio) { worst_ratio = e / bound; worst_err = e; worst_bound = bound; }
            }
        }

        // Timing: GPU call (activation upload + kernel + readback) vs the
        // CPU int8 path production uses.
        int reps = n_tok == 1 ? 50 : (n_tok <= 37 ? 10 : 3);
        if (const char* e = std::getenv("FISH_GPU_TEST_REPS"))
            if (n_tok == 1) reps = std::max(1, std::atoi(e));
#if FISH_HAVE_VULKAN
        gpu::vulkan_profile(nullptr, nullptr, nullptr, true);
#endif
        auto g0 = Clock::now();
        for (int r = 0; r < reps; ++r) {
            x[0] += 1e-6f;   // defeat the same-activation shortcut
            gpu->matmul(&t, x.data(), nullptr, n_tok, n_in, n_out, got.data());
        }
        auto g1 = Clock::now();
#if FISH_HAVE_VULKAN
        double kms = 0, wms = 0;
        long subs = 0;
        if (gpu::vulkan_profile(&kms, &wms, &subs, true) && subs > 0)
            std::printf("        profile: kernel %.3f ms/call (%.0f GB/s), submit+wait %.3f ms/call\n",
                        kms / subs, t.nbytes / (kms / subs * 1e-3) / 1e9, wms / subs);
#endif
        ActBatch acts;
        acts.quantize(x.data(), n_tok, n_in);
        auto c0 = Clock::now();
        for (int r = 0; r < reps; ++r)
            matmul_batch(t.type, t.data, x.data(), &acts, n_tok, n_in, n_out, ref.data());
        auto c1 = Clock::now();
        const double gms = secs(g0, g1) * 1e3 / reps, cms = secs(c0, c1) * 1e3 / reps;

        std::snprintf(what, sizeof(what),
                      "n_tok=%3d  max err %.3g (bound %.3g, %.2f of bound)  gpu %.3f ms  cpu-int8 %.3f ms  (%.1fx, %.0f GB/s)",
                      n_tok, worst_err, worst_bound, worst_ratio, gms, cms, cms / std::max(gms, 1e-9),
                      t.nbytes / (gms * 1e-3) / 1e9);
        check(ok && finite && worst_ratio <= 1.0, what);
    }
    gpu->release(&t);
    check(!gpu->resident(&t), "released");
}

// IEEE half from a float in the normal range (test data only).
static uint16_t to_half(float f) {
    uint32_t b;
    std::memcpy(&b, &f, 4);
    const uint32_t sign = (b >> 16) & 0x8000u;
    int exp = static_cast<int>((b >> 23) & 0xFF) - 127 + 15;
    uint32_t man = (b >> 13) & 0x3FFu;
    if (exp <= 0) return static_cast<uint16_t>(sign);          // flush tiny to zero
    if (exp >= 31) { exp = 30; man = 0x3FFu; }
    return static_cast<uint16_t>(sign | (static_cast<uint32_t>(exp) << 10) | man);
}

// Random-but-valid blocks for types no local model file contains: any byte
// pattern is a legal quantised block once its f16 scale fields are finite.
struct Synthetic { TensorInfo t; std::vector<uint8_t> bytes; };

static Synthetic make_synthetic(GgmlType type, int n_in, int n_out, uint32_t seed) {
    Synthetic s;
    s.t.name = std::string("synthetic.") + ggml_type_name(type);
    s.t.type = type;
    s.t.ne = {static_cast<uint64_t>(n_in), static_cast<uint64_t>(n_out)};
    s.t.nbytes = ggml_row_size(type, static_cast<uint64_t>(n_in)) * static_cast<uint64_t>(n_out);
    s.bytes.resize(static_cast<size_t>(s.t.nbytes));
    std::mt19937 rng(seed);
    for (auto& b : s.bytes) b = static_cast<uint8_t>(rng());
    std::uniform_real_distribution<float> sc(0.002f, 0.02f), val(-1.0f, 1.0f);
    auto put_half = [&](size_t off, float v) { const uint16_t h = to_half(v); std::memcpy(&s.bytes[off], &h, 2); };
    const size_t bs = ggml_type_size(type);
    const size_t nblk = s.bytes.size() / bs;
    for (size_t i = 0; i < nblk; ++i) {
        const size_t o = i * bs;
        switch (type) {
            case GgmlType::Q4_0: case GgmlType::Q8_0: put_half(o, sc(rng)); break;
            case GgmlType::Q4_K: case GgmlType::Q5_K: put_half(o, sc(rng)); put_half(o + 2, sc(rng)); break;
            case GgmlType::Q6_K: put_half(o + 208, sc(rng)); break;
            case GgmlType::F16: put_half(o, val(rng)); break;
            case GgmlType::F32: { const float v = val(rng); std::memcpy(&s.bytes[o], &v, 4); break; }
            default: break;
        }
    }
    s.t.data = s.bytes.data();
    return s;
}

#if FISH_HAVE_VULKAN
// FISH_GPU_REQUANT set: the backend uploads a re-quantised copy. Check
// (a) the re-quantisation error against the GGUF weights and (b) the GPU
// kernel against the CPU decode of the very same re-quantised bytes.
// Tolerance of "GPU kernel == CPU decode of the same bytes": f32 activations
// for decode (n_tok <= 4, matvec); for prefill the tensor-core kernel
// (matmul_coop.comp) rounds activations to f16 (2^-11 relative), measured
// up to ~4e-4 x max|y| on Llama-3.2-1B tensors, no measurable perplexity change.
static double act_tol(int n_tok) { return n_tok <= 4 ? 1e-4 : 2e-3; }
static void test_requant(ComputeBackend* gpu, const TensorInfo& t, int mode) {
    const int n_in = static_cast<int>(t.ne[0]), n_out = static_cast<int>(t.ne[1]);
    const bool to_fq3 = mode == 1 && t.type == GgmlType::Q4_K;
    const bool to_q4k = t.type == GgmlType::Q5_K || t.type == GgmlType::Q6_K;
    if ((!to_fq3 && !to_q4k) || n_in % 256) return;
    const char* dst = to_fq3 ? "FQ3" : "Q4_K";
    std::printf("\nrequant %s  %s -> %s  [%d x %d]\n", t.name.c_str(), ggml_type_name(t.type), dst, n_in, n_out);

    auto q0 = Clock::now();
    const bool up = gpu->upload(&t);
    auto q1 = Clock::now();
    char what[220];
    std::snprintf(what, sizeof(what), "upload with re-quantisation (%.0f ms, %.1f MB on device for %.1f MB in the file)",
                  secs(q0, q1) * 1e3, gpu->bytes_resident() / 1e6, t.nbytes / 1e6);
    check(up, what);
    if (!up) return;

    // CPU reference of the device copy + error of the copy itself.
    const size_t rb = to_fq3 ? static_cast<size_t>(n_in) / 256 * gpu::kFQ3BlockBytes : ggml_row_size(GgmlType::Q4_K, n_in);
    std::vector<uint8_t> qrow(rb);
    std::vector<float> orig(static_cast<size_t>(n_in)), back(static_cast<size_t>(n_in));
    std::vector<float> wq(static_cast<size_t>(n_in) * n_out);
    double se = 0, ss = 0;
    for (int i = 0; i < n_out; ++i) {
        dequant_row(t.type, t.data + static_cast<size_t>(i) * t.row_bytes(), orig.data(), static_cast<uint64_t>(n_in));
        if (to_fq3) { gpu::quantize_row_fq3(orig.data(), n_in, qrow.data()); gpu::dequant_row_fq3(qrow.data(), back.data(), n_in); }
        else { gpu::quantize_row_q4k(orig.data(), n_in, qrow.data()); dequant_row(GgmlType::Q4_K, qrow.data(), back.data(), static_cast<uint64_t>(n_in)); }
        for (int k = 0; k < n_in; ++k) {
            const double d = back[static_cast<size_t>(k)] - orig[static_cast<size_t>(k)];
            se += d * d;
            ss += static_cast<double>(orig[static_cast<size_t>(k)]) * orig[static_cast<size_t>(k)];
        }
        std::memcpy(wq.data() + static_cast<size_t>(i) * n_in, back.data(), sizeof(float) * n_in);
    }
    std::printf("        re-quantisation error: relative RMS %.4f (%.2f%%)\n", std::sqrt(se / std::max(ss, 1e-30)),
                100.0 * std::sqrt(se / std::max(ss, 1e-30)));

    std::mt19937 rng(4321);
    std::normal_distribution<float> nd(0.0f, 1.0f);
    for (int n_tok : {1, 37}) {
        std::vector<float> x(static_cast<size_t>(n_tok) * n_in), got(static_cast<size_t>(n_tok) * n_out), ref(got.size());
        for (float& v : x) v = nd(rng);
        matmul_batch(GgmlType::F32, wq.data(), x.data(), nullptr, n_tok, n_in, n_out, ref.data());
        const bool ok = gpu->matmul(&t, x.data(), nullptr, n_tok, n_in, n_out, got.data());
        double worst = 0, scale = 0;
        for (size_t j = 0; j < ref.size(); ++j) {
            worst = std::max(worst, static_cast<double>(std::fabs(got[j] - ref[j])));
            scale = std::max(scale, static_cast<double>(std::fabs(ref[j])));
        }
        std::snprintf(what, sizeof(what), "GPU %s kernel == CPU decode of the same bytes, n_tok=%d: max err %.3g (max |y| %.3g)",
                      dst, n_tok, worst, scale);
        check(ok && worst <= act_tol(n_tok) * std::max(1.0, scale), what);
    }
    gpu->release(&t);
}

// Planner device formats (upload_as): the GPU FQ3S kernel against the CPU
// decoder of the same bytes, n_tok 1 (matvec) and 37 (tiled), plus the
// re-quantisation error of the format (unweighted fit, information only).
static void test_devformat(ComputeBackend* gpu, const TensorInfo& t) {
    const int n_in = static_cast<int>(t.ne[0]), n_out = static_cast<int>(t.ne[1]);
    if (n_in % 256 || gpu->device_bytes(&t, ComputeBackend::kDevFQ3S) == 0) return;
    std::printf("\ndevice format %s  %s -> FQ3S  [%d x %d]\n", t.name.c_str(), ggml_type_name(t.type), n_in, n_out);
    auto q0 = Clock::now();
    const bool up = gpu->upload_as(&t, ComputeBackend::kDevFQ3S, nullptr, std::string());
    char what[220];
    std::snprintf(what, sizeof(what), "upload_as FQ3S (%.0f ms, %.1f MB on device for %.1f MB in the file)",
                  secs(q0, Clock::now()) * 1e3, gpu->bytes_resident() / 1e6, t.nbytes / 1e6);
    check(up, what);
    if (!up) return;
    std::vector<uint8_t> qrow(static_cast<size_t>(n_in) / 256 * gpu::kFQ3SBlockBytes);
    std::vector<float> orig(static_cast<size_t>(n_in)), back(static_cast<size_t>(n_in));
    std::vector<float> wq(static_cast<size_t>(n_in) * n_out);
    double se = 0, ss = 0;
    for (int i = 0; i < n_out; ++i) {
        dequant_row(t.type, t.data + static_cast<size_t>(i) * t.row_bytes(), orig.data(), static_cast<uint64_t>(n_in));
        gpu::quantize_row_fq3s(orig.data(), n_in, qrow.data(), nullptr);
        gpu::dequant_row_fq3s(qrow.data(), back.data(), n_in);
        for (int k = 0; k < n_in; ++k) {
            const double d = back[static_cast<size_t>(k)] - orig[static_cast<size_t>(k)];
            se += d * d;
            ss += static_cast<double>(orig[static_cast<size_t>(k)]) * orig[static_cast<size_t>(k)];
        }
        std::memcpy(wq.data() + static_cast<size_t>(i) * n_in, back.data(), sizeof(float) * n_in);
    }
    std::printf("        re-quantisation error: relative RMS %.2f%%\n", 100.0 * std::sqrt(se / std::max(ss, 1e-30)));
    std::mt19937 rng(777);
    std::normal_distribution<float> nd(0.0f, 1.0f);
    for (int n_tok : {1, 37}) {
        std::vector<float> x(static_cast<size_t>(n_tok) * n_in), got(static_cast<size_t>(n_tok) * n_out), ref(got.size());
        for (float& v : x) v = nd(rng);
        matmul_batch(GgmlType::F32, wq.data(), x.data(), nullptr, n_tok, n_in, n_out, ref.data());
        const bool ok = gpu->matmul(&t, x.data(), nullptr, n_tok, n_in, n_out, got.data());
        double worst = 0, scale = 0;
        for (size_t j = 0; j < ref.size(); ++j) {
            worst = std::max(worst, static_cast<double>(std::fabs(got[j] - ref[j])));
            scale = std::max(scale, static_cast<double>(std::fabs(ref[j])));
        }
        std::snprintf(what, sizeof(what), "GPU FQ3S kernel == CPU decode of the same bytes, n_tok=%d: max err %.3g (max |y| %.3g)",
                      n_tok, worst, scale);
        check(ok && worst <= act_tol(n_tok) * std::max(1.0, scale), what);
        if (n_tok == 1) {
            // Decode kernel throughput over the device bytes (FISH_GPU_PROFILE=1).
            const double dev_bytes = static_cast<double>(gpu->device_bytes(&t, ComputeBackend::kDevFQ3S));
            gpu::vulkan_profile(nullptr, nullptr, nullptr, true);
            for (int r = 0; r < 50; ++r) { x[0] += 1e-6f; gpu->matmul(&t, x.data(), nullptr, 1, n_in, n_out, got.data()); }
            double kms = 0, wms = 0;
            long subs = 0;
            if (gpu::vulkan_profile(&kms, &wms, &subs, true) && subs > 0)
                std::printf("        profile: FQ3S kernel %.3f ms/call (%.0f GB/s over %.1f MB)\n",
                            kms / subs, dev_bytes / (kms / subs * 1e-3) / 1e9, dev_bytes / 1e6);
        }
    }
    gpu->release(&t);
}
#endif

static void run_kernel_tests(ComputeBackend* gpu, const std::string& path) {
    // Every supported type, independent of what the model file contains.
    // Widths chosen so rows are NOT 4-byte aligned where the block size allows
    // it (Q6_K 15 x 210 B, Q4_0/Q8_0 127 x 18/34 B): every straddling read in
    // dequant.glsl gets exercised. 515 rows: partial workgroups on both kernels.
    for (GgmlType ty : {GgmlType::Q4_K, GgmlType::Q5_K, GgmlType::Q6_K, GgmlType::Q8_0,
                        GgmlType::Q4_0, GgmlType::F16, GgmlType::F32}) {
        if (!gpu->supports(ty)) { std::printf("\n%s: not supported by the backend\n", ggml_type_name(ty)); continue; }
        const char* rq = std::getenv("FISH_GPU_REQUANT");
        if (rq && std::atoi(rq) > 0 && (ty == GgmlType::Q4_K || ty == GgmlType::Q5_K || ty == GgmlType::Q6_K))
            continue;   // re-quantised on upload: covered by test_requant below
        int w = 3840;
        if (ty == GgmlType::Q8_0 || ty == GgmlType::Q4_0) w = 4064;
        if (ty == GgmlType::F16 || ty == GgmlType::F32) w = 1056;
        Synthetic s = make_synthetic(ty, w, 515, 99u + static_cast<uint32_t>(ty));
        test_tensor(gpu, s.t);
    }

    std::string err;
    auto g = GgufFile::open(path, &err);
    if (!g) { check(false, ("open " + path + ": " + err).c_str()); return; }

    // blk.0 tensors first; then one tensor of every other supported type.
    std::vector<const TensorInfo*> picks;
    std::map<GgmlType, bool> seen;
    for (const auto& t : g->tensors()) {
        if (t.name.rfind("blk.0.", 0) != 0 || t.ne.size() != 2 || !gpu->supports(t.type)) continue;
        if (t.ne[0] % 32 != 0 || t.ne[1] < 32) continue;   // skip norms / vectors
        picks.push_back(&t);
        seen[t.type] = true;
    }
    for (const auto& t : g->tensors()) {
        if (t.ne.size() != 2 || !gpu->supports(t.type) || seen[t.type]) continue;
        if (t.ne[0] % 32 != 0 || t.ne[1] < 32) continue;
        picks.push_back(&t);
        seen[t.type] = true;
    }
    std::printf("types under test:");
    for (const auto& kv : seen) std::printf(" %s", ggml_type_name(kv.first));
    std::printf("\n");
    check(!picks.empty(), "model has tensors of a supported type");
#if FISH_HAVE_VULKAN
    if (const char* e = std::getenv("FISH_GPU_REQUANT")) {
        const int mode = std::atoi(e);
        if (mode > 0) {
            // With re-quantisation on, the f32-oracle test below would measure the
            // re-quantisation error, not the kernel; test the requant path instead.
            for (const TensorInfo* t : picks) test_requant(gpu, *t, mode);
            check(gpu->bytes_resident() == 0, "every test upload released");
            return;
        }
    }
#endif
    for (const TensorInfo* t : picks) test_tensor(gpu, *t);
#if FISH_HAVE_VULKAN
    for (const TensorInfo* t : picks) if (t->name.rfind("blk.0.", 0) == 0) test_devformat(gpu, *t);
#endif
    check(gpu->bytes_resident() == 0, "every test upload released");

    // matmul_group: q/k/v and gate/up share one input and one submit.
    const char* groups[][3] = {{"blk.0.attn_q.weight", "blk.0.attn_k.weight", "blk.0.attn_v.weight"},
                               {"blk.0.ffn_gate.weight", "blk.0.ffn_up.weight", nullptr}};
    for (const auto& names : groups) {
        const TensorInfo* ts[3] = {};
        int n = 0;
        bool ok = true;
        for (const char* nm : names) {
            if (!nm) break;
            const TensorInfo* t = g->find(nm);
            if (!t || !gpu->supports(t->type) || !gpu->upload(t)) { ok = false; break; }
            ts[n++] = t;
        }
        if (!ok || n == 0) { std::printf("  group %s: skipped\n", names[0]); continue; }
        const int n_in = static_cast<int>(ts[0]->ne[0]);
        for (int n_tok : {1, 37}) {
            std::mt19937 rng(77);
            std::normal_distribution<float> nd(0.0f, 1.0f);
            std::vector<float> x(static_cast<size_t>(n_tok) * n_in);
            for (float& v : x) v = nd(rng);
            std::vector<std::vector<float>> ref(n), got(n);
            int n_outs[3];
            float* ys[3];
            for (int i = 0; i < n; ++i) {
                n_outs[i] = static_cast<int>(ts[i]->ne[1]);
                ref[i].resize(static_cast<size_t>(n_tok) * n_outs[i]);
                got[i].assign(ref[i].size(), NAN);
                ys[i] = got[i].data();
                matmul_batch(ts[i]->type, ts[i]->data, x.data(), nullptr, n_tok, n_in, n_outs[i], ref[i].data());
            }
            const bool ran = gpu->matmul_group(ts, n, x.data(), nullptr, n_tok, n_in, n_outs, ys);
            double worst = 0, scale = 0;
            for (int i = 0; i < n; ++i)
                for (size_t j = 0; j < ref[i].size(); ++j) {
                    worst = std::max(worst, static_cast<double>(std::fabs(got[i][j] - ref[i][j])));
                    scale = std::max(scale, static_cast<double>(std::fabs(ref[i][j])));
                }
            const int reps = n_tok == 1 ? 50 : 5;
            auto a = Clock::now();
            for (int r = 0; r < reps; ++r) { x[0] += 1e-6f; gpu->matmul_group(ts, n, x.data(), nullptr, n_tok, n_in, n_outs, ys); }
            auto b = Clock::now();
            double sep = 0;
            {
                auto c0 = Clock::now();
                for (int r = 0; r < reps; ++r) {
                    x[0] += 1e-6f;
                    for (int i = 0; i < n; ++i) gpu->matmul(ts[i], x.data(), nullptr, n_tok, n_in, n_outs[i], ys[i]);
                }
                sep = secs(c0, Clock::now()) * 1e3 / reps;
            }
            char what[200];
            std::snprintf(what, sizeof(what), "matmul_group %s.. x%d n_tok=%d: max err %.3g (max |y| %.3g)  %.3f ms vs %.3f ms as %d calls",
                          names[0], n, n_tok, worst, scale, secs(a, b) * 1e3 / reps, sep, n);
            check(ran && worst <= act_tol(n_tok) * std::max(1.0, scale), what);
        }
        for (int i = 0; i < n; ++i) gpu->release(ts[i]);
    }
}

// ------------------------------------------------------------------ end to end

static const std::vector<std::string>* vocab_of(const GgufFile& g) {
    const MetaValue* v = g.meta("tokenizer.ggml.tokens");
    return v ? &v->sarr : nullptr;
}

static std::string pretty(const std::string& tok) {
    std::string out;
    for (size_t i = 0; i < tok.size(); ++i) {
        if (i + 1 < tok.size() && static_cast<unsigned char>(tok[i]) == 0xC4 &&
            static_cast<unsigned char>(tok[i + 1]) == 0xA0) { out += ' '; ++i; }
        else if (i + 2 < tok.size() && static_cast<unsigned char>(tok[i]) == 0xE2 &&
                 static_cast<unsigned char>(tok[i + 1]) == 0x96 && static_cast<unsigned char>(tok[i + 2]) == 0x81) { out += ' '; i += 2; }
        else out += tok[i];
    }
    return out;
}

struct E2E {
    bool ok = false;
    int gpu_layers = 0;
    double prefill_tps = 0, prefill128_tps = 0, decode_tps = 0;
    std::string text;
    std::vector<int32_t> tokens;                 // fed decode inputs (= greedy output of the run)
    std::vector<int32_t> argmaxes;               // argmax after the prompt and after each step
    std::vector<std::vector<float>> logits;      // same points, when keep_logits
};

// Load context for the end-to-end runs: 512, or FISH_TEST_CTX (the planner
// fits the device KV to it, so production behaviour needs the server's value).
static int test_ctx() {
    const char* e = std::getenv("FISH_TEST_CTX");
    return e && std::atoi(e) > 0 ? std::atoi(e) : 512;
}

// forced: feed these tokens instead of the run's own argmax (teacher forcing),
// so two runs are compared on identical inputs and a near-tie cannot cascade.
static E2E run_e2e(const std::string& path, const char* label,
                   const std::vector<int32_t>* forced = nullptr, bool keep_logits = false, int n_free = 32) {
    E2E r;
    std::string err;
    auto l0 = Clock::now();
    auto model = LlamaModel::load(path, test_ctx(), &err);
    auto l1 = Clock::now();
    if (!model) { std::printf("  load failed: %s\n", err.c_str()); return r; }
    const LlamaConfig& c = model->config();
    r.gpu_layers = model->gpu_layers();
    ComputeBackend* gpu = gpu_backend();
    std::printf("\n[%s] loaded in %.2f s: %d layers -> gpu %d (%.0f MB), ram %d, disk %d%s%s\n",
                label, secs(l0, l1), c.n_layers, model->gpu_layers(), model->gpu_bytes() / 1048576.0,
                model->ram_layers(), model->disk_layers(), gpu ? ", device " : "", gpu ? gpu->device_name() : "");

    const auto* vocab = vocab_of(model->gguf());
    if (!vocab) return r;
    auto find = [&](const std::string& s) {
        for (size_t i = 0; i < vocab->size(); ++i) if ((*vocab)[i] == s) return static_cast<int>(i);
        return -1;
    };
    std::vector<int32_t> prompt = {c.bos};
    // Byte-level BPE spells a leading space "\xC4\xA0" (GPT-2), SentencePiece
    // (Phi-3, Llama-2 vocabularies) "\xE2\x96\x81"; try both.
    const bool spm = find("\xC4\xA0" "capital") < 0 && find("\xE2\x96\x81" "capital") >= 0;
    for (const char* p : {"The", "\xC4\xA0" "capital", "\xC4\xA0" "of", "\xC4\xA0" "France", "\xC4\xA0" "is"}) {
        std::string piece = p;
        if (spm && piece.rfind("\xC4\xA0", 0) == 0) piece = "\xE2\x96\x81" + piece.substr(2);
        else if (spm) piece = "\xE2\x96\x81" + piece;
        const int id = find(piece);
        if (id < 0) { std::printf("  token not found: %s\n", p); return r; }
        prompt.push_back(id);
    }
    auto record = [&] {
        r.argmaxes.push_back(model->argmax());
        if (keep_logits) r.logits.emplace_back(model->logits(), model->logits() + c.vocab);
    };

    // Longer prefill for a throughput number (the 6-token prompt is latency-bound).
    {
        const int n_pf = std::getenv("FISH_TEST_PREFILL") ? std::atoi(std::getenv("FISH_TEST_PREFILL")) : 128;
        std::vector<int32_t> longp(static_cast<size_t>(n_pf), prompt[1]);
        longp[0] = c.bos;
        auto q0 = Clock::now();
        const bool pf_ok = model->forward(longp.data(), static_cast<int>(longp.size()), 0);
        auto q1 = Clock::now();
        r.prefill128_tps = n_pf / secs(q0, q1);
        if (!pf_ok) std::printf("  PREFILL FORWARD FAILED\n");
        std::printf("  prefill %d tokens: %.3f s (%.1f tok/s)\n", n_pf, secs(q0, q1), r.prefill128_tps);
        model->reset_kv();
    }
    auto p0 = Clock::now();
    if (!model->forward(prompt.data(), static_cast<int>(prompt.size()), 0)) return r;
    auto p1 = Clock::now();
    r.prefill_tps = prompt.size() / secs(p0, p1);
    record();

    const int n_gen = forced ? static_cast<int>(forced->size()) : n_free;
    int produced = 0;
#if FISH_HAVE_VULKAN
    gpu::vulkan_profile(nullptr, nullptr, nullptr, true);
#endif
    auto d0 = Clock::now();
    for (int step = 0; step < n_gen; ++step) {
        const int32_t next = forced ? (*forced)[static_cast<size_t>(step)] : r.argmaxes.back();
        r.tokens.push_back(next);
        r.text += pretty((*vocab)[static_cast<size_t>(next)]);
        ++produced;
        if (!forced && next == c.eos) break;
        if (!model->forward(&next, 1, static_cast<int>(prompt.size()) + step)) break;
        record();
    }
    auto d1 = Clock::now();
    r.decode_tps = produced / secs(d0, d1);
    if (r.text.size() > 400) r.text = r.text.substr(0, 400) + "...";
    std::printf("  continuation%s: '%s'\n", forced ? " (forced)" : "", r.text.c_str());
#if FISH_HAVE_VULKAN
    if (gpu) std::printf("  %s\n", gpu::vulkan_describe().c_str());
#endif
    std::printf("  decode: %d tokens, %.2f tok/s (%.1f ms/token)\n", produced, r.decode_tps, 1000.0 / r.decode_tps);
#if FISH_HAVE_VULKAN
    {
        double kms = 0, wms = 0;
        long subs = 0;
        if (gpu::vulkan_profile(&kms, &wms, &subs, true) && subs > 0 && produced > 0)
            std::printf("  gpu per token: %.1f submits, GPU busy %.2f ms, submit+wait %.2f ms, CPU rest %.2f ms (of %.1f ms)\n",
                        static_cast<double>(subs) / produced, kms / produced, wms / produced,
                        1000.0 / r.decode_tps - wms / produced, 1000.0 / r.decode_tps);
    }
#endif
    r.ok = true;

    // ~LlamaModel releases its device copies; check that it really does.
    model.reset();
    if (gpu) check(gpu->bytes_resident() == 0, "unloading the model frees all VRAM it used");
    return r;
}

// Same inputs, two engines: argmax agreement and logit distance per step.
static void compare_runs(const E2E& a, const E2E& b, const char* what, bool require_same_argmax) {
    const size_t n = std::min(a.logits.size(), b.logits.size());
    double max_diff = 0, max_abs = 0;
    int same = 0;
    for (size_t i = 0; i < n; ++i) {
        for (size_t v = 0; v < a.logits[i].size() && v < b.logits[i].size(); ++v) {
            max_diff = std::max(max_diff, static_cast<double>(std::fabs(a.logits[i][v] - b.logits[i][v])));
            max_abs = std::max(max_abs, static_cast<double>(std::fabs(a.logits[i][v])));
        }
        if (a.argmaxes[i] == b.argmaxes[i]) ++same;
    }
    char msg[256];
    std::snprintf(msg, sizeof(msg), "%s: argmax equal at %d/%zu steps, max |logit diff| %.4f (max |logit| %.2f, %.2f%%)",
                  what, same, n, max_diff, max_abs, 100.0 * max_diff / std::max(max_abs, 1e-9));
    if (require_same_argmax) check(n > 0 && same == static_cast<int>(n) && max_diff <= 0.02 * max_abs, msg);
    else std::printf("  [INFO] %s\n", msg);
}

// Quality of a GPU placement (e.g. with FISH_GPU_REQUANT) against the pure CPU
// path: the CPU greedy run of n steps is the reference; the GPU run is
// teacher-forced on the same tokens. Reports top-1 agreement and KL(P_cpu||P_gpu).
static void run_quality(const std::string& path, int n, double min_top1) {
    const char* user_layers = std::getenv("FISH_GPU_LAYERS");
    set_env("FISH_GPU_LAYERS", "0");
    E2E c = run_e2e(path, "quality reference: cpu", nullptr, true, n);
    set_env("FISH_GPU_LAYERS", user_layers);
    if (!c.ok) { check(false, "cpu reference run"); return; }
    E2E g = run_e2e(path, "quality candidate: gpu placement", &c.tokens, true);
    if (!g.ok) { check(false, "gpu run"); return; }
    const size_t steps = std::min(c.logits.size(), g.logits.size());
    int same = 0;
    double kl_sum = 0, kl_max = 0;
    for (size_t s = 0; s < steps; ++s) {
        const auto& p = c.logits[s];
        const auto& q = g.logits[s];
        double mp = -1e300, mq = -1e300;
        for (size_t v = 0; v < p.size(); ++v) { mp = std::max(mp, static_cast<double>(p[v])); mq = std::max(mq, static_cast<double>(q[v])); }
        double zp = 0, zq = 0;
        for (size_t v = 0; v < p.size(); ++v) { zp += std::exp(p[v] - mp); zq += std::exp(q[v] - mq); }
        const double lzp = std::log(zp) + mp, lzq = std::log(zq) + mq;
        double kl = 0;
        for (size_t v = 0; v < p.size(); ++v) {
            const double lp = p[v] - lzp, lq = q[v] - lzq;
            kl += std::exp(lp) * (lp - lq);
        }
        kl_sum += kl;
        kl_max = std::max(kl_max, kl);
        if (c.argmaxes[s] == g.argmaxes[s]) ++same;
    }
    const double top1 = 100.0 * same / std::max<size_t>(1, steps);
    char what[220];
    std::snprintf(what, sizeof(what), "quality over %zu teacher-forced steps: top-1 agreement %.1f%% (need >= %.0f%%), KL mean %.4f max %.4f nats",
                  steps, top1, min_top1, kl_sum / std::max<size_t>(1, steps), kl_max);
    check(top1 >= min_top1, what);
    std::printf("\nquality summary: gpu %.2f tok/s vs cpu %.2f tok/s (teacher-forced decode)\n", g.decode_tps, c.decode_tps);
}

// ------------------------------------------------------------------ perplexity
//
// Held-out text (NOT the engine's calibration text), teacher-forced in chunks
// of 64 with all logits: perplexity of the CPU path at file precision (the
// reference: the Q4 file itself) vs the GPU placement, plus top-1 agreement
// and KL(P_cpu || P_gpu) at every position.
static const char* kHeldOut =
    "The old bridge across the river had been closed for repairs since the spring floods. "
    "Every morning the ferryman rowed the children to the school on the far bank, and every "
    "afternoon he waited under the willow tree to bring them home again. He knew each of them "
    "by name, and he knew which ones would trail their fingers in the cold water.\n\n"
    "A compiler translates source code into machine instructions in several stages. The lexer "
    "turns characters into tokens, the parser builds a syntax tree, semantic analysis checks "
    "types and scopes, and the optimiser rewrites the intermediate representation before the "
    "code generator selects instructions and allocates registers.\n\n"
    "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n\n"
    "Suppose a shop sells notebooks for 4 dollars each and pens for 1.50 dollars. If Maria buys "
    "three notebooks and four pens, she pays 12 + 6 = 18 dollars in total.\n\n"
    "Q: What causes the seasons on Earth?\nA: The tilt of the Earth's axis, about 23.4 degrees. "
    "When the northern hemisphere leans towards the Sun it receives more direct sunlight and "
    "longer days, which is summer there, while the southern hemisphere has winter.\n\n"
    "The committee agreed on three actions: publish the survey results by Thursday, invite the "
    "regional managers to the review, and prepare a revised budget for the second quarter.\n";

struct PplRun { bool ok = false; double nll = 0; int n = 0; std::vector<std::vector<float>> lp; double tps = 0; };

static PplRun run_ppl_one(const std::string& path, const char* label) {
    PplRun r;
    std::string err;
    auto l0 = Clock::now();
    auto model = LlamaModel::load(path, test_ctx(), &err);
    auto l1 = Clock::now();
    if (!model) { std::printf("  load failed: %s\n", err.c_str()); return r; }
    std::printf("\n[%s] loaded in %.2f s: gpu %d layers (%.0f MB), ctx %d\n", label, secs(l0, l1),
                model->gpu_layers(), model->gpu_bytes() / 1048576.0, model->config().n_ctx);
    if (!model->placement_note().empty()) std::printf("  %s\n", model->placement_note().c_str());
#if FISH_HAVE_VULKAN
    if (gpu_backend() && model->gpu_layers() > 0) std::printf("  %s\n", gpu::vulkan_describe().c_str());
#endif
    auto tok = Tokenizer::create(model->gguf(), &err);
    if (!tok) { std::printf("  tokenizer: %s\n", err.c_str()); return r; }
    std::vector<int32_t> ids = tok->encode(kHeldOut, true, false);
    const int V = model->config().vocab;
    const int n = std::min<int>(static_cast<int>(ids.size()), model->config().n_ctx);
    for (int p0 = 0; p0 < n - 1; p0 += 64) {
        const int m = std::min(64, n - p0);
        if (!model->forward(ids.data() + p0, m, p0, true)) { std::printf("  forward failed at %d\n", p0); return r; }
        for (int t = 0; t < m && p0 + t + 1 < n; ++t) {
            const float* lg = model->logits_at(t);
            double mx = -1e300;
            for (int v = 0; v < V; ++v) mx = std::max(mx, static_cast<double>(lg[v]));
            double z = 0;
            for (int v = 0; v < V; ++v) z += std::exp(lg[v] - mx);
            const double lz = std::log(z) + mx;
            std::vector<float> lp(static_cast<size_t>(V));
            for (int v = 0; v < V; ++v) lp[static_cast<size_t>(v)] = static_cast<float>(lg[v] - lz);
            r.nll -= lp[static_cast<size_t>(ids[static_cast<size_t>(p0 + t + 1)])];
            r.lp.push_back(std::move(lp));
            ++r.n;
        }
    }
    // Decode speed of this placement on the same model object.
    model->reset_kv();
    int32_t cur = ids[0];
    model->forward(&cur, 1, 0);
    auto d0 = Clock::now();
    const int steps = 64;
    for (int s = 1; s <= steps; ++s) { cur = model->argmax(); if (!model->forward(&cur, 1, s)) break; }
    r.tps = steps / secs(d0, Clock::now());
    std::printf("  %d positions, perplexity %.3f, decode %.2f tok/s\n", r.n, std::exp(r.nll / std::max(1, r.n)), r.tps);
    r.ok = r.n > 0;
    return r;
}

static void run_ppl(const std::string& path) {
    const char* user_layers = std::getenv("FISH_GPU_LAYERS");
    set_env("FISH_GPU_LAYERS", "0");
    PplRun c = run_ppl_one(path, "ppl reference: cpu, file weights");
    set_env("FISH_GPU_LAYERS", user_layers);
    PplRun g = run_ppl_one(path, "ppl candidate: gpu placement");
    if (!c.ok || !g.ok || c.n != g.n) { check(false, "perplexity runs"); return; }
    int same = 0;
    double kl = 0, klmax = 0;
    for (int i = 0; i < c.n; ++i) {
        const auto& a = c.lp[static_cast<size_t>(i)];
        const auto& b = g.lp[static_cast<size_t>(i)];
        size_t ia = 0, ib = 0;
        double k = 0;
        for (size_t v = 0; v < a.size(); ++v) {
            if (a[v] > a[ia]) ia = v;
            if (b[v] > b[ib]) ib = v;
            k += std::exp(static_cast<double>(a[v])) * (a[v] - b[v]);
        }
        same += ia == ib;
        kl += k;
        klmax = std::max(klmax, k);
    }
    const double pc = std::exp(c.nll / c.n), pg = std::exp(g.nll / g.n);
    std::printf("\nppl summary: %d positions | perplexity cpu/file %.3f, gpu %.3f (%+.2f%%) | top-1 agreement %.1f%% | "
                "KL mean %.4f max %.3f nats | decode gpu %.2f tok/s, cpu %.2f tok/s\n",
                c.n, pc, pg, 100.0 * (pg / pc - 1.0), 100.0 * same / c.n, kl / c.n, klmax, g.tps, c.tps);
}

int main(int argc, char** argv) {
    std::string path = argc > 1 ? argv[1] : "models/Llama-3.2-1B-Instruct-Q4_K_M.gguf";
    bool e2e = true, cpu_compare = true, kernels = true;
    int quality = 0;
    for (int i = 2; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--no-e2e")) e2e = false;
        if (!std::strcmp(argv[i], "--no-kernels")) kernels = false;
        if (!std::strcmp(argv[i], "--no-cpu-compare")) cpu_compare = false;
        if (!std::strcmp(argv[i], "--quality") && i + 1 < argc) quality = std::atoi(argv[++i]);
        if (!std::strcmp(argv[i], "--fq3s-scan")) {
            // Plain FQ3S round-trip error of every re-quantisable tensor (study).
            std::string err;
            auto g = GgufFile::open(path, &err);
            if (!g) return 1;
            for (const auto& t : g->tensors()) {
                if (t.ne.size() != 2 || t.ne[0] % 256) continue;
                const int n_in = static_cast<int>(t.ne[0]), n_out = static_cast<int>(t.ne[1]);
                std::vector<float> a(static_cast<size_t>(n_in)), b(a.size());
                std::vector<uint8_t> q(static_cast<size_t>(n_in) / 256 * gpu::kFQ3SBlockBytes);
                double se = 0, ss = 0, worst_row = 0;
                for (int r = 0; r < n_out; r += std::max(1, n_out / 512)) {
                    dequant_row(t.type, t.data + static_cast<size_t>(r) * t.row_bytes(), a.data(), static_cast<uint64_t>(n_in));
                    gpu::quantize_row_fq3s(a.data(), n_in, q.data(), nullptr);
                    gpu::dequant_row_fq3s(q.data(), b.data(), n_in);
                    double rse = 0, rss = 0;
                    for (int k = 0; k < n_in; ++k) { const double d = b[k] - a[k]; rse += d * d; rss += static_cast<double>(a[k]) * a[k]; }
                    se += rse; ss += rss;
                    worst_row = std::max(worst_row, std::sqrt(rse / std::max(rss, 1e-30)));
                }
                std::printf("%-28s %-5s rel RMS %6.2f%%  worst row %6.2f%%\n", t.name.c_str(), ggml_type_name(t.type),
                            100.0 * std::sqrt(se / std::max(ss, 1e-30)), 100.0 * worst_row);
            }
            return 0;
        }
        if (!std::strcmp(argv[i], "--ppl")) { run_ppl(path); std::printf("\n%s\n", failures ? "PPL FAILED" : "PPL DONE"); return failures ? 1 : 0; }
    }
    if (quality > 0) {
        run_quality(path, quality, 95.0);
        std::printf("\n%s (%d failure%s)\n", failures ? "GPU TEST FAILED" : "GPU TEST PASSED",
                    failures, failures == 1 ? "" : "s");
        return failures ? 1 : 0;
    }

#if FISH_HAVE_VULKAN
    gpu::vulkan_init_now();
    std::printf("vulkan: %s\n", gpu::vulkan_status());
#else
    std::printf("vulkan: not compiled in (FISH_VULKAN=OFF or no SDK)\n");
#endif
    ComputeBackend* gpu = gpu_backend();
    if (gpu) {
        std::printf("backend %s on %s: %.0f MB total, %.0f MB free\n", gpu->name(), gpu->device_name(),
                    gpu->memory_total() / 1048576.0, gpu->memory_free() / 1048576.0);
        if (kernels) run_kernel_tests(gpu, path);
    } else {
        std::printf("no GPU backend: kernel checks SKIPPED\n");
    }

    if (e2e) {
        const char* user_layers = std::getenv("FISH_GPU_LAYERS");
        const char* user_span = std::getenv("FISH_GPU_SPAN");
        if (user_layers) std::printf("note: FISH_GPU_LAYERS=%s is set by the caller\n", user_layers);
        E2E a = run_e2e(path, gpu ? "auto placement" : "cpu (no backend)", nullptr, true);
        check(a.ok, "end-to-end run");
        if (gpu) check(a.gpu_layers > 0, "automatic placement puts layers on the GPU");
        check(a.text.find("Paris") != std::string::npos, "continues 'The capital of France is' with Paris");
        if (gpu && cpu_compare && a.ok) {
            // Same GPU layers, per-matmul routing (attention etc. on the CPU).
            set_env("FISH_GPU_SPAN", "0");
            E2E s = run_e2e(path, "gpu per-matmul (FISH_GPU_SPAN=0)", &a.tokens, true);
            set_env("FISH_GPU_SPAN", user_span);
            // Everything on the CPU.
            set_env("FISH_GPU_LAYERS", "0");
            E2E b = run_e2e(path, "cpu only (FISH_GPU_LAYERS=0)", &a.tokens, true);
            set_env("FISH_GPU_LAYERS", user_layers);
            check(b.ok && b.gpu_layers == 0, "FISH_GPU_LAYERS=0 keeps everything on the CPU");
            if (s.ok) compare_runs(a, s, "auto vs FISH_GPU_SPAN=0 (teacher-forced)", true);
            if (b.ok) compare_runs(a, b, "auto vs CPU (teacher-forced)", false);
            std::printf("\nsummary: decode %.2f tok/s auto (%d GPU layers) | %.2f per-matmul | %.2f CPU  (%.2fx vs CPU)\n",
                        a.decode_tps, a.gpu_layers, s.decode_tps, b.decode_tps, a.decode_tps / std::max(b.decode_tps, 1e-9));
            std::printf("         prefill 128: %.1f | %.1f | %.1f tok/s\n", a.prefill128_tps, s.prefill128_tps, b.prefill128_tps);
        }
    }

    std::printf("\n%s (%d failure%s)\n", failures ? "GPU TEST FAILED" : "GPU TEST PASSED",
                failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
