/*
 * FISH — C ABI implementation.
 */
#include <fish/c_api.h>

#include <fish/backend.h>
#include <fish/model.h>
#include <fish/quant.h>
#include <fish/sampler.h>
#include <fish/threadpool.h>
#include <fish/tokenizer.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <random>
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
    // Token ids whose KV is currently live in the model, used for prefix reuse.
    std::vector<int32_t> cache_ids;
    // Last truncation of an over-long prompt: kept = ids[0, trunc_head) +
    // ids[trunc_drop_end, end). Re-applied while it still fits so the
    // prefix cache keeps hitting on an append-only conversation.
    int trunc_head = 0, trunc_drop_end = 0;
    std::vector<int32_t> trunc_head_ids;
    // Speculative decoding: a small model with the same vocabulary.
    std::unique_ptr<LlamaModel> draft;
    std::vector<int32_t> draft_cache_ids;
    int draft_k = 5;
    // LoRA adapter applied at every load (empty = none), and what the last
    // load was asked for, so a change of adapter can reload the same model.
    std::string adapter_path;
    float adapter_scale = 1.0f;
    std::string model_path;
    int n_ctx_req = 0;
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
    // model/fish_native.py), so FISH never needs that capability.
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

std::vector<std::pair<std::string, std::string>> to_messages(
        const char* const* roles, const char* const* contents, int n) {
    std::vector<std::pair<std::string, std::string>> msgs;
    msgs.reserve(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) {
        msgs.emplace_back(roles && roles[i] ? roles[i] : "user",
                          contents && contents[i] ? contents[i] : "");
    }
    return msgs;
}

} // namespace

