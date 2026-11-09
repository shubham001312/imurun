// FISH — NEON kernel bodies, included twice by quant_arm.cpp.
//
// Not a standalone file. Before each inclusion quant_arm.cpp defines
//   FISH_K        attribute for every function (empty, or the dotprod target)
//   FISH_K_DOT    1: integer dot products use SDOT (vdotq_s32)
//                 0: plain NEON widening multiplies (vmull_s8 + vpadalq_s16)
// inside its own namespace, so the two sets have distinct symbols. The plain
// set must not carry the dotprod target: the compiler may auto-vectorise into
// SDOT anywhere inside a function that has it, and the plain set exists for
// CPUs without the instruction.
//
// Integer sums are exact in both variants (int32 lanes never overflow, see
// the budgets per kernel), so the two sets give identical int32 group sums;
// only the float accumulation order differs from the scalar reference.

// ---------------------------------------------------------------- helpers

// sum(a[i] * b[i]) for 16 signed byte pairs, added into 4 int32 lanes.
// Which lane holds which products differs between the variants; callers only
// ever reduce over all four lanes (or scale them all by the same factor).
FISH_K static inline int32x4_t dot16(int32x4_t acc, int8x16_t a, int8x16_t b) {
#if FISH_K_DOT
    return vdotq_s32(acc, a, b);
#else
    acc = vpadalq_s16(acc, vmull_s8(vget_low_s8(a), vget_low_s8(b)));
    return vpadalq_s16(acc, vmull_high_s8(a, b));
#endif
}

// 32-wide dot: two 16-byte halves.
FISH_K static inline int32x4_t dot32(int8x16_t a0, int8x16_t a1, const int8_t* x) {
    return dot16(dot16(vdupq_n_s32(0), a0, vld1q_s8(x)), a1, vld1q_s8(x + 16));
}

// Half -> float through the FP16 conversion instruction (part of base ARMv8
// NEON). h2f() in quant.cpp is a table lookup in another translation unit.
FISH_K static inline float h2f(uint16_t h) {
    return vgetq_lane_f32(vcvt_f32_f16(vreinterpret_f16_u16(vdup_n_u16(h))), 0);
}

// ---------------------------------------------------------------- weights x f32

// acc += sum over 16 values of (v[i] * scale - offset) * y[i], v unsigned bytes.
FISH_K static inline float32x4_t dot_u8x16_f32(float32x4_t acc, uint8x16_t v, float scale,
                                               float offset, const float* y) {
    const float32x4_t s = vdupq_n_f32(scale);
    const float32x4_t o = vdupq_n_f32(offset);
    const uint16x8_t w0 = vmovl_u8(vget_low_u8(v));
    const uint16x8_t w1 = vmovl_high_u8(v);
    const float32x4_t f0 = vcvtq_f32_u32(vmovl_u16(vget_low_u16(w0)));
    const float32x4_t f1 = vcvtq_f32_u32(vmovl_high_u16(w0));
    const float32x4_t f2 = vcvtq_f32_u32(vmovl_u16(vget_low_u16(w1)));
    const float32x4_t f3 = vcvtq_f32_u32(vmovl_high_u16(w1));
    acc = vfmaq_f32(acc, vsubq_f32(vmulq_f32(f0, s), o), vld1q_f32(y));
    acc = vfmaq_f32(acc, vsubq_f32(vmulq_f32(f1, s), o), vld1q_f32(y + 4));
    acc = vfmaq_f32(acc, vsubq_f32(vmulq_f32(f2, s), o), vld1q_f32(y + 8));
    acc = vfmaq_f32(acc, vsubq_f32(vmulq_f32(f3, s), o), vld1q_f32(y + 12));
    return acc;
}

