/*
 * FISH â€” AVX2 kernels for the formats that dominate real models.
 *
 * The translation unit is compiled for baseline x86-64; each kernel carries
 * its own target attribute and is installed only after a runtime CPU check,
 * so the same binary still runs on older machines (and the scalar path stays
 * the correctness oracle).
 *
 * Two families of kernel live here:
 *
 *   weights x f32      the original path; still used for the reference
 *                      comparison and for the output projection when the
 *                      activations are not quantised.
 *
 *   weights x int8     the fast path. The activations are quantised once per
 *                      matmul to Q8Act (32 values, one scale). The dot
 *                      product then runs on packed bytes: `vpmaddubsw`
 *                      multiplies 32 unsigned weight bytes by 32 signed
 *                      activation bytes and adds adjacent pairs in a single
 *                      instruction, so a 4-bit row never expands to f32.
 *
 * Saturation budget for vpmaddubsw (result is int16, |x| <= 32767):
 *   Q4_0 / Q4_K / Q5_K nibbles <= 31, times 127, times 2 -> 7874    fine
 *   Q6_K |q - 32| <= 32,            times 127, times 2 -> 8128    fine
 *   Q8_0 |w| <= 127 (sign trick),   times 127, times 2 -> 32258   fine
 */
#include <fish/quant.h>

#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)

#include <immintrin.h>
#include <cstring>

#define FISH_AVX2 __attribute__((target("avx2,fma,f16c")))

