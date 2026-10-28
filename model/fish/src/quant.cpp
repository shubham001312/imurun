/*
 * FISH — quantised kernels, scalar reference implementation.
 *
 * Every routine here is written from the GGUF block specification. The
 * reference is deliberately simple and exact: it is the correctness oracle
 * that the AVX2 path is validated against.
 */
#include <fish/quant.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

namespace fish {

namespace {

// ---------------------------------------------------------------- fp16

float half_to_float_soft(uint16_t h) {
    uint32_t sign = static_cast<uint32_t>(h & 0x8000u) << 16;
    uint32_t exp  = (h >> 10) & 0x1Fu;
    uint32_t mant = h & 0x3FFu;
    uint32_t bits;
    if (exp == 0) {
        if (mant == 0) {
            bits = sign;
        } else {
            // subnormal: renormalise
            exp = 127 - 15 + 1;
            while ((mant & 0x400u) == 0) { mant <<= 1; --exp; }
            mant &= 0x3FFu;
            bits = sign | (exp << 23) | (mant << 13);
        }
    } else if (exp == 31) {
        bits = sign | 0x7F800000u | (mant << 13);
    } else {
        bits = sign | ((exp - 15 + 127) << 23) | (mant << 13);
    }
    float f;
    std::memcpy(&f, &bits, 4);
    return f;
}

const float* fp16_table() {
    static const std::vector<float> table = [] {
        std::vector<float> t(65536);
        for (int i = 0; i < 65536; ++i) t[i] = half_to_float_soft(static_cast<uint16_t>(i));
        return t;
    }();
    return table.data();
}

inline float h2f(uint16_t h) { return fp16_table()[h]; }

inline float bf16_to_f32(uint16_t v) {
    uint32_t bits = static_cast<uint32_t>(v) << 16;
    float f;
    std::memcpy(&f, &bits, 4);
    return f;
}

// ---------------------------------------------------------------- dot helpers

float dot_f32(const float* a, const float* b, size_t n) {
    float s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        s0 += a[i] * b[i];
        s1 += a[i + 1] * b[i + 1];
        s2 += a[i + 2] * b[i + 2];
        s3 += a[i + 3] * b[i + 3];
    }
    for (; i < n; ++i) s0 += a[i] * b[i];
    return (s0 + s1) + (s2 + s3);
}

// ---------------------------------------------------------------- block dequantisers
// Each writes exactly `block` floats and never reads outside its block.

void deq_q4_0(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ4_0*>(p);
    const float d = h2f(b->d);
    for (int j = 0; j < 16; ++j) {
        y[j]      = d * static_cast<float>((b->qs[j] & 0x0F) - 8);
        y[j + 16] = d * static_cast<float>((b->qs[j] >> 4) - 8);
    }
}

void deq_q4_1(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ4_1*>(p);
    const float d = h2f(b->d);
    const float m = h2f(b->m);
    for (int j = 0; j < 16; ++j) {
        y[j]      = d * static_cast<float>(b->qs[j] & 0x0F) + m;
        y[j + 16] = d * static_cast<float>(b->qs[j] >> 4) + m;
    }
}

void deq_q5_0(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ5_0*>(p);
    const float d = h2f(b->d);
    uint32_t qh = 0;
    std::memcpy(&qh, b->qh, 4);
    for (int j = 0; j < 16; ++j) {
        const int lo = (b->qs[j] & 0x0F) | (static_cast<int>((qh >> j) & 1) << 4);
        const int hi = (b->qs[j] >> 4)    | (static_cast<int>((qh >> (j + 16)) & 1) << 4);
        y[j]      = d * static_cast<float>(lo - 16);
        y[j + 16] = d * static_cast<float>(hi - 16);
    }
}

void deq_q5_1(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ5_1*>(p);
    const float d = h2f(b->d);
    const float m = h2f(b->m);
    uint32_t qh = 0;
    std::memcpy(&qh, b->qh, 4);
    for (int j = 0; j < 16; ++j) {
        const int lo = (b->qs[j] & 0x0F) | (static_cast<int>((qh >> j) & 1) << 4);
        const int hi = (b->qs[j] >> 4)    | (static_cast<int>((qh >> (j + 16)) & 1) << 4);
        y[j]      = d * static_cast<float>(lo) + m;
        y[j + 16] = d * static_cast<float>(hi) + m;
    }
}

void deq_q8_0(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ8_0*>(p);
    const float d = h2f(b->d);
    for (int j = 0; j < 32; ++j) y[j] = d * static_cast<float>(b->qs[j]);
}

// get_scale_min_k4 from the GGUF 4-bit super-block layout.
inline void scale_min_k4(int j, const uint8_t* q, uint8_t* d, uint8_t* m) {
    if (j < 4) {
        *d = q[j] & 63;
        *m = q[j + 4] & 63;
    } else {
        *d = static_cast<uint8_t>((q[j + 4] & 0x0F) | ((q[j - 4] >> 6) << 4));
        *m = static_cast<uint8_t>((q[j + 4] >> 4)    | ((q[j]     >> 6) << 4));
    }
}

void deq_q4_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ4_K*>(p);
    const float d   = h2f(b->d);
    const float min = h2f(b->dmin);
    const uint8_t* q = b->qs;
    int is = 0;
    for (int j = 0; j < 256; j += 64) {
        uint8_t sc, m;
        scale_min_k4(is + 0, b->scales, &sc, &m);
        const float d1 = d * sc, m1 = min * m;
        scale_min_k4(is + 1, b->scales, &sc, &m);
        const float d2 = d * sc, m2 = min * m;
        for (int l = 0; l < 32; ++l) y[j + l]      = d1 * static_cast<float>(q[l] & 0x0F) - m1;
        for (int l = 0; l < 32; ++l) y[j + 32 + l] = d2 * static_cast<float>(q[l] >> 4)   - m2;
        q += 32;
        is += 2;
    }
}

