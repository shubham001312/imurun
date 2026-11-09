/*
 * Kernel unit test — every SIMD kernel slot vs the scalar reference, no model.
 *
 * fish_test_m1 checks the kernels on real weight rows, but needs a GGUF. This
 * test builds its own blocks so it can run anywhere, including CI runners
 * (the macOS arm64 job runs it for the NEON kernels):
 *
 *   - rows of every type with an int8 / f32 kernel slot (Q4_0, Q8_0, Q4_K,
 *     Q5_K, Q6_K), half from quantize_row() on random floats, half random
 *     bytes with sane fp16 scales (covers every scale / bit pattern);
 *   - activation quantisation (Q8Act, Q8ActK) must be byte-identical to the
 *     scalar quantiser, including round-half-to-even ties and zero blocks;
 *   - every dot product must equal the scalar reference to float rounding,
 *     measured against sum |w x| (the same criterion as fish_test_m1).
 *
 * Tables checked: the installed one (avx2 / neon-dotprod / neon / scalar),
 * and on arm64 builds both NEON variants explicitly (plain NEON is not what
 * an M-series installs, so it would otherwise never run). Ends with a short
 * per-kernel timing, scalar vs each table.
 *
 * Usage: fish_test_kernels [--no-bench]
 */
#include <fish/gguf.h>
#include <fish/quant.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace fish;

typedef float (*dot_f32_ptr)(const float*, const float*, uint64_t);

static int failures = 0;

static void check(bool cond, const char* what) {
    std::printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) ++failures;
}

struct Rng {
    uint64_t s;
    explicit Rng(uint64_t seed) : s(seed) {}
    uint32_t u32() {
        s = s * 6364136223846793005ull + 1442695040888963407ull;
        return static_cast<uint32_t>(s >> 33);
    }
    float uni() { return static_cast<float>(u32() & 0xFFFFFF) / 16777216.0f; }   // [0, 1)
    float sym(float a) { return (uni() * 2.0f - 1.0f) * a; }
};

struct Table {
    std::string label;
    QuantKernels k;
    dot_f32_ptr dot_f32;
};

static float dot_f32_scalar_ref(const float* a, const float* b, uint64_t n) {
    double s = 0;
    for (uint64_t i = 0; i < n; ++i) s += static_cast<double>(a[i]) * b[i];
    return static_cast<float>(s);
}

// ---------------------------------------------------------------- rows

struct TypeInfo {
    GgmlType type;
    const char* name;
    size_t bsize;
    int block;
    int d_off;     // byte offset of the fp16 d in a block
    int dmin_off;  // byte offset of the fp16 dmin, or -1
};

static const TypeInfo kTypes[] = {
    {GgmlType::Q4_0, "Q4_0", 18, 32, 0, -1},
    {GgmlType::Q8_0, "Q8_0", 34, 32, 0, -1},
    {GgmlType::Q4_K, "Q4_K", 144, 256, 0, 2},
    {GgmlType::Q5_K, "Q5_K", 176, 256, 0, 2},
    {GgmlType::Q6_K, "Q6_K", 210, 256, 208, -1},
};

// One row of `n` values. mode 0: quantise random floats (realistic blocks);
// mode 1: random bytes with sane scales (every bit pattern of qs/qh/scales).
static std::vector<uint8_t> make_row(const TypeInfo& t, uint64_t n, int mode, Rng& rng) {
    std::vector<uint8_t> row(static_cast<size_t>(n / t.block) * t.bsize);
    if (mode == 0) {
        std::vector<float> w(n);
        for (auto& v : w) v = rng.sym(1.0f) * (rng.uni() < 0.02f ? 8.0f : 1.0f);
        if (!quantize_row(t.type, w.data(), row.data(), n)) row.clear();
        return row;
    }
    for (auto& b : row) b = static_cast<uint8_t>(rng.u32());
    for (size_t off = 0; off < row.size(); off += t.bsize) {
        const uint16_t d = fp32_to_fp16(0.001f + rng.uni() * 0.05f);
        std::memcpy(row.data() + off + t.d_off, &d, 2);
        if (t.dmin_off >= 0) {
            const uint16_t m = fp32_to_fp16(0.001f + rng.uni() * 0.05f);
            std::memcpy(row.data() + off + t.dmin_off, &m, 2);
        }
    }
    return row;
}

