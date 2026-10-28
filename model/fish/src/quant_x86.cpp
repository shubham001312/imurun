/*
 * FISH — AVX2 kernels for the k-quant formats that dominate real models.
 *
 * The translation unit is compiled for baseline x86-64; each kernel carries
 * its own target attribute and is installed only after a runtime CPU check,
 * so the same binary still runs on older machines (and the scalar path stays
 * the correctness oracle).
 *
 * Every kernel computes the dot product while the block is still packed. A
 * 4-bit matrix would expand 8x if it were dequantised to f32 first, which is
 * exactly what a memory-constrained engine must not do.
 */
#include <fish/quant.h>

#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)

#include <immintrin.h>
#include <cstring>

#define FISH_AVX2 __attribute__((target("avx2,fma")))

namespace fish {
namespace {

FISH_AVX2 static inline float hsum8(__m256 v) {
    const __m128 lo = _mm256_castps256_ps128(v);
    const __m128 hi = _mm256_extractf128_ps(v, 1);
    __m128 s = _mm_add_ps(lo, hi);
    s = _mm_hadd_ps(s, s);
    s = _mm_hadd_ps(s, s);
    return _mm_cvtss_f32(s);
}

// 16 unsigned bytes -> sum((byte * scale - offset) * y)
FISH_AVX2 static inline __m256 dot_u8x16(__m128i b, __m256 scale, __m256 offset,
                                         const float* y) {
    const __m256i i0 = _mm256_cvtepu8_epi32(b);
    const __m256i i1 = _mm256_cvtepu8_epi32(_mm_srli_si128(b, 8));
    const __m256 t0 = _mm256_fmsub_ps(_mm256_cvtepi32_ps(i0), scale, offset);
    const __m256 t1 = _mm256_fmsub_ps(_mm256_cvtepi32_ps(i1), scale, offset);
    __m256 acc = _mm256_mul_ps(t0, _mm256_loadu_ps(y));
    acc = _mm256_fmadd_ps(t1, _mm256_loadu_ps(y + 8), acc);
    return acc;
}

// 16 signed bytes -> sum((byte * scale) * y)
FISH_AVX2 static inline __m256 dot_i8x16(__m128i b, __m256 scale, const float* y) {
    const __m256i i0 = _mm256_cvtepi8_epi32(b);
    const __m256i i1 = _mm256_cvtepi8_epi32(_mm_srli_si128(b, 8));
    const __m256 t0 = _mm256_mul_ps(_mm256_cvtepi32_ps(i0), scale);
    const __m256 t1 = _mm256_mul_ps(_mm256_cvtepi32_ps(i1), scale);
    __m256 acc = _mm256_mul_ps(t0, _mm256_loadu_ps(y));
    acc = _mm256_fmadd_ps(t1, _mm256_loadu_ps(y + 8), acc);
    return acc;
}

// 32 unsigned bytes with a possibly different scale/offset in each 16-byte half.
FISH_AVX2 static inline __m256 dot_u8x32_2s(__m256i bytes,
                                            __m256 s_lo, __m256 o_lo,
                                            __m256 s_hi, __m256 o_hi,
                                            const float* y) {
    __m256 acc = dot_u8x16(_mm256_castsi256_si128(bytes), s_lo, o_lo, y);
    acc = _mm256_add_ps(
        acc, dot_u8x16(_mm256_extracti128_si256(bytes, 1), s_hi, o_hi, y + 16));
    return acc;
}

// get_scale_min_k4 from the GGUF 4-bit super-block layout.
inline void scale_min_k4(int j, const uint8_t* q, uint8_t* d, uint8_t* m) {
    if (j < 4) {
        *d = q[j] & 63;
        *m = q[j + 4] & 63;
    } else {
        *d = static_cast<uint8_t>((q[j + 4] & 0x0F) | ((q[j - 4] >> 6) << 4));
        *m = static_cast<uint8_t>((q[j + 4] >> 4) | ((q[j] >> 6) << 4));
    }
}

FISH_AVX2 static float dot_q4_K_avx2(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m256i mask = _mm256_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();

    for (uint64_t i = 0; i + 256 <= n; i += 256) {
        const BlockQ4_K* b = reinterpret_cast<const BlockQ4_K*>(p);
        const float d = fp16_to_fp32(b->d);
        const float mn = fp16_to_fp32(b->dmin);

        for (int j = 0; j < 4; ++j) {
            uint8_t sc, m;
            scale_min_k4(2 * j, b->scales, &sc, &m);
            const __m256 s1 = _mm256_set1_ps(d * static_cast<float>(sc));
            const __m256 o1 = _mm256_set1_ps(mn * static_cast<float>(m));
            scale_min_k4(2 * j + 1, b->scales, &sc, &m);
            const __m256 s2 = _mm256_set1_ps(d * static_cast<float>(sc));
            const __m256 o2 = _mm256_set1_ps(mn * static_cast<float>(m));

            const __m256i q =
                _mm256_loadu_si256(reinterpret_cast<const __m256i*>(b->qs + j * 32));
            const __m256i lo = _mm256_and_si256(q, mask);
            const __m256i hi = _mm256_and_si256(_mm256_srli_epi16(q, 4), mask);

            acc = _mm256_add_ps(acc, dot_u8x32_2s(lo, s1, o1, s1, o1, y + i + j * 64));
            acc = _mm256_add_ps(
                acc, dot_u8x32_2s(hi, s2, o2, s2, o2, y + i + j * 64 + 32));
        }
        p += 144;
    }
    return hsum8(acc);
}

FISH_AVX2 static inline void q6_pair(float d, const int8_t* sc, int a, int b,
                                     __m256& s_lo, __m256& o_lo,
                                     __m256& s_hi, __m256& o_hi) {
    const __m256 thirty_two = _mm256_set1_ps(32.0f);
    s_lo = _mm256_set1_ps(d * static_cast<float>(sc[a]));
    s_hi = _mm256_set1_ps(d * static_cast<float>(sc[b]));
    o_lo = _mm256_mul_ps(s_lo, thirty_two);
    o_hi = _mm256_mul_ps(s_hi, thirty_two);
}

FISH_AVX2 static float dot_q6_K_avx2(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m256i m3 = _mm256_set1_epi8(0x03);
    const __m256i mF = _mm256_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();

    for (uint64_t base = 0; base + 256 <= n; base += 256) {
        const BlockQ6_K* b = reinterpret_cast<const BlockQ6_K*>(p);
        const float d = fp16_to_fp32(b->d);
        const uint8_t* ql = b->ql;
        const uint8_t* qh = b->qh;
        const int8_t* sc = b->scales;

        for (int half = 0; half < 2; ++half) {
            const __m256i ql0 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(ql));
            const __m256i ql1 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(ql + 32));
            const __m256i qh0 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(qh));