void deq_q5_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ5_K*>(p);
    const float d   = h2f(b->d);
    const float min = h2f(b->dmin);
    const uint8_t* ql = b->qs;
    const uint8_t* qh = b->qh;
    int is = 0;
    uint8_t u1 = 1, u2 = 2;
    for (int j = 0; j < 256; j += 64) {
        uint8_t sc, m;
        scale_min_k4(is + 0, b->scales, &sc, &m);
        const float d1 = d * sc, m1 = min * m;
        scale_min_k4(is + 1, b->scales, &sc, &m);
        const float d2 = d * sc, m2 = min * m;
        for (int l = 0; l < 32; ++l) {
            const int q = (ql[l] & 0x0F) + ((qh[l] & u1) ? 16 : 0);
            y[j + l] = d1 * static_cast<float>(q) - m1;
        }
        for (int l = 0; l < 32; ++l) {
            const int q = (ql[l] >> 4) + ((qh[l] & u2) ? 16 : 0);
            y[j + 32 + l] = d2 * static_cast<float>(q) - m2;
        }
        ql += 32;
        is += 2;
        u1 = static_cast<uint8_t>(u1 << 2);
        u2 = static_cast<uint8_t>(u2 << 2);
    }
}

void deq_q6_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ6_K*>(p);
    const float d = h2f(b->d);
    const uint8_t* ql = b->ql;
    const uint8_t* qh = b->qh;
    const int8_t*  sc = b->scales;
    for (int n = 0; n < 256; n += 128) {
        for (int l = 0; l < 32; ++l) {
            const int is = l / 16;
            const int8_t q1 = static_cast<int8_t>((ql[l + 0] & 0x0F) | (((qh[l] >> 0) & 3) << 4)) - 32;
            const int8_t q2 = static_cast<int8_t>((ql[l + 32] & 0x0F) | (((qh[l] >> 2) & 3) << 4)) - 32;
            const int8_t q3 = static_cast<int8_t>((ql[l + 0] >> 4)  | (((qh[l] >> 4) & 3) << 4)) - 32;
            const int8_t q4 = static_cast<int8_t>((ql[l + 32] >> 4) | (((qh[l] >> 6) & 3) << 4)) - 32;
            y[n + l +  0] = d * static_cast<float>(sc[is + 0]) * q1;
            y[n + l + 32] = d * static_cast<float>(sc[is + 2]) * q2;
            y[n + l + 64] = d * static_cast<float>(sc[is + 4]) * q3;
            y[n + l + 96] = d * static_cast<float>(sc[is + 6]) * q4;
        }
        ql += 64;
        qh += 32;
        sc += 8;
    }
}