extern "C" {

const char* fish_version(void) { return "fish 2.0.0"; }
int fish_abi_version(void) { return FISH_ABI_VERSION; }
const char* fish_supported_archs(void) { return "llama,qwen2,qwen2moe,qwen3,qwen3moe,phi3,gemma2"; }

fish_ctx* fish_create(void) {
    auto* c = new (std::nothrow) fish_ctx();
    return c;
}

void fish_destroy(fish_ctx* ctx) {
    if (!ctx) return;
    ctx->cancel = true;
    // Wait for any generation in flight before freeing the weights.
    std::lock_guard<std::mutex> lock(ctx->mu);
    ctx->model.reset();
    ctx->tok.reset();
    delete ctx;
}

// Caller holds ctx->mu. Returns 0, -2 model load failed, -3 tokenizer,
// -4 the adapter was rejected (the adapter setting is then cleared).
static int load_locked(fish_ctx* ctx, const char* path, int n_ctx) {
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
    LlamaModel::LoadOptions opts;
    opts.adapter_path = ctx->adapter_path;
    opts.adapter_scale = ctx->adapter_scale;
    auto model = LlamaModel::load(path, ctx_tokens, opts, &err);
    if (!model) {
        ctx->err = err;
        if (!ctx->adapter_path.empty() && err.rfind("adapter: ", 0) == 0) {
            ctx->adapter_path.clear();
            ctx->adapter_scale = 1.0f;
            return -4;
        }
        return -2;
    }

    auto tok = Tokenizer::create(model->gguf(), &err);
    if (!tok) { ctx->err = err; return -3; }

    ctx->model = std::move(model);
    ctx->tok = std::move(tok);
    ctx->model_path = path;
    ctx->n_ctx_req = n_ctx;
    ctx->cache_ids.clear();
    ctx->draft.reset();          // a draft belongs to one target vocabulary
    ctx->draft_cache_ids.clear();
    ctx->stats = fish_stats{};
    ctx->stats.load_seconds = now_s() - t0;
    ctx->stats.weights_bytes = ctx->model->weights_bytes();
    ctx->stats.kv_bytes = ctx->model->kv_bytes();
    ctx->stats.context_size = ctx->model->config().n_ctx;
    ctx->stats.n_threads = ThreadPool::instance().active_threads();
    ctx->stats.streaming = ctx->model->streaming() ? 1 : 0;
    ctx->stats.gpu_layers = ctx->model->gpu_layers();
    ctx->stats.ram_layers = ctx->model->ram_layers();
    ctx->stats.disk_layers = ctx->model->disk_layers();
    ctx->stats.gpu_bytes = ctx->model->gpu_bytes();
    if (ComputeBackend* gpu = gpu_backend()) {
        std::snprintf(ctx->stats.gpu_name, sizeof(ctx->stats.gpu_name), "%s (%s)",
                      gpu->device_name(), gpu->name());
        const std::string mix = gpu->describe();
        std::snprintf(ctx->stats.gpu_mix, sizeof(ctx->stats.gpu_mix), "%s%s",
                      ctx->model->gpu_span() ? "span: " : "", mix.c_str());
        ctx->stats.vram_total = gpu->memory_total();
        ctx->stats.vram_headroom = vram_headroom_bytes(ctx->stats.vram_total);
    }
    std::snprintf(ctx->stats.placement, sizeof(ctx->stats.placement), "%s", ctx->model->placement_note().c_str());
    std::snprintf(ctx->stats.load_profile, sizeof(ctx->stats.load_profile), "%s", ctx->model->load_profile().c_str());
    ctx->err.clear();
    return 0;
}

int fish_load(fish_ctx* ctx, const char* path, int n_ctx) {
    if (!ctx || !path) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    return load_locked(ctx, path, n_ctx);
}

// Reload the current model with the current adapter setting. The old model
// goes first: the same weights twice would not fit VRAM (or RAM, for a big
// model). On an adapter rejection the plain model is loaded back.
static int reload_locked(fish_ctx* ctx) {
    const std::string path = ctx->model_path;
    const int n_ctx = ctx->n_ctx_req;
    ctx->draft.reset();
    ctx->draft_cache_ids.clear();
    ctx->cache_ids.clear();
    ctx->model.reset();
    ctx->tok.reset();
    const int rc = load_locked(ctx, path.c_str(), n_ctx);
    if (rc == 0) return 0;
    if (rc == -4) {
        const std::string why = ctx->err;
        if (load_locked(ctx, path.c_str(), n_ctx) == 0) { ctx->err = why; return -2; }
        ctx->err = why + "; reloading the plain model also failed: " + ctx->err;
    }
    return -3;
}

int fish_set_adapter(fish_ctx* ctx, const char* path, float scale) {
    if (!ctx || !path || !*path || !(scale == scale)) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    ctx->adapter_path = path;
    ctx->adapter_scale = scale;
    if (!ctx->model) return 0;
    return reload_locked(ctx);
}

int fish_clear_adapter(fish_ctx* ctx) {
    if (!ctx) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    const bool applied = ctx->model && ctx->model->adapter().active;
    ctx->adapter_path.clear();
    ctx->adapter_scale = 1.0f;
    if (!applied) return 0;
    return reload_locked(ctx) == 0 ? 0 : -3;
}

int fish_adapter_info_of(fish_ctx* ctx, fish_adapter_info* out) {
    if (!ctx || !out) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    std::memset(out, 0, sizeof(*out));
    if (ctx->model && ctx->model->adapter().active) {
        const LoraReport& r = ctx->model->adapter();
        out->active = 1;
        std::snprintf(out->name, sizeof(out->name), "%s", r.name.c_str());
        out->rank = r.rank_max;
        out->rank_min = r.rank_min;
        out->alpha = r.alpha;
        out->scale = r.scale;
        out->effective_scale = r.effective_scale;
        out->tensors = r.tensors;
        out->merged_bytes = r.merged_bytes;
        out->merge_seconds = r.seconds;
        out->retention = r.retention();
        out->residual = r.residual();
        out->blocks = r.stats.blocks;
        out->blocks_refit = r.stats.blocks_refit;
    } else if (!ctx->adapter_path.empty()) {
        out->pending = 1;
        const std::string& p = ctx->adapter_path;
        const size_t slash = p.find_last_of("/\\");
        std::snprintf(out->name, sizeof(out->name), "%s",
                      (slash == std::string::npos ? p : p.substr(slash + 1)).c_str());
        out->scale = ctx->adapter_scale;
    }
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
    if (!ctx->model || !ctx->tok) return -1;
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
    out->n_experts = c.n_experts;
    out->n_experts_per_tok = c.n_experts_per_tok;
    out->n_ctx_train = c.n_ctx_train;
    std::snprintf(out->chat_format, sizeof(out->chat_format), "%s",
                  chat_format_name(ctx->tok->detect_format()));
    std::snprintf(out->tokenizer, sizeof(out->tokenizer), "%s",
                  ctx->tok->kind() == VocabKind::SPM ? "spm" : "bpe");

    // Summarise the quantisation mix so the UI can show it honestly.
    std::vector<std::pair<const char*, int>> counts;
    for (const auto& t : ctx->model->gguf().tensors()) {
        const char* name = ggml_type_name(t.type);
        bool found = false;
        for (auto& kv : counts) if (std::strcmp(kv.first, name) == 0) { ++kv.second; found = true; break; }
        if (!found) counts.emplace_back(name, 1);
    }
    std::sort(counts.begin(), counts.end(), [](const auto& a, const auto& b) { return a.second > b.second; });
    std::string mix;
    for (size_t i = 0; i < counts.size() && i < 4; ++i) {
        if (!mix.empty()) mix += " ";
        mix += std::string(counts[i].first) + ":" + std::to_string(counts[i].second);
    }
    std::snprintf(out->quant, sizeof(out->quant), "%s", mix.c_str());
    return 0;
}

int fish_tokenize(fish_ctx* ctx, const char* text, int add_bos,
                  int32_t* out, int cap) {
    if (!ctx || !text) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return -1;
    const std::vector<int32_t> ids = ctx->tok->encode(text, add_bos != 0, true);
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
    const auto msgs = to_messages(roles, contents, n_messages);
    return write_string(ctx->tok->apply_chat(msgs, ctx->tok->detect_format()), out, cap);
}

int fish_count_chat_tokens(fish_ctx* ctx, const char* const* roles,
                           const char* const* contents, int n_messages) {
    if (!ctx || n_messages < 0) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return -1;
    return static_cast<int>(ctx->tok->encode_chat(to_messages(roles, contents, n_messages)).size());
}

const char* fish_chat_format(fish_ctx* ctx) {
    if (!ctx) return "none";
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) return "none";
    return chat_format_name(ctx->tok->detect_format());
}

