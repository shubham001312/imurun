// FISH Vulkan backend — in-shader dequantisation of GGUF weight blocks.
//
// The weight buffer is bound as an array of 32-bit words and every byte is
// extracted with shifts. That is the portable path: it needs no 8/16-bit
// storage feature, so the same SPIR-V runs on every Vulkan 1.1 device
// (desktop NVIDIA/AMD/Intel and mobile GPUs that lack storageBuffer8BitAccess).
// Block sizes 18/34/210 bytes are not 4-aligned, so reads may straddle two
// words; the host pads every weight buffer by 16 bytes so the second word of
// the last read always exists.
//
// One primitive per type: dequant8(row_byte_offset, k, v) writes the 8
// consecutive row elements k..k+7 (k is a multiple of 8) to v. Both kernels
// (matvec.comp, matmul_tiled.comp) are built on it, so adding a type means
// adding one function here and one -D variant in CMakeLists.txt.
//
// Exactly one of TYPE_F32, TYPE_F16, TYPE_Q4_0, TYPE_Q8_0, TYPE_Q4_K,
// TYPE_Q5_K, TYPE_Q6_K is defined by the build.

layout(std430, binding = 0) readonly buffer WBuf { uint w[]; };

uint rd8(uint o) {
    return (w[o >> 2] >> ((o & 3u) << 3)) & 0xFFu;
}

// 16-bit little-endian read at an even byte offset.
uint rd16(uint o) {
    return (w[o >> 2] >> ((o & 2u) << 3)) & 0xFFFFu;
}

// 32-bit little-endian read at any byte offset.
uint rd32(uint o) {
    const uint s = (o & 3u) << 3;
    const uint lo = w[o >> 2];
    if (s == 0u) return lo;
    return (lo >> s) | (w[(o >> 2) + 1u] << (32u - s));
}

float rdh(uint o) { return unpackHalf2x16(rd16(o)).x; }

// Eight consecutive 32-bit words starting at any byte offset: nine loads and
// a funnel shift instead of sixteen loads through rd32.
void rd32x8(uint o, out uint q[8]) {
    const uint s = (o & 3u) << 3;
    const uint w0 = o >> 2;
    if (s == 0u) {
        for (uint i = 0u; i < 8u; ++i) q[i] = w[w0 + i];
    } else {
        uint prev = w[w0];
        for (uint i = 0u; i < 8u; ++i) {
            const uint nx = w[w0 + i + 1u];
            q[i] = (prev >> s) | (nx << (32u - s));
            prev = nx;
        }
    }
}

// Byte i (0..3) of a word as unsigned / signed.
uint  ub(uint x, uint i) { return (x >> (i << 3)) & 0xFFu; }
int   sb(uint x, uint i) { return int(x << (24u - (i << 3))) >> 24; }

#if defined(TYPE_F32)
// ------------------------------------------------------------------ F32
const uint BLOCK_K = 1u;
void dequant8(uint ro, uint k, out float v[8]) {
    const uint o = (ro + k * 4u) >> 2;   // F32 rows are 4-aligned
    for (uint i = 0u; i < 8u; ++i) v[i] = uintBitsToFloat(w[o + i]);
}

#elif defined(TYPE_F16)
// ------------------------------------------------------------------ F16
const uint BLOCK_K = 1u;
void dequant8(uint ro, uint k, out float v[8]) {
    const uint o = ro + k * 2u;
    for (uint i = 0u; i < 4u; ++i) {
        const vec2 h = unpackHalf2x16(rd32(o + i * 4u));
        v[2u * i] = h.x;
        v[2u * i + 1u] = h.y;
    }
}

