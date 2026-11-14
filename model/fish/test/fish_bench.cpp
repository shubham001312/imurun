/*
 * fish_bench — standalone speed and determinism check through the C ABI.
 *
 *   fish_bench <model.gguf> [options]
 *     -p N        prompt length in tokens (synthetic text, default 128)
 *     -n N        tokens to generate (default 64)
 *     -t N        threads (sets FISH_THREADS before the pool exists)
 *     -r N        measured repetitions after one cold run (default 2)
 *     --text S    use S as the prompt instead of the synthetic text
 *     --chat S    wrap S as one ChatML user turn (Qwen style, empty think)
 *     --ids       print the generated token ids (greedy, for A/B checks)
 *     --sample    temperature 0.7 / top-k 20 / top-p 0.8 / penalty 1.1
 *                 (what the phone app uses) instead of greedy
 *     --prefill   also time fish_prefill + a generate that continues it
 *     --partial   time to first token of a voice turn: system prompt
 *                 cached / partial user text prefilled first / partial
 *                 text revised by the final transcript
 *     --cancel-ms N  also start a fish_prefill, fish_cancel it after N ms,
 *                 report how long the cancel took, then check that a
 *                 generate afterwards gives the same tokens as a clean run
 *
 * Every run resets the cache first so prefill is measured from zero.
 * Output is plain text, one "key value" pair per line, easy to grep.
 */
#include <fish/c_api.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Sink {
    std::vector<int> ids;
    std::string text;
    double first_token_s = -1;
    std::chrono::steady_clock::time_point t0;
};

void on_token(const char* text, int id, void* user) {
    auto* s = static_cast<Sink*>(user);
    if (s->first_token_s < 0)
        s->first_token_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - s->t0).count();
    s->ids.push_back(id);
    s->text += text;
}