void deq_q2_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ2_K*>(p);
    const float d   = h2f(b->d);
    const float min = h2f(b->dmin);
    const uint8_t* q = b->qs;
    int is = 0;
    for (int n = 0; n < 256; n += 128) {
        int shift = 0;
        for (int j = 0; j < 4; ++j) {
            uint8_t sc = b->scales[is++];
            const float dl = d * (sc & 0x0F), ml = min * (sc >> 4);
            for (int l = 0; l < 16; ++l)
                y[n + j * 32 + l] = dl * static_cast<float>((q[l] >> shift) & 3) - ml;

            sc = b->scales[is++];
            const float dl2 = d * (sc & 0x0F), ml2 = min * (sc >> 4);
            for (int l = 0; l < 16; ++l)
                y[n + j * 32 + 16 + l] = dl2 * static_cast<float>((q[l + 16] >> shift) & 3) - ml2;
            shift += 2;
        }
        q += 32;
    }
}

void deq_q3_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ3_K*>(p);
    const float d_all = h2f(b->d);
    const uint8_t* q = b->qs;
    const uint8_t* hm = b->hmask;

    const uint32_t kmask1 = 0x03030303u, kmask2 = 0x0F0F0F0Fu;
    uint32_t aux[4];
    std::memcpy(aux, b->scales, 12);
    const uint32_t tmp = aux[2];
    aux[2] = ((aux[0] >> 4) & kmask2) | (((tmp >> 4) & kmask1) << 4);
    aux[3] = ((aux[1] >> 4) & kmask2) | (((tmp >> 6) & kmask1) << 4);
    aux[0] = (aux[0] & kmask2) | (((tmp >> 0) & kmask1) << 4);
    aux[1] = (aux[1] & kmask2) | (((tmp >> 2) & kmask1) << 4);
    const int8_t* scales = reinterpret_cast<const int8_t*>(aux);

    int is = 0;
    uint8_t m = 1;
    for (int n = 0; n < 256; n += 128) {
        int shift = 0;
        for (int j = 0; j < 4; ++j) {
            const float dl = d_all * static_cast<float>(scales[is++] - 32);
            for (int l = 0; l < 16; ++l) {
                const int qv = static_cast<int8_t>((q[l] >> shift) & 3) - ((hm[l] & m) ? 0 : 4);
                y[n + j * 32 + l] = dl * static_cast<float>(qv);
            }
            const float dl2 = d_all * static_cast<float>(scales[is++] - 32);
            for (int l = 0; l < 16; ++l) {
                const int qv = static_cast<int8_t>((q[l + 16] >> shift) & 3) - ((hm[l + 16] & m) ? 0 : 4);
                y[n + j * 32 + 16 + l] = dl2 * static_cast<float>(qv);
            }
            shift += 2;
            m = static_cast<uint8_t>(m << 1);
        }
        q += 32;
    }
}

void deq_q8_K(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockQ8_K*>(p);
    const float d = b->d;
    for (int j = 0; j < 256; ++j) y[j] = d * static_cast<float>(b->qs[j]);
}

// E2M1 values used by MXFP4.
constexpr float kE2M1[8] = {0.0f, 0.5f, 1.0f, 1.5f, 2.0f, 3.0f, 4.0f, 6.0f};

void deq_mxfp4(const uint8_t* p, float* y) {
    const auto* b = reinterpret_cast<const BlockMXFP4*>(p);
    // E8M0 scale: 2^(e - 127)
    const float d = std::ldexp(1.0f, static_cast<int>(b->e) - 127);
    for (int j = 0; j < 16; ++j) {
        y[j]      = d * kE2M1[b->qs[j] & 0x0F];
        y[j + 16] = d * kE2M1[b->qs[j] >> 4];
    }
}

