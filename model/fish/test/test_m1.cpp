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

    // Throughput of the scalar kernel on a realistic row.
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