double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: fish_bench <model.gguf> [-p N] [-n N] [-t N] [-r N] [--text S] [--chat S] [--ids] [--sample] [--prefill]\n");
        return 2;
    }
    const char* path = argv[1];
    int n_prompt = 128, n_gen = 64, reps = 2, threads = 0;
    bool print_ids = false, sample = false, test_prefill = false;
    int cancel_ms = -1;
    bool partial_test = false;
    std::string text, chat;
    for (int i = 2; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> const char* { return i + 1 < argc ? argv[++i] : ""; };
        if (a == "-p") n_prompt = std::atoi(next());
        else if (a == "-n") n_gen = std::atoi(next());
        else if (a == "-t") threads = std::atoi(next());
        else if (a == "-r") reps = std::atoi(next());
        else if (a == "--text") text = next();
        else if (a == "--chat") chat = next();
        else if (a == "--ids") print_ids = true;
        else if (a == "--sample") sample = true;
        else if (a == "--prefill") test_prefill = true;
        else if (a == "--cancel-ms") cancel_ms = std::atoi(next());
        else if (a == "--partial") partial_test = true;
    }
    if (threads > 0) {
        char buf[16];
        std::snprintf(buf, sizeof(buf), "%d", threads);
#ifdef _WIN32
        _putenv_s("FISH_THREADS", buf);
#else
        setenv("FISH_THREADS", buf, 1);
#endif
    }

    std::printf("version %s\nkernel %s\n", fish_version(), fish_kernel_name());
    fish_ctx* ctx = fish_create();
    const double tl = now_s();
    if (fish_load(ctx, path, 2048) != 0) {
        std::printf("load_error %s\n", fish_last_error(ctx));
        return 1;
    }
    std::printf("load_s %.3f\n", now_s() - tl);
    fish_model_info mi{};
    fish_model_info_of(ctx, &mi);
    std::printf("arch %s layers %d embd %d vocab %d quant %s\n", mi.arch, mi.n_layers, mi.n_embd, mi.vocab, mi.quant);

    std::string prompt;
    if (!chat.empty()) {
        prompt = "<|im_start|>user\n" + chat + "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n";
    } else if (!text.empty()) {
        prompt = text;
    } else {
        // Synthetic prose, cut to n_prompt tokens.
        const char* base = "The quick brown fox jumps over the lazy dog while the river runs past the old mill. ";
        while (fish_tokenize(ctx, prompt.c_str(), 0, nullptr, 0) < n_prompt) prompt += base;
        std::vector<int32_t> ids(static_cast<size_t>(n_prompt) + 64);
        const int n = fish_tokenize(ctx, prompt.c_str(), 0, ids.data(), static_cast<int>(ids.size()));
        ids.resize(static_cast<size_t>(std::min(n, n_prompt)));
        std::vector<char> back(65536);
        fish_detokenize(ctx, ids.data(), static_cast<int>(ids.size()), back.data(), static_cast<int>(back.size()));
        prompt = back.data();
    }
    const int ptoks = fish_tokenize(ctx, prompt.c_str(), 0, nullptr, 0);
    std::printf("prompt_tokens %d\n", ptoks);
    if (print_ids) {
        std::vector<int32_t> pids(static_cast<size_t>(ptoks));
        fish_tokenize(ctx, prompt.c_str(), 0, pids.data(), ptoks);
        std::printf("prompt_ids");
        for (int id : pids) std::printf(" %d", id);
        std::printf("\n");
    }

    const float temp = sample ? 0.7f : 0.0f;
    const int top_k = sample ? 20 : 1;
    const float top_p = sample ? 0.8f : 1.0f;
    const float pen = sample ? 1.1f : 1.0f;

    std::vector<int> ref_ids;
    for (int r = 0; r <= reps; ++r) {
        fish_reset(ctx);
        Sink s;
        s.t0 = std::chrono::steady_clock::now();
        const int got = fish_generate(ctx, prompt.c_str(), 0, n_gen, temp, top_k, top_p, pen, on_token, &s);
        if (got < 0) std::printf("generate_error %s\n", fish_last_error(ctx));
        fish_stats st{};
        fish_get_stats(ctx, &st);
        std::printf("run %d%s threads %d prefill_tok %d prefill_s %.3f prefill_tps %.2f gen %d decode_s %.3f decode_tps %.2f ttft_s %.3f\n",
                    r, r == 0 ? " (cold)" : "", st.n_threads, st.prompt_tokens - st.cached_tokens,
                    st.prefill_seconds, st.prefill_tokens_per_second, got, st.decode_seconds,
                    st.tokens_per_second, s.first_token_s);
        if (r == reps) {
            ref_ids = s.ids;
            if (print_ids) {
                std::printf("ids");
                for (int id : s.ids) std::printf(" %d", id);
                std::printf("\n");
            }
            std::printf("text %s\n", s.text.c_str());
        }
    }

    if (test_prefill) {
#ifdef FISH_HAVE_PREFILL
        fish_reset(ctx);
        const double t0 = now_s();
        const int pr = fish_prefill(ctx, prompt.c_str(), 0);
        const double t1 = now_s();
        Sink s;
        s.t0 = std::chrono::steady_clock::now();
        const int got = fish_generate(ctx, prompt.c_str(), 0, n_gen, temp, top_k, top_p, pen, on_token, &s);
        fish_stats st{};
        fish_get_stats(ctx, &st);
        std::printf("prefill_call rc %d s %.3f then generate cached %d of %d, ttft_s %.3f gen %d\n",
                    pr, t1 - t0, st.cached_tokens, st.prompt_tokens, s.first_token_s, got);
        if (print_ids) {
            std::printf("ids_after_prefill");
            for (int id : s.ids) std::printf(" %d", id);
            std::printf("\n");
        }
        std::printf("prefill_then_generate_same_ids %s\n", s.ids == ref_ids ? "yes" : "NO");
#else
        std::printf("prefill_call unavailable in this build\n");
#endif
    }

    if (partial_test) {
#ifdef FISH_HAVE_PREFILL
        // Time to first token for a voice turn, three ways: system prompt
        // cached only; partial user text prefilled while "speaking" and then
        // extended; partial text that the final transcript revises (hybrid
        // models then fall back to a checkpoint). Greedy output must be the
        // same in every case.
        const std::string sys =
            "<|im_start|>system\nYou are Orange, a warm, cheerful friend from Kolkata. You speak "
            "simple English mixed with a little Hindi and Bengali, keep replies short, and ask "
            "one gentle question back. Never mention that you are an AI model.<|im_end|>\n"
            "<|im_start|>user\n";
        const std::string user = chat.empty() ? "Can you tell me what the weather is like in Kolkata today?" : chat;
        const std::string tail = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n";
        const std::string full = sys + user + tail;
        auto turn = [&](const char* label, const std::vector<std::string>& partials) {
            fish_reset(ctx);
            fish_prefill(ctx, sys.c_str(), 0);
            double t_partials = 0;
            for (const std::string& p : partials) {
                const double a = now_s();
                fish_prefill(ctx, (sys + p).c_str(), 0);
                t_partials += now_s() - a;
            }
            Sink s;
            s.t0 = std::chrono::steady_clock::now();
            fish_generate(ctx, full.c_str(), 0, n_gen, temp, top_k, top_p, pen, on_token, &s);
            fish_stats st{};
            fish_get_stats(ctx, &st);
            std::printf("%s: partial prefills %.3f s, then generate reused %d of %d tokens, ttft %.3f s\n",
                        label, t_partials, st.cached_tokens, st.prompt_tokens, s.first_token_s);
            return s.ids;
        };
        const size_t cut1 = user.size() * 2 / 5, cut2 = user.size() * 7 / 10;
        const auto a = turn("system only", {});
        const auto b = turn("partial text, then final", {user.substr(0, cut1), user.substr(0, cut2), user});
        std::string revised = user.substr(0, cut2);
        if (!revised.empty()) revised[revised.size() - 1] = revised[revised.size() - 1] == 'x' ? 'y' : 'x';
        const auto c = turn("partial text revised", {user.substr(0, cut1), revised});
        std::printf("partial_test same tokens: %s %s\n", a == b ? "yes" : "NO", a == c ? "yes" : "NO");
#endif
    }

    if (cancel_ms >= 0) {
#ifdef FISH_HAVE_PREFILL
        fish_reset(ctx);
        double t_cancel = 0;
        std::thread killer([&] {
            std::this_thread::sleep_for(std::chrono::milliseconds(cancel_ms));
            t_cancel = now_s();
            fish_cancel(ctx);
        });
        const int pr = fish_prefill(ctx, prompt.c_str(), 0);
        const double t_ret = now_s();
        killer.join();
        std::printf("cancel after %d ms: prefill returned %d of %d tokens, %.1f ms after the cancel\n",
                    cancel_ms, pr, ptoks, (t_ret - t_cancel) * 1000.0);
        Sink s;
        s.t0 = std::chrono::steady_clock::now();
        fish_generate(ctx, prompt.c_str(), 0, n_gen, temp, top_k, top_p, pen, on_token, &s);
        fish_stats st{};
        fish_get_stats(ctx, &st);
        std::printf("after cancel: generate reused %d tokens, same ids as clean run: %s\n",
                    st.cached_tokens, s.ids == ref_ids ? "yes" : "NO");
#endif
    }
    fish_destroy(ctx);
    return 0;
}