// ---------------------------------------------------------------- slot access

static dot_f32_fn f32_slot(const QuantKernels& k, GgmlType t) {
    switch (t) {
        case GgmlType::Q4_K: return k.q4_K;
        case GgmlType::Q6_K: return k.q6_K;
        case GgmlType::Q8_0: return k.q8_0;
        case GgmlType::Q4_0: return k.q4_0;
        default: return nullptr;   // Q5_K has no f32 slot
    }
}

static float int_dot(const QuantKernels& k, GgmlType t, const void* row,
                     const Q8Act* x32, const Q8ActK* x256, uint64_t n) {
    switch (t) {
        case GgmlType::Q4_K: return k.q4_K_q8k(row, x256, n);
        case GgmlType::Q5_K: return k.q5_K_q8k(row, x256, n);
        case GgmlType::Q6_K: return k.q6_K_q8k(row, x256, n);
        case GgmlType::Q8_0: return k.q8_0_q8(row, x32, n);
        case GgmlType::Q4_0: return k.q4_0_q8(row, x32, n);
        default: return 0.0f;
    }
}

// ---------------------------------------------------------------- tests

static std::vector<float> make_acts(uint64_t n, int kind, Rng& rng) {
    std::vector<float> x(n);
    for (uint64_t i = 0; i < n; ++i) {
        switch (kind) {
            case 0: x[i] = rng.sym(2.0f); break;                                      // plain
            case 1: x[i] = rng.sym(1.0f) * (rng.uni() < 0.01f ? 50.0f : 1.0f); break; // outliers
            case 2: x[i] = (i / 64) % 3 == 0 ? 0.0f : rng.sym(0.01f); break;          // zero blocks
            default: {
                // Exact ties: one value of 127 per block pins d = 1, id = 1, so
                // k + 0.5 must round to even exactly as lrintf does.
                const int r = static_cast<int>(rng.u32() % 253) - 126;
                x[i] = (i % 32 == 0) ? 127.0f : static_cast<float>(r) + 0.5f * (rng.u32() & 1);
                break;
            }
        }
    }
    return x;
}

static void test_quantize(const Table& t, const QuantKernels& ref) {
    Rng rng(7);
    bool same32 = true, samek = true;
    for (int kind = 0; kind < 4; ++kind) {
        for (uint64_t n : {256ull, 4096ull}) {
            const auto x = make_acts(n, kind, rng);
            std::vector<Q8Act> a(n / 32), b(n / 32);
            std::vector<Q8ActK> ak(n / 256), bk(n / 256);
            std::memset(a.data(), 0x55, a.size() * sizeof(Q8Act));
            std::memset(b.data(), 0xAA, b.size() * sizeof(Q8Act));
            std::memset(ak.data(), 0x55, ak.size() * sizeof(Q8ActK));
            std::memset(bk.data(), 0xAA, bk.size() * sizeof(Q8ActK));
            ref.quantize(x.data(), n, a.data());
            t.k.quantize(x.data(), n, b.data());
            ref.quantize_k(x.data(), n, ak.data());
            t.k.quantize_k(x.data(), n, bk.data());
            if (std::memcmp(a.data(), b.data(), a.size() * sizeof(Q8Act)) != 0) same32 = false;
            if (std::memcmp(ak.data(), bk.data(), ak.size() * sizeof(Q8ActK)) != 0) samek = false;
        }
    }
    char what[160];
    std::snprintf(what, sizeof(what), "%s: Q8Act quantisation byte-identical to scalar", t.label.c_str());
    check(same32, what);
    std::snprintf(what, sizeof(what), "%s: Q8ActK quantisation byte-identical to scalar", t.label.c_str());
    check(samek, what);
}