// acc += sum over 16 values of (v[i] * scale) * y[i], v signed bytes.
FISH_K static inline float32x4_t dot_i8x16_f32(float32x4_t acc, int8x16_t v, float scale,
                                               const float* y) {
    const float32x4_t s = vdupq_n_f32(scale);
    const int16x8_t w0 = vmovl_s8(vget_low_s8(v));
    const int16x8_t w1 = vmovl_high_s8(v);
    const float32x4_t f0 = vcvtq_f32_s32(vmovl_s16(vget_low_s16(w0)));
    const float32x4_t f1 = vcvtq_f32_s32(vmovl_high_s16(w0));
    const float32x4_t f2 = vcvtq_f32_s32(vmovl_s16(vget_low_s16(w1)));
    const float32x4_t f3 = vcvtq_f32_s32(vmovl_high_s16(w1));
    acc = vfmaq_f32(acc, vmulq_f32(f0, s), vld1q_f32(y));
    acc = vfmaq_f32(acc, vmulq_f32(f1, s), vld1q_f32(y + 4));
    acc = vfmaq_f32(acc, vmulq_f32(f2, s), vld1q_f32(y + 8));
    acc = vfmaq_f32(acc, vmulq_f32(f3, s), vld1q_f32(y + 12));
    return acc;
}

FISH_K static float dot_q4_K_f32(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t i = 0; i + 256 <= n; i += 256) {
        const BlockQ4_K* b = reinterpret_cast<const BlockQ4_K*>(p);
        const float d = h2f(b->d);
        const float mn = h2f(b->dmin);
        for (int j = 0; j < 4; ++j) {
            uint8_t sc, m;
            scale_min_k4(2 * j, b->scales, &sc, &m);
            const float s1 = d * static_cast<float>(sc), o1 = mn * static_cast<float>(m);
            scale_min_k4(2 * j + 1, b->scales, &sc, &m);
            const float s2 = d * static_cast<float>(sc), o2 = mn * static_cast<float>(m);
            const uint8x16_t q0 = vld1q_u8(b->qs + j * 32);
            const uint8x16_t q1 = vld1q_u8(b->qs + j * 32 + 16);
            const float* yb = y + i + j * 64;
            acc = dot_u8x16_f32(acc, vandq_u8(q0, mF), s1, o1, yb);
            acc = dot_u8x16_f32(acc, vandq_u8(q1, mF), s1, o1, yb + 16);
            acc = dot_u8x16_f32(acc, vshrq_n_u8(q0, 4), s2, o2, yb + 32);
            acc = dot_u8x16_f32(acc, vshrq_n_u8(q1, 4), s2, o2, yb + 48);
        }
        p += 144;
    }
    return vaddvq_f32(acc);
}

FISH_K static float dot_q6_K_f32(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    const uint8x16_t m30 = vdupq_n_u8(0x30);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t base = 0; base + 256 <= n; base += 256) {
        const BlockQ6_K* b = reinterpret_cast<const BlockQ6_K*>(p);
        const float d = h2f(b->d);
        const uint8_t* ql = b->ql;
        const uint8_t* qh = b->qh;
        const int8_t* sc = b->scales;
        for (int half = 0; half < 2; ++half) {
            const float* yb = y + base + half * 128;
            for (int v = 0; v < 2; ++v) {          // l in [16v, 16v + 16)
                const uint8x16_t la = vld1q_u8(ql + 16 * v);
                const uint8x16_t lb = vld1q_u8(ql + 32 + 16 * v);
                const uint8x16_t h = vld1q_u8(qh + 16 * v);
                // 6-bit values 0..63 (the -32 is folded into the offset)
                const uint8x16_t q1 = vorrq_u8(vandq_u8(la, mF), vandq_u8(vshlq_n_u8(h, 4), m30));
                const uint8x16_t q2 = vorrq_u8(vandq_u8(lb, mF), vandq_u8(vshlq_n_u8(h, 2), m30));
                const uint8x16_t q3 = vorrq_u8(vshrq_n_u8(la, 4), vandq_u8(h, m30));
                const uint8x16_t q4 = vorrq_u8(vshrq_n_u8(lb, 4), vandq_u8(vshrq_n_u8(h, 2), m30));
                const float s1 = d * static_cast<float>(sc[v + 0]);
                const float s2 = d * static_cast<float>(sc[v + 2]);
                const float s3 = d * static_cast<float>(sc[v + 4]);
                const float s4 = d * static_cast<float>(sc[v + 6]);
                acc = dot_u8x16_f32(acc, q1, s1, s1 * 32.0f, yb + 16 * v);
                acc = dot_u8x16_f32(acc, q2, s2, s2 * 32.0f, yb + 32 + 16 * v);
                acc = dot_u8x16_f32(acc, q3, s3, s3 * 32.0f, yb + 64 + 16 * v);
                acc = dot_u8x16_f32(acc, q4, s4, s4 * 32.0f, yb + 96 + 16 * v);
            }
            ql += 64;
            qh += 32;
            sc += 8;
        }
        p += 210;
    }
    return vaddvq_f32(acc);
}

