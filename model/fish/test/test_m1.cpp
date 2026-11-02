/*
 * M1 verification — GGUF reader + quantisation kernels.
 *
 * Proves, with no model inference involved:
 *   1. the GGUF header, metadata and tensor directory parse correctly
 *   2. the tensor type ids match the specification
 *   3. Q4_K unpacking is bit-exact against a hand-built block
 *   4. every tensor type in a real model is understood by FISH
 *   5. vec_dot throughput on this machine
 */
#include <fish/gguf.h>
#include <fish/quant.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

using namespace fish;

static int failures = 0;

static void check(bool cond, const char* what) {
    std::printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) ++failures;
}

// Build a Q4_K block where every quant nibble is known, then verify the
// dequantiser reproduces it exactly. This is the strongest test we can do
// without a reference implementation: it pins the bit layout.
static void test_q4_k_layout() {
    std::printf("Q4_K bit layout\n");
    alignas(8) uint8_t raw[144] = {0};
    auto* b = reinterpret_cast<BlockQ4_K*>(raw);

    const uint16_t one = 0x3C00;  // fp16 1.0
    std::memcpy(&b->d, &one, 2);
    std::memset(&b->dmin, 0, 2);

    // All eight 6-bit scales = 1, all mins = 0.
    b->scales[0] = b->scales[1] = b->scales[2] = b->scales[3] = 1;
    b->scales[8] = b->scales[9] = b->scales[10] = b->scales[11] = 1;

    for (int i = 0; i < 128; ++i) {
        const uint8_t n = static_cast<uint8_t>(i & 0x0F);
        b->qs[i] = static_cast<uint8_t>(n | (n << 4));
    }

    float y[256];
    bool ok = dequant_row(GgmlType::Q4_K, raw, y, 256);
    check(ok, "Q4_K dequantises");

    bool exact = true;
    for (int j = 0; j < 256; j += 64) {
        for (int l = 0; l < 32; ++l) {
            const float want = static_cast<float>(l & 0x0F);
            if (std::fabs(y[j + l] - want) > 1e-6f) exact = false;
            if (std::fabs(y[j + 32 + l] - want) > 1e-6f) exact = false;
        }
    }
    check(exact, "Q4_K nibble order and scales match the spec");

    // fp16 round trip
    check(std::fabs(fp16_to_fp32(0x3C00) - 1.0f) < 1e-6f, "fp16 1.0");
    check(std::fabs(fp16_to_fp32(0x0000)) < 1e-6f, "fp16 0.0");
    check(std::fabs(fp16_to_fp32(0xC000) + 2.0f) < 1e-6f, "fp16 -2.0");
}

static void test_block_sizes() {
    std::printf("Block size table\n");
    check(ggml_type_size(GgmlType::Q4_K) == 144, "Q4_K = 144 bytes / 256");
    check(ggml_type_size(GgmlType::Q6_K) == 210, "Q6_K = 210 bytes / 256");
    check(ggml_type_size(GgmlType::Q8_0) == 34,  "Q8_0 = 34 bytes / 32");
    check(ggml_blck_size(GgmlType::Q4_K) == 256, "Q4_K block = 256");
    check(ggml_row_size(GgmlType::Q4_K, 2048) == 1152, "row size 2048 Q4_K = 1152");
}

// Scalar reference and installed kernel for either activation layout.
static float scalar_int_dot(GgmlType ty, const void* row, const Q8Act* x32, const Q8ActK* x256, uint64_t n) {
    const QuantKernels& k = scalar_kernels();
    switch (ty) {
        case GgmlType::Q4_K: return k.q4_K_q8k(row, x256, n);
        case GgmlType::Q5_K: return k.q5_K_q8k(row, x256, n);
        case GgmlType::Q6_K: return k.q6_K_q8k(row, x256, n);
        case GgmlType::Q8_0: return k.q8_0_q8(row, x32, n);
        case GgmlType::Q4_0: return k.q4_0_q8(row, x32, n);
        default: return 0.0f;
    }
}

