/*
 * FISH — C ABI implementation.
 */
#include <fish/c_api.h>

#include <fish/model.h>
#include <fish/quant.h>
#include <fish/sampler.h>
#include <fish/tokenizer.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#ifndef _WIN32
#include <sys/resource.h>
#endif

using namespace fish;

struct fish_ctx {
    std::mutex mu;
    std::unique_ptr<LlamaModel> model;
    std::unique_ptr<Tokenizer> tok;
    Sampler sampler{0};
    std::string err;
    std::atomic<bool> cancel{false};
    fish_stats stats{};
    int last_pos = 0;
};

namespace {

double now_s() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

double peak_rss_mb() {
#ifdef _WIN32
    // Deliberately no psapi: Smart App Control's reputation engine treats
    // process-memory inspection as suspicious and blocks the whole library.
    // The host process measures its own resident set instead (see
    // model/fish_client.py), so FISH never needs that capability.
    return 0.0;
#else
    struct rusage ru{};
    if (getrusage(RUSAGE_SELF, &ru) == 0)
        return static_cast<double>(ru.ru_maxrss) / 1024.0;
    return 0.0;
#endif
}

// Append bytes to `pending` and emit only complete UTF-8 sequences, so the
// caller never receives half a multi-byte character.
void emit_utf8(fish_token_cb cb, void* user, const std::string& bytes,
               int token_id, std::string& pending, std::string& sink) {
    pending += bytes;
    size_t i = 0;
    size_t complete = 0;
    while (i < pending.size()) {
        const unsigned char c = static_cast<unsigned char>(pending[i]);
        size_t len = 1;
        if ((c & 0xE0) == 0xC0) len = 2;
        else if ((c & 0xF0) == 0xE0) len = 3;
        else if ((c & 0xF8) == 0xF0) len = 4;
        if (i + len > pending.size()) break;
        i += len;
        complete = i;
    }
    if (complete == 0) return;
    std::string ready = pending.substr(0, complete);
    pending.erase(0, complete);
    sink += ready;
    if (cb) cb(ready.c_str(), token_id, user);
}

int write_string(const std::string& s, char* out, int cap) {
    const int need = static_cast<int>(s.size());
    if (out && cap > 0) {
        const int n = std::min(need, cap - 1);
        std::memcpy(out, s.data(), static_cast<size_t>(n));
        out[n] = '\0';
    }
    return need;
}

} // namespace

extern "C" {

const char* fish_version(void) { return "fish 1.0.0"; }

fish_ctx* fish_create(void) {
    auto* c = new (std::nothrow) fish_ctx();
    return c;
}

void fish_destroy(fish_ctx* ctx) { delete ctx; }

int fish_load(fish_ctx* ctx, const char* path, int n_ctx) {
    if (!ctx || !path) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);

    const double t0 = now_s();

    // n_ctx <= 0 means "let the specs engine decide". It measures the device,
    // reads the model's own profile, and picks a context that leaves the
    // machine usable.
    int ctx_tokens = n_ctx;
    if (ctx_tokens <= 0) {
        ModelProfile prof;
        std::string perr;
        if (probe_model(path, &prof, &perr)) {
            SpecsEngine& specs = SpecsEngine::instance();
            specs.refresh(path);
            ctx_tokens = specs.plan(prof, PlanRequest{}).n_ctx;
        } else {
            ctx_tokens = 4096;
        }
    }

    std::string err;
    auto model = LlamaModel::load(path, ctx_tokens, &err);
    if (!model) { ctx->err = err; return -2; }

    auto tok = Tokenizer::create(model->gguf(), &err);
    if (!tok) { ctx->err = err; return -3; }

    ctx->model = std::move(model);
    ctx->tok = std::move(tok);
    ctx->last_pos = 0;
    ctx->stats = fish_stats{};
    ctx->stats.load_seconds = now_s() - t0;
    ctx->stats.weights_bytes = ctx->model->weights_bytes();
    ctx->stats.kv_bytes = ctx->model->kv_bytes();
    ctx->stats.context_size = ctx->model->config().n_ctx;
    ctx->err.clear();
    return 0;
}

int fish_loaded(fish_ctx* ctx) {
    if (!ctx) return 0;
    std::lock_guard<std::mutex> lock(ctx->mu);
    return (ctx->model && ctx->tok) ? 1 : 0;
}

const char* fish_last_error(fish_ctx* ctx) {
    return ctx ? ctx->err.c_str() : "null context";
}