FISH_K static float dot_q8_0_f32(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t i = 0; i + 32 <= n; i += 32) {
        const BlockQ8_0* b = reinterpret_cast<const BlockQ8_0*>(p);
        const float s = h2f(b->d);
        acc = dot_i8x16_f32(acc, vld1q_s8(b->qs), s, y + i);
        acc = dot_i8x16_f32(acc, vld1q_s8(b->qs + 16), s, y + i + 16);
        p += 34;
    }
    return vaddvq_f32(acc);
}

FISH_K static float dot_q4_0_f32(const void* src, const float* y, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t i = 0; i + 32 <= n; i += 32) {
        const BlockQ4_0* b = reinterpret_cast<const BlockQ4_0*>(p);
        const float d = h2f(b->d);
        const uint8x16_t q = vld1q_u8(b->qs);
        acc = dot_u8x16_f32(acc, vandq_u8(q, mF), d, d * 8.0f, y + i);
        acc = dot_u8x16_f32(acc, vshrq_n_u8(q, 4), d, d * 8.0f, y + i + 16);
        p += 18;
    }
    return vaddvq_f32(acc);
}

FISH_K static float dot_f32_neon(const float* a, const float* b, uint64_t n) {
    float32x4_t a0 = vdupq_n_f32(0.0f), a1 = a0, a2 = a0, a3 = a0;
    uint64_t i = 0;
    for (; i + 16 <= n; i += 16) {
        a0 = vfmaq_f32(a0, vld1q_f32(a + i), vld1q_f32(b + i));
        a1 = vfmaq_f32(a1, vld1q_f32(a + i + 4), vld1q_f32(b + i + 4));
        a2 = vfmaq_f32(a2, vld1q_f32(a + i + 8), vld1q_f32(b + i + 8));
        a3 = vfmaq_f32(a3, vld1q_f32(a + i + 12), vld1q_f32(b + i + 12));
    }
    for (; i + 4 <= n; i += 4) a0 = vfmaq_f32(a0, vld1q_f32(a + i), vld1q_f32(b + i));
    float s = vaddvq_f32(vaddq_f32(vaddq_f32(a0, a1), vaddq_f32(a2, a3)));
    for (; i < n; ++i) s += a[i] * b[i];
    return s;
}

// ---------------------------------------------------------------- weights x int8 (32 blocks)
//
// Budgets (int32 lane, worst case): Q4_0 |q-8| <= 8, 8 products per lane per
// block -> 8128; Q8_0 |q| <= 128 -> 129032. A fresh int32 per block, so no
// accumulation across blocks happens in integers.

FISH_K static float dot_q4_0_q8(const void* src, const Q8Act* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    const int8x16_t s8 = vdupq_n_s8(8);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t i = 0; i + 32 <= n; i += 32, ++x) {
        const BlockQ4_0* b = reinterpret_cast<const BlockQ4_0*>(p);
        const uint8x16_t q = vld1q_u8(b->qs);
        // (nibble - 8) is a signed byte, so the -8 offset needs no x->sum term.
        const int8x16_t lo = vsubq_s8(vreinterpretq_s8_u8(vandq_u8(q, mF)), s8);
        const int8x16_t hi = vsubq_s8(vreinterpretq_s8_u8(vshrq_n_u8(q, 4)), s8);
        const int32x4_t sumi = dot32(lo, hi, x->qs);
        acc = vfmaq_f32(acc, vcvtq_f32_s32(sumi), vdupq_n_f32(h2f(b->d) * x->d));
        p += 18;
    }
    return vaddvq_f32(acc);
}

FISH_K static float dot_q8_0_q8(const void* src, const Q8Act* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t i = 0; i + 32 <= n; i += 32, ++x) {
        const BlockQ8_0* b = reinterpret_cast<const BlockQ8_0*>(p);
        const int32x4_t sumi = dot32(vld1q_s8(b->qs), vld1q_s8(b->qs + 16), x->qs);
        acc = vfmaq_f32(acc, vcvtq_f32_s32(sumi), vdupq_n_f32(h2f(b->d) * x->d));
        p += 34;
    }
    return vaddvq_f32(acc);
}