static void test_dots(const Table& t, const QuantKernels& ref) {
    for (const TypeInfo& ti : kTypes) {
        double max_int = 0.0, max_f32 = 0.0;
        bool built = true;
        Rng rng(1000 + static_cast<uint64_t>(ti.type));
        for (uint64_t n : {256ull, 4096ull}) {
            for (int r = 0; r < 24; ++r) {
                const auto row = make_row(ti, n, r & 1, rng);
                if (row.empty()) { built = false; continue; }
                const auto x = make_acts(n, r % 3, rng);
                std::vector<Q8Act> x32(n / 32);
                std::vector<Q8ActK> x256(n / 256);
                ref.quantize(x.data(), n, x32.data());
                ref.quantize_k(x.data(), n, x256.data());

                std::vector<float> w(n), xq(n);
                dequant_row(ti.type, row.data(), w.data(), n);
                const bool k_layout = has_q8k_kernel(ti.type);
                for (uint64_t i = 0; i < n; ++i)
                    xq[i] = k_layout ? x256[i / 256].d * x256[i / 256].qs[i % 256]
                                     : x32[i / 32].d * x32[i / 32].qs[i % 32];
                double mass_i = 1e-9, mass_f = 1e-9;
                for (uint64_t i = 0; i < n; ++i) {
                    mass_i += std::fabs(static_cast<double>(w[i]) * xq[i]);
                    mass_f += std::fabs(static_cast<double>(w[i]) * x[i]);
                }

                const float want_i = int_dot(ref, ti.type, row.data(), x32.data(), x256.data(), n);
                const float got_i = int_dot(t.k, ti.type, row.data(), x32.data(), x256.data(), n);
                max_int = std::max(max_int, std::fabs(static_cast<double>(got_i) - want_i) / mass_i);
                if (!std::isfinite(got_i)) max_int = 1e9;

                if (dot_f32_fn f = f32_slot(t.k, ti.type)) {
                    const float want_f = f32_slot(ref, ti.type)(row.data(), x.data(), n);
                    const float got_f = f(row.data(), x.data(), n);
                    max_f32 = std::max(max_f32, std::fabs(static_cast<double>(got_f) - want_f) / mass_f);
                    if (!std::isfinite(got_f)) max_f32 = 1e9;
                }
            }
        }
        char what[200];
        if (!built) {
            std::snprintf(what, sizeof(what), "%s: quantize_row builds %s rows", t.label.c_str(), ti.name);
            check(false, what);
            continue;
        }
        std::snprintf(what, sizeof(what), "%s: %s x int8 == scalar (max %.2e of |w||x|)",
                      t.label.c_str(), ti.name, max_int);
        check(max_int < 1e-5, what);
        if (f32_slot(t.k, ti.type)) {
            std::snprintf(what, sizeof(what), "%s: %s x f32 == scalar (max %.2e of |w||x|)",
                          t.label.c_str(), ti.name, max_f32);
            check(max_f32 < 1e-5, what);
        }
    }

    // Plain f32 dot, including lengths that are not a multiple of 16 / 4.
    Rng rng(99);
    double max_rel = 0.0;
    for (uint64_t n : {1ull, 3ull, 7ull, 16ull, 37ull, 64ull, 1000ull, 4096ull}) {
        std::vector<float> a(n), b(n);
        double mass = 1e-9;
        for (uint64_t i = 0; i < n; ++i) {
            a[i] = rng.sym(1.0f);
            b[i] = rng.sym(1.0f);
            mass += std::fabs(static_cast<double>(a[i]) * b[i]);
        }
        const float want = dot_f32_scalar_ref(a.data(), b.data(), n);
        const float got = t.dot_f32(a.data(), b.data(), n);
        max_rel = std::max(max_rel, std::fabs(static_cast<double>(got) - want) / mass);
    }
    char what[160];
    std::snprintf(what, sizeof(what), "%s: f32 dot == double reference (max %.2e of |a||b|)",
                  t.label.c_str(), max_rel);
    check(max_rel < 1e-5, what);
}

// ---------------------------------------------------------------- timing