#elif defined(TYPE_Q4_0)
// ------------------------------------------------------------------ Q4_0
// 18 B / 32: f16 d, qs[16]. Element j<16 = low nibble of qs[j], j>=16 = high
// nibble of qs[j-16]; value = d * (q - 8).
const uint BLOCK_K = 32u;
void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 5) * 18u;
    const uint kk = k & 31u;
    const float d = rdh(b);
    const uint sh = kk >= 16u ? 4u : 0u;
    const uint o = b + 2u + (kk & 15u);
    const uint q0 = rd32(o), q1 = rd32(o + 4u);
    for (uint i = 0u; i < 4u; ++i) {
        v[i]      = d * (float((ub(q0, i) >> sh) & 15u) - 8.0);
        v[i + 4u] = d * (float((ub(q1, i) >> sh) & 15u) - 8.0);
    }
}

#elif defined(TYPE_Q8_0)
// ------------------------------------------------------------------ Q8_0
// 34 B / 32: f16 d, int8 qs[32]; value = d * q.
const uint BLOCK_K = 32u;
void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 5) * 34u;
    const float d = rdh(b);
    const uint o = b + 2u + (k & 31u);
    const uint q0 = rd32(o), q1 = rd32(o + 4u);
    for (uint i = 0u; i < 4u; ++i) {
        v[i]      = d * float(sb(q0, i));
        v[i + 4u] = d * float(sb(q1, i));
    }
}

#elif defined(TYPE_Q4_K) || defined(TYPE_Q5_K)
// ------------------------------------------------------------------ Q4_K / Q5_K
// Q4_K 144 B / 256: f16 d, f16 dmin, scales[12] (8 x 6-bit scale + 6-bit min),
//                   qs[128]. Sub-block s (32 values): pair p = s/2; elements
//                   are the low (s even) or high (s odd) nibbles of qs[32p..32p+31].
// Q5_K 176 B / 256: same header, qh[32] at 16, qs[128] at 48; the fifth bit of
//                   element l of sub-block s is bit s of qh[l].
// value = d * sc * q - dmin * m.
const uint BLOCK_K = 256u;
#if defined(TYPE_Q4_K)
const uint BLOCK_B = 144u;
const uint QS_OFF = 16u;
#else
const uint BLOCK_B = 176u;
const uint QS_OFF = 48u;
#endif
void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint kk = k & 255u;
    const uint s = kk >> 5;
    const uint l0 = kk & 31u;
    const vec2 dm = unpackHalf2x16(rd32(b));
    uint sc, m;
    if (s < 4u) {
        sc = rd8(b + 4u + s) & 63u;
        m  = rd8(b + 8u + s) & 63u;
    } else {
        const uint e = rd8(b + 8u + s);
        sc = (e & 15u) | ((rd8(b + s) >> 6) << 4);
        m  = (e >> 4)  | ((rd8(b + 4u + s) >> 6) << 4);
    }
    const float d1 = dm.x * float(sc);
    const float m1 = dm.y * float(m);
    const uint sh = (s & 1u) << 2;
    const uint o = b + QS_OFF + ((s >> 1) << 5) + l0;
    const uint q0 = rd32(o), q1 = rd32(o + 4u);
#if defined(TYPE_Q5_K)
    const uint h0 = rd32(b + 16u + l0), h1 = rd32(b + 20u + l0);
    for (uint i = 0u; i < 4u; ++i) {
        v[i]      = d1 * float(((ub(q0, i) >> sh) & 15u) | (((ub(h0, i) >> s) & 1u) << 4)) - m1;
        v[i + 4u] = d1 * float(((ub(q1, i) >> sh) & 15u) | (((ub(h1, i) >> s) & 1u) << 4)) - m1;
    }
#else
    for (uint i = 0u; i < 4u; ++i) {
        v[i]      = d1 * float((ub(q0, i) >> sh) & 15u) - m1;
        v[i + 4u] = d1 * float((ub(q1, i) >> sh) & 15u) - m1;
    }
#endif
}

