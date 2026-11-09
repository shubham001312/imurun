#pragma once
/*
 * FISH — quantised tensor kernels (scalar reference + dispatch).
 *
 * Every block format below is the GGUF on-disk layout. FISH dequantises
 * inside the dot product rather than expanding a whole matrix into f32:
 * a 4-bit matrix expands 8x in memory, which is exactly what a
 * memory-constrained engine must not do.
 *
 * Three entry points:
 *   dequant_row()   full row to f32, used for embeddings and norms
 *   vec_dot()       quantised row x f32 vector (reference path)
 *   vec_dot_q8()    quantised row x 8-bit activations (fast path)
 *
 * The fast path is how a CPU engine gets its speed: the activations are
 * quantised once per matmul to int8, and the dot product runs on packed
 * bytes with integer multiply-adds (32 products per instruction on AVX2)
 * instead of expanding every nibble to a float.
 */
#include <cstdint>
#include <cstddef>
#include "gguf.h"

namespace fish {

// ---- block layouts (identical to the GGUF specification) -------------------
#pragma pack(push, 1)
struct BlockQ4_0 { uint16_t d; uint8_t qs[16]; };                        // 18 B / 32
struct BlockQ4_1 { uint16_t d; uint16_t m; uint8_t qs[16]; };            // 20 B / 32
struct BlockQ5_0 { uint16_t d; uint8_t qh[4]; uint8_t qs[16]; };         // 22 B / 32
struct BlockQ5_1 { uint16_t d; uint16_t m; uint8_t qh[4]; uint8_t qs[16]; }; // 24 B / 32
struct BlockQ8_0 { uint16_t d; int8_t qs[32]; };                         // 34 B / 32
struct BlockQ2_K { uint8_t scales[16]; uint8_t qs[64]; uint16_t d; uint16_t dmin; };  // 84 B
struct BlockQ3_K { uint8_t hmask[32]; uint8_t qs[64]; uint8_t scales[12]; uint16_t d; }; // 110 B
struct BlockQ4_K { uint16_t d; uint16_t dmin; uint8_t scales[12]; uint8_t qs[128]; };  // 144 B
struct BlockQ5_K { uint16_t d; uint16_t dmin; uint8_t scales[12]; uint8_t qh[32]; uint8_t qs[128]; }; // 176 B
struct BlockQ6_K { uint8_t ql[128]; uint8_t qh[64]; int8_t scales[16]; uint16_t d; };  // 210 B
struct BlockQ8_K { float d; int8_t qs[256]; int16_t bsums[16]; };        // 292 B
struct BlockIQ4_NL { uint16_t d; uint8_t qs[16]; };                      // 18 B / 32
struct BlockIQ4_XS { uint16_t d; uint16_t scales_h; uint8_t scales_l[4]; uint8_t qs[128]; }; // 136 B / 256
struct BlockMXFP4 { uint8_t e; uint8_t qs[16]; };                        // 17 B / 32
#pragma pack(pop)

static_assert(sizeof(BlockQ4_K) == 144, "Q4_K block size");
static_assert(sizeof(BlockQ5_K) == 176, "Q5_K block size");
static_assert(sizeof(BlockQ6_K) == 210, "Q6_K block size");
static_assert(sizeof(BlockQ8_0) == 34, "Q8_0 block size");
static_assert(sizeof(BlockIQ4_XS) == 136, "IQ4_XS block size");

// ---- 8-bit activations ------------------------------------------------------
// Two layouts, matching the two weight block sizes:
//
//   Q8Act   32 values, one f32 scale, integer sum. Pairs with Q4_0 / Q8_0.
//   Q8ActK  256 values, one f32 scale, eight per-32 sums. Pairs with the
//           K-quants. One scale per super-block is what lets the whole block
//           accumulate in int32 (3 vector ops per 32 weights) instead of
//           converting to float every 32 values.
struct Q8Act {
    float   d;
    int32_t sum;
    int8_t  qs[32];
};
static_assert(sizeof(Q8Act) == 40, "Q8Act size");

struct Q8ActK {
    float   d;
    int16_t bsums[8];
    int8_t  qs[256];
};
static_assert(sizeof(Q8ActK) == 276, "Q8ActK size");

constexpr int kQ8ActBlock = 32;
constexpr int kQ8ActKBlock = 256;

// n must be a multiple of the block size; writes n / block blocks.
void quantize_acts(const float* x, uint64_t n, Q8Act* out);
void quantize_acts_k(const float* x, uint64_t n, Q8ActK* out);

// ---- half precision -------------------------------------------------------
float fp16_to_fp32(uint16_t h);
void  init_fp16_table();          // safe to call more than once

// ---- dequantisation -------------------------------------------------------
// Returns false if the type is not implemented by FISH yet.
bool dequant_row(GgmlType type, const void* src, float* dst, uint64_t n);

// Dot product of a quantised row with an f32 vector of the same length.
// Returns 0 and sets *ok=false when the type is unsupported.
float vec_dot(GgmlType type, const void* src, const float* y, uint64_t n,
              bool* ok = nullptr);

// Dot product of a quantised row with 8-bit activations. Returns false when
// the type has no integer kernel of that layout; the caller then uses vec_dot.
bool vec_dot_q8(GgmlType type, const void* src, const Q8Act* x, uint64_t n, float* out);
bool vec_dot_q8k(GgmlType type, const void* src, const Q8ActK* x, uint64_t n, float* out);
bool has_q8_kernel(GgmlType type);    // 32-block layout (Q4_0, Q8_0)
bool has_q8k_kernel(GgmlType type);   // 256-block layout (Q4_K, Q5_K, Q6_K)

// Plain f32 dot product through the fastest installed kernel.
float dot_f32_fast(const float* a, const float* b, uint64_t n);

// True when vec_dot/dequant_row understand this type.
bool quant_supported(GgmlType type);

// ---- quantisation (writing blocks) ---------------------------------------
// Used by the LoRA merge (src/lora.cpp): a weight row is dequantised, the
// adapter delta is added, and the row is written back in the tensor's OWN
// type, so placement, kernels and GPU upload see an ordinary tensor.
// Implemented in src/quantize.cpp.

uint16_t fp32_to_fp16(float f);   // round to nearest even, subnormals kept

// Types quantize_row()/merge_row() can write: F32 F16 BF16 Q8_0 Q4_0 Q4_K
// Q5_K Q6_K. Everything else is refused by the caller with a load error.
bool quantize_supported(GgmlType type);
const char* quantize_supported_list();

// Fresh quantisation of n values (n a multiple of the type's block size).
bool quantize_row(GgmlType type, const float* x, void* dst, uint64_t n);

// What a merge did to the weights, summed over blocks. `delta` is the
// adapter's exact f32 contribution, `target` = dequant(src) + delta.
struct MergeStats {
    double delta_sq = 0;     // sum delta^2
    double moved_dot = 0;    // sum (merged - base) * delta
    double err_sq = 0;       // sum (merged - target)^2
    uint64_t blocks = 0;
    uint64_t blocks_copied = 0;  // delta exactly zero: source bytes kept
    uint64_t blocks_grid = 0;    // re-rounded on the source block's own scales
    uint64_t blocks_refit = 0;   // freshly quantised (scales refitted)
    void add(const MergeStats& o) {
        delta_sq += o.delta_sq; moved_dot += o.moved_dot; err_sq += o.err_sq;
        blocks += o.blocks; blocks_copied += o.blocks_copied;
        blocks_grid += o.blocks_grid; blocks_refit += o.blocks_refit;
    }
};

// How merge_row() places base + delta on the source block's levels.
//   Stochastic  round up with probability = the fractional part (unbiased:
//               E[merged] = base + delta). The default, because a trained
//               LoRA delta is usually far below half a quantisation step:
//               nearest rounding would snap almost every weight back to its
//               old code and silently drop the adapter (measured: 0% of a
//               real 5-step adapter kept at Q4_K/Q6_K). The added noise is
//               zero-mean and, for small deltas, much smaller than the
//               tensor's existing quantisation noise.
//   Nearest     deterministic; never farther from the target than the
//               unmerged block, but loses deltas below half a step.
// The random stream is a hash of (seed, element), so merges are
// reproducible and independent of the thread count.
enum class MergeRounding { Stochastic, Nearest };

// dst = the encoding of base + delta in `type`, for one row of n values.
// src is the source row (same type), base = dequant(src). Per block:
//   grid   the source block's scales kept, codes moved by delta/step with
//          the rounding above; the base is not re-quantised;
//   refit  a fresh quantisation of base + delta, tried only when the delta
//          pushes a value outside the block's levels; the lower squared
//          error of the two is written.
// A block whose delta is exactly zero is copied byte for byte (and so is
// every element with zero delta), so a merge with scale 0 reproduces the
// source tensor exactly. F32/F16/BF16 rows are written element-wise (nearest).
bool merge_row(GgmlType type, const void* src, const float* base, const float* delta,
               void* dst, uint64_t n, MergeStats* stats,
               MergeRounding rounding = MergeRounding::Stochastic, uint64_t seed = 0);

// ---- dispatch hooks used by the SIMD builds -------------------------------
// The scalar implementations live in quant.cpp. quant_x86.cpp (AVX2) and
// quant_arm.cpp (NEON) install faster function pointers at startup when the
// build and the CPU support them; `name` then reads "avx2", "neon-dotprod"
// or "neon" (fish_kernel_name()).
typedef float (*dot_f32_fn)(const void*, const float*, uint64_t);
typedef float (*dot_q8_fn)(const void*, const Q8Act*, uint64_t);
typedef float (*dot_q8k_fn)(const void*, const Q8ActK*, uint64_t);
typedef void  (*quantize_fn)(const float*, uint64_t, Q8Act*);
typedef void  (*quantize_k_fn)(const float*, uint64_t, Q8ActK*);

struct QuantKernels {
    dot_f32_fn q4_K, q6_K, q8_0, q4_0;             // weights x f32
    dot_q8_fn  q8_0_q8, q4_0_q8;                   // 32-block weights x int8
    dot_q8k_fn q4_K_q8k, q5_K_q8k, q6_K_q8k;       // K-quant weights x int8
    quantize_fn quantize;
    quantize_k_fn quantize_k;
    const char* name;
};
const QuantKernels& quant_kernels();
const QuantKernels& scalar_kernels();   // the reference table, for tests
void install_avx2_kernels();      // no-op when the CPU or build lacks AVX2
bool avx2_available();
void install_neon_kernels();      // no-op off arm64; FISH_NEON_DOTPROD=0 forces plain NEON
bool neon_dotprod_available();    // arm64 build and the CPU has SDOT (ARMv8.2 DotProd)

#if defined(FISH_HAVE_X86_KERNELS) && FISH_HAVE_X86_KERNELS
// Implemented in quant_x86.cpp (compiled with per-function AVX2 targets, so
// the translation unit itself stays baseline x86-64).
void  fish_install_x86(QuantKernels& k,
                       float (**dot_f32)(const float*, const float*, uint64_t));
bool  fish_x86_avx2();
#endif

#if defined(FISH_HAVE_ARM_KERNELS) && FISH_HAVE_ARM_KERNELS
// Implemented in quant_arm.cpp. Fills `k` (and *dot_f32 when non-null) with
// the NEON set: the SDOT variant when allow_dotprod and the CPU has it
// (returns true), else plain NEON (returns false). Tests call it on a copy of
// scalar_kernels() to check both variants against the reference.
bool  fish_install_arm(QuantKernels& k,
                       float (**dot_f32)(const float*, const float*, uint64_t),
                       bool allow_dotprod);
bool  fish_arm_dotprod();
bool  fish_arm_dotprod_allowed();  // false when FISH_NEON_DOTPROD=0
#endif

} // namespace fish