template <typename Fn>
static double time_us(Fn fn) {
    // Repeat until ~40 ms have passed so slow (scalar / emulated) tables stay
    // quick and fast ones still get a stable number.
    int iters = 0;
    const auto t0 = std::chrono::steady_clock::now();
    double el = 0.0;
    do {
        for (int i = 0; i < 16; ++i) fn();
        iters += 16;
        el = std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count();
    } while (el < 40000.0);
    return el / iters;
}

static void bench(const std::vector<Table>& tables, const QuantKernels& ref) {
    const uint64_t n = 4096;
    Rng rng(5);
    const auto x = make_acts(n, 0, rng);
    std::vector<Q8Act> x32(n / 32);
    std::vector<Q8ActK> x256(n / 256);
    ref.quantize(x.data(), n, x32.data());
    ref.quantize_k(x.data(), n, x256.data());
    volatile float sink = 0;

    std::printf("\nTiming, one %llu-wide row (us; speed-up vs scalar)\n",
                static_cast<unsigned long long>(n));
    std::printf("  %-12s", "kernel");
    std::printf(" %14s", "scalar");
    for (const Table& t : tables) std::printf(" %20s", t.label.c_str());
    std::printf("\n");
    for (const TypeInfo& ti : kTypes) {
        const auto row = make_row(ti, n, 0, rng);
        auto run = [&](const QuantKernels& k) {
            return time_us([&] { sink = sink + int_dot(k, ti.type, row.data(), x32.data(), x256.data(), n); });
        };
        const double s = run(ref);
        std::printf("  %-12s %12.2f  ", (std::string(ti.name) + " x i8").c_str(), s);
        for (const Table& t : tables) {
            const double v = run(t.k);
            std::printf(" %12.2f (%4.1fx)", v, s / std::max(v, 1e-9));
        }
        std::printf("\n");
    }
    {
        std::vector<Q8ActK> out(n / 256);
        const double s = time_us([&] { ref.quantize_k(x.data(), n, out.data()); });
        std::printf("  %-12s %12.2f  ", "quantize_k", s);
        for (const Table& t : tables) {
            const double v = time_us([&] { t.k.quantize_k(x.data(), n, out.data()); });
            std::printf(" %12.2f (%4.1fx)", v, s / std::max(v, 1e-9));
        }
        std::printf("\n");
    }
    (void)sink;
}

int main(int argc, char** argv) {
    bool do_bench = true;
    for (int i = 1; i < argc; ++i)
        if (std::strcmp(argv[i], "--no-bench") == 0) do_bench = false;

    init_fp16_table();
    const QuantKernels& ref = scalar_kernels();

    std::vector<Table> tables;
    tables.push_back({std::string("installed:") + quant_kernels().name, quant_kernels(), dot_f32_fast});
#if defined(FISH_HAVE_ARM_KERNELS) && FISH_HAVE_ARM_KERNELS
    {
        Table t{"", ref, nullptr};
        fish_install_arm(t.k, &t.dot_f32, false);
        t.label = t.k.name;
        tables.push_back(t);
    }
    if (fish_arm_dotprod()) {
        Table t{"", ref, nullptr};
        fish_install_arm(t.k, &t.dot_f32, true);
        t.label = t.k.name;
        tables.push_back(t);
    } else {
        std::printf("(CPU or build has no DotProd: neon-dotprod variant not checked)\n");
    }
#endif
#if defined(FISH_NEON_EMULATE)
    std::printf("NEON EMULATION on the host CPU (test/neon_emu): checks kernel logic, "
                "not real NEON code generation; timings are meaningless\n");
#endif
    std::printf("Installed kernel set: %s\n", quant_kernels().name);
    if (std::strcmp(quant_kernels().name, "scalar") == 0)
        std::printf("(no SIMD set installed: the installed table is the reference itself)\n");

    for (const Table& t : tables) {
        std::printf("\n%s\n", t.label.c_str());
        test_quantize(t, ref);
        test_dots(t, ref);
    }

    if (do_bench) bench(tables, ref);

    std::printf("\n%s (%d failure%s)\n", failures ? "KERNELS FAILED" : "KERNELS PASSED",
                failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
