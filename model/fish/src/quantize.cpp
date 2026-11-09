/*
 * FISH — writing quantised blocks (the inverse of quant.cpp).
 *
 * Used by the LoRA merge at load (src/lora.cpp). Two encoders per type:
 *
 *   grid    keep the source block's scales, re-round every code to the
 *           nearest level of the new value. This is what makes merging into
 *           an already quantised tensor safe: a typical LoRA delta is far
 *           smaller than one quantisation step, and a fresh quantisation of
 *           (dequantised base + delta) would add a whole new rounding error
 *           to the base weights — more error than the delta it is trying to
 *           apply. With the scales kept, the old code is always one of the
 *           candidates, so the merged block is never farther from the target
 *           than the unmerged one.
 *   refit   a fresh quantisation, for deltas big enough to push values out
 *           of the block's range.
 *
 * merge_row() writes whichever is closer to the target, per block.
 *
 * The K-quant fit follows the same simple min/max + least-squares scheme as
 * the GPU re-quantiser (src/gpu/requant.cpp); see the note there on why the
 * grid search of the reference quantiser was not worth its cost.
 */
#include <fish/quant.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace fish {

uint16_t fp32_to_fp16(float f) {
    uint32_t x;
    std::memcpy(&x, &f, 4);
    const uint32_t sign = (x >> 16) & 0x8000u;
    const int32_t e = static_cast<int32_t>((x >> 23) & 0xFF);
    uint32_t m = x & 0x7FFFFFu;
    if (e == 255) return static_cast<uint16_t>(sign | 0x7C00u | (m ? 0x200u : 0));
    const int32_t he = e - 127 + 15;
    if (he >= 31) return static_cast<uint16_t>(sign | 0x7C00u);
    if (he <= 0) {
        if (he < -10) return static_cast<uint16_t>(sign);
        m |= 0x800000u;
        const int shift = 14 - he;
        uint32_t hm = m >> shift;
        const uint32_t rem = m & ((1u << shift) - 1), half = 1u << (shift - 1);
        if (rem > half || (rem == half && (hm & 1u))) ++hm;
        return static_cast<uint16_t>(sign | hm);
    }
    const uint32_t hm = m >> 13;
    const uint32_t rem = m & 0x1FFFu;
    uint32_t out = sign | (static_cast<uint32_t>(he) << 10) | hm;
    if (rem > 0x1000u || (rem == 0x1000u && (hm & 1u))) ++out;   // carry into the exponent is correct
    return static_cast<uint16_t>(out);
}