void fish_cancel(fish_ctx* ctx) { if (ctx) ctx->cancel = true; }

void fish_reset(fish_ctx* ctx) {
    if (!ctx) return;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (ctx->model) ctx->model->reset_kv();
    if (ctx->draft) ctx->draft->reset_kv();
    ctx->cache_ids.clear();
    ctx->draft_cache_ids.clear();
}

int fish_set_draft(fish_ctx* ctx, const char* path, int k) {
    if (!ctx || !path) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->model || !ctx->tok) { ctx->err = "load the target model first"; return -1; }
    std::string err;
    // The draft stays on the CPU: VRAM belongs to the target, whose layers
    // are what the verify step (and plain decode) is bound by.
    LlamaModel::LoadOptions opts;
    opts.gpu_layers = 0;
    opts.allow_span = false;
    auto d = LlamaModel::load(path, ctx->model->config().n_ctx, opts, &err);
    if (!d) { ctx->err = "draft: " + err; return -2; }
    // Same vocabulary is the whole premise: ids must mean the same tokens.
    const LlamaConfig& t = ctx->model->config();
    const LlamaConfig& c = d->config();
    if (c.vocab != t.vocab || c.bos != t.bos) {
        ctx->err = "draft model has a different vocabulary (" + std::to_string(c.vocab) +
                   " vs " + std::to_string(t.vocab) + ")";
        return -3;
    }
    const MetaValue* tt = ctx->model->gguf().meta("tokenizer.ggml.tokens");
    const MetaValue* dt = d->gguf().meta("tokenizer.ggml.tokens");
    if (tt && dt && (tt->sarr.size() != dt->sarr.size() ||
                     (tt->sarr.size() > 1000 && tt->sarr[1000] != dt->sarr[1000]))) {
        ctx->err = "draft model tokenizer differs from the target";
        return -3;
    }
    ctx->draft = std::move(d);
    ctx->draft_cache_ids.clear();
    ctx->draft_k = k > 0 ? std::min(k, 16) : 5;
    ctx->stats.draft_active = 1;
    ctx->err.clear();
    return 0;
}