// ---------------------------------------------------------------- K-quants x Q8ActK
//
// One activation scale per 256 values, so a whole super-block accumulates in
// int32 and converts to float once. Budgets (int32 lane, per super-block):
//   Q4_K q <= 15,  8 products/lane/group * 127 * scale 63 * 8 groups -> 7.7e6
//   Q5_K q <= 31   -> 1.6e7
//   Q6_K |q-32| <= 32, 4 products/lane/16-group * 127 * |scale| 128 * 16 -> 3.3e7

// sum over the eight 32-groups of mins[g] * bsums[g] (the Q4_K/Q5_K offset).
FISH_K static inline int32_t k4_mins_dot(const uint8_t* sm, const int16_t* bsums) {
    const int16x8_t mn = vreinterpretq_s16_u16(vmovl_u8(vld1_u8(sm + 8)));
    const int16x8_t bs = vld1q_s16(bsums);
    int32x4_t s = vmull_s16(vget_low_s16(mn), vget_low_s16(bs));
    s = vmlal_high_s16(s, mn, bs);
    return vaddvq_s32(s);
}

FISH_K static float dot_q4_K_q8k(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    float32x4_t acc = vdupq_n_f32(0.0f);
    float minsf = 0.0f;
    for (uint64_t i = 0; i + 256 <= n; i += 256, ++x) {
        const BlockQ4_K* b = reinterpret_cast<const BlockQ4_K*>(p);
        const float d = h2f(b->d) * x->d;
        const float dmin = h2f(b->dmin) * x->d;
        uint8_t sm[16];
        k4_unpack(b->scales, sm);
        minsf += dmin * static_cast<float>(k4_mins_dot(sm, x->bsums));

        int32x4_t isum = vdupq_n_s32(0);
        for (int j = 0; j < 4; ++j) {
            const uint8x16_t q0 = vld1q_u8(b->qs + j * 32);
            const uint8x16_t q1 = vld1q_u8(b->qs + j * 32 + 16);
            const int8_t* xa = x->qs + j * 64;
            const int32x4_t lo = dot32(vreinterpretq_s8_u8(vandq_u8(q0, mF)),
                                       vreinterpretq_s8_u8(vandq_u8(q1, mF)), xa);
            const int32x4_t hi = dot32(vreinterpretq_s8_u8(vshrq_n_u8(q0, 4)),
                                       vreinterpretq_s8_u8(vshrq_n_u8(q1, 4)), xa + 32);
            isum = vmlaq_n_s32(isum, lo, sm[2 * j]);
            isum = vmlaq_n_s32(isum, hi, sm[2 * j + 1]);
        }
        acc = vfmaq_f32(acc, vcvtq_f32_s32(isum), vdupq_n_f32(d));
        p += 144;
    }
    return vaddvq_f32(acc) - minsf;
}

