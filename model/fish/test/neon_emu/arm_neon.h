#pragma once
/*
 * Scalar model of the NEON intrinsics used by src/quant_arm_kernels.inc.
 *
 * Test-only: target fish_test_kernels_emu compiles quant_arm.cpp on x86 with
 * FISH_NEON_EMULATE and this directory first on the include path, so the
 * NEON kernels' indexing and arithmetic run against the scalar reference on
 * a machine without ARM hardware. Each function follows the Arm ACLE
 * definition lane by lane (sources: the Arm intrinsics reference); only the
 * intrinsics the kernels use are modelled. This does NOT validate a real
 * compiler's NEON code generation; CI runs fish_test_kernels on an arm64
 * runner for that.
 */
#include <cmath>
#include <cstdint>
#include <cstring>

template <typename T, int N, int Tag = 0>
struct fish_emu_vec { T v[N]; };

typedef fish_emu_vec<int8_t, 8> int8x8_t;
typedef fish_emu_vec<int8_t, 16> int8x16_t;
typedef fish_emu_vec<uint8_t, 8> uint8x8_t;
typedef fish_emu_vec<uint8_t, 16> uint8x16_t;
typedef fish_emu_vec<int16_t, 4> int16x4_t;
typedef fish_emu_vec<int16_t, 8> int16x8_t;
typedef fish_emu_vec<uint16_t, 4> uint16x4_t;
typedef fish_emu_vec<uint16_t, 8> uint16x8_t;
typedef fish_emu_vec<int32_t, 4> int32x4_t;
typedef fish_emu_vec<uint32_t, 4> uint32x4_t;
typedef fish_emu_vec<float, 4> float32x4_t;
typedef fish_emu_vec<uint16_t, 4, 1> float16x4_t;   // raw IEEE half bits

#define FISH_EMU_MAP(R, N, expr) { R r; for (int i = 0; i < (N); ++i) r.v[i] = (expr); return r; }

// ---- loads / stores / dup
static inline int8x16_t vld1q_s8(const int8_t* p) { int8x16_t r; std::memcpy(r.v, p, 16); return r; }
static inline uint8x16_t vld1q_u8(const uint8_t* p) { uint8x16_t r; std::memcpy(r.v, p, 16); return r; }
static inline uint8x8_t vld1_u8(const uint8_t* p) { uint8x8_t r; std::memcpy(r.v, p, 8); return r; }
static inline int16x8_t vld1q_s16(const int16_t* p) { int16x8_t r; std::memcpy(r.v, p, 16); return r; }
static inline float32x4_t vld1q_f32(const float* p) { float32x4_t r; std::memcpy(r.v, p, 16); return r; }
static inline void vst1_s8(int8_t* p, int8x8_t a) { std::memcpy(p, a.v, 8); }
static inline int8x16_t vdupq_n_s8(int8_t x) FISH_EMU_MAP(int8x16_t, 16, x)
static inline uint8x16_t vdupq_n_u8(uint8_t x) FISH_EMU_MAP(uint8x16_t, 16, x)
static inline int32x4_t vdupq_n_s32(int32_t x) FISH_EMU_MAP(int32x4_t, 4, x)
static inline float32x4_t vdupq_n_f32(float x) FISH_EMU_MAP(float32x4_t, 4, x)
static inline uint16x4_t vdup_n_u16(uint16_t x) FISH_EMU_MAP(uint16x4_t, 4, x)

// ---- reinterpret / halves / combine
static inline int8x16_t vreinterpretq_s8_u8(uint8x16_t a) { int8x16_t r; std::memcpy(r.v, a.v, 16); return r; }
static inline int16x8_t vreinterpretq_s16_u16(uint16x8_t a) { int16x8_t r; std::memcpy(r.v, a.v, 16); return r; }
static inline float16x4_t vreinterpret_f16_u16(uint16x4_t a) { float16x4_t r; std::memcpy(r.v, a.v, 8); return r; }
static inline int8x8_t vget_low_s8(int8x16_t a) FISH_EMU_MAP(int8x8_t, 8, a.v[i])
static inline uint8x8_t vget_low_u8(uint8x16_t a) FISH_EMU_MAP(uint8x8_t, 8, a.v[i])
static inline int16x4_t vget_low_s16(int16x8_t a) FISH_EMU_MAP(int16x4_t, 4, a.v[i])
static inline uint16x4_t vget_low_u16(uint16x8_t a) FISH_EMU_MAP(uint16x4_t, 4, a.v[i])
static inline int16x8_t vcombine_s16(int16x4_t a, int16x4_t b) FISH_EMU_MAP(int16x8_t, 8, i < 4 ? a.v[i] : b.v[i - 4])
static inline float vgetq_lane_f32(float32x4_t a, int lane) { return a.v[lane]; }