void fish_clear_draft(fish_ctx* ctx) {
    if (!ctx) return;
    std::lock_guard<std::mutex> lock(ctx->mu);
    ctx->draft.reset();
    ctx->draft_cache_ids.clear();
    ctx->stats.draft_active = 0;
}

void fish_get_stats(fish_ctx* ctx, fish_stats* out) {
    if (!ctx || !out) return;
    std::lock_guard<std::mutex> lock(ctx->mu);
    *out = ctx->stats;
    out->peak_rss_mb = peak_rss_mb();
    if (ctx->model) out->context_used = ctx->model->kv_len();
    out->n_threads = ThreadPool::instance().active_threads();
    if (ComputeBackend* gpu = gpu_backend()) if (ctx->model && out->vram_total) out->vram_free = gpu->memory_free();
}

int fish_set_threads(fish_ctx* ctx, int n) {
    if (!ctx) return -1;
    // Under the context lock: never while a generation of this context runs.
    std::lock_guard<std::mutex> lock(ctx->mu);
    ThreadPool::instance().set_active_threads(n);
    ctx->stats.n_threads = ThreadPool::instance().active_threads();
    return 0;
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
    buf = "{\"hardware\":" + hardware_json(hw) + ",\"plan\":" + plan_part +
          ",\"threads\":" + std::to_string(ThreadPool::instance().active_threads()) +
          ",\"pool_threads\":" + std::to_string(ThreadPool::instance().size()) +
          ",\"kernel\":\"" + std::string(fish_kernel_name()) + "\"}";
    return buf.c_str();
}

// ---------------------------------------------------------------- generation