FISH_K static float dot_q5_K_q8k(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    const uint8x16_t m1 = vdupq_n_u8(0x01);
    const uint8x16_t m2 = vdupq_n_u8(0x02);
    float32x4_t acc = vdupq_n_f32(0.0f);
    float minsf = 0.0f;
    for (uint64_t i = 0; i + 256 <= n; i += 256, ++x) {
        const BlockQ5_K* b = reinterpret_cast<const BlockQ5_K*>(p);
        const float d = h2f(b->d) * x->d;
        const float dmin = h2f(b->dmin) * x->d;
        uint8_t sm[16];
        k4_unpack(b->scales, sm);
        minsf += dmin * static_cast<float>(k4_mins_dot(sm, x->bsums));

        // Fifth bit of group pair j: bit 2j of qh[l] for the low nibble, bit
        // 2j+1 for the high one. Shifting qh right by 2 per pair keeps the
        // masks (and the shift immediates) constant.
        uint8x16_t qh0 = vld1q_u8(b->qh);
        uint8x16_t qh1 = vld1q_u8(b->qh + 16);
        int32x4_t isum = vdupq_n_s32(0);
        for (int j = 0; j < 4; ++j) {
            const uint8x16_t q0 = vld1q_u8(b->qs + j * 32);
            const uint8x16_t q1 = vld1q_u8(b->qs + j * 32 + 16);
            const uint8x16_t l0 = vorrq_u8(vandq_u8(q0, mF), vshlq_n_u8(vandq_u8(qh0, m1), 4));
            const uint8x16_t l1 = vorrq_u8(vandq_u8(q1, mF), vshlq_n_u8(vandq_u8(qh1, m1), 4));
            const uint8x16_t h0 = vorrq_u8(vshrq_n_u8(q0, 4), vshlq_n_u8(vandq_u8(qh0, m2), 3));
            const uint8x16_t h1 = vorrq_u8(vshrq_n_u8(q1, 4), vshlq_n_u8(vandq_u8(qh1, m2), 3));
            const int8_t* xa = x->qs + j * 64;
            const int32x4_t lo = dot32(vreinterpretq_s8_u8(l0), vreinterpretq_s8_u8(l1), xa);
            const int32x4_t hi = dot32(vreinterpretq_s8_u8(h0), vreinterpretq_s8_u8(h1), xa + 32);
            isum = vmlaq_n_s32(isum, lo, sm[2 * j]);
            isum = vmlaq_n_s32(isum, hi, sm[2 * j + 1]);
            qh0 = vshrq_n_u8(qh0, 2);
            qh1 = vshrq_n_u8(qh1, 2);
        }
        acc = vfmaq_f32(acc, vcvtq_f32_s32(isum), vdupq_n_f32(d));
        p += 176;
    }
    return vaddvq_f32(acc) - minsf;
}

FISH_K static float dot_q6_K_q8k(const void* src, const Q8ActK* x, uint64_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(src);
    const uint8x16_t mF = vdupq_n_u8(0x0F);
    const uint8x16_t m30 = vdupq_n_u8(0x30);
    const int8x16_t s32 = vdupq_n_s8(32);
    float32x4_t acc = vdupq_n_f32(0.0f);
    for (uint64_t base = 0; base + 256 <= n; base += 256, ++x) {
        const BlockQ6_K* b = reinterpret_cast<const BlockQ6_K*>(p);
        const float d = h2f(b->d) * x->d;
        const uint8_t* ql = b->ql;
        const uint8_t* qh = b->qh;
        const int8_t* sc = b->scales;
        int32x4_t isum = vdupq_n_s32(0);
        for (int half = 0; half < 2; ++half) {
            const int8_t* xa = x->qs + half * 128;
            for (int v = 0; v < 2; ++v) {          // l in [16v, 16v + 16), scale sc[v + 2k]
                const uint8x16_t la = vld1q_u8(ql + 16 * v);
                const uint8x16_t lb = vld1q_u8(ql + 32 + 16 * v);
                const uint8x16_t h = vld1q_u8(qh + 16 * v);
                const uint8x16_t q1 = vorrq_u8(vandq_u8(la, mF), vandq_u8(vshlq_n_u8(h, 4), m30));
                const uint8x16_t q2 = vorrq_u8(vandq_u8(lb, mF), vandq_u8(vshlq_n_u8(h, 2), m30));
                const uint8x16_t q3 = vorrq_u8(vshrq_n_u8(la, 4), vandq_u8(h, m30));
                const uint8x16_t q4 = vorrq_u8(vshrq_n_u8(lb, 4), vandq_u8(vshrq_n_u8(h, 2), m30));
                const int32x4_t z = vdupq_n_s32(0);
                const int32x4_t p1 = dot16(z, vsubq_s8(vreinterpretq_s8_u8(q1), s32), vld1q_s8(xa + 16 * v));
                const int32x4_t p2 = dot16(z, vsubq_s8(vreinterpretq_s8_u8(q2), s32), vld1q_s8(xa + 32 + 16 * v));
                const int32x4_t p3 = dot16(z, vsubq_s8(vreinterpretq_s8_u8(q3), s32), vld1q_s8(xa + 64 + 16 * v));
                const int32x4_t p4 = dot16(z, vsubq_s8(vreinterpretq_s8_u8(q4), s32), vld1q_s8(xa + 96 + 16 * v));
                isum = vmlaq_n_s32(isum, p1, sc[v + 0]);
                isum = vmlaq_n_s32(isum, p2, sc[v + 2]);
                isum = vmlaq_n_s32(isum, p3, sc[v + 4]);
                isum = vmlaq_n_s32(isum, p4, sc[v + 6]);
            }
            ql += 64;
            qh += 32;
            sc += 8;
        }
        acc = vfmaq_f32(acc, vcvtq_f32_s32(isum), vdupq_n_f32(d));
        p += 210;
    }
    return vaddvq_f32(acc);
}