            const __m256i lo0 = _mm256_and_si256(ql0, mF);
            const __m256i lo1 = _mm256_and_si256(ql1, mF);
            const __m256i hi0 = _mm256_and_si256(_mm256_srli_epi16(ql0, 4), mF);
            const __m256i hi1 = _mm256_and_si256(_mm256_srli_epi16(ql1, 4), mF);

            const __m256i h0 = _mm256_and_si256(qh0, m3);
            const __m256i h1 = _mm256_and_si256(_mm256_srli_epi16(qh0, 2), m3);
            const __m256i h2 = _mm256_and_si256(_mm256_srli_epi16(qh0, 4), m3);
            const __m256i h3 = _mm256_and_si256(_mm256_srli_epi16(qh0, 6), m3);

            const __m256i q1 = _mm256_or_si256(lo0, _mm256_slli_epi16(h0, 4));
            const __m256i q2 = _mm256_or_si256(lo1, _mm256_slli_epi16(h1, 4));
            const __m256i q3 = _mm256_or_si256(hi0, _mm256_slli_epi16(h2, 4));
            const __m256i q4 = _mm256_or_si256(hi1, _mm256_slli_epi16(h3, 4));

            const float* yb = y + base + half * 128;
            __m256 s_lo, o_lo, s_hi, o_hi;