static int generate_ids_locked(fish_ctx* ctx, std::vector<int32_t> ids,
                               int max_tokens, float temperature, int top_k,
                               float top_p, float repeat_penalty,
                               fish_token_cb cb, void* user,
                               std::string* collected) {
    if (!ctx->model || !ctx->tok) { ctx->err = "no model loaded"; return -1; }
    if (ids.empty()) { ctx->err = "empty prompt"; return -1; }

    LlamaModel& m = *ctx->model;
    const LlamaConfig& c = m.config();
    if (max_tokens <= 0) max_tokens = c.n_ctx;

    // ---- fit the prompt. Keep the head (system prompt, BOS) and the tail
    // (the latest turns); drop the middle. Leave room for the answer.
    ctx->stats.truncated = 0;
    {
        const int reserve = std::max(64, std::min(max_tokens, c.n_ctx / 4));
        const int budget = std::max(32, c.n_ctx - reserve);
        const int n = static_cast<int>(ids.size());
        if (n > budget) {
            // Same cut as last time if it still fits and the head is the same:
            // the kept tokens are then a prefix-extension of the cached ones.
            // Measured before (8B, CPU): every turn after the first cut moved
            // the cut point, the cache matched 497 of 1984 tokens and the turn
            // took 222 s instead of ~2 s.
            int head = ctx->trunc_head, drop_end = ctx->trunc_drop_end;
            bool reuse = head > 0 && drop_end > head && drop_end < n && n - (drop_end - head) <= budget &&
                         static_cast<int>(ctx->trunc_head_ids.size()) == head &&
                         std::equal(ctx->trunc_head_ids.begin(), ctx->trunc_head_ids.end(), ids.begin());
            if (!reuse) {
                // A new cut drops a quarter of the budget more than needed, so
                // the next turns fit under the same cut.
                head = budget / 4;
                const int tail = std::max(1, budget - head - budget / 4);
                drop_end = n - tail;
                ctx->trunc_head = head;
                ctx->trunc_drop_end = drop_end;
                ctx->trunc_head_ids.assign(ids.begin(), ids.begin() + head);
            }
            std::vector<int32_t> kept(ids.begin(), ids.begin() + head);
            kept.insert(kept.end(), ids.begin() + drop_end, ids.end());
            ids.swap(kept);
            ctx->stats.truncated = 1;
        } else {
            ctx->trunc_head = ctx->trunc_drop_end = 0;
            ctx->trunc_head_ids.clear();
        }
    }

    ctx->cancel = false;

    // ---- prefix cache: reuse the KV already computed for the longest common
    // token prefix. A repeated system prompt plus history then costs almost
    // nothing; only the new user message is prefilled.
    int prefix = 0;
    {
        const int lim = static_cast<int>(std::min(ids.size(), ctx->cache_ids.size()));
        while (prefix < lim && ids[static_cast<size_t>(prefix)] == ctx->cache_ids[static_cast<size_t>(prefix)])
            ++prefix;
        // An identical prompt still needs a fresh forward for the logits.
        if (prefix >= static_cast<int>(ids.size()))
            prefix = static_cast<int>(ids.size()) - 1;
        // Too little overlap is not worth the bookkeeping; start clean.
        if (prefix < 8) prefix = 0;
    }
    ctx->stats.cached_tokens = prefix;

    // ---- prefill in batches
    const double t0 = now_s();
    const int batch = std::min(m.max_batch(), c.n_ctx);
    int pos = 0;
    if (prefix > 0) {
        pos = prefix;
        ctx->cache_ids.resize(static_cast<size_t>(prefix));
        m.set_kv_len(prefix);
    } else {
        m.reset_kv();
        ctx->cache_ids.clear();
    }
    while (pos < static_cast<int>(ids.size())) {
        const int n = std::min(batch, static_cast<int>(ids.size()) - pos);
        if (!m.forward(ids.data() + pos, n, pos)) {
            ctx->err = "prefill failed";
            ctx->cache_ids.clear();
            return -1;
        }
        pos += n;
        if (ctx->cancel) break;
    }
    if (ctx->cancel)
        ctx->cache_ids.assign(ids.begin(), ids.begin() + pos);
    else
        ctx->cache_ids = ids;
    const double t1 = now_s();
    const int prefilled = pos - prefix;

    // ---- decode
    SampleParams sp;
    sp.temperature = temperature;
    sp.top_k = top_k;
    sp.top_p = top_p;
    sp.repeat_penalty = repeat_penalty;
    sp.max_tokens = max_tokens;

    std::string pending, sink;
    std::vector<int32_t> recent;
    const size_t recent_cap = 128;
    recent.reserve(recent_cap + 1);
    for (size_t i = ids.size() > recent_cap ? ids.size() - recent_cap : 0; i < ids.size(); ++i)
        recent.push_back(ids[i]);
    auto remember = [&](int32_t t) {
        recent.push_back(t);
        if (recent.size() > recent_cap) recent.erase(recent.begin());
    };

    int generated = 0;
    std::vector<float> logits(static_cast<size_t>(c.vocab));
    int next = -1;
    if (!ctx->cancel) {
        std::memcpy(logits.data(), m.logits(), sizeof(float) * static_cast<size_t>(c.vocab));
        next = ctx->sampler.sample(logits.data(), c.vocab, sp, recent);
    }

    // Speculative decoding (draft model present). Each round:
    //   1. the draft proposes k tokens, one cheap forward each;
    //   2. the target verifies all of them plus the pending token in ONE
    //      forward (memory-bound, so it costs about the same as one token);
    //   3. tokens are accepted left to right with the standard rejection rule
    //      (accept d with probability min(1, p(d)/q(d)); on rejection sample
    //      the replacement from max(0, p - q)), which reproduces the target's
    //      own distribution exactly. The KV caches are cut back to what was
    //      committed.
    LlamaModel* D = ctx->draft.get();
    const int k_max = ctx->draft_k;
    // Adaptive draft length: a pairing that is rarely right (different model
    // families) must not pay for five rejected tokens per round. Grow on a
    // fully accepted round, shrink to what was accepted plus one otherwise.
    int k = std::max(1, std::min(k_max, 3));
    ctx->stats.draft_proposed = 0;
    ctx->stats.draft_accepted = 0;
    std::vector<std::vector<float>> q(static_cast<size_t>(k_max));
    std::vector<float> p, residual;
    std::vector<int32_t> drafts;
    std::vector<int32_t> verify;
    bool stopped = false;

    while (!stopped && next >= 0 && generated < max_tokens && pos < c.n_ctx) {
        if (ctx->cancel) break;
        if (ctx->tok->is_eog(next)) break;

        emit_utf8(cb, user, ctx->tok->token_text(next), next, pending, sink);
        remember(next);
        ++generated;

        const bool speculate = D != nullptr && generated < max_tokens && pos + k_max + 2 <= c.n_ctx;
        if (!speculate) {
            if (!m.forward(&next, 1, pos)) {
                ctx->err = "decode failed";
                ctx->cache_ids.clear();
                break;
            }
            ctx->cache_ids.push_back(next);
            ++pos;
            std::memcpy(logits.data(), m.logits(), sizeof(float) * static_cast<size_t>(c.vocab));
            next = ctx->sampler.sample(logits.data(), c.vocab, sp, recent);
            continue;
        }

        // 0. bring the draft's KV cache in line with the committed tokens
        //    (positions 0..pos-1 hold ctx->cache_ids).
        if (D->kv_len() > pos) D->set_kv_len(pos);
        if (D->kv_len() < pos) {
            int dp = D->kv_len();
            // The draft's own prefix cache: a shared prefix is already there.
            while (dp < pos) {
                const int n = std::min(batch, pos - dp);
                if (!D->forward(ctx->cache_ids.data() + dp, n, dp)) break;
                dp += n;
            }
            if (dp < pos) { D = nullptr; ctx->stats.draft_active = 0; continue; }  // draft broken: plain decode from now on
        }

        // 1. draft proposes
        drafts.clear();
        std::vector<int32_t> recent_d = recent;
        int32_t cur = next;
        int dpos = pos;
        for (int j = 0; j < k; ++j) {
            if (!D->forward(&cur, 1, dpos)) break;
            std::memcpy(logits.data(), D->logits(), sizeof(float) * static_cast<size_t>(c.vocab));
            ctx->sampler.distribution(logits.data(), c.vocab, sp, recent_d, q[static_cast<size_t>(j)]);
            const int32_t d = ctx->sampler.draw(q[static_cast<size_t>(j)].data(), c.vocab);
            if (d < 0) break;
            drafts.push_back(d);
            recent_d.push_back(d);
            if (recent_d.size() > recent_cap) recent_d.erase(recent_d.begin());
            cur = d;
            ++dpos;
            if (ctx->tok->is_eog(d)) break;   // nothing useful can follow
        }
        if (drafts.empty()) { D = nullptr; continue; }
        ctx->stats.draft_proposed += static_cast<int>(drafts.size());

        // 2. target verifies [next, d_0 .. d_{n-1}] in one forward
        verify.clear();
        verify.push_back(next);
        verify.insert(verify.end(), drafts.begin(), drafts.end());
        if (!m.forward(verify.data(), static_cast<int>(verify.size()), pos, /*all_logits=*/true)) {
            ctx->err = "decode failed";
            ctx->cache_ids.clear();
            break;
        }
        ctx->cache_ids.push_back(next);
        ++pos;

        // 3. accept / reject left to right
        int accepted = 0;
        int32_t replacement = -1;
        for (size_t j = 0; j < drafts.size(); ++j) {
            std::memcpy(logits.data(), m.logits_at(static_cast<int>(j)), sizeof(float) * static_cast<size_t>(c.vocab));
            ctx->sampler.distribution(logits.data(), c.vocab, sp, recent, p);
            const int32_t d = drafts[j];
            const float pd = p[static_cast<size_t>(d)];
            const float qd = q[j][static_cast<size_t>(d)];
            bool accept = pd >= qd;
            if (!accept && qd > 0.0f && pd > 0.0f) {
                std::uniform_real_distribution<float> u(0.0f, 1.0f);
                static thread_local std::mt19937 rng{std::random_device{}()};
                accept = u(rng) < pd / qd;
            }
            if (!accept) {
                residual.resize(p.size());
                float total = 0.0f;
                for (size_t i = 0; i < p.size(); ++i) {
                    const float r = p[i] - q[j][i];
                    residual[i] = r > 0.0f ? r : 0.0f;
                    total += residual[i];
                }
                replacement = total > 0.0f ? ctx->sampler.draw(residual.data(), c.vocab)
                                           : ctx->sampler.draw(p.data(), c.vocab);
                break;
            }
            // accepted: commit d at position pos
            ++accepted;
            ++ctx->stats.draft_accepted;
            if (ctx->tok->is_eog(d)) { stopped = true; break; }
            emit_utf8(cb, user, ctx->tok->token_text(d), d, pending, sink);
            remember(d);
            ++generated;
            ctx->cache_ids.push_back(d);
            ++pos;
            if (generated >= max_tokens) { stopped = true; break; }
        }
        // 4. cut both KV caches back to the committed length
        m.set_kv_len(pos);
        k = (accepted == static_cast<int>(drafts.size())) ? std::min(k_max, k + 1)
                                                         : std::max(1, accepted + 1);
        if (stopped) break;
        if (replacement >= 0) {
            next = replacement;
        } else {
            // every draft token accepted: the bonus token from the target's own
            // distribution after the last one
            std::memcpy(logits.data(), m.logits_at(static_cast<int>(drafts.size())), sizeof(float) * static_cast<size_t>(c.vocab));
            next = ctx->sampler.sample(logits.data(), c.vocab, sp, recent);
        }
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
    ctx->stats.prefill_tokens_per_second =
        ctx->stats.prefill_seconds > 0 ? prefilled / ctx->stats.prefill_seconds : 0.0;

    if (collected) *collected = sink;
    return generated;
}

int fish_generate(fish_ctx* ctx, const char* prompt, int add_bos,
                  int max_tokens, float temperature, int top_k,
                  float top_p, float repeat_penalty,
                  fish_token_cb cb, void* user) {
    if (!ctx || !prompt) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) { ctx->err = "no model loaded"; return -1; }
    return generate_ids_locked(ctx, ctx->tok->encode(prompt, add_bos != 0, true),
                               max_tokens, temperature, top_k, top_p, repeat_penalty,
                               cb, user, nullptr);
}