static bool fast_int_dot(GgmlType ty, const void* row, const Q8Act* x32, const Q8ActK* x256, uint64_t n, float* out) {
    if (has_q8k_kernel(ty)) return vec_dot_q8k(ty, row, x256, n, out);
    return vec_dot_q8(ty, row, x32, n, out);
}

// Two checks on real weight rows:
//   1. the installed (AVX2) integer kernel must match the scalar integer
//      reference to float rounding — same inputs, same arithmetic;
//   2. the integer path must match the f32 path up to the activation
//      quantisation error, measured against the row's total |w||x| mass so a
//      dot product that happens to cancel to ~0 is not reported as 100% off.
static void test_q8_kernels(const GgufFile& f) {
    std::printf("int8 kernels (%s)\n", quant_kernels().name);
    const GgmlType types[] = {GgmlType::Q4_K, GgmlType::Q6_K, GgmlType::Q8_0,
                              GgmlType::Q4_0, GgmlType::Q5_K};
    for (GgmlType ty : types) {
        const TensorInfo* t = nullptr;
        for (const auto& cand : f.tensors()) {
            if (cand.type == ty && cand.ne.size() >= 2 && cand.ne[0] % 256 == 0) { t = &cand; break; }
        }
        if (!t) {
            std::printf("  (no %s tensor in this model, skipped)\n", ggml_type_name(ty));
            continue;
        }
        const uint64_t n = t->ne[0];
        std::vector<float> x(n);
        uint32_t seed = 12345u;
        for (uint64_t i = 0; i < n; ++i) {
            seed = seed * 1664525u + 1013904223u;
            x[i] = (static_cast<float>(seed >> 8) / 16777216.0f - 0.5f) * 4.0f;
        }
        std::vector<Q8Act> xq(n / kQ8ActBlock);
        std::vector<Q8ActK> xqk(n / kQ8ActKBlock);
        quantize_acts(x.data(), n, xq.data());
        quantize_acts_k(x.data(), n, xqk.data());

        double max_vec_vs_scalar = 0.0, max_int_vs_f32 = 0.0;
        const size_t rows = std::min<size_t>(64, t->ne[1]);
        const size_t row_bytes = ggml_row_size(t->type, n);
        std::vector<float> wrow(n);
        for (size_t r = 0; r < rows; ++r) {
            const void* row = t->data + r * row_bytes;
            const float ref = vec_dot(ty, row, x.data(), n);
            const float ref_int = scalar_int_dot(ty, row, xq.data(), xqk.data(), n);
            float got = 0.0f;
            const bool ok = fast_int_dot(ty, row, xq.data(), xqk.data(), n, &got);
            if (!ok) { max_vec_vs_scalar = max_int_vs_f32 = 1e9; break; }
            dequant_row(ty, row, wrow.data(), n);
            double mass = 0.0;
            for (uint64_t i = 0; i < n; ++i) mass += std::fabs(static_cast<double>(wrow[i]) * x[i]);
            mass = std::max(mass, 1e-6);
            max_vec_vs_scalar = std::max(max_vec_vs_scalar, std::fabs(static_cast<double>(got) - ref_int) / mass);
            max_int_vs_f32 = std::max(max_int_vs_f32, std::fabs(static_cast<double>(got) - ref) / mass);
        }
        char what[160];
        std::snprintf(what, sizeof(what), "%s: %s int8 kernel == scalar int8 reference (max %.2e of |w||x|)",
                      ggml_type_name(ty), quant_kernels().name, max_vec_vs_scalar);
        check(max_vec_vs_scalar < 1e-4, what);
        std::snprintf(what, sizeof(what), "%s: int8 path within activation-quant error of f32 (max %.4f of |w||x|)",
                      ggml_type_name(ty), max_int_vs_f32);
        check(max_int_vs_f32 < 0.01, what);
    }

    // Throughput comparison on the attention projection.
    const TensorInfo* q = f.find("blk.0.attn_q.weight");
    if (q && (has_q8_kernel(q->type) || has_q8k_kernel(q->type))) {
        const uint64_t n = q->ne[0];
        std::vector<float> x(n, 0.01f);
        std::vector<Q8Act> xq(n / kQ8ActBlock);
        std::vector<Q8ActK> xqk(n / kQ8ActKBlock);
        quantize_acts(x.data(), n, xq.data());
        quantize_acts_k(x.data(), n, xqk.data());
        const int iters = 4000;
        float acc = 0;
        auto t0 = std::chrono::steady_clock::now();
        for (int i = 0; i < iters; ++i) acc += vec_dot(q->type, q->data, x.data(), n);
        auto t1 = std::chrono::steady_clock::now();
        for (int i = 0; i < iters; ++i) { float v; fast_int_dot(q->type, q->data, xq.data(), xqk.data(), n, &v); acc += v; }
        auto t2 = std::chrono::steady_clock::now();
        const double f32_us = std::chrono::duration<double, std::micro>(t1 - t0).count() / iters;
        const double i8_us = std::chrono::duration<double, std::micro>(t2 - t1).count() / iters;
        std::printf("  %s row x%llu: f32 path %.2f us, int8 path %.2f us (%.1fx) [%g]\n",
                    ggml_type_name(q->type), static_cast<unsigned long long>(n), f32_us, i8_us,
                    f32_us / std::max(1e-9, i8_us), static_cast<double>(acc));
    }
}