// 32 elements = one whole sub-block: one scale decode, eight word loads.
#define HAVE_DEQUANT32 1
void dequant32(uint ro, uint k, out float v[32]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint s = (k & 255u) >> 5;
    const vec2 dm = unpackHalf2x16(rd32(b));
    uint sc, m;
    if (s < 4u) {
        sc = rd8(b + 4u + s) & 63u;
        m  = rd8(b + 8u + s) & 63u;
    } else {
        const uint e = rd8(b + 8u + s);
        sc = (e & 15u) | ((rd8(b + s) >> 6) << 4);
        m  = (e >> 4)  | ((rd8(b + 4u + s) >> 6) << 4);
    }
    // Whole words at a time: mask 4 nibbles with one AND, convert 4 bytes
    // with one unpackUnorm4x8 (byte/255; the 255 is folded into the scale).
    const float d255 = dm.x * float(sc) * 255.0;
    const float m1 = dm.y * float(m);
    const uint sh = (s & 1u) << 2;
    uint q[8];
    rd32x8(b + QS_OFF + ((s >> 1) << 5), q);
#if defined(TYPE_Q5_K)
    uint h[8];
    rd32x8(b + 16u, h);
    for (uint i = 0u; i < 8u; ++i) {
        const uint w4 = ((q[i] >> sh) & 0x0F0F0F0Fu) | (((h[i] >> s) & 0x01010101u) << 4);
        const vec4 f = unpackUnorm4x8(w4) * d255 - m1;
        v[4u * i] = f.x; v[4u * i + 1u] = f.y; v[4u * i + 2u] = f.z; v[4u * i + 3u] = f.w;
    }
#else
    for (uint i = 0u; i < 8u; ++i) {
        const vec4 f = unpackUnorm4x8((q[i] >> sh) & 0x0F0F0F0Fu) * d255 - m1;
        v[4u * i] = f.x; v[4u * i + 1u] = f.y; v[4u * i + 2u] = f.z; v[4u * i + 3u] = f.w;
    }
#endif
}

#elif defined(TYPE_Q6_K)
// ------------------------------------------------------------------ Q6_K
// 210 B / 256: ql[128], qh[64] at 128, int8 scales[16] at 192, f16 d at 208.
// Element kk: half n = kk/128, quarter q = (kk%128)/32, l = kk%32:
//   low 4 bits  = ql[64n + l + 32(q&1)] >> 4(q>>1)
//   high 2 bits = qh[32n + l] >> 2q
//   scale       = scales[8n + l/16 + 2q]
// value = d * scale * (q6 - 32).
const uint BLOCK_K = 256u;
void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 8) * 210u;
    const uint kk = k & 255u;
    const uint n = kk >> 7;
    const uint q = (kk >> 5) & 3u;
    const uint l0 = kk & 31u;
    const float d = rdh(b + 208u);
    const float dl = d * float(int(rd8(b + 192u + 8u * n + (l0 >> 4) + 2u * q) << 24) >> 24);
    const uint ol = b + 64u * n + l0 + 32u * (q & 1u);
    const uint oh = b + 128u + 32u * n + l0;
    const uint sl = (q >> 1) << 2;
    const uint shh = q << 1;
    const uint a0 = rd32(ol), a1 = rd32(ol + 4u);
    const uint c0 = rd32(oh), c1 = rd32(oh + 4u);
    for (uint i = 0u; i < 4u; ++i) {
        v[i]      = dl * float(int(((ub(a0, i) >> sl) & 15u) | (((ub(c0, i) >> shh) & 3u) << 4)) - 32);
        v[i + 4u] = dl * float(int(((ub(a1, i) >> sl) & 15u) | (((ub(c1, i) >> shh) & 3u) << 4)) - 32);
    }
}