int fish_generate_chat(fish_ctx* ctx, const char* const* roles,
                       const char* const* contents, int n_messages,
                       int max_tokens, float temperature, int top_k,
                       float top_p, float repeat_penalty,
                       fish_token_cb cb, void* user) {
    if (!ctx || n_messages <= 0) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) { ctx->err = "no model loaded"; return -1; }
    const auto msgs = to_messages(roles, contents, n_messages);
    return generate_ids_locked(ctx, ctx->tok->encode_chat(msgs),
                               max_tokens, temperature, top_k, top_p, repeat_penalty,
                               cb, user, nullptr);
}

int fish_generate_to(fish_ctx* ctx, const char* prompt, int add_bos,
                     int max_tokens, float temperature, int top_k,
                     float top_p, float repeat_penalty,
                     char* out, int cap) {
    if (!ctx || !prompt) return -1;
    std::lock_guard<std::mutex> lock(ctx->mu);
    if (!ctx->tok) { ctx->err = "no model loaded"; return -1; }
    std::string collected;
    const int n = generate_ids_locked(ctx, ctx->tok->encode(prompt, add_bos != 0, true),
                                      max_tokens, temperature, top_k, top_p, repeat_penalty,
                                      nullptr, nullptr, &collected);
    if (n < 0) return n;
    return write_string(collected, out, cap);
}

} // extern "C"