namespace {

inline uint16_t f32_to_bf16(float f) {
    uint32_t x;
    std::memcpy(&x, &f, 4);
    if ((x & 0x7F800000u) == 0x7F800000u && (x & 0x7FFFFFu))
        return static_cast<uint16_t>((x >> 16) | 0x40u);          // quiet NaN
    const uint32_t lsb = (x >> 16) & 1u;
    return static_cast<uint16_t>((x + 0x7FFFu + lsb) >> 16);
}

inline int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
inline int nearest(float v) { return static_cast<int>(std::lround(v)); }

// ---------------------------------------------------------------- block grids
//
// For one block: per element, value = s[i] * q[i] + o[i] with q[i] in
// [qmin, qmax]. unpack() reads codes and levels; pack() overwrites the codes
// of a block whose scale bytes are already in place.

constexpr int kMaxQK = 256;

struct Grid {
    int q[kMaxQK];
    float s[kMaxQK];
    float o[kMaxQK];
};

// get_scale_min_k4 of the K-quant 12-byte scale table.
inline void scale_min_k4(int j, const uint8_t* q, int* d, int* m) {
    if (j < 4) { *d = q[j] & 63; *m = q[j + 4] & 63; }
    else {
        *d = (q[j + 4] & 0x0F) | ((q[j - 4] >> 6) << 4);
        *m = (q[j + 4] >> 4) | ((q[j] >> 6) << 4);
    }
}

void pack_scales_k4(const int* ls, const int* lm, uint8_t* s12) {
    std::memset(s12, 0, 12);
    for (int j = 0; j < 8; ++j) {
        if (j < 4) {
            s12[j] |= static_cast<uint8_t>(ls[j] & 63);
            s12[j + 4] |= static_cast<uint8_t>(lm[j] & 63);
        } else {
            s12[j + 4] = static_cast<uint8_t>((ls[j] & 0xF) | ((lm[j] & 0xF) << 4));
            s12[j - 4] |= static_cast<uint8_t>((ls[j] >> 4) << 6);
            s12[j] |= static_cast<uint8_t>((lm[j] >> 4) << 6);
        }
    }
}

// --- Q8_0: value = d * q, q int8
void unpack_q8_0(const uint8_t* p, Grid& g) {
    const auto* b = reinterpret_cast<const BlockQ8_0*>(p);
    const float d = fp16_to_fp32(b->d);
    for (int i = 0; i < 32; ++i) { g.q[i] = b->qs[i]; g.s[i] = d; g.o[i] = 0.0f; }
}
void pack_q8_0(uint8_t* p, const int* q) {
    auto* b = reinterpret_cast<BlockQ8_0*>(p);
    for (int i = 0; i < 32; ++i) b->qs[i] = static_cast<int8_t>(q[i]);
}
void refit_q8_0(const float* x, uint8_t* p) {
    auto* b = reinterpret_cast<BlockQ8_0*>(p);
    float amax = 0.0f;
    for (int i = 0; i < 32; ++i) amax = std::max(amax, std::fabs(x[i]));
    b->d = fp32_to_fp16(amax / 127.0f);
    const float d = fp16_to_fp32(b->d);
    for (int i = 0; i < 32; ++i) b->qs[i] = static_cast<int8_t>(d > 0 ? clampi(nearest(x[i] / d), -128, 127) : 0);
}

// --- Q4_0: value = d * (q - 8), q in 0..15; qs[j] low = elem j, high = elem j+16
void unpack_q4_0(const uint8_t* p, Grid& g) {
    const auto* b = reinterpret_cast<const BlockQ4_0*>(p);
    const float d = fp16_to_fp32(b->d);
    for (int j = 0; j < 16; ++j) {
        g.q[j] = b->qs[j] & 0x0F;
        g.q[j + 16] = b->qs[j] >> 4;
    }
    for (int i = 0; i < 32; ++i) { g.s[i] = d; g.o[i] = -8.0f * d; }
}
void pack_q4_0(uint8_t* p, const int* q) {
    auto* b = reinterpret_cast<BlockQ4_0*>(p);
    for (int j = 0; j < 16; ++j) b->qs[j] = static_cast<uint8_t>(q[j] | (q[j + 16] << 4));
}
void refit_q4_0(const float* x, uint8_t* p) {
    auto* b = reinterpret_cast<BlockQ4_0*>(p);
    // The reference rule: the signed value of largest magnitude maps to -8.
    float amax = 0.0f, mx = 0.0f;
    for (int i = 0; i < 32; ++i) if (std::fabs(x[i]) > amax) { amax = std::fabs(x[i]); mx = x[i]; }
    b->d = fp32_to_fp16(mx / -8.0f);
    const float d = fp16_to_fp32(b->d);
    int q[32];
    for (int i = 0; i < 32; ++i) q[i] = d != 0.0f ? clampi(nearest(x[i] / d) + 8, 0, 15) : 8;
    pack_q4_0(p, q);
}

// --- Q4_K / Q5_K: value = d*sc[g] * q - dmin*m[g], 8 groups of 32
void unpack_k45(const uint8_t* scales, uint16_t dh, uint16_t dminh, Grid& g) {
    const float d = fp16_to_fp32(dh), dmin = fp16_to_fp32(dminh);
    for (int grp = 0; grp < 8; ++grp) {
        int sc, m;
        scale_min_k4(grp, scales, &sc, &m);
        const float s = d * static_cast<float>(sc), o = -(dmin * static_cast<float>(m));
        for (int l = 0; l < 32; ++l) { g.s[32 * grp + l] = s; g.o[32 * grp + l] = o; }
    }
}
void unpack_q4_K(const uint8_t* p, Grid& g) {
    const auto* b = reinterpret_cast<const BlockQ4_K*>(p);
    unpack_k45(b->scales, b->d, b->dmin, g);
    for (int j = 0; j < 4; ++j)
        for (int l = 0; l < 32; ++l) {
            g.q[64 * j + l] = b->qs[32 * j + l] & 0x0F;
            g.q[64 * j + 32 + l] = b->qs[32 * j + l] >> 4;
        }
}
void pack_q4_K(uint8_t* p, const int* q) {
    auto* b = reinterpret_cast<BlockQ4_K*>(p);
    for (int j = 0; j < 4; ++j)
        for (int l = 0; l < 32; ++l)
            b->qs[32 * j + l] = static_cast<uint8_t>(q[64 * j + l] | (q[64 * j + 32 + l] << 4));
}
void unpack_q5_K(const uint8_t* p, Grid& g) {
    const auto* b = reinterpret_cast<const BlockQ5_K*>(p);
    unpack_k45(b->scales, b->d, b->dmin, g);
    for (int j = 0; j < 4; ++j) {
        const uint8_t u1 = static_cast<uint8_t>(1u << (2 * j)), u2 = static_cast<uint8_t>(2u << (2 * j));
        for (int l = 0; l < 32; ++l) {
            g.q[64 * j + l] = (b->qs[32 * j + l] & 0x0F) + ((b->qh[l] & u1) ? 16 : 0);
            g.q[64 * j + 32 + l] = (b->qs[32 * j + l] >> 4) + ((b->qh[l] & u2) ? 16 : 0);
        }
    }
}
void pack_q5_K(uint8_t* p, const int* q) {
    auto* b = reinterpret_cast<BlockQ5_K*>(p);
    std::memset(b->qh, 0, sizeof(b->qh));
    for (int j = 0; j < 4; ++j) {
        const uint8_t u1 = static_cast<uint8_t>(1u << (2 * j)), u2 = static_cast<uint8_t>(2u << (2 * j));
        for (int l = 0; l < 32; ++l) {
            const int lo = q[64 * j + l], hi = q[64 * j + 32 + l];
            b->qs[32 * j + l] = static_cast<uint8_t>((lo & 0x0F) | ((hi & 0x0F) << 4));
            if (lo & 16) b->qh[l] |= u1;
            if (hi & 16) b->qh[l] |= u2;
        }
    }
}

// Fit x ~ scale * q - m over one group of 32, q in [0, nmax], m >= 0.
void fit_group(const float* x, int nmax, float* scale, float* mout) {
    float xmin = 0.0f, xmax = x[0];
    for (int i = 0; i < 32; ++i) { xmin = std::min(xmin, x[i]); xmax = std::max(xmax, x[i]); }
    if (xmax <= xmin) { *scale = 0.0f; *mout = -xmin; return; }
    float sc = (xmax - xmin) / static_cast<float>(nmax), b = xmin;
    for (int it = 0; it < 4; ++it) {
        double sq = 0, sqq = 0, sx = 0, sqx = 0;
        const float inv = 1.0f / sc;
        for (int i = 0; i < 32; ++i) {
            const int q = clampi(nearest((x[i] - b) * inv), 0, nmax);
            sq += q; sqq += static_cast<double>(q) * q; sx += x[i]; sqx += static_cast<double>(q) * x[i];
        }
        const double det = 32.0 * sqq - sq * sq;
        if (det <= 0) break;
        double nsc = (32.0 * sqx - sq * sx) / det;
        double nb = (sx - nsc * sq) / 32.0;
        if (nb > 0) { nb = 0; nsc = sqq > 0 ? sqx / sqq : 0; }
        if (!(nsc > 0)) break;
        sc = static_cast<float>(nsc);
        b = static_cast<float>(nb);
    }
    *scale = sc;
    *mout = -b;
}

// Fresh K-quant super-block (Q4_K nmax 15, Q5_K nmax 31): fills d, dmin,
// the 12-byte scale table and the codes.
void refit_k45(const float* x, int nmax, uint16_t* dh, uint16_t* dminh, uint8_t* s12, int* q) {
    float sc[8], mn[8], maxsc = 0.0f, maxm = 0.0f;
    for (int g = 0; g < 8; ++g) {
        fit_group(x + 32 * g, nmax, &sc[g], &mn[g]);
        maxsc = std::max(maxsc, sc[g]);
        maxm = std::max(maxm, mn[g]);
    }
    *dh = fp32_to_fp16(maxsc / 63.0f);
    *dminh = fp32_to_fp16(maxm / 63.0f);
    const float dd = fp16_to_fp32(*dh), dm = fp16_to_fp32(*dminh);
    int ls[8], lm[8];
    for (int g = 0; g < 8; ++g) {
        ls[g] = dd > 0 ? clampi(nearest(sc[g] / dd), 0, 63) : 0;
        lm[g] = dm > 0 ? clampi(nearest(mn[g] / dm), 0, 63) : 0;
        const float s = dd * static_cast<float>(ls[g]), m = dm * static_cast<float>(lm[g]);
        for (int i = 0; i < 32; ++i)
            q[32 * g + i] = s > 0 ? clampi(nearest((x[32 * g + i] + m) / s), 0, nmax) : 0;
    }
    pack_scales_k4(ls, lm, s12);
}
void refit_q4_K(const float* x, uint8_t* p) {
    auto* b = reinterpret_cast<BlockQ4_K*>(p);
    int q[256];
    refit_k45(x, 15, &b->d, &b->dmin, b->scales, q);
    pack_q4_K(p, q);
}
void refit_q5_K(const float* x, uint8_t* p) {
    auto* b = reinterpret_cast<BlockQ5_K*>(p);
    int q[256];
    refit_k45(x, 31, &b->d, &b->dmin, b->scales, q);
    pack_q5_K(p, q);
}

// --- Q6_K: value = d * sc[g] * (q - 32), q in 0..63, 16 groups of 16.
// Element 128n + 32r + l (n half, r quarter, l < 32): low 4 bits in
// ql[64n + l + 32*(r&1)] (low nibble for r < 2, high for r >= 2), high 2 bits
// in qh[32n + l] bits 2r..2r+1, scale sc[8n + l/16 + 2r].
void unpack_q6_K(const uint8_t* p, Grid& g) {
    const auto* b = reinterpret_cast<const BlockQ6_K*>(p);
    const float d = fp16_to_fp32(b->d);
    for (int n = 0; n < 2; ++n)
        for (int r = 0; r < 4; ++r)
            for (int l = 0; l < 32; ++l) {
                const int e = 128 * n + 32 * r + l;
                const uint8_t lq = b->ql[64 * n + l + 32 * (r & 1)];
                const int lo = (r < 2) ? (lq & 0x0F) : (lq >> 4);
                const int hi = (b->qh[32 * n + l] >> (2 * r)) & 3;
                g.q[e] = lo | (hi << 4);
                g.s[e] = d * static_cast<float>(b->scales[8 * n + l / 16 + 2 * r]);
                g.o[e] = -32.0f * g.s[e];
            }
}
void pack_q6_K(uint8_t* p, const int* q) {
    auto* b = reinterpret_cast<BlockQ6_K*>(p);
    std::memset(b->ql, 0, sizeof(b->ql));
    std::memset(b->qh, 0, sizeof(b->qh));
    for (int n = 0; n < 2; ++n)
        for (int r = 0; r < 4; ++r)
            for (int l = 0; l < 32; ++l) {
                const int v = q[128 * n + 32 * r + l];
                uint8_t& lq = b->ql[64 * n + l + 32 * (r & 1)];
                lq = static_cast<uint8_t>(lq | ((r < 2) ? (v & 0x0F) : ((v & 0x0F) << 4)));
                b->qh[32 * n + l] = static_cast<uint8_t>(b->qh[32 * n + l] | (((v >> 4) & 3) << (2 * r)));
            }
}
// Group index of element e in the Q6_K scale table.
inline int q6_group(int e) {
    const int n = e / 128, r = (e % 128) / 32, l = e % 32;
    return 8 * n + l / 16 + 2 * r;
}
void refit_q6_K(const float* x, uint8_t* p) {
    auto* b = reinterpret_cast<BlockQ6_K*>(p);
    // Per group of 16 (in scale-table order): signed max-magnitude value maps
    // to -32, then one least-squares refinement of the group scale.
    float gscale[16];
    int members[16][16];
    int count[16] = {0};
    for (int e = 0; e < 256; ++e) {
        const int g = q6_group(e);
        members[g][count[g]++] = e;
    }
    float maxs = 0.0f, maxs_signed = 0.0f;
    for (int g = 0; g < 16; ++g) {
        float amax = 0.0f, mx = 0.0f;
        for (int k = 0; k < 16; ++k) {
            const float v = x[members[g][k]];
            if (std::fabs(v) > amax) { amax = std::fabs(v); mx = v; }
        }
        float s = mx / -32.0f;
        if (s != 0.0f) {
            double sqx = 0, sqq = 0;
            for (int k = 0; k < 16; ++k) {
                const int q = clampi(nearest(x[members[g][k]] / s), -32, 31);
                sqx += static_cast<double>(q) * x[members[g][k]];
                sqq += static_cast<double>(q) * q;
            }
            if (sqq > 0) {
                const float ls = static_cast<float>(sqx / sqq);
                if (ls != 0.0f) s = ls;
            }
        }
        gscale[g] = s;
        if (std::fabs(s) > maxs) { maxs = std::fabs(s); maxs_signed = s; }
    }
    // Group scales are int8 under one f16 super-scale.
    const float iscale = maxs_signed != 0.0f ? -128.0f / maxs_signed : 0.0f;
    b->d = fp32_to_fp16(iscale != 0.0f ? 1.0f / iscale : 0.0f);
    const float d = fp16_to_fp32(b->d);
    for (int g = 0; g < 16; ++g)
        b->scales[g] = static_cast<int8_t>(clampi(nearest(iscale * gscale[g]), -128, 127));
    int q[256];
    for (int e = 0; e < 256; ++e) {
        const float s = d * static_cast<float>(b->scales[q6_group(e)]);
        q[e] = s != 0.0f ? clampi(nearest(x[e] / s), -32, 31) + 32 : 32;
    }
    pack_q6_K(p, q);
}

struct Codec {
    int qk;
    size_t bytes;
    int qmin, qmax;
    void (*unpack)(const uint8_t*, Grid&);
    void (*pack)(uint8_t*, const int*);
    void (*refit)(const float*, uint8_t*);
};

const Codec* codec_for(GgmlType t) {
    static const Codec q8_0{32, sizeof(BlockQ8_0), -128, 127, unpack_q8_0, pack_q8_0, refit_q8_0};
    static const Codec q4_0{32, sizeof(BlockQ4_0), 0, 15, unpack_q4_0, pack_q4_0, refit_q4_0};
    static const Codec q4_k{256, sizeof(BlockQ4_K), 0, 15, unpack_q4_K, pack_q4_K, refit_q4_K};
    static const Codec q5_k{256, sizeof(BlockQ5_K), 0, 31, unpack_q5_K, pack_q5_K, refit_q5_K};
    static const Codec q6_k{256, sizeof(BlockQ6_K), 0, 63, unpack_q6_K, pack_q6_K, refit_q6_K};
    switch (t) {
        case GgmlType::Q8_0: return &q8_0;
        case GgmlType::Q4_0: return &q4_0;
        case GgmlType::Q4_K: return &q4_k;
        case GgmlType::Q5_K: return &q5_k;
        case GgmlType::Q6_K: return &q6_k;
        default: return nullptr;
    }
}

inline uint64_t mix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ull;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
    return x ^ (x >> 31);
}
// Uniform in [0, 1) from (seed, index).
inline float uniform01(uint64_t seed, uint64_t i) {
    return static_cast<float>(mix64(seed ^ mix64(i)) >> 40) * (1.0f / 16777216.0f);
}