            q6_pair(d, sc, 0, 1, s_lo, o_lo, s_hi, o_hi);
            acc = _mm256_add_ps(acc, dot_u8x32_2s(q1, s_lo, o_lo, s_hi, o_hi, yb));
            q6_pair(d, sc, 2, 3, s_lo, o_lo, s_hi, o_hi);
            acc = _mm256_add_ps(acc, dot_u8x32_2s(q2, s_lo, o_lo, s_hi, o_hi, yb + 32));
            q6_pair(d, sc, 4, 5, s_lo, o_lo, s_hi, o_hi);
            acc = _mm256_add_ps(acc, dot_u8x32_2s(q3, s_lo, o_lo, s_hi, o_hi, yb + 64));
            q6_pair(d, sc, 6, 7, s_lo, o_lo, s_hi, o_hi);
            acc = _mm256_add_ps(acc, dot_u8x32_2s(q4, s_lo, o_lo, s_hi, o_hi, yb + 96));

            ql += 64;
            qh += 32;
            sc += 8;
        }
        p += 210;
    }
    return hsum8(acc);
}

FISH_AVX2 static float dot_q8_0_avx2(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    __m256 acc = _mm256_setzero_ps();
    for (uint64_t i = 0; i + 32 <= n; i += 32) {
        const BlockQ8_0* b = reinterpret_cast<const BlockQ8_0*>(p);
        const __m256 s = _mm256_set1_ps(fp16_to_fp32(b->d));
        const __m128i q0 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(b->qs));
        const __m128i q1 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(b->qs + 16));
        acc = _mm256_add_ps(acc, dot_i8x16(q0, s, y + i));
        acc = _mm256_add_ps(acc, dot_i8x16(q1, s, y + i + 16));
        p += 34;
    }
    return hsum8(acc);
}

FISH_AVX2 static float dot_q4_0_avx2(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m128i mask = _mm_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();
    for (uint64_t i = 0; i + 32 <= n; i += 32) {
        const BlockQ4_0* b = reinterpret_cast<const BlockQ4_0*>(p);
        const float d = fp16_to_fp32(b->d);
        const __m256 s = _mm256_set1_ps(d);
        const __m256 o = _mm256_set1_ps(d * 8.0f);
        const __m128i q = _mm_loadu_si128(reinterpret_cast<const __m128i*>(b->qs));
        const __m128i lo = _mm_and_si128(q, mask);
        const __m128i hi = _mm_and_si128(_mm_srli_epi16(q, 4), mask);
        acc = _mm256_add_ps(acc, dot_u8x16(lo, s, o, y + i));
        acc = _mm256_add_ps(acc, dot_u8x16(hi, s, o, y + i + 16));
        p += 18;
    }
    return hsum8(acc);
}

FISH_AVX2 static float dot_f32_avx2(const float* a, const float* b, uint64_t n) {
    __m256 acc0 = _mm256_setzero_ps();
    __m256 acc1 = _mm256_setzero_ps();
    uint64_t i = 0;
    for (; i + 16 <= n; i += 16) {
        acc0 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i), _mm256_loadu_ps(b + i), acc0);
        acc1 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i + 8), _mm256_loadu_ps(b + i + 8), acc1);
    }
    float s = hsum8(_mm256_add_ps(acc0, acc1));
    for (; i < n; ++i) s += a[i] * b[i];
    return s;
}

} // namespace

bool fish_x86_avx2() {
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
}

void fish_install_x86(QuantKernels& k,
                      float (**dot_f32)(const float*, const float*, uint64_t)) {
    if (!fish_x86_avx2()) return;
    k.q4_K = dot_q4_K_avx2;
    k.q6_K = dot_q6_K_avx2;
    k.q8_0 = dot_q8_0_avx2;
    k.q4_0 = dot_q4_0_avx2;
    k.name = "avx2";
    if (dot_f32) *dot_f32 = dot_f32_avx2;
}

} // namespace fish

#endif // x86
