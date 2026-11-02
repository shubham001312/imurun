#include "requant.h"

#include <fish/quant.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

namespace fish {
namespace gpu {

namespace {

// IEEE f32 -> f16, round to nearest even, with subnormals (super-block scales
// of small weights are often below the f16 normal range).
uint16_t f2h(float f) {
    uint32_t x;
    std::memcpy(&x, &f, 4);
    const uint32_t sign = (x >> 16) & 0x8000u;
    const int32_t e = static_cast<int32_t>((x >> 23) & 0xFF);
    uint32_t m = x & 0x7FFFFFu;
    if (e == 255) return static_cast<uint16_t>(sign | 0x7C00u | (m ? 0x200u : 0));
    int32_t he = e - 127 + 15;
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
    uint32_t hm = m >> 13;
    const uint32_t rem = m & 0x1FFFu;
    uint32_t out = sign | (static_cast<uint32_t>(he) << 10) | hm;
    if (rem > 0x1000u || (rem == 0x1000u && (hm & 1u))) ++out;   // may carry into the exponent: correct
    return static_cast<uint16_t>(out);
}

// Fit x ~ scale * q + b over one 32-value group, q in [0, nmax], b <= 0
// (stored as m = -b): min/max start, then a few rounds of "assign q,
// least-squares (scale, b)". An importance-weighted 21-point grid search (the
// reference K-quant approach) was measured too: same relative RMS (13.87% for
// Q4_K -> FQ3 on Llama-3.2-1B blk.0.ffn_gate) at 8x the load time, so the
// simple fit stays. The loss is inherent: 16 source levels onto 8.
void fit_group(const float* x, int nmax, float* scale, float* mout) {
    float xmin = 0.0f, xmax = x[0];
    for (int i = 0; i < 32; ++i) { xmin = std::min(xmin, x[i]); xmax = std::max(xmax, x[i]); }
    if (xmax <= xmin) { *scale = 0.0f; *mout = -xmin; return; }
    float sc = (xmax - xmin) / nmax, b = xmin;
    for (int it = 0; it < 4; ++it) {
        double sq = 0, sqq = 0, sx = 0, sqx = 0;
        const float inv = 1.0f / sc;
        for (int i = 0; i < 32; ++i) {
            const int q = std::max(0, std::min(nmax, static_cast<int>(std::lround((x[i] - b) * inv))));
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

// Super-block of 256: per-group (scale, min) -> 6-bit codes under f16 d/dmin,
// then the final integer codes with the quantised scales. Shared by Q4_K and FQ3.
struct SuperBlock {
    uint16_t d, dmin;
    uint8_t ls[8], lm[8];
    uint8_t q[256];
};

void quantize_super(const float* x, int nmax, SuperBlock& sb) {
    float sc[8], mn[8], maxsc = 0, maxm = 0;
    for (int g = 0; g < 8; ++g) {
        fit_group(x + 32 * g, nmax, &sc[g], &mn[g]);
        maxsc = std::max(maxsc, sc[g]);
        maxm = std::max(maxm, mn[g]);
    }
    sb.d = f2h(maxsc / 63.0f);
    sb.dmin = f2h(maxm / 63.0f);
    const float dd = fp16_to_fp32(sb.d), dm = fp16_to_fp32(sb.dmin);
    for (int g = 0; g < 8; ++g) {
        sb.ls[g] = static_cast<uint8_t>(dd > 0 ? std::max(0, std::min(63, static_cast<int>(std::lround(sc[g] / dd)))) : 0);
        sb.lm[g] = static_cast<uint8_t>(dm > 0 ? std::max(0, std::min(63, static_cast<int>(std::lround(mn[g] / dm)))) : 0);
        const float s = dd * sb.ls[g], m = dm * sb.lm[g];
        for (int i = 0; i < 32; ++i) {
            int q = 0;
            if (s > 0) q = static_cast<int>(std::lround((x[32 * g + i] + m) / s));
            sb.q[32 * g + i] = static_cast<uint8_t>(std::max(0, std::min(nmax, q)));
        }
    }
}

// The 12-byte 6-bit scale/min packing of Q4_K (inverse of get_scale_min_k4).
void pack_scales(const SuperBlock& sb, uint8_t* s12) {
    std::memset(s12, 0, 12);
    for (int j = 0; j < 8; ++j) {
        if (j < 4) {
            s12[j] |= sb.ls[j] & 63;
            s12[j + 4] |= sb.lm[j] & 63;
        } else {
            s12[j + 4] = static_cast<uint8_t>((sb.ls[j] & 0xF) | ((sb.lm[j] & 0xF) << 4));
            s12[j - 4] |= static_cast<uint8_t>((sb.ls[j] >> 4) << 6);
            s12[j] |= static_cast<uint8_t>((sb.lm[j] >> 4) << 6);
        }
    }
}

void unpack_scale(const uint8_t* q, int j, int* d, int* m) {
    if (j < 4) { *d = q[j] & 63; *m = q[j + 4] & 63; }
    else {
        *d = (q[j + 4] & 0xF) | ((q[j - 4] >> 6) << 4);
        *m = (q[j + 4] >> 4) | ((q[j] >> 6) << 4);
    }
}

} // namespace

void quantize_row_q4k(const float* x, int64_t n, uint8_t* out) {
    for (int64_t b = 0; b < n / kQK; ++b) {
        SuperBlock sb;
        quantize_super(x + b * kQK, 15, sb);
        uint8_t* o = out + b * 144;
        std::memcpy(o, &sb.d, 2);
        std::memcpy(o + 2, &sb.dmin, 2);
        pack_scales(sb, o + 4);
        for (int p = 0; p < 4; ++p)
            for (int l = 0; l < 32; ++l)
                o[16 + 32 * p + l] = static_cast<uint8_t>(sb.q[64 * p + l] | (sb.q[64 * p + 32 + l] << 4));
    }
}

void quantize_row_fq3(const float* x, int64_t n, uint8_t* out) {
    for (int64_t b = 0; b < n / kQK; ++b) {
        SuperBlock sb;
        quantize_super(x + b * kQK, 7, sb);
        uint8_t* o = out + b * kFQ3BlockBytes;
        std::memcpy(o, &sb.d, 2);
        std::memcpy(o + 2, &sb.dmin, 2);
        pack_scales(sb, o + 4);
        for (int g = 0; g < 8; ++g) {
            const uint8_t* q = sb.q + 32 * g;
            uint32_t A = 0, B = 0, C = 0;
            for (int bb = 0; bb < 4; ++bb) {
                for (int s = 0; s < 4; ++s) {
                    A |= static_cast<uint32_t>(q[bb + 4 * s] & 3u) << (8 * bb + 2 * s);
                    B |= static_cast<uint32_t>(q[16 + bb + 4 * s] & 3u) << (8 * bb + 2 * s);
                }
                for (int s = 0; s < 8; ++s)
                    C |= static_cast<uint32_t>((q[bb + 4 * s] >> 2) & 1u) << (8 * bb + s);
            }
            std::memcpy(o + 16 + 12 * g, &A, 4);
            std::memcpy(o + 20 + 12 * g, &B, 4);
            std::memcpy(o + 24 + 12 * g, &C, 4);
        }
    }
}

// ---------------------------------------------------------------- weighted fits

namespace {

// Per-element weights for one 256-block: imp_j * sqrt(sigma2 + x_j^2)
// (sigma2 = 2 * mean x^2 of the block), or 1 without importance.
void block_weights(const float* x, const float* imp, float* w) {
    if (!imp) { for (int i = 0; i < kQK; ++i) w[i] = 1.0f; return; }
    double ss = 0;
    for (int i = 0; i < kQK; ++i) ss += static_cast<double>(x[i]) * x[i];
    const float sigma2 = static_cast<float>(2.0 * ss / kQK);
    for (int i = 0; i < kQK; ++i) w[i] = std::max(imp[i], 0.0f) * std::sqrt(sigma2 + x[i] * x[i]) + 1e-30f;
}

// Weighted asymmetric fit of one 32-value group: x ~ sc * q - m, q in 0..nmax.
// Several starting ranges (the min/max one and shrunk ones), each refined by
// weighted least squares; the lowest weighted error wins.
void fit_group_w(const float* x, const float* w, int nmax, float* scale, float* mout) {
    float xmin = 0.0f, xmax = x[0];
    for (int i = 0; i < 32; ++i) { xmin = std::min(xmin, x[i]); xmax = std::max(xmax, x[i]); }
    if (xmax <= xmin) { *scale = 0.0f; *mout = -xmin; return; }
    double best_err = INFINITY;
    float best_sc = (xmax - xmin) / nmax, best_b = xmin;
    for (int start = 0; start < 5; ++start) {
        const float shrink = 1.0f - 0.06f * start;
        const float lo = xmin * shrink, hi = xmax * shrink;
        float sc = (hi - lo) / nmax, b = lo;
        for (int it = 0; it < 3; ++it) {
            double sw = 0, sq = 0, sqq = 0, sx = 0, sqx = 0;
            const float inv = 1.0f / sc;
            for (int i = 0; i < 32; ++i) {
                const int q = std::max(0, std::min(nmax, static_cast<int>(std::lround((x[i] - b) * inv))));
                sw += w[i]; sq += w[i] * q; sqq += w[i] * static_cast<double>(q) * q;
                sx += w[i] * x[i]; sqx += w[i] * static_cast<double>(q) * x[i];
            }
            const double det = sw * sqq - sq * sq;
            if (det <= 0) break;
            double nsc = (sw * sqx - sq * sx) / det;
            double nb = (sx - nsc * sq) / sw;
            if (nb > 0) { nb = 0; nsc = sqq > 0 ? sqx / sqq : 0; }
            if (!(nsc > 0)) break;
            sc = static_cast<float>(nsc);
            b = static_cast<float>(nb);
        }
        double err = 0;
        const float inv = 1.0f / sc;
        for (int i = 0; i < 32; ++i) {
            const int q = std::max(0, std::min(nmax, static_cast<int>(std::lround((x[i] - b) * inv))));
            const float d = sc * q + b - x[i];
            err += w[i] * d * d;
        }
        if (err < best_err) { best_err = err; best_sc = sc; best_b = b; }
    }
    *scale = best_sc;
    *mout = -best_b;
}

// As quantize_super, weighted: group fits, 6-bit scale/min codes (each tried
// at round-1..round+1 against the weighted error), final codes.
void quantize_super_w(const float* x, const float* w, int nmax, SuperBlock& sb) {
    float sc[8], mn[8], maxsc = 0, maxm = 0;
    for (int g = 0; g < 8; ++g) {
        fit_group_w(x + 32 * g, w + 32 * g, nmax, &sc[g], &mn[g]);
        maxsc = std::max(maxsc, sc[g]);
        maxm = std::max(maxm, mn[g]);
    }
    sb.d = f2h(maxsc / 63.0f);
    sb.dmin = f2h(maxm / 63.0f);
    const float dd = fp16_to_fp32(sb.d), dm = fp16_to_fp32(sb.dmin);
    for (int g = 0; g < 8; ++g) {
        const float* xg = x + 32 * g;
        const float* wg = w + 32 * g;
        const int ls0 = dd > 0 ? std::max(0, std::min(63, static_cast<int>(std::lround(sc[g] / dd)))) : 0;
        const int lm0 = dm > 0 ? std::max(0, std::min(63, static_cast<int>(std::lround(mn[g] / dm)))) : 0;
        double best = INFINITY;
        int bls = ls0, blm = lm0;
        for (int a = std::max(0, ls0 - 1); a <= std::min(63, ls0 + 1); ++a) {
            for (int c = std::max(0, lm0 - 1); c <= std::min(63, lm0 + 1); ++c) {
                const float s = dd * a, m = dm * c;
                double err = 0;
                for (int i = 0; i < 32; ++i) {
                    int q = s > 0 ? static_cast<int>(std::lround((xg[i] + m) / s)) : 0;
                    q = std::max(0, std::min(nmax, q));
                    const float e = s * q - m - xg[i];
                    err += wg[i] * e * e;
                }
                if (err < best) { best = err; bls = a; blm = c; }
            }
        }
        sb.ls[g] = static_cast<uint8_t>(bls);
        sb.lm[g] = static_cast<uint8_t>(blm);
        const float s = dd * bls, m = dm * blm;
        for (int i = 0; i < 32; ++i) {
            int q = 0;
            if (s > 0) q = static_cast<int>(std::lround((xg[i] + m) / s));
            sb.q[32 * g + i] = static_cast<uint8_t>(std::max(0, std::min(nmax, q)));
        }
    }
}

void pack_fq3_groups(const uint8_t* qall, uint8_t* o) {
    for (int g = 0; g < 8; ++g) {
        const uint8_t* q = qall + 32 * g;
        uint32_t A = 0, B = 0, C = 0;
        for (int bb = 0; bb < 4; ++bb) {
            for (int s = 0; s < 4; ++s) {
                A |= static_cast<uint32_t>(q[bb + 4 * s] & 3u) << (8 * bb + 2 * s);
                B |= static_cast<uint32_t>(q[16 + bb + 4 * s] & 3u) << (8 * bb + 2 * s);
            }
            for (int s = 0; s < 8; ++s)
                C |= static_cast<uint32_t>((q[bb + 4 * s] >> 2) & 1u) << (8 * bb + s);
        }
        std::memcpy(o + 12 * g, &A, 4);
        std::memcpy(o + 12 * g + 4, &B, 4);
        std::memcpy(o + 12 * g + 8, &C, 4);
    }
}

// FQ3S levels and decision thresholds (midpoints), built once.
struct Fq3sTable {
    float L[8], T[7];
    explicit Fq3sTable(float c = kFQ3SCubic) {
        for (int q = 0; q < 8; ++q) {
            const float u = (2.0f * q - 7.0f) / 7.0f;
            L[q] = u * (1.0f - c + c * u * u);
        }
        for (int i = 0; i < 7; ++i) T[i] = 0.5f * (L[i] + L[i + 1]);
    }
};
const Fq3sTable& fq3s_table() { static const Fq3sTable t; return t; }

inline int fq3s_nearest(const Fq3sTable& tb, float y) {
    int q = 0;
    for (int i = 0; i < 7; ++i) q += y > tb.T[i];
    return q;
}

// Weighted error of one group at scale s (codes chosen nearest).
inline double fq3s_err(const Fq3sTable& tb, const float* x, const float* w, float s, uint8_t* q) {
    double err = 0;
    const float inv = s > 0 ? 1.0f / s : 0.0f;
    for (int i = 0; i < 32; ++i) {
        const int c = fq3s_nearest(tb, x[i] * inv);
        if (q) q[i] = static_cast<uint8_t>(c);
        const float e = s * tb.L[c] - x[i];
        err += w[i] * e * e;
    }
    return err;
}

} // namespace

float fq3s_level(int q) { return fq3s_table().L[q & 7]; }

void quantize_row_q4k_w(const float* x, int64_t n, uint8_t* out, const float* imp) {
    float w[kQK];
    for (int64_t b = 0; b < n / kQK; ++b) {
        SuperBlock sb;
        block_weights(x + b * kQK, imp ? imp + b * kQK : nullptr, w);
        quantize_super_w(x + b * kQK, w, 15, sb);
        uint8_t* o = out + b * 144;
        std::memcpy(o, &sb.d, 2);
        std::memcpy(o + 2, &sb.dmin, 2);
        pack_scales(sb, o + 4);
        for (int p = 0; p < 4; ++p)
            for (int l = 0; l < 32; ++l)
                o[16 + 32 * p + l] = static_cast<uint8_t>(sb.q[64 * p + l] | (sb.q[64 * p + 32 + l] << 4));
    }
}

void quantize_row_fq3_w(const float* x, int64_t n, uint8_t* out, const float* imp) {
    float w[kQK];
    for (int64_t b = 0; b < n / kQK; ++b) {
        SuperBlock sb;
        block_weights(x + b * kQK, imp ? imp + b * kQK : nullptr, w);
        quantize_super_w(x + b * kQK, w, 7, sb);
        uint8_t* o = out + b * kFQ3BlockBytes;
        std::memcpy(o, &sb.d, 2);
        std::memcpy(o + 2, &sb.dmin, 2);
        pack_scales(sb, o + 4);
        pack_fq3_groups(sb.q, o + 16);
    }
}

namespace {
void quantize_row_fq3s_tb(const Fq3sTable& tb, const float* x, int64_t n, uint8_t* out, const float* imp);
void dequant_row_fq3s_tb(const Fq3sTable& tb, const uint8_t* src, float* y, int64_t n);
}

void quantize_row_fq3s(const float* x, int64_t n, uint8_t* out, const float* imp) {
    quantize_row_fq3s_tb(fq3s_table(), x, n, out, imp);
}
void dequant_row_fq3s(const uint8_t* src, float* y, int64_t n) { dequant_row_fq3s_tb(fq3s_table(), src, y, n); }

void fq3s_study_roundtrip(float cubic, const float* x, int64_t n, const float* imp, float* y) {
    const Fq3sTable tb(cubic);
    std::vector<uint8_t> buf(static_cast<size_t>(n / kQK) * kFQ3SBlockBytes);
    quantize_row_fq3s_tb(tb, x, n, buf.data(), imp);
    dequant_row_fq3s_tb(tb, buf.data(), y, n);
}

namespace {
void quantize_row_fq3s_tb(const Fq3sTable& tb, const float* x, int64_t n, uint8_t* out, const float* imp) {
    float w[kQK];
    for (int64_t b = 0; b < n / kQK; ++b) {
        const float* xb = x + b * kQK;
        block_weights(xb, imp ? imp + b * kQK : nullptr, w);
        // 1. Per group: the best real scale. Start from amax / L7 shrunk by a
        //    few factors, refine each by weighted least squares on its codes.
        float sc[8], maxsc = 0;
        for (int g = 0; g < 8; ++g) {
            const float* xg = xb + 32 * g;
            const float* wg = w + 32 * g;
            float amax = 0;
            for (int i = 0; i < 32; ++i) amax = std::max(amax, std::fabs(xg[i]));
            float best_s = amax / tb.L[7];
            double best = INFINITY;
            if (amax > 0) {
                uint8_t q[32];
                for (int k = 0; k < 6; ++k) {
                    float s = amax / tb.L[7] * (1.0f - 0.05f * k);
                    fq3s_err(tb, xg, wg, s, q);
                    double num = 0, den = 0;
                    for (int i = 0; i < 32; ++i) { num += wg[i] * xg[i] * tb.L[q[i]]; den += wg[i] * tb.L[q[i]] * tb.L[q[i]]; }
                    if (den > 0 && num > 0) s = static_cast<float>(num / den);
                    const double e = fq3s_err(tb, xg, wg, s, nullptr);
                    if (e < best) { best = e; best_s = s; }
                }
            }
            sc[g] = best_s;
            maxsc = std::max(maxsc, best_s);
        }
        // 2. 6-bit group scales under one f16 super-scale; each code tried at
        //    round-1..round+1 against the weighted error.
        uint8_t* o = out + b * kFQ3SBlockBytes;
        const uint16_t hd = f2h(maxsc / 63.0f);
        const float d = fp16_to_fp32(hd);
        uint8_t ls[8];
        uint8_t qall[256];
        for (int g = 0; g < 8; ++g) {
            const float* xg = xb + 32 * g;
            const float* wg = w + 32 * g;
            const int l0 = d > 0 ? std::max(0, std::min(63, static_cast<int>(std::lround(sc[g] / d)))) : 0;
            int bl = l0;
            double best = INFINITY;
            for (int l = std::max(0, l0 - 1); l <= std::min(63, l0 + 1); ++l) {
                const double e = fq3s_err(tb, xg, wg, d * l, nullptr);
                if (e < best) { best = e; bl = l; }
            }
            ls[g] = static_cast<uint8_t>(bl);
            fq3s_err(tb, xg, wg, d * bl, qall + 32 * g);
        }
        std::memcpy(o, &hd, 2);
        uint64_t bits = 0;
        for (int g = 0; g < 8; ++g) bits |= static_cast<uint64_t>(ls[g] & 63u) << (6 * g);
        for (int i = 0; i < 6; ++i) o[2 + i] = static_cast<uint8_t>(bits >> (8 * i));
        pack_fq3_groups(qall, o + 8);
    }
}

void dequant_row_fq3s_tb(const Fq3sTable& tb, const uint8_t* src, float* y, int64_t n) {
    for (int64_t b = 0; b < n / kQK; ++b) {
        const uint8_t* o = src + b * kFQ3SBlockBytes;
        uint16_t hd;
        std::memcpy(&hd, o, 2);
        const float d = fp16_to_fp32(hd);
        uint64_t bits = 0;
        for (int i = 0; i < 6; ++i) bits |= static_cast<uint64_t>(o[2 + i]) << (8 * i);
        for (int g = 0; g < 8; ++g) {
            const float s = d * static_cast<float>((bits >> (6 * g)) & 63u);
            uint32_t A, B, C;
            std::memcpy(&A, o + 8 + 12 * g, 4);
            std::memcpy(&B, o + 12 + 12 * g, 4);
            std::memcpy(&C, o + 16 + 12 * g, 4);
            for (int sidx = 0; sidx < 8; ++sidx)
                for (int bb = 0; bb < 4; ++bb) {
                    const uint32_t lo = (((sidx < 4 ? A : B) >> (8 * bb + 2 * (sidx & 3))) & 3u);
                    const uint32_t hi = (C >> (8 * bb + sidx)) & 1u;
                    y[b * kQK + 32 * g + 4 * sidx + bb] = s * tb.L[lo | (hi << 2)];
                }
        }
    }
}
} // namespace

void dequant_row_fq3(const uint8_t* src, float* y, int64_t n) {
    for (int64_t b = 0; b < n / kQK; ++b) {
        const uint8_t* o = src + b * kFQ3BlockBytes;
        uint16_t hd, hm;
        std::memcpy(&hd, o, 2);
        std::memcpy(&hm, o + 2, 2);
        const float d = fp16_to_fp32(hd), dmin = fp16_to_fp32(hm);
        for (int g = 0; g < 8; ++g) {
            int sc, m;
            unpack_scale(o + 4, g, &sc, &m);
            uint32_t A, B, C;
            std::memcpy(&A, o + 16 + 12 * g, 4);
            std::memcpy(&B, o + 20 + 12 * g, 4);
            std::memcpy(&C, o + 24 + 12 * g, 4);
            for (int s = 0; s < 8; ++s)
                for (int bb = 0; bb < 4; ++bb) {
                    const uint32_t lo = (((s < 4 ? A : B) >> (8 * bb + 2 * (s & 3))) & 3u);
                    const uint32_t hi = (C >> (8 * bb + s)) & 1u;
                    y[b * kQK + 32 * g + 4 * s + bb] = d * sc * static_cast<float>(lo | (hi << 2)) - dmin * m;
                }
        }
    }
}

} // namespace gpu
} // namespace fish