int fish_model_info_of(fish_ctx* ctx, fish_model_info* out) {
    if (!ctx || !out) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->model) return -1;
    const LlamaConfig& c = ctx->model->config();
    std::memset(out, 0, sizeof(*out));
    std::snprintf(out->arch, sizeof(out->arch), "%s", c.arch.c_str());
    out->n_layers = c.n_layers;
    out->n_embd = c.n_embd;
    out->n_ff = c.n_ff;
    out->n_head = c.n_head;
    out->n_head_kv = c.n_head_kv;
    out->head_dim = c.head_dim;
    out->n_ctx = c.n_ctx;
    out->vocab = c.vocab;
    out->tied_output = c.tied_output ? 1 : 0;

    // Summarise the quantisation mix so the UI can show it honestly.
    int n_q4k = 0, n_q6k = 0, n_other = 0;
    for (const auto& t : ctx->model->gguf().tensors()) {
        switch (t.type) {
            case GgmlType::Q4_K: ++n_q4k; break;
            case GgmlType::Q6_K: ++n_q6k; break;
            default: ++n_other; break;
        }
    }
    std::snprintf(out->quant, sizeof(out->quant), "Q4_K:%d Q6_K:%d other:%d",
                  n_q4k, n_q6k, n_other);
    return 0;
}

int fish_tokenize(fish_ctx* ctx, const char* text, int add_bos,
                  int32_t* out, int cap) {
    if (!ctx || !text) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return -1;
    const std::vector<int32_t> ids = ctx->tok->encode(text, add_bos != 0);
    if (!out || cap <= 0) return static_cast<int>(ids.size());
    const int n = std::min(static_cast<int>(ids.size()), cap);
    std::memcpy(out, ids.data(), sizeof(int32_t) * static_cast<size_t>(n));
    return n;
}

int fish_detokenize(fish_ctx* ctx, const int32_t* ids, int n, char* out, int cap) {
    if (!ctx || !ids || n < 0) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return -1;
    const std::vector<int32_t> v(ids, ids + n);
    return write_string(ctx->tok->decode(v), out, cap);
}

int fish_chat_prompt(fish_ctx* ctx, const char* const* roles,
                     const char* const* contents, int n_messages,
                     char* out, int cap) {
    if (!ctx || n_messages < 0) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return -1;
    std::vector<std::pair<std::string, std::string>> msgs;
    msgs.reserve(static_cast<size_t>(n_messages));
    for (int i = 0; i < n_messages; ++i) {
        msgs.emplace_back(roles && roles[i] ? roles[i] : "user",
                          contents && contents[i] ? contents[i] : "");
    }
    return write_string(ctx->tok->apply_chat(msgs, ctx->tok->detect_format()), out, cap);
}

const char* fish_chat_format(fish_ctx* ctx) {
    if (!ctx) return "none";
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return "none";
    switch (ctx->tok->detect_format()) {
        case ChatFormat::Llama3: return "llama3";
        case ChatFormat::ChatML: return "chatml";
        case ChatFormat::Phi3:   return "phi3";
        case ChatFormat::Gemma:  return "gemma";
        default:                 return "plain";
    }
}

void fish_cancel(fish_ctx* ctx) { if (ctx) ctx->cancel = true; }

void fish_reset(fish_ctx* ctx) {
    if (!ctx) return;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (ctx->model) ctx->model->reset_kv();
    ctx->last_pos = 0;
}

void fish_get_stats(fish_ctx* ctx, fish_stats* out) {
    if (!ctx || !out) return;
    std::lock_guard<std::mutex> lock(ctx->mu);
    *out = ctx->stats;
    out->peak_rss_mb = peak_rss_mb();
    if (ctx->model) out->context_used = ctx->model->kv_len();
}

const char* fish_kernel_name(void) { return quant_kernels().name; }

const char* fish_specs_json(fish_ctx* ctx) {
    thread_local std::string buf;
    SpecsEngine& specs = SpecsEngine::instance();
    const HardwareSpecs& hw = specs.hardware();

    std::string plan_part = "null";
    if (ctx) {
        std::lock_guard<std::mutex> lock(ctx->mu);
        if (ctx->model) {
            ModelProfile prof;
            std::string e;
            if (probe_model(ctx->model->gguf().path(), &prof, &e)) {
                plan_part = plan_json(specs.plan(prof, PlanRequest{}));
            }
        }
    }
    buf = "{\"hardware\":" + hardware_json(hw) + ",\"plan\":" + plan_part + "}";
    return buf.c_str();
}

// ---------------------------------------------------------------- generation