inline double sse(const float* a, const float* b, int n) {
    double s = 0;
    for (int i = 0; i < n; ++i) { const double d = static_cast<double>(a[i]) - b[i]; s += d * d; }
    return s;
}

} // namespace

bool quantize_supported(GgmlType type) {
    return type == GgmlType::F32 || type == GgmlType::F16 || type == GgmlType::BF16 ||
           codec_for(type) != nullptr;
}

const char* quantize_supported_list() { return "F32, F16, BF16, Q8_0, Q4_0, Q4_K, Q5_K, Q6_K"; }

bool quantize_row(GgmlType type, const float* x, void* dst, uint64_t n) {
    uint8_t* out = static_cast<uint8_t*>(dst);
    switch (type) {
        case GgmlType::F32: std::memcpy(out, x, n * sizeof(float)); return true;
        case GgmlType::F16:
            for (uint64_t i = 0; i < n; ++i) { const uint16_t h = fp32_to_fp16(x[i]); std::memcpy(out + 2 * i, &h, 2); }
            return true;
        case GgmlType::BF16:
            for (uint64_t i = 0; i < n; ++i) { const uint16_t h = f32_to_bf16(x[i]); std::memcpy(out + 2 * i, &h, 2); }
            return true;
        default: break;
    }
    const Codec* c = codec_for(type);
    if (!c || n % static_cast<uint64_t>(c->qk) != 0) return false;
    for (uint64_t b = 0; b < n / c->qk; ++b) c->refit(x + b * c->qk, out + b * c->bytes);
    return true;
}