// ---------------------------------------------------------------- activations
//
// Must reproduce quantize_acts_scalar byte for byte: the same amax (max is
// exact), the same d = amax / 127 and id = 1 / d computed in scalar float,
// the same product x * id (one IEEE multiply, no contraction possible), and
// FCVTNS rounds to nearest with ties to even, exactly like lrintf in the
// default rounding mode. The clamp to +-127 matches the scalar clamp.

FISH_K static inline float amax_f32(const float* x, int n) {
    float32x4_t m = vdupq_n_f32(0.0f);
    for (int k = 0; k < n; k += 4) m = vmaxnmq_f32(m, vabsq_f32(vld1q_f32(x + k)));
    return vmaxnmvq_f32(m);
}

// Quantise 32 floats with inverse scale id into out; returns the int sum.
FISH_K static inline int32_t quantize32(const float* x, float id, int8_t* out) {
    const float32x4_t mul = vdupq_n_f32(id);
    const int32x4_t lo = vdupq_n_s32(-127), hi = vdupq_n_s32(127);
    int32x4_t sum = vdupq_n_s32(0);
    for (int k = 0; k < 32; k += 8) {
        int32x4_t a = vcvtnq_s32_f32(vmulq_f32(vld1q_f32(x + k), mul));
        int32x4_t b = vcvtnq_s32_f32(vmulq_f32(vld1q_f32(x + k + 4), mul));
        a = vminq_s32(vmaxq_s32(a, lo), hi);
        b = vminq_s32(vmaxq_s32(b, lo), hi);
        sum = vaddq_s32(sum, vaddq_s32(a, b));
        const int16x8_t h = vcombine_s16(vmovn_s32(a), vmovn_s32(b));
        vst1_s8(out + k, vmovn_s16(h));
    }
    return vaddvq_s32(sum);
}

FISH_K static void quantize_acts_neon(const float* x, uint64_t n, Q8Act* out) {
    for (uint64_t i = 0; i < n; i += 32, ++out) {
        const float amax = amax_f32(x + i, 32);
        const float d = amax / 127.0f;
        const float id = d > 0.0f ? 1.0f / d : 0.0f;
        out->d = d;
        out->sum = quantize32(x + i, id, out->qs);
    }
}

FISH_K static void quantize_acts_k_neon(const float* x, uint64_t n, Q8ActK* out) {
    for (uint64_t i = 0; i < n; i += 256, ++out) {
        const float amax = amax_f32(x + i, 256);
        const float d = amax / 127.0f;
        const float id = d > 0.0f ? 1.0f / d : 0.0f;
        out->d = d;
        for (int b = 0; b < 8; ++b)
            out->bsums[b] = static_cast<int16_t>(quantize32(x + i + b * 32, id, out->qs + b * 32));
    }
}

// ---------------------------------------------------------------- table

static void fill(QuantKernels& k, float (**dot_f32)(const float*, const float*, uint64_t),
                 const char* name) {
    k.q4_K = dot_q4_K_f32;
    k.q6_K = dot_q6_K_f32;
    k.q8_0 = dot_q8_0_f32;
    k.q4_0 = dot_q4_0_f32;
    k.q8_0_q8 = dot_q8_0_q8;
    k.q4_0_q8 = dot_q4_0_q8;
    k.q4_K_q8k = dot_q4_K_q8k;
    k.q5_K_q8k = dot_q5_K_q8k;
    k.q6_K_q8k = dot_q6_K_q8k;
    k.quantize = quantize_acts_neon;
    k.quantize_k = quantize_acts_k_neon;
    k.name = name;
    if (dot_f32) *dot_f32 = dot_f32_neon;
}