static int generate_locked(fish_ctx* ctx, const char* prompt, int add_bos,
                           int max_tokens, float temperature, int top_k,
                           float top_p, float repeat_penalty,
                           fish_token_cb cb, void* user,
                           std::string* collected) {
    if (!ctx->model || !ctx->tok) { ctx->err = "no model loaded"; return -1; }

    std::vector<int32_t> ids = ctx->tok->encode(prompt, add_bos != 0);
    if (ids.empty()) { ctx->err = "empty prompt"; return -1; }

    LlamaModel& m = *ctx->model;
    const LlamaConfig& c = m.config();
    if (static_cast<int>(ids.size()) >= c.n_ctx) {
        // Keep the tail of the conversation, which is what matters.
        const int keep = c.n_ctx - 1;
        ids.erase(ids.begin(), ids.end() - keep);
        ctx->err = "prompt truncated to fit the context";
    }

    m.reset_kv();
    ctx->cancel = false;

    // ---- prefill in batches
    const double t0 = now_s();
    const int batch = std::min(256, c.n_ctx);
    int pos = 0;
    while (pos < static_cast<int>(ids.size())) {
        const int n = std::min(batch, static_cast<int>(ids.size()) - pos);
        if (!m.forward(ids.data() + pos, n, pos)) { ctx->err = "prefill failed"; return -1; }
        pos += n;
        if (ctx->cancel) break;
    }
    const double t1 = now_s();

    // ---- decode
    SampleParams sp;
    sp.temperature = temperature;
    sp.top_k = top_k;
    sp.top_p = top_p;
    sp.repeat_penalty = repeat_penalty;
    sp.max_tokens = max_tokens;

    std::string pending, sink;
    std::vector<int32_t> recent;
    recent.reserve(static_cast<size_t>(max_tokens) + ids.size());
    const size_t recent_cap = 128;
    for (size_t i = ids.size() > recent_cap ? ids.size() - recent_cap : 0; i < ids.size(); ++i)
        recent.push_back(ids[i]);

    int generated = 0;
    int next = ctx->tok->bos();
    std::vector<float> logits(static_cast<size_t>(c.vocab));
    std::memcpy(logits.data(), m.logits(), sizeof(float) * static_cast<size_t>(c.vocab));
    next = ctx->sampler.sample(logits.data(), c.vocab, sp, recent);

    while (generated < max_tokens && pos < c.n_ctx) {
        if (ctx->cancel) break;
        if (ctx->tok->is_eog(next)) break;

        const std::string piece = ctx->tok->token_text(next);
        emit_utf8(cb, user, piece, next, pending, sink);

        recent.push_back(next);
        if (recent.size() > recent_cap) recent.erase(recent.begin());
        ++generated;

        if (!m.forward(&next, 1, pos)) { ctx->err = "decode failed"; break; }
        ++pos;

        std::memcpy(logits.data(), m.logits(), sizeof(float) * static_cast<size_t>(c.vocab));
        next = ctx->sampler.sample(logits.data(), c.vocab, sp, recent);
    }
    // flush any remaining bytes (should be none, but never lose text)
    if (!pending.empty()) {
        sink += pending;
        if (cb) cb(pending.c_str(), -1, user);
        pending.clear();
    }
    const double t2 = now_s();

    ctx->stats.prefill_seconds = t1 - t0;
    ctx->stats.decode_seconds = t2 - t1;
    ctx->stats.prompt_tokens = static_cast<int>(ids.size());
    ctx->stats.generated_tokens = generated;
    ctx->stats.context_used = pos;
    ctx->stats.tokens_per_second =
        ctx->stats.decode_seconds > 0 ? generated / ctx->stats.decode_seconds : 0.0;

    if (collected) *collected = sink;
    return generated;
}

int fish_generate(fish_ctx* ctx, const char* prompt, int add_bos,
                  int max_tokens, float temperature, int top_k,
                  float top_p, float repeat_penalty,
                  fish_token_cb cb, void* user) {
    if (!ctx || !prompt) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    return generate_locked(ctx, prompt, add_bos, max_tokens, temperature, top_k,
                           top_p, repeat_penalty, cb, user, nullptr);
}

int fish_generate_to(fish_ctx* ctx, const char* prompt, int add_bos,
                     int max_tokens, float temperature, int top_k,
                     float top_p, float repeat_penalty,
                     char* out, int cap) {
    if (!ctx || !prompt) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    std::string collected;
    const int n = generate_locked(ctx, prompt, add_bos, max_tokens, temperature,
                                  top_k, top_p, repeat_penalty, nullptr, nullptr,
                                  &collected);
    if (n < 0) return n;
    return write_string(collected, out, cap);
}

} // extern "C"