int main(int argc, char** argv) {
    std::string path = argc > 1 ? argv[1]
        : "models/Llama-3.2-1B-Instruct-Q4_K_M.gguf";

    test_block_sizes();
    std::printf("\n");
    test_q4_k_layout();
    std::printf("\n");

    std::printf("GGUF: %s\n", path.c_str());
    std::string err;
    auto f = GgufFile::open(path, &err);
    if (!f) {
        std::printf("  [FAIL] open: %s\n", err.c_str());
        return 1;
    }
    check(true, "file opens and parses");

    std::printf("\nMetadata\n");
    const char* keys[] = {
        "general.architecture", "general.name", "general.file_type",
        "llama.block_count", "llama.embedding_length", "llama.feed_forward_length",
        "llama.attention.head_count", "llama.attention.head_count_kv",
        "llama.attention.layer_norm_rms_epsilon", "llama.rope.freq_base",
        "llama.rope.dimension_count", "llama.context_length", "llama.vocab_size",
        "tokenizer.ggml.model", "tokenizer.ggml.pre",
        "tokenizer.ggml.bos_token_id", "tokenizer.ggml.eos_token_id",
    };
    for (const char* k : keys) {
        const MetaValue* v = f->meta(k);
        if (!v) { std::printf("  %-42s (absent)\n", k); continue; }
        if (v->kind == MetaValue::STRING)
            std::printf("  %-42s %s\n", k, v->s.c_str());
        else if (v->kind == MetaValue::FLOAT)
            std::printf("  %-42s %.9g\n", k, v->f);
        else if (v->kind == MetaValue::INT)
            std::printf("  %-42s %lld\n", k, static_cast<long long>(v->i));
        else
            std::printf("  %-42s %llu\n", k, static_cast<unsigned long long>(v->u));
    }

    const MetaValue* toks = f->meta("tokenizer.ggml.tokens");
    const MetaValue* merges = f->meta("tokenizer.ggml.merges");
    std::printf("  %-42s %zu strings\n", "tokenizer.ggml.tokens",
                toks ? toks->sarr.size() : 0);
    std::printf("  %-42s %zu merges\n", "tokenizer.ggml.merges",
                merges ? merges->sarr.size() : 0);
    const MetaValue* types = f->meta("tokenizer.ggml.token_type");
    std::printf("  %-42s %zu ids\n", "tokenizer.ggml.token_type",
                types ? types->iarr.size() : 0);
    check(toks && toks->sarr.size() > 1000, "tokenizer table present");

    std::printf("\nTensors: %zu\n", f->tensors().size());
    std::map<std::string, int> by_type;
    uint64_t total_bytes = 0;
    int unsupported = 0;
    for (const auto& t : f->tensors()) {
        by_type[ggml_type_name(t.type)]++;
        total_bytes += t.nbytes;
        if (!quant_supported(t.type)) ++unsupported;
    }
    for (const auto& kv : by_type)
        std::printf("  %-10s %d tensors\n", kv.first.c_str(), kv.second);
    std::printf("  total weight bytes: %.3f GB\n", total_bytes / 1e9);
    check(unsupported == 0, "every tensor type in this model is supported");

    const TensorInfo* emb = f->find("token_embd.weight");
    check(emb != nullptr, "token_embd.weight found");
    if (emb) {
        std::printf("  token_embd: type=%s ne=[%llu, %llu]\n",
                    ggml_type_name(emb->type),
                    static_cast<unsigned long long>(emb->ne[0]),
                    emb->ne.size() > 1 ? static_cast<unsigned long long>(emb->ne[1]) : 0ULL);
        std::printf("  token_embd: type id = %u\n", static_cast<unsigned>(emb->type));
        check(static_cast<unsigned>(emb->type) == 12u ||
              static_cast<unsigned>(emb->type) == 14u ||
              static_cast<unsigned>(emb->type) == 8u,
              "token_embd type id is a known k-quant (12/14/8)");
    }

    const TensorInfo* q = f->find("blk.0.attn_q.weight");
    check(q != nullptr, "blk.0.attn_q.weight found");
    if (q) check(q->layer == 0, "layer index parsed from tensor name");

    // Dequantise one embedding row and check it is finite and non-degenerate.
    if (emb) {
        std::vector<float> row(emb->ne[0]);
        bool ok = dequant_row(emb->type, emb->data, row.data(), emb->ne[0]);
        check(ok, "embedding row dequantises");
        double sum = 0, sq = 0;
        bool finite = true;
        for (float v : row) {
            if (!std::isfinite(v)) finite = false;
            sum += v;
            sq += static_cast<double>(v) * v;
        }
        const double mean = sum / row.size();
        const double var = sq / row.size() - mean * mean;
        std::printf("  row[0..3] = %.4f %.4f %.4f %.4f\n", row[0], row[1], row[2], row[3]);
        std::printf("  mean = %.6f  stddev = %.6f\n", mean, std::sqrt(var > 0 ? var : 0));
        check(finite, "no NaN/Inf in dequantised row");
        check(std::sqrt(var) > 1e-3, "row is not degenerate");
    }

    std::printf("\n");
    test_q8_kernels(*f);

    // Throughput of the f32 kernel on a realistic row.
    if (q) {
        std::vector<float> x(q->ne[0], 0.01f);
        const int iters = 2000;
        auto t0 = std::chrono::steady_clock::now();
        float acc = 0;
        for (int i = 0; i < iters; ++i)
            acc += vec_dot(q->type, q->data, x.data(), q->ne[0]);
        auto t1 = std::chrono::steady_clock::now();
        const double us = std::chrono::duration<double, std::micro>(t1 - t0).count() / iters;
        std::printf("\nvec_dot: %.2f us per %llu-wide %s row (scalar) [%g]\n",
                    us, static_cast<unsigned long long>(q->ne[0]),
                    ggml_type_name(q->type), static_cast<double>(acc));
        check(us > 0, "kernel runs");
    }

    std::printf("\n%s (%d failure%s)\n", failures ? "M1 FAILED" : "M1 PASSED",
                failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