bool merge_row(GgmlType type, const void* src_v, const float* base, const float* delta,
               void* dst_v, uint64_t n, MergeStats* st, MergeRounding rounding, uint64_t seed) {
    const uint8_t* src = static_cast<const uint8_t*>(src_v);
    uint8_t* dst = static_cast<uint8_t*>(dst_v);
    MergeStats local;
    float target[kMaxQK], cand[kMaxQK], merged[kMaxQK];

    // Element-wise types: nearest representable value of base + delta.
    if (type == GgmlType::F32 || type == GgmlType::F16 || type == GgmlType::BF16) {
        const size_t es = type == GgmlType::F32 ? 4 : 2;
        for (uint64_t i0 = 0; i0 < n; i0 += kMaxQK) {
            const int m = static_cast<int>(std::min<uint64_t>(kMaxQK, n - i0));
            bool zero = true;
            for (int i = 0; i < m; ++i) if (delta[i0 + i] != 0.0f) { zero = false; break; }
            ++local.blocks;
            if (zero) {
                std::memcpy(dst + i0 * es, src + i0 * es, static_cast<size_t>(m) * es);
                ++local.blocks_copied;
                continue;
            }
            for (int i = 0; i < m; ++i) target[i] = base[i0 + i] + delta[i0 + i];
            quantize_row(type, target, dst + i0 * es, static_cast<uint64_t>(m));
            dequant_row(type, dst + i0 * es, merged, static_cast<uint64_t>(m));
            for (int i = 0; i < m; ++i) {
                const double dv = delta[i0 + i];
                local.delta_sq += dv * dv;
                local.moved_dot += (static_cast<double>(merged[i]) - base[i0 + i]) * dv;
                const double e = static_cast<double>(merged[i]) - target[i];
                local.err_sq += e * e;
            }
            ++local.blocks_grid;
        }
        if (st) st->add(local);
        return true;
    }

    const Codec* c = codec_for(type);
    if (!c || n % static_cast<uint64_t>(c->qk) != 0) return false;
    const int qk = c->qk;
    Grid g;
    int q[kMaxQK];
    alignas(16) uint8_t refit_blk[sizeof(BlockQ6_K) + 16];
    for (uint64_t b = 0; b < n / qk; ++b) {
        const uint8_t* sb = src + b * c->bytes;
        uint8_t* db = dst + b * c->bytes;
        const float* bs = base + b * qk;
        const float* dl = delta + b * qk;
        ++local.blocks;

        bool zero = true;
        for (int i = 0; i < qk; ++i) if (dl[i] != 0.0f) { zero = false; break; }
        if (zero) {
            std::memcpy(db, sb, c->bytes);
            ++local.blocks_copied;
            continue;
        }
        for (int i = 0; i < qk; ++i) target[i] = bs[i] + dl[i];

        // grid candidate: the source scales, each code moved by delta / step.
        // Working from the old code (not from (target - offset) / step) keeps
        // zero-delta elements exact whatever the float rounding.
        c->unpack(sb, g);
        bool clamped = false;
        for (int i = 0; i < qk; ++i) {
            if (dl[i] == 0.0f) { q[i] = g.q[i]; continue; }
            if (g.s[i] == 0.0f) { q[i] = g.q[i]; clamped = true; continue; }
            // clamp: a huge v (tiny step) must not overflow the int below
            const float v = std::max(-1.0e6f, std::min(1.0e6f, dl[i] / g.s[i]));
            int step;
            if (rounding == MergeRounding::Nearest) {
                step = nearest(v);
            } else {
                const float fl = std::floor(v);
                step = static_cast<int>(fl) + (uniform01(seed, b * qk + i) < v - fl ? 1 : 0);
            }
            const int raw = g.q[i] + step;
            q[i] = clampi(raw, c->qmin, c->qmax);
            clamped = clamped || raw != q[i];
        }
        std::memcpy(db, sb, c->bytes);
        c->pack(db, q);
        dequant_row(type, db, merged, static_cast<uint64_t>(qk));
        double best = sse(merged, target, qk);

        // refit candidate, only when some target fell outside the block's
        // levels (clamped) or on a zero-scale group: otherwise the grid is
        // already per-element nearest at the source step, and a fresh fit
        // would re-quantise the base weights too (more error, ~10x the cost).
        double base_err = 0;
        for (int i = 0; i < qk; ++i) base_err += static_cast<double>(dl[i]) * dl[i];
        bool refit = false;
        if (clamped) {
            c->refit(target, refit_blk);
            dequant_row(type, refit_blk, cand, static_cast<uint64_t>(qk));
            const double e = sse(cand, target, qk);
            if (e < best) {
                best = e;
                std::memcpy(db, refit_blk, c->bytes);
                std::memcpy(merged, cand, sizeof(float) * qk);
                refit = true;
            }
        }
        if (refit) ++local.blocks_refit; else ++local.blocks_grid;
        local.delta_sq += base_err;
        local.err_sq += best;
        for (int i = 0; i < qk; ++i)
            local.moved_dot += (static_cast<double>(merged[i]) - bs[i]) * dl[i];
    }
    if (st) st->add(local);
    return true;
}

} // namespace fish
