#pragma once
/*
 * FISH — quantised tensor kernels (scalar reference + dispatch).
 *
 * Every block format below is the GGUF on-disk layout. FISH dequantises
 * inside the dot product rather than expanding a whole matrix into f32:
 * a 4-bit matrix expands 8x in memory, which is exactly what a
 * memory-constrained engine must not do.
 *
 * Two entry points:
 *   dequant_row()  full row to f32, used for embeddings and norms
 *   vec_dot()      quantised row x f32 vector, used for every projection
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
struct BlockMXFP4 { uint8_t e; uint8_t qs[16]; };                        // 17 B / 32
#pragma pack(pop)

static_assert(sizeof(BlockQ4_K) == 144, "Q4_K block size");
static_assert(sizeof(BlockQ6_K) == 210, "Q6_K block size");
static_assert(sizeof(BlockQ8_0) == 34, "Q8_0 block size");

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

// Plain f32 dot product through the fastest installed kernel.
float dot_f32_fast(const float* a, const float* b, uint64_t n);

// True when vec_dot/dequant_row understand this type.
bool quant_supported(GgmlType type);

// ---- dispatch hook used by the AVX2 build --------------------------------
// The scalar implementations live in quant.cpp. quant_avx2.cpp installs
// faster function pointers at startup when the CPU supports them.
struct QuantKernels {
    float (*q4_K)(const void*, const float*, uint64_t);
    float (*q6_K)(const void*, const float*, uint64_t);
    float (*q8_0)(const void*, const float*, uint64_t);
    float (*q4_0)(const void*, const float*, uint64_t);
    const char* name;
};
const QuantKernels& quant_kernels();
void install_avx2_kernels();      // no-op when the CPU or build lacks AVX2
bool avx2_available();

#if defined(FISH_HAVE_X86_KERNELS) && FISH_HAVE_X86_KERNELS
// Implemented in quant_x86.cpp (compiled with per-function AVX2 targets, so
// the translation unit itself stays baseline x86-64).
void  fish_install_x86(QuantKernels& k,
                       float (**dot_f32)(const float*, const float*, uint64_t));
bool  fish_x86_avx2();
#endif

} // namespace fish