#define HAVE_DEQUANT32 1
void dequant32(uint ro, uint k, out float v[32]) {
    const uint b = ro + (k >> 8) * 210u;
    const uint kk = k & 255u;
    const uint n = kk >> 7;
    const uint q = (kk >> 5) & 3u;
    const float d = rdh(b + 208u);
    const uint so = b + 192u + 8u * n + 2u * q;
    const float dl0 = d * float(int(rd8(so) << 24) >> 24);
    const float dl1 = d * float(int(rd8(so + 1u) << 24) >> 24);
    const uint sl = (q >> 1) << 2;
    const uint shh = q << 1;
    uint a[8], c[8];
    rd32x8(b + 64u * n + 32u * (q & 1u), a);
    rd32x8(b + 128u + 32u * n, c);
    // 4 six-bit values per word with two ANDs/one OR; one unpackUnorm4x8.
    // value = dl * (q6 - 32) = (dl * 255) * (q6 / 255) - 32 * dl.
    for (uint i = 0u; i < 8u; ++i) {
        const float dl = i < 4u ? dl0 : dl1;
        const uint w4 = ((a[i] >> sl) & 0x0F0F0F0Fu) | (((c[i] >> shh) & 0x03030303u) << 4);
        const vec4 f = unpackUnorm4x8(w4) * (dl * 255.0) - 32.0 * dl;
        v[4u * i] = f.x; v[4u * i + 1u] = f.y; v[4u * i + 2u] = f.z; v[4u * i + 3u] = f.w;
    }
}

#elif defined(TYPE_FQ3)
// ------------------------------------------------------------------ FQ3
// FISH 3-bit re-quantised layout (src/gpu/requant.h), 112 B / 256:
// f16 d, f16 dmin, 12 B Q4_K-style 6-bit scale/min, then per 32-value group
// three words A, B, C: values 4s..4s+3 = bytes of
//   ((s < 4 ? A : B) >> 2(s & 3)) & 0x03030303  |  ((C >> s) & 0x01010101) << 2
// value = d * sc * q - dmin * m. Rows are 4-aligned (112 = 28 words).
const uint BLOCK_K = 256u;
const uint BLOCK_B = 112u;

void fq3_scale(uint b, uint s, out float d255, out float m1) {
    const vec2 dm = unpackHalf2x16(w[b >> 2]);
    uint sc, m;
    if (s < 4u) {
        sc = rd8(b + 4u + s) & 63u;
        m  = rd8(b + 8u + s) & 63u;
    } else {
        const uint e = rd8(b + 8u + s);
        sc = (e & 15u) | ((rd8(b + s) >> 6) << 4);
        m  = (e >> 4)  | ((rd8(b + 4u + s) >> 6) << 4);
    }
    d255 = dm.x * float(sc) * 255.0;
    m1 = dm.y * float(m);
}

vec4 fq3_quad(uint A, uint B, uint C, uint s, float d255, float m1) {
    const uint lo = ((s < 4u ? A : B) >> ((s & 3u) << 1)) & 0x03030303u;
    const uint hi = ((C >> s) & 0x01010101u) << 2;
    return unpackUnorm4x8(lo | hi) * d255 - m1;
}

void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint g = (k & 255u) >> 5;
    const uint s0 = (k & 31u) >> 2;           // 0, 2, 4, 6
    float d255, m1;
    fq3_scale(b, g, d255, m1);
    const uint o = (b + 16u + 12u * g) >> 2;
    const uint A = w[o], B = w[o + 1u], C = w[o + 2u];
    const vec4 f0 = fq3_quad(A, B, C, s0, d255, m1), f1 = fq3_quad(A, B, C, s0 + 1u, d255, m1);
    v[0] = f0.x; v[1] = f0.y; v[2] = f0.z; v[3] = f0.w;
    v[4] = f1.x; v[5] = f1.y; v[6] = f1.z; v[7] = f1.w;
}

#define HAVE_DEQUANT32 1
void dequant32(uint ro, uint k, out float v[32]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint g = (k & 255u) >> 5;
    float d255, m1;
    fq3_scale(b, g, d255, m1);
    const uint o = (b + 16u + 12u * g) >> 2;
    const uint A = w[o], B = w[o + 1u], C = w[o + 2u];
    for (uint s = 0u; s < 8u; ++s) {
        const vec4 f = fq3_quad(A, B, C, s, d255, m1);
        v[4u * s] = f.x; v[4u * s + 1u] = f.y; v[4u * s + 2u] = f.z; v[4u * s + 3u] = f.w;
    }
}

