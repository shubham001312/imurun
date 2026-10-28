/*
 * FISH — token sampler implementation.
 */
#include <fish/sampler.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>

namespace fish {

namespace {

struct Candidate {
    float logit;
    int32_t id;
};

// One scratch buffer per thread: the vocabulary scan happens once per token,
// so reallocating a 128k-entry vector every time would dominate the sampler.
std::vector<Candidate>& scratch(int n) {
    static thread_local std::vector<Candidate> buf;
    if (static_cast<int>(buf.size()) < n) buf.resize(static_cast<size_t>(n));
    return buf;
}

} // namespace

Sampler::Sampler(uint64_t seed) {
    reseed(seed);
}

void Sampler::reseed(uint64_t seed) {
    if (seed == 0) {
        seed = static_cast<uint64_t>(
            std::chrono::high_resolution_clock::now().time_since_epoch().count());
    }
    rng_.seed(seed);
}

int32_t Sampler::sample(float* logits, int n_vocab, const SampleParams& p,
                        const std::vector<int32_t>& recent) {
    if (!logits || n_vocab <= 0) return 0;

    // ---- repetition penalty over the recent window
    if (p.repeat_penalty > 0.0f && p.repeat_penalty != 1.0f && !recent.empty()) {
        const int window = p.repeat_last_n > 0 ? p.repeat_last_n : 64;
        const int start = static_cast<int>(recent.size()) - window;
        for (int i = start < 0 ? 0 : start; i < static_cast<int>(recent.size()); ++i) {
            const int32_t id = recent[static_cast<size_t>(i)];
            if (id < 0 || id >= n_vocab) continue;
            float& v = logits[id];
            v = (v > 0.0f) ? v / p.repeat_penalty : v * p.repeat_penalty;
        }
    }

    // ---- greedy
    if (p.temperature <= 0.0f) {
        int32_t best = 0;
        float bv = -std::numeric_limits<float>::infinity();
        for (int i = 0; i < n_vocab; ++i) {
            if (logits[i] > bv) { bv = logits[i]; best = i; }
        }
        return best;
    }

    // ---- temperature
    const float inv_t = 1.0f / p.temperature;
    for (int i = 0; i < n_vocab; ++i) logits[i] *= inv_t;

    // ---- top-k
    const int k = p.top_k > 0 ? std::min(p.top_k, n_vocab) : n_vocab;
    std::vector<Candidate>& all = scratch(n_vocab);
    for (int i = 0; i < n_vocab; ++i) all[static_cast<size_t>(i)] = {logits[i], i};
    std::partial_sort(all.begin(), all.begin() + k, all.begin() + n_vocab,
                      [](const Candidate& a, const Candidate& b) { return a.logit > b.logit; });

    // ---- softmax over the top-k
    const float maxv = all[0].logit;
    double sum = 0.0;
    for (int i = 0; i < k; ++i) {
        const float e = std::exp(all[static_cast<size_t>(i)].logit - maxv);
        all[static_cast<size_t>(i)].logit = e;
        sum += e;
    }
    if (sum <= 0.0) return all[0].id;

    // ---- nucleus
    double cum = 0.0;
    int keep = k;
    for (int i = 0; i < k; ++i) {
        cum += all[static_cast<size_t>(i)].logit / sum;
        if (cum >= p.top_p) { keep = i + 1; break; }
    }

    double norm = 0.0;
    for (int i = 0; i < keep; ++i) norm += all[static_cast<size_t>(i)].logit;
    if (norm <= 0.0) return all[0].id;

    std::uniform_real_distribution<double> dist(0.0, norm);
    const double r = dist(rng_);
    double acc = 0.0;
    for (int i = 0; i < keep; ++i) {
        acc += all[static_cast<size_t>(i)].logit;
        if (r <= acc) return all[static_cast<size_t>(i)].id;
    }
    return all[static_cast<size_t>(keep - 1)].id;
}

} // namespace fish