void deq_f16(const uint8_t* p, float* y, int n) {
    const uint16_t* s = reinterpret_cast<const uint16_t*>(p);
    for (int i = 0; i < n; ++i) y[i] = h2f(s[i]);
}

void deq_bf16(const uint8_t* p, float* y, int n) {
    const uint16_t* s = reinterpret_cast<const uint16_t*>(p);
    for (int i = 0; i < n; ++i) y[i] = bf16_to_f32(s[i]);
}

// Generic block loop: dequantise one block, dot it, advance.
template <typename Fn>
float dot_blockwise(const void* src, const float* y, uint64_t n, int block, size_t bsize, Fn fn) {
    float buf[256];
    const uint8_t* p = static_cast<const uint8_t*>(src);
    float sum = 0;
    for (uint64_t i = 0; i < n; i += static_cast<uint64_t>(block)) {
        fn(p, buf);
        sum += dot_f32(buf, y + i, static_cast<size_t>(block));
        p += bsize;
    }
    return sum;
}

float dot_q4_K_scalar(const void* s, const float* y, uint64_t n) { return dot_blockwise(s, y, n, 256, 144, deq_q4_K); }
float dot_q6_K_scalar(const void* s, const float* y, uint64_t n) { return dot_blockwise(s, y, n, 256, 210, deq_q6_K); }
float dot_q8_0_scalar(const void* s, const float* y, uint64_t n) { return dot_blockwise(s, y, n, 32, 34, deq_q8_0); }
float dot_q4_0_scalar(const void* s, const float* y, uint64_t n) { return dot_blockwise(s, y, n, 32, 18, deq_q4_0); }

QuantKernels g_kernels = {
    dot_q4_K_scalar, dot_q6_K_scalar, dot_q8_0_scalar, dot_q4_0_scalar, "scalar"
};

float dot_f32_scalar_entry(const float* a, const float* b, uint64_t n) {
    return dot_f32(a, b, static_cast<size_t>(n));
}

float (*g_dot_f32)(const float*, const float*, uint64_t) = dot_f32_scalar_entry;

} // namespace

// ---------------------------------------------------------------- public

void init_fp16_table() { (void)fp16_table(); }

float fp16_to_fp32(uint16_t h) { return h2f(h); }

bool quant_supported(GgmlType t) {
    switch (t) {
        case GgmlType::F32: case GgmlType::F16: case GgmlType::BF16:
        case GgmlType::Q4_0: case GgmlType::Q4_1:
        case GgmlType::Q5_0: case GgmlType::Q5_1:
        case GgmlType::Q8_0:
        case GgmlType::Q2_K: case GgmlType::Q3_K: case GgmlType::Q4_K:
        case GgmlType::Q5_K: case GgmlType::Q6_K: case GgmlType::Q8_K:
        case GgmlType::MXFP4:
            return true;
        default:
            return false;
    }
}

bool dequant_row(GgmlType type, const void* src, float* dst, uint64_t n) {
    if (!src || !dst) return false;
    const uint8_t* p = static_cast<const uint8_t*>(src);
    switch (type) {
        case GgmlType::F32:
            std::memcpy(dst, src, static_cast<size_t>(n) * 4);
            return true;
        case GgmlType::F16:  deq_f16(p, dst, static_cast<int>(n)); return true;
        case GgmlType::BF16: deq_bf16(p, dst, static_cast<int>(n)); return true;
        default: break;
    }

    const int block = ggml_blck_size(type);
    const size_t bsize = ggml_type_size(type);
    if (!block || !bsize || n % static_cast<uint64_t>(block) != 0) return false;

    for (uint64_t i = 0; i < n; i += static_cast<uint64_t>(block)) {
        switch (type) {
            case GgmlType::Q4_0: deq_q4_0(p, dst + i); break;
            case GgmlType::Q4_1: deq_q4_1(p, dst + i); break;
            case GgmlType::Q5_0: deq_q5_0(p, dst + i); break;
            case GgmlType::Q5_1: deq_q5_1(p, dst + i); break;
            case GgmlType::Q8_0: deq_q8_0(p, dst + i); break;
            case GgmlType::Q2_K: deq_q2_K(p, dst + i); break;
            case GgmlType::Q3_K: deq_q3_K(p, dst + i); break;
            case GgmlType::Q4_K: deq_q4_K(p, dst + i); break;
            case GgmlType::Q5_K: deq_q5_K(p, dst + i); break;
            case GgmlType::Q6_K: deq_q6_K(p, dst + i); break;
            case GgmlType::Q8_K: deq_q8_K(p, dst + i); break;
            case GgmlType::MXFP4: deq_mxfp4(p, dst + i); break;
            default: return false;
        }
        p += bsize;
    }
    return true;
}

