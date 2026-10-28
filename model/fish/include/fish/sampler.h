#pragma once
/*
 * FISH — token sampler.
 *
 * Temperature, top-k, nucleus and repetition penalty, in the order
 * llama.cpp applies them: penalty, temperature, top-k, top-p, draw.
 */
#include <cstdint>
#include <random>
#include <vector>

namespace fish {

struct SampleParams {
    float temperature = 0.7f;
    int   top_k = 40;
    float top_p = 0.9f;
    float repeat_penalty = 1.1f;
    int   repeat_last_n = 64;
    int   max_tokens = 512;
    uint64_t seed = 0;   // 0 = entropy from the clock
};

class Sampler {
public:
    explicit Sampler(uint64_t seed = 0);

    // logits is modified in place (penalty, temperature). Returns a token id.
    int32_t sample(float* logits, int n_vocab, const SampleParams& p,
                   const std::vector<int32_t>& recent);

    void reseed(uint64_t seed);

private:
    std::mt19937_64 rng_;
};

} // namespace fish