#elif defined(TYPE_FQ3S)
// ------------------------------------------------------------------ FQ3S
// FISH 3-bit symmetric layout (src/gpu/requant.h), 104 B / 256:
// [0] f16 d, [2] 6 B = 8 x 6-bit group scales (bit stream), [8] per 32-value
// group the FQ3 words A, B, C. value = d * sc * L(q) with
// u = (2q - 7) / 7 and L = u (1 - c + c u^2): one FMA to get u from the
// unpackUnorm4x8 byte, then u * (a + b u^2) with a, b folded per group.
// Rows are 4-aligned (104 = 26 words).
const uint BLOCK_K = 256u;
const uint BLOCK_B = 104u;
const float FQ3S_C = 0.20;   // = kFQ3SCubic

float fq3s_scale(uint b, uint g) {
    const uint w0 = w[b >> 2], w1 = w[(b >> 2) + 1u];
    const float d = unpackHalf2x16(w0).x;
    const uint lo = (w0 >> 16) | (w1 << 16);   // scale bits 0..31
    const uint hi = w1 >> 16;                  // scale bits 32..47
    const uint p = 6u * g;
    uint s;
    if (p + 6u <= 32u) s = (lo >> p) & 63u;
    else if (p >= 32u) s = (hi >> (p - 32u)) & 63u;
    else s = ((lo >> p) | (hi << (32u - p))) & 63u;
    return d * float(s);
}

vec4 fq3s_quad(uint A, uint B, uint C, uint s, float a, float bc) {
    const uint lo = ((s < 4u ? A : B) >> ((s & 3u) << 1)) & 0x03030303u;
    const uint hi = ((C >> s) & 0x01010101u) << 2;
    const vec4 u = unpackUnorm4x8(lo | hi) * (510.0 / 7.0) - 1.0;
    return u * (u * u * bc + a);
}

void dequant8(uint ro, uint k, out float v[8]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint g = (k & 255u) >> 5;
    const uint s0 = (k & 31u) >> 2;           // 0, 2, 4, 6
    const float sc = fq3s_scale(b, g);
    const float a = sc * (1.0 - FQ3S_C), bc = sc * FQ3S_C;
    const uint o = (b + 8u + 12u * g) >> 2;
    const uint A = w[o], B = w[o + 1u], C = w[o + 2u];
    const vec4 f0 = fq3s_quad(A, B, C, s0, a, bc), f1 = fq3s_quad(A, B, C, s0 + 1u, a, bc);
    v[0] = f0.x; v[1] = f0.y; v[2] = f0.z; v[3] = f0.w;
    v[4] = f1.x; v[5] = f1.y; v[6] = f1.z; v[7] = f1.w;
}

#define HAVE_DEQUANT32 1
void dequant32(uint ro, uint k, out float v[32]) {
    const uint b = ro + (k >> 8) * BLOCK_B;
    const uint g = (k & 255u) >> 5;
    const float sc = fq3s_scale(b, g);
    const float a = sc * (1.0 - FQ3S_C), bc = sc * FQ3S_C;
    const uint o = (b + 8u + 12u * g) >> 2;
    const uint A = w[o], B = w[o + 1u], C = w[o + 2u];
    for (uint s = 0u; s < 8u; ++s) {
        const vec4 f = fq3s_quad(A, B, C, s, a, bc);
        v[4u * s] = f.x; v[4u * s + 1u] = f.y; v[4u * s + 2u] = f.z; v[4u * s + 3u] = f.w;
    }
}

#else
#error "no weight type defined"
#endif

#ifndef HAVE_DEQUANT32
// Types whose block is 32 elements (or 1): four 8-element pieces.
void dequant32(uint ro, uint k, out float v[32]) {
    for (uint p = 0u; p < 4u; ++p) {
        float t[8];
        dequant8(ro, k + 8u * p, t);
        for (uint i = 0u; i < 8u; ++i) v[8u * p + i] = t[i];
    }
}
#endif