namespace fish {
namespace {

// Half -> float with the F16C instruction. h2f() lives in another
// translation unit and cannot be inlined into these target-attributed
// functions; two calls per 256-weight block were a third of the block time.
FISH_AVX2 static inline float h2f(uint16_t h) {
    return _mm_cvtss_f32(_mm_cvtph_ps(_mm_cvtsi32_si128(h)));
}

FISH_AVX2 static inline float hsum8(__m256 v) {
    const __m128 lo = _mm256_castps256_ps128(v);
    const __m128 hi = _mm256_extractf128_ps(v, 1);
    __m128 s = _mm_add_ps(lo, hi);
    s = _mm_hadd_ps(s, s);
    s = _mm_hadd_ps(s, s);
    return _mm_cvtss_f32(s);
}

FISH_AVX2 static inline int hsum_i32x8(__m256i v) {
    const __m128i lo = _mm256_castsi256_si128(v);
    const __m128i hi = _mm256_extracti128_si256(v, 1);
    __m128i s = _mm_add_epi32(lo, hi);
    s = _mm_hadd_epi32(s, s);
    s = _mm_hadd_epi32(s, s);
    return _mm_cvtsi128_si32(s);
}

// ============================================================ weights x f32

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
        const float d = h2f(b->d);
        const float mn = h2f(b->dmin);

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
        const float d = h2f(b->d);
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
        const __m256 s = _mm256_set1_ps(h2f(b->d));
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
        const float d = h2f(b->d);
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

// ============================================================ weights x int8

// sum over 32 lanes of (u8 * i8), as 8 x int32 partial sums.
FISH_AVX2 static inline __m256i dot_us8_i8(__m256i us, __m256i s) {
    const __m256i p16 = _mm256_maddubs_epi16(us, s);
    return _mm256_madd_epi16(p16, _mm256_set1_epi16(1));
}

// Same for two signed operands: move the sign of `a` onto `b` so the
// unsigned-by-signed instruction can be used.
FISH_AVX2 static inline __m256i dot_i8_i8(__m256i a, __m256i b) {
    const __m256i ax = _mm256_sign_epi8(a, a);
    const __m256i sy = _mm256_sign_epi8(b, a);
    return dot_us8_i8(ax, sy);
}

FISH_AVX2 static inline __m256i load256(const void* p) {
    return _mm256_loadu_si256(reinterpret_cast<const __m256i*>(p));
}

// acc += (float)sumi * scale
FISH_AVX2 static inline __m256 acc_scaled(__m256 acc, __m256i sumi, float scale) {
    return _mm256_fmadd_ps(_mm256_cvtepi32_ps(sumi), _mm256_set1_ps(scale), acc);
}

FISH_AVX2 static float dot_q4_0_q8_avx2(const void* src, const Q8Act* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m128i mask = _mm_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();
    float mins = 0.0f;
    for (uint64_t i = 0; i + 32 <= n; i += 32, ++x) {
        const BlockQ4_0* b = reinterpret_cast<const BlockQ4_0*>(p);
        const float s = h2f(b->d) * x->d;
        const __m128i q = _mm_loadu_si128(reinterpret_cast<const __m128i*>(b->qs));
        const __m128i lo = _mm_and_si128(q, mask);
        const __m128i hi = _mm_and_si128(_mm_srli_epi16(q, 4), mask);
        const __m256i w = _mm256_set_m128i(hi, lo);
        acc = acc_scaled(acc, dot_us8_i8(w, load256(x->qs)), s);
        mins += s * 8.0f * static_cast<float>(x->sum);
        p += 18;
    }
    return hsum8(acc) - mins;
}

FISH_AVX2 static float dot_q8_0_q8_avx2(const void* src, const Q8Act* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    __m256 acc = _mm256_setzero_ps();
    for (uint64_t i = 0; i + 32 <= n; i += 32, ++x) {
        const BlockQ8_0* b = reinterpret_cast<const BlockQ8_0*>(p);
        const float s = h2f(b->d) * x->d;
        acc = acc_scaled(acc, dot_i8_i8(load256(b->qs), load256(x->qs)), s);
        p += 34;
    }
    return hsum8(acc);
}

// ---- K-quants x Q8ActK: one activation scale per 256, so the whole
// super-block accumulates in int32 and converts to float once.

// Unpack the 12 packed scale bytes of a Q4_K/Q5_K block: bytes 0..7 of the
// result are the eight 6-bit scales, bytes 8..15 the eight 6-bit mins. Pure
// 32-bit bit twiddling, no branches (equivalent to scale_min_k4 for j=0..7).
FISH_AVX2 static inline __m128i k4_unpack(const uint8_t* s) {
    uint32_t u[4];
    std::memcpy(u, s, 12);
    const uint32_t kmask1 = 0x3f3f3f3fu, kmask2 = 0x0f0f0f0fu, kmask3 = 0x03030303u;
    u[3] = ((u[2] >> 4) & kmask2) | (((u[1] >> 6) & kmask3) << 4);
    const uint32_t uaux = u[1] & kmask1;
    u[1] = (u[2] & kmask2) | (((u[0] >> 6) & kmask3) << 4);
    u[2] = uaux;
    u[0] &= kmask1;
    return _mm_loadu_si128(reinterpret_cast<const __m128i*>(u));
}

// Broadcast 16-bit lane `g` of a 128-bit vector (duplicated into both halves
// of `v256`) across all 16 lanes: one byte shuffle with a constant mask.
#define FISH_BCAST16(v256, g) \
    _mm256_shuffle_epi8((v256), _mm256_set1_epi16(static_cast<short>((2 * (g) + 1) * 256 + 2 * (g))))

// sumi += scale16 * (q . x) for one 32-wide group.
FISH_AVX2 static inline __m256i k_group(__m256i sumi, __m256i q_u8, const int8_t* x, __m256i scale16) {
    const __m256i p16 = _mm256_maddubs_epi16(q_u8, load256(x));
    return _mm256_add_epi32(sumi, _mm256_madd_epi16(p16, scale16));
}

FISH_AVX2 static inline int hsum_i32x4(__m128i v) {
    v = _mm_hadd_epi32(v, v);
    v = _mm_hadd_epi32(v, v);
    return _mm_cvtsi128_si32(v);
}

FISH_AVX2 static float dot_q4_K_q8k_avx2(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m256i mF = _mm256_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();
    float minsf = 0.0f;

    for (uint64_t i = 0; i + 256 <= n; i += 256, ++x) {
        const BlockQ4_K* b = reinterpret_cast<const BlockQ4_K*>(p);
        const float d = h2f(b->d) * x->d;
        const float dmin = h2f(b->dmin) * x->d;

        const __m128i sm = k4_unpack(b->scales);
        const __m128i sc16 = _mm_cvtepu8_epi16(sm);
        const __m128i mn16 = _mm_cvtepu8_epi16(_mm_srli_si128(sm, 8));
        const __m256i sc256 = _mm256_broadcastsi128_si256(sc16);
        const __m128i bs = _mm_loadu_si128(reinterpret_cast<const __m128i*>(x->bsums));
        minsf += dmin * static_cast<float>(hsum_i32x4(_mm_madd_epi16(mn16, bs)));

        __m256i sumi = _mm256_setzero_si256();
        const __m256i q0 = load256(b->qs);
        const __m256i q1 = load256(b->qs + 32);
        const __m256i q2 = load256(b->qs + 64);
        const __m256i q3 = load256(b->qs + 96);
        sumi = k_group(sumi, _mm256_and_si256(q0, mF), x->qs, FISH_BCAST16(sc256, 0));
        sumi = k_group(sumi, _mm256_and_si256(_mm256_srli_epi16(q0, 4), mF), x->qs + 32, FISH_BCAST16(sc256, 1));
        sumi = k_group(sumi, _mm256_and_si256(q1, mF), x->qs + 64, FISH_BCAST16(sc256, 2));
        sumi = k_group(sumi, _mm256_and_si256(_mm256_srli_epi16(q1, 4), mF), x->qs + 96, FISH_BCAST16(sc256, 3));
        sumi = k_group(sumi, _mm256_and_si256(q2, mF), x->qs + 128, FISH_BCAST16(sc256, 4));
        sumi = k_group(sumi, _mm256_and_si256(_mm256_srli_epi16(q2, 4), mF), x->qs + 160, FISH_BCAST16(sc256, 5));
        sumi = k_group(sumi, _mm256_and_si256(q3, mF), x->qs + 192, FISH_BCAST16(sc256, 6));
        sumi = k_group(sumi, _mm256_and_si256(_mm256_srli_epi16(q3, 4), mF), x->qs + 224, FISH_BCAST16(sc256, 7));

        acc = _mm256_fmadd_ps(_mm256_cvtepi32_ps(sumi), _mm256_set1_ps(d), acc);
        p += 144;
    }
    return hsum8(acc) - minsf;
}

FISH_AVX2 static float dot_q5_K_q8k_avx2(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m256i mF = _mm256_set1_epi8(0x0F);
    const __m256i m1 = _mm256_set1_epi8(0x01);
    __m256 acc = _mm256_setzero_ps();
    float minsf = 0.0f;

    for (uint64_t i = 0; i + 256 <= n; i += 256, ++x) {
        const BlockQ5_K* b = reinterpret_cast<const BlockQ5_K*>(p);
        const float d = h2f(b->d) * x->d;
        const float dmin = h2f(b->dmin) * x->d;

        const __m128i sm = k4_unpack(b->scales);
        const __m128i sc16 = _mm_cvtepu8_epi16(sm);
        const __m128i mn16 = _mm_cvtepu8_epi16(_mm_srli_si128(sm, 8));
        const __m256i sc256 = _mm256_broadcastsi128_si256(sc16);
        const __m128i bs = _mm_loadu_si128(reinterpret_cast<const __m128i*>(x->bsums));
        minsf += dmin * static_cast<float>(hsum_i32x4(_mm_madd_epi16(mn16, bs)));

        const __m256i qh = load256(b->qh);
        __m256i sumi = _mm256_setzero_si256();
        for (int j = 0; j < 4; ++j) {
            const __m256i q = load256(b->qs + j * 32);
            // fifth bit: bit (2j) of qh for the low nibble, bit (2j+1) for the high.
            const __m256i h_lo = _mm256_slli_epi16(_mm256_and_si256(_mm256_srli_epi16(qh, 2 * j), m1), 4);
            const __m256i h_hi = _mm256_slli_epi16(_mm256_and_si256(_mm256_srli_epi16(qh, 2 * j + 1), m1), 4);
            const __m256i lo = _mm256_or_si256(_mm256_and_si256(q, mF), h_lo);
            const __m256i hi = _mm256_or_si256(_mm256_and_si256(_mm256_srli_epi16(q, 4), mF), h_hi);
            // the shuffle masks must be compile-time constants: unroll j by hand
            __m256i s_lo, s_hi;
            switch (j) {
                case 0:  s_lo = FISH_BCAST16(sc256, 0); s_hi = FISH_BCAST16(sc256, 1); break;
                case 1:  s_lo = FISH_BCAST16(sc256, 2); s_hi = FISH_BCAST16(sc256, 3); break;
                case 2:  s_lo = FISH_BCAST16(sc256, 4); s_hi = FISH_BCAST16(sc256, 5); break;
                default: s_lo = FISH_BCAST16(sc256, 6); s_hi = FISH_BCAST16(sc256, 7); break;
            }
            sumi = k_group(sumi, lo, x->qs + j * 64, s_lo);
            sumi = k_group(sumi, hi, x->qs + j * 64 + 32, s_hi);
        }
        acc = _mm256_fmadd_ps(_mm256_cvtepi32_ps(sumi), _mm256_set1_ps(d), acc);
        p += 176;
    }
    return hsum8(acc) - minsf;
}

// One 32-wide Q6_K group: q holds values 0..63; `scales` carries sc_a in the
// low 128 bits (first 16 values) and sc_b in the high (last 16).
FISH_AVX2 static inline __m256i q6_group(__m256i sumi, __m256i q, __m256i scales, const int8_t* x) {
    const __m256i qs = _mm256_sub_epi8(q, _mm256_set1_epi8(32));
    const __m256i xq = load256(x);
    const __m256i p16 = _mm256_maddubs_epi16(_mm256_sign_epi8(qs, qs), _mm256_sign_epi8(xq, qs));
    return _mm256_add_epi32(sumi, _mm256_madd_epi16(p16, scales));
}

// [sc[2g] x8 | sc[2g+1] x8] from a 256-bit vector holding the 8 int16 scales
// of one half-block in both 128-bit lanes.
#define FISH_Q6_SCALES(v256, g) \
    _mm256_shuffle_epi8((v256), _mm256_set_m128i( \
        _mm_set1_epi16(static_cast<short>((4 * (g) + 3) * 256 + 4 * (g) + 2)), \
        _mm_set1_epi16(static_cast<short>((4 * (g) + 1) * 256 + 4 * (g)))))

FISH_AVX2 static float dot_q6_K_q8k_avx2(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const __m256i m3 = _mm256_set1_epi8(0x03);
    const __m256i mF = _mm256_set1_epi8(0x0F);
    __m256 acc = _mm256_setzero_ps();

    for (uint64_t base = 0; base + 256 <= n; base += 256, ++x) {
        const BlockQ6_K* b = reinterpret_cast<const BlockQ6_K*>(p);
        const float d = h2f(b->d) * x->d;
        const uint8_t* ql = b->ql;
        const uint8_t* qh = b->qh;
        const int8_t* sc = b->scales;
        __m256i sumi = _mm256_setzero_si256();

        for (int half = 0; half < 2; ++half) {
            const __m256i sc256 = _mm256_broadcastsi128_si256(
                _mm_cvtepi8_epi16(_mm_loadl_epi64(reinterpret_cast<const __m128i*>(sc))));
            const __m256i ql0 = load256(ql);
            const __m256i ql1 = load256(ql + 32);
            const __m256i qh0 = load256(qh);

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

            const int8_t* xa = x->qs + half * 128;
            sumi = q6_group(sumi, q1, FISH_Q6_SCALES(sc256, 0), xa);
            sumi = q6_group(sumi, q2, FISH_Q6_SCALES(sc256, 1), xa + 32);
            sumi = q6_group(sumi, q3, FISH_Q6_SCALES(sc256, 2), xa + 64);
            sumi = q6_group(sumi, q4, FISH_Q6_SCALES(sc256, 3), xa + 96);

            ql += 64;
            qh += 32;
            sc += 8;
        }
        acc = _mm256_fmadd_ps(_mm256_cvtepi32_ps(sumi), _mm256_set1_ps(d), acc);
        p += 210;
    }
    return hsum8(acc);
}

// ============================================================ activations

FISH_AVX2 static void quantize_acts_avx2(const float* x, uint64_t n, Q8Act* out) {
    const __m256 sign_mask = _mm256_castsi256_ps(_mm256_set1_epi32(0x7FFFFFFF));
    const __m256i perm = _mm256_setr_epi32(0, 4, 1, 5, 2, 6, 3, 7);
    for (uint64_t i = 0; i < n; i += 32, ++out) {
        const __m256 v0 = _mm256_loadu_ps(x + i);
        const __m256 v1 = _mm256_loadu_ps(x + i + 8);
        const __m256 v2 = _mm256_loadu_ps(x + i + 16);
        const __m256 v3 = _mm256_loadu_ps(x + i + 24);

        __m256 mx = _mm256_max_ps(_mm256_and_ps(v0, sign_mask), _mm256_and_ps(v1, sign_mask));
        mx = _mm256_max_ps(mx, _mm256_and_ps(v2, sign_mask));
        mx = _mm256_max_ps(mx, _mm256_and_ps(v3, sign_mask));
        __m128 m4 = _mm_max_ps(_mm256_castps256_ps128(mx), _mm256_extractf128_ps(mx, 1));
        m4 = _mm_max_ps(m4, _mm_movehl_ps(m4, m4));
        m4 = _mm_max_ss(m4, _mm_movehdup_ps(m4));
        const float amax = _mm_cvtss_f32(m4);

        const float d = amax / 127.0f;
        const float id = d > 0.0f ? 1.0f / d : 0.0f;
        out->d = d;
        const __m256 mul = _mm256_set1_ps(id);

        __m256i i0 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(v0, mul), _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC));
        __m256i i1 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(v1, mul), _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC));
        __m256i i2 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(v2, mul), _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC));
        __m256i i3 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(v3, mul), _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC));

        const __m256i sum = _mm256_add_epi32(_mm256_add_epi32(i0, i1), _mm256_add_epi32(i2, i3));
        out->sum = hsum_i32x8(sum);

        // packs saturate to int8 (values are already within -127..127).
        const __m256i p01 = _mm256_packs_epi32(i0, i1);
        const __m256i p23 = _mm256_packs_epi32(i2, i3);
        __m256i bytes = _mm256_packs_epi16(p01, p23);
        bytes = _mm256_permutevar8x32_epi32(bytes, perm);
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(out->qs), bytes);
    }
}