// ---- widen / narrow (narrowing truncates, like XTN)
static inline uint16x8_t vmovl_u8(uint8x8_t a) FISH_EMU_MAP(uint16x8_t, 8, a.v[i])
static inline uint16x8_t vmovl_high_u8(uint8x16_t a) FISH_EMU_MAP(uint16x8_t, 8, a.v[i + 8])
static inline int16x8_t vmovl_s8(int8x8_t a) FISH_EMU_MAP(int16x8_t, 8, a.v[i])
static inline int16x8_t vmovl_high_s8(int8x16_t a) FISH_EMU_MAP(int16x8_t, 8, a.v[i + 8])
static inline uint32x4_t vmovl_u16(uint16x4_t a) FISH_EMU_MAP(uint32x4_t, 4, a.v[i])
static inline uint32x4_t vmovl_high_u16(uint16x8_t a) FISH_EMU_MAP(uint32x4_t, 4, a.v[i + 4])
static inline int32x4_t vmovl_s16(int16x4_t a) FISH_EMU_MAP(int32x4_t, 4, a.v[i])
static inline int32x4_t vmovl_high_s16(int16x8_t a) FISH_EMU_MAP(int32x4_t, 4, a.v[i + 4])
static inline int16x4_t vmovn_s32(int32x4_t a) FISH_EMU_MAP(int16x4_t, 4, static_cast<int16_t>(static_cast<uint16_t>(a.v[i])))
static inline int8x8_t vmovn_s16(int16x8_t a) FISH_EMU_MAP(int8x8_t, 8, static_cast<int8_t>(static_cast<uint8_t>(a.v[i])))

// ---- integer arithmetic (8-bit lanes wrap modulo 256)
static inline uint8x16_t vandq_u8(uint8x16_t a, uint8x16_t b) FISH_EMU_MAP(uint8x16_t, 16, static_cast<uint8_t>(a.v[i] & b.v[i]))
static inline uint8x16_t vorrq_u8(uint8x16_t a, uint8x16_t b) FISH_EMU_MAP(uint8x16_t, 16, static_cast<uint8_t>(a.v[i] | b.v[i]))
static inline uint8x16_t vshrq_n_u8(uint8x16_t a, int n) FISH_EMU_MAP(uint8x16_t, 16, static_cast<uint8_t>(a.v[i] >> n))
static inline uint8x16_t vshlq_n_u8(uint8x16_t a, int n) FISH_EMU_MAP(uint8x16_t, 16, static_cast<uint8_t>(a.v[i] << n))
static inline int8x16_t vsubq_s8(int8x16_t a, int8x16_t b) FISH_EMU_MAP(int8x16_t, 16, static_cast<int8_t>(static_cast<uint8_t>(a.v[i] - b.v[i])))
static inline int32x4_t vaddq_s32(int32x4_t a, int32x4_t b) FISH_EMU_MAP(int32x4_t, 4, a.v[i] + b.v[i])
static inline int32x4_t vmaxq_s32(int32x4_t a, int32x4_t b) FISH_EMU_MAP(int32x4_t, 4, a.v[i] > b.v[i] ? a.v[i] : b.v[i])
static inline int32x4_t vminq_s32(int32x4_t a, int32x4_t b) FISH_EMU_MAP(int32x4_t, 4, a.v[i] < b.v[i] ? a.v[i] : b.v[i])
static inline int32x4_t vmlaq_n_s32(int32x4_t a, int32x4_t b, int32_t c) FISH_EMU_MAP(int32x4_t, 4, a.v[i] + b.v[i] * c)
static inline int16x8_t vmull_s8(int8x8_t a, int8x8_t b) FISH_EMU_MAP(int16x8_t, 8, static_cast<int16_t>(a.v[i] * b.v[i]))
static inline int16x8_t vmull_high_s8(int8x16_t a, int8x16_t b) FISH_EMU_MAP(int16x8_t, 8, static_cast<int16_t>(a.v[i + 8] * b.v[i + 8]))
static inline int32x4_t vmull_s16(int16x4_t a, int16x4_t b) FISH_EMU_MAP(int32x4_t, 4, static_cast<int32_t>(a.v[i]) * b.v[i])
static inline int32x4_t vmlal_high_s16(int32x4_t acc, int16x8_t a, int16x8_t b) FISH_EMU_MAP(int32x4_t, 4, acc.v[i] + static_cast<int32_t>(a.v[i + 4]) * b.v[i + 4])
// SADALP: add adjacent pairs of b, accumulate into a.
static inline int32x4_t vpadalq_s16(int32x4_t a, int16x8_t b) FISH_EMU_MAP(int32x4_t, 4, a.v[i] + b.v[2 * i] + b.v[2 * i + 1])
// SDOT: lane i += sum of the four products of bytes 4i..4i+3.
static inline int32x4_t vdotq_s32(int32x4_t acc, int8x16_t a, int8x16_t b)
    FISH_EMU_MAP(int32x4_t, 4, acc.v[i] + a.v[4 * i] * b.v[4 * i] + a.v[4 * i + 1] * b.v[4 * i + 1] +
                               a.v[4 * i + 2] * b.v[4 * i + 2] + a.v[4 * i + 3] * b.v[4 * i + 3])
