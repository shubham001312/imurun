/*
 * M2 verification — full transformer forward pass.
 *
 * Uses exact token strings from the model's own vocabulary (GPT-2 byte-level
 * form, so a leading space is U+0120) and checks that greedy decoding
 * continues a factual sentence coherently. A wrong RoPE, a wrong GQA mapping,
 * a wrong quant layout or a wrong norm would all destroy this.
 */
#include <fish/model.h>
#include <fish/quant.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

using namespace fish;

static int failures = 0;
static void check(bool cond, const char* what) {
    std::printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) ++failures;
}

static const std::vector<std::string>* vocab_of(const GgufFile& g) {
    const MetaValue* v = g.meta("tokenizer.ggml.tokens");
    return v ? &v->sarr : nullptr;
}

static int find_token(const std::vector<std::string>& vocab, const std::string& s) {
    for (size_t i = 0; i < vocab.size(); ++i) if (vocab[i] == s) return static_cast<int>(i);
    return -1;
}

// Render a byte-level BPE token back to readable text for the log.
static std::string pretty(const std::string& tok) {
    std::string out;
    for (size_t i = 0; i < tok.size(); ++i) {
        if (i + 1 < tok.size() &&
            static_cast<unsigned char>(tok[i]) == 0xC4 &&
            static_cast<unsigned char>(tok[i + 1]) == 0xA0) {
            out += ' ';
            ++i;
        } else {
            out += tok[i];
        }
    }
    return out;
}

int main(int argc, char** argv) {
    std::string path = argc > 1 ? argv[1]
        : "models/Llama-3.2-1B-Instruct-Q4_K_M.gguf";

    std::string err;
    auto t_load0 = std::chrono::steady_clock::now();
    auto model = LlamaModel::load(path, 512, &err);
    auto t_load1 = std::chrono::steady_clock::now();
    if (!model) { std::printf("[FAIL] load: %s\n", err.c_str()); return 1; }

    const LlamaConfig& c = model->config();
    std::printf("Loaded in %.2f s\n", std::chrono::duration<double>(t_load1 - t_load0).count());
    std::printf("  layers=%d embd=%d ff=%d head=%d kv_head=%d dim=%d ctx=%d vocab=%d\n",
                c.n_layers, c.n_embd, c.n_ff, c.n_head, c.n_head_kv, c.head_dim, c.n_ctx, c.vocab);
    std::printf("  weights=%.3f GB  kv_cache=%.1f MB  tied_output=%d\n",
                model->weights_bytes() / 1e9, model->kv_bytes() / 1e6, c.tied_output);
    check(c.n_layers > 0 && c.n_embd > 0, "config parsed");
    check(c.n_head_kv < c.n_head, "GQA detected (kv heads < q heads)");

    const auto* vocab = vocab_of(model->gguf());
    check(vocab != nullptr && vocab->size() == static_cast<size_t>(c.vocab),
          "vocabulary size matches config");
    if (!vocab) return 1;

    // Build "The capital of France is" in the model's own byte-level tokens.
    const char* parts[] = {"<|begin_of_text|>", "The", "\xC4\xA0" "capital",
                           "\xC4\xA0" "of", "\xC4\xA0" "France", "\xC4\xA0" "is"};
    std::vector<int32_t> prompt;
    for (const char* p : parts) {
        const int id = find_token(*vocab, p);
        if (id < 0) {
            std::printf("  token not found: %s\n", p);
            ++failures;
            continue;
        }
        prompt.push_back(id);
    }
    check(static_cast<int>(prompt.size()) == 6, "prompt tokenised");

    // Prefill the whole prompt in one batched pass.
    auto t0 = std::chrono::steady_clock::now();
    if (!model->forward(prompt.data(), static_cast<int>(prompt.size()), 0)) {
        std::printf("[FAIL] prefill\n");
        return 1;
    }
    auto t1 = std::chrono::steady_clock::now();
    const double prefill_s = std::chrono::duration<double>(t1 - t0).count();
    std::printf("\nprefill: %d tokens in %.2f s (%.2f tok/s)\n",
                static_cast<int>(prompt.size()), prefill_s,
                prompt.size() / prefill_s);

    bool finite = true;
    for (int i = 0; i < c.vocab; ++i) if (!std::isfinite(model->logits()[i])) finite = false;
    check(finite, "logits are finite");

    // Show the top five candidates for the next token.
    std::vector<float> lg(model->logits(), model->logits() + c.vocab);
    std::printf("top-5 after prompt:\n");
    for (int k = 0; k < 5; ++k) {
        int best = 0;
        float bv = -INFINITY;
        for (int i = 0; i < c.vocab; ++i) {
            if (lg[i] > bv) { bv = lg[i]; best = i; }
        }
        std::printf("  %6d  %8.3f  '%s'\n", best, bv, pretty((*vocab)[best]).c_str());
        lg[best] = -INFINITY;
    }

    // Greedy decode 12 tokens.
    std::vector<int32_t> gen;
    std::string text;
    auto t2 = std::chrono::steady_clock::now();
    int32_t next = model->argmax();
    for (int step = 0; step < 12; ++step) {
        gen.push_back(next);
        text += pretty((*vocab)[next]);
        if (next == c.eos) break;
        const int pos = static_cast<int>(prompt.size()) + step;
        if (!model->forward(&next, 1, pos)) break;
        next = model->argmax();
    }
    auto t3 = std::chrono::steady_clock::now();
    const double gen_s = std::chrono::duration<double>(t3 - t2).count();
    const double tok_s = gen.size() / gen_s;

    std::printf("\ncontinuation: '%s'\n", text.c_str());
    std::printf("decode: %zu tokens in %.2f s (%.2f tok/s, %.1f ms/token)\n",
                gen.size(), gen_s, tok_s, 1000.0 / tok_s);

    check(!gen.empty(), "produced tokens");
    check(text.find("Paris") != std::string::npos,
          "continues 'The capital of France is' with Paris");
    check(tok_s > 0.2, "decode throughput is usable (scalar kernels)");

    std::printf("\n%s (%d failure%s)\n", failures ? "M2 FAILED" : "M2 PASSED",
                failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
