#pragma once
/*
 * FISH GPU backend — online re-quantisation at upload (FISH_GPU_REQUANT=1).
 *
 * When the full model does not fit in VRAM, the backend can upload a smaller
 * copy of a weight tensor: rows are dequantised with the engine's own
 * dequant_row() and re-quantised on the CPU. The GGUF file and the CPU path
 * are untouched; only the device copy changes.
 *
 * Formats produced:
 *   Q4_K  the GGUF layout (144 B / 256), for Q5_K / Q6_K sources;
 *   FQ3   FISH's own 3-bit layout, for Q4_K sources:
 *           112 B / 256 elements = 3.5 bits per weight
 *           [0]  f16 d       super-block scale of the group scales
 *           [2]  f16 dmin    super-block scale of the group mins
 *           [4]  12 B        8 x (6-bit scale, 6-bit min), packed exactly like Q4_K
 *           [16] 8 groups x 12 B: words A, B, C for the group's 32 values q in 0..7
 *                A byte b, bits 2s..2s+1 = low 2 bits of q[b + 4s]       (s = 0..3)
 *                B byte b, bits 2s..2s+1 = low 2 bits of q[16 + b + 4s]  (s = 0..3)
 *                C byte b, bit s         = bit 2 of q[b + 4s]            (s = 0..7)
 *         value = d * sc * q - dmin * m. The plane layout lets the shader build
 *         four values per word with two masks and one unpackUnorm4x8.
 */
#include <cstddef>
#include <cstdint>

namespace fish {
namespace gpu {

constexpr size_t kFQ3BlockBytes = 112;
constexpr size_t kFQ3SBlockBytes = 104;
constexpr int kQK = 256;

// n must be a multiple of 256. Writes n/256 blocks.
void quantize_row_q4k(const float* x, int64_t n, uint8_t* out);
void quantize_row_fq3(const float* x, int64_t n, uint8_t* out);

// Importance-weighted variants (planner path, docs/engine/GPU.md "Device
// formats"). `imp` holds one non-negative weight per element of the row
// (the calibration pass's E[x_j^2] for input column j), or null for a
// uniform weight. The fit minimises sum_j w_j (x_j - x^_j)^2 with
// w_j = imp_j * sqrt(sigma2 + x_j^2), the weighting llama.cpp's imatrix
// quantisers use: importance of the column times a mild magnitude term.
void quantize_row_q4k_w(const float* x, int64_t n, uint8_t* out, const float* imp);
void quantize_row_fq3_w(const float* x, int64_t n, uint8_t* out, const float* imp);

/*
 * FQ3S - FISH 3-bit symmetric, 104 B / 256 elements = 3.25 bits per weight.
 *   [0]  f16 d                 super-block scale of the group scales
 *   [2]  6 B                   8 x 6-bit group scales, little-endian bit stream
 *                              (scale g = bits 6g..6g+5)
 *   [8]  8 groups x 12 B       words A, B, C exactly as FQ3 (q in 0..7)
 * value = d * sc_g * L(q), L(q) = u (1 - c + c u^2), u = (2q - 7) / 7:
 * eight levels symmetric around zero, denser near zero than a uniform grid
 * (c = kFQ3SCubic), because weights within a group are bell-shaped. One scale
 * per 32 values and no min (0.25 bpw less than FQ3).
 */
constexpr float kFQ3SCubic = 0.20f;
float fq3s_level(int q);
void quantize_row_fq3s(const float* x, int64_t n, uint8_t* out, const float* imp);
void dequant_row_fq3s(const uint8_t* src, float* y, int64_t n);
// Study helper (fish_test_gpu --requant-study): quantise + decode with another
// level curvature. The GPU kernel only knows kFQ3SCubic.
void fq3s_study_roundtrip(float cubic, const float* x, int64_t n, const float* imp, float* y);

// Reference decoder for FQ3 (tests, CPU-side checks).
void dequant_row_fq3(const uint8_t* src, float* y, int64_t n);

} // namespace gpu
} // namespace fish