static inline int32_t vaddvq_s32(int32x4_t a) { return a.v[0] + a.v[1] + a.v[2] + a.v[3]; }

// ---- float
static inline float32x4_t vaddq_f32(float32x4_t a, float32x4_t b) FISH_EMU_MAP(float32x4_t, 4, a.v[i] + b.v[i])
static inline float32x4_t vsubq_f32(float32x4_t a, float32x4_t b) FISH_EMU_MAP(float32x4_t, 4, a.v[i] - b.v[i])
static inline float32x4_t vmulq_f32(float32x4_t a, float32x4_t b) FISH_EMU_MAP(float32x4_t, 4, a.v[i] * b.v[i])
// FMLA: a + b * c with a single rounding.
static inline float32x4_t vfmaq_f32(float32x4_t a, float32x4_t b, float32x4_t c) FISH_EMU_MAP(float32x4_t, 4, std::fma(b.v[i], c.v[i], a.v[i]))
static inline float32x4_t vabsq_f32(float32x4_t a) FISH_EMU_MAP(float32x4_t, 4, std::fabs(a.v[i]))
// FMAXNM: IEEE maxNum (a quiet NaN loses to a number).
static inline float32x4_t vmaxnmq_f32(float32x4_t a, float32x4_t b) FISH_EMU_MAP(float32x4_t, 4, std::fmax(a.v[i], b.v[i]))
static inline float vmaxnmvq_f32(float32x4_t a) { return std::fmax(std::fmax(a.v[0], a.v[1]), std::fmax(a.v[2], a.v[3])); }
// FADDP-based reduction order: (a0 + a1) + (a2 + a3).
static inline float vaddvq_f32(float32x4_t a) { return (a.v[0] + a.v[1]) + (a.v[2] + a.v[3]); }
static inline float32x4_t vcvtq_f32_s32(int32x4_t a) FISH_EMU_MAP(float32x4_t, 4, static_cast<float>(a.v[i]))
static inline float32x4_t vcvtq_f32_u32(uint32x4_t a) FISH_EMU_MAP(float32x4_t, 4, static_cast<float>(a.v[i]))

// FCVTNS: round to nearest, ties to even, saturating to int32 (NaN -> 0).
static inline int32_t fish_emu_fcvtns(float x) {
    if (std::isnan(x)) return 0;
    const double r = std::nearbyint(static_cast<double>(x));   // default mode: ties to even
    if (r >= 2147483647.0) return INT32_MAX;
    if (r <= -2147483648.0) return INT32_MIN;
    return static_cast<int32_t>(r);
}
static inline int32x4_t vcvtnq_s32_f32(float32x4_t a) FISH_EMU_MAP(int32x4_t, 4, fish_emu_fcvtns(a.v[i]))

// FCVTL: IEEE half -> float (exact).
static inline float fish_emu_h2f(uint16_t h) {
    const int s = h >> 15, e = (h >> 10) & 0x1F, m = h & 0x3FF;
    float v;
    if (e == 0) v = std::ldexp(static_cast<float>(m), -24);
    else if (e == 31) v = m ? NAN : INFINITY;
    else v = std::ldexp(static_cast<float>(m | 0x400), e - 25);
    return s ? -v : v;
}
static inline float32x4_t vcvt_f32_f16(float16x4_t a) FISH_EMU_MAP(float32x4_t, 4, fish_emu_h2f(a.v[i]))

#undef FISH_EMU_MAP