// Quantise 32 floats with a given inverse scale; returns the int sum.
FISH_AVX2 static inline int quantize32(const float* x, __m256 mul, int8_t* out) {
    const __m256i perm = _mm256_setr_epi32(0, 4, 1, 5, 2, 6, 3, 7);
    const int rnd = _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC;
    const __m256i i0 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(_mm256_loadu_ps(x), mul), rnd));
    const __m256i i1 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(_mm256_loadu_ps(x + 8), mul), rnd));
    const __m256i i2 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(_mm256_loadu_ps(x + 16), mul), rnd));
    const __m256i i3 = _mm256_cvtps_epi32(_mm256_round_ps(_mm256_mul_ps(_mm256_loadu_ps(x + 24), mul), rnd));
    const __m256i sum = _mm256_add_epi32(_mm256_add_epi32(i0, i1), _mm256_add_epi32(i2, i3));
    __m256i bytes = _mm256_packs_epi16(_mm256_packs_epi32(i0, i1), _mm256_packs_epi32(i2, i3));
    bytes = _mm256_permutevar8x32_epi32(bytes, perm);
    _mm256_storeu_si256(reinterpret_cast<__m256i*>(out), bytes);
    return hsum_i32x8(sum);
}

FISH_AVX2 static void quantize_acts_k_avx2(const float* x, uint64_t n, Q8ActK* out) {
    const __m256 sign_mask = _mm256_castsi256_ps(_mm256_set1_epi32(0x7FFFFFFF));
    for (uint64_t i = 0; i < n; i += 256, ++out) {
        __m256 mx = _mm256_setzero_ps();
        for (int k = 0; k < 256; k += 8)
            mx = _mm256_max_ps(mx, _mm256_and_ps(_mm256_loadu_ps(x + i + k), sign_mask));
        __m128 m4 = _mm_max_ps(_mm256_castps256_ps128(mx), _mm256_extractf128_ps(mx, 1));
        m4 = _mm_max_ps(m4, _mm_movehl_ps(m4, m4));
        m4 = _mm_max_ss(m4, _mm_movehdup_ps(m4));
        const float amax = _mm_cvtss_f32(m4);
        const float d = amax / 127.0f;
        const float id = d > 0.0f ? 1.0f / d : 0.0f;
        out->d = d;
        const __m256 mul = _mm256_set1_ps(id);
        for (int b = 0; b < 8; ++b)
            out->bsums[b] = static_cast<int16_t>(quantize32(x + i + b * 32, mul, out->qs + b * 32));
    }
}

} // namespace

bool fish_x86_avx2() {
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma") &&
           __builtin_cpu_supports("f16c");
}

void fish_install_x86(QuantKernels& k,
                      float (**dot_f32)(const float*, const float*, uint64_t)) {
    if (!fish_x86_avx2()) return;
    k.q4_K = dot_q4_K_avx2;
    k.q6_K = dot_q6_K_avx2;
    k.q8_0 = dot_q8_0_avx2;
    k.q4_0 = dot_q4_0_avx2;
    k.q8_0_q8 = dot_q8_0_q8_avx2;
    k.q4_0_q8 = dot_q4_0_q8_avx2;
    k.q4_K_q8k = dot_q4_K_q8k_avx2;
    k.q5_K_q8k = dot_q5_K_q8k_avx2;
    k.q6_K_q8k = dot_q6_K_q8k_avx2;
    k.quantize = quantize_acts_avx2;
    k.quantize_k = quantize_acts_k_avx2;
    k.name = "avx2";
    if (dot_f32) *dot_f32 = dot_f32_avx2;
}

} // namespace fish

#endif // x86
