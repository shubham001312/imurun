#pragma once
/*
 * FISH — LoRA adapters merged into the weights at load.
 *
 * Input: the GGUF that llama.cpp's convert_lora_to_gguf.py writes
 *   general.type = "adapter", adapter.type = "lora", adapter.lora.alpha,
 *   general.architecture = the base model's architecture,
 *   tensors "<base tensor name>.lora_a" / ".lora_b" (F32/F16/BF16/Q8_0).
 *
 * Shapes and scaling follow llama.cpp exactly (src/llama-adapter.cpp,
 * llm_build_inp_embd):
 *   ordinary weight W [n_in = ne0][n_out = ne1]:
 *       lora_a ne = [n_in, r], lora_b ne = [r, n_out]
 *       delta[o][i] = sum_r b[o][r] * a[r][i]
 *   token_embd (the converter transposes lora_a):
 *       lora_a ne = [r, n_vocab], lora_b ne = [r, n_embd]
 *       delta[tok][e] = sum_r a[tok][r] * b[e][r]
 *   effective scale = scale * alpha / r   (r = lora_b ne0), or scale when alpha == 0.
 *
 * The merged tensor keeps the base tensor's type (see merge_row in quant.h)
 * and lives in an owned buffer; the model file mapping is never written.
 */
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>

#include "gguf.h"
#include "quant.h"

namespace fish {

struct LoraTarget {
    const TensorInfo* base = nullptr;   // tensor in the model GGUF
    const TensorInfo* a = nullptr;      // in the adapter GGUF
    const TensorInfo* b = nullptr;
    bool embd = false;                  // token_embd orientation
    int rank = 0;
};

struct LoraReport {
    bool active = false;
    std::string path, name;
    int rank_min = 0, rank_max = 0;
    float alpha = 0.0f;
    float scale = 1.0f;
    float effective_scale = 0.0f;       // for rank_max
    int tensors = 0;
    uint64_t merged_bytes = 0;
    double seconds = 0.0;
    bool stochastic = true;
    std::string merged_type = "same";   // or the override type's name
    MergeStats stats;
    // fraction of the adapter's delta present in the merged weights
    // (<merged - base, delta> / |delta|^2); 1 = all of it.
    double retention() const { return stats.delta_sq > 0 ? stats.moved_dot / stats.delta_sq : 1.0; }
    // |merged - (base + delta)| / |delta|: 0 = exact, 1 = no better than unmerged.
    double residual() const { return stats.delta_sq > 0 ? std::sqrt(stats.err_sq / stats.delta_sq) : 0.0; }
};

// Validate an adapter against the model file and pair up its tensors.
// Refuses (false + *err) anything it cannot merge exactly as llama.cpp would
// apply it: wrong file type, other architecture, unpaired or non-LoRA
// tensors, targets missing from the model, wrong shapes, base types the
// requantiser cannot write, 3-D (MoE expert) targets, activated LoRA.
bool lora_plan(const GgufFile& model, const GgufFile& adapter,
               std::vector<LoraTarget>* targets, float* alpha, std::string* err);

struct LoraMergeOptions {
    float scale = 1.0f;
    float alpha = 0.0f;
    MergeRounding rounding = MergeRounding::Stochastic;
    // Write the merged tensor in this type instead of the base tensor's own
    // (diagnostics / quality oracle: F16 keeps the delta almost exactly at
    // ~3.5x the bytes of Q4_K). keep_type = true is the normal path.
    bool keep_type = true;
    GgmlType out_type = GgmlType::F16;
};

// Bytes of the merged copy of t under these options.
uint64_t lora_merged_bytes(const LoraTarget& t, const LoraMergeOptions& o);

// Merge one target into dst (lora_merged_bytes bytes), row block by row
// block on the thread pool. Peak extra memory is the f32 factors
// (r x (n_in + n_out)) plus one row of scratch per thread.
bool lora_merge(const LoraTarget& t, const LoraMergeOptions& o, uint8_t* dst,
                MergeStats* stats, std::string* err);

inline float lora_effective_scale(float scale, float alpha, int rank) {
    return (alpha != 0.0f && rank > 0) ? scale * alpha / static_cast<float>(rank) : scale;
}

} // namespace fish