float vec_dot(GgmlType type, const void* src, const float* y, uint64_t n, bool* ok) {
    if (ok) *ok = true;
    if (!src || !y) { if (ok) *ok = false; return 0.0f; }

    switch (type) {
        case GgmlType::F32:
            return dot_f32(static_cast<const float*>(src), y, static_cast<size_t>(n));
        case GgmlType::F16: {
            float buf[64];
            const uint16_t* s = static_cast<const uint16_t*>(src);
            float sum = 0;
            for (uint64_t i = 0; i < n; i += 64) {
                const int k = static_cast<int>(std::min<uint64_t>(64, n - i));
                for (int j = 0; j < k; ++j) buf[j] = h2f(s[i + j]);
                sum += dot_f32(buf, y + i, static_cast<size_t>(k));
            }
            return sum;
        }
        case GgmlType::BF16: {
            float buf[64];
            const uint16_t* s = static_cast<const uint16_t*>(src);
            float sum = 0;
            for (uint64_t i = 0; i < n; i += 64) {
                const int k = static_cast<int>(std::min<uint64_t>(64, n - i));
                for (int j = 0; j < k; ++j) buf[j] = bf16_to_f32(s[i + j]);
                sum += dot_f32(buf, y + i, static_cast<size_t>(k));
            }
            return sum;
        }
        case GgmlType::Q4_0: return g_kernels.q4_0(src, y, n);
        case GgmlType::Q8_0: return g_kernels.q8_0(src, y, n);
        case GgmlType::Q4_K: return g_kernels.q4_K(src, y, n);
        case GgmlType::Q6_K: return g_kernels.q6_K(src, y, n);
        default: break;
    }

    // Remaining formats share the generic block loop.
    switch (type) {
        case GgmlType::Q4_1: return dot_blockwise(src, y, n, 32, 20, deq_q4_1);
        case GgmlType::Q5_0: return dot_blockwise(src, y, n, 32, 22, deq_q5_0);
        case GgmlType::Q5_1: return dot_blockwise(src, y, n, 32, 24, deq_q5_1);
        case GgmlType::Q2_K: return dot_blockwise(src, y, n, 256, 84, deq_q2_K);
        case GgmlType::Q3_K: return dot_blockwise(src, y, n, 256, 110, deq_q3_K);
        case GgmlType::Q5_K: return dot_blockwise(src, y, n, 256, 176, deq_q5_K);
        case GgmlType::Q8_K: return dot_blockwise(src, y, n, 256, 292, deq_q8_K);
        case GgmlType::MXFP4: return dot_blockwise(src, y, n, 32, 17, deq_mxfp4);
        default:
            if (ok) *ok = false;
            return 0.0f;
    }
}

const QuantKernels& quant_kernels() { return g_kernels; }

float dot_f32_fast(const float* a, const float* b, uint64_t n) {
    return g_dot_f32(a, b, n);
}

bool avx2_available() {
#if defined(FISH_HAVE_X86_KERNELS) && FISH_HAVE_X86_KERNELS
    return fish_x86_avx2();
#else
    return false;
#endif
}

void install_avx2_kernels() {
#if defined(FISH_HAVE_X86_KERNELS) && FISH_HAVE_X86_KERNELS
    fish_install_x86(g_kernels, &g_dot_f32);
#endif
}

namespace {
// Installed before any model can load, so the dispatch pointers are never
// observed in a half-initialised state.
struct KernelInstaller {
    KernelInstaller() { install_avx2_kernels(); }
} g_kernel_installer;
} // namespace

} // namespace fish
