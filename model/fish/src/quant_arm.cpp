/*
 * FISH — NEON kernels for arm64 (Apple Silicon, Linux/Windows on ARM).
 *
 * The arm64 counterpart of quant_x86.cpp: the same QuantKernels slots, the
 * same block layouts, validated against the same scalar reference
 * (test/test_kernels.cpp, test/test_m1.cpp).
 *
 * Two kernel sets are compiled from one body (quant_arm_kernels.inc):
 *
 *   "neon-dotprod"  integer dot products with SDOT (vdotq_s32, ARMv8.2
 *                   DotProd): 16 int8 x int8 products summed into four
 *                   int32 lanes per instruction. Every Apple M-series and
 *                   A12+ core has it, as do Cortex-A55/A76 and later.
 *   "neon"          plain ARMv8.0 NEON (vmull_s8 + vpadalq_s16) for cores
 *                   without DotProd (Cortex-A53/A72, e.g. Raspberry Pi 4).
 *
 * NEON itself is mandatory on AArch64, so the plain set needs no check. The
 * dotprod set is installed when the build baseline already includes DotProd
 * (Apple clang targets apple-m1 by default) or a runtime check finds it.
 * FISH_NEON_DOTPROD=0 in the environment forces the plain set (comparison).
 *
 * Unlike AVX2's vpmaddubsw (unsigned x signed), SDOT multiplies signed by
 * signed, so offsets such as Q4_0's "- 8" and Q6_K's "- 32" are applied to
 * the weight bytes before the dot product instead of through a separate
 * activation-sum term.
 *
 * FISH_NEON_EMULATE compiles this file on x86 against test/neon_emu/arm_neon.h
 * (a scalar model of the intrinsics used here), so the kernel logic can be
 * run and checked against the scalar reference on a machine without ARM
 * hardware (target fish_test_kernels_emu). It proves the indexing and the
 * arithmetic, not the compiler's code generation for real NEON.
 */
#include <fish/quant.h>

#if defined(__aarch64__) || defined(_M_ARM64) || defined(FISH_NEON_EMULATE)

#include <arm_neon.h>
#include <cstdlib>
#include <cstring>

#if !defined(FISH_NEON_EMULATE)
#  if defined(__linux__) || defined(__ANDROID__)
#    include <sys/auxv.h>
#  elif defined(__APPLE__)
#    include <sys/sysctl.h>
#  elif defined(_WIN32)
#    include <windows.h>
#  endif
#endif

// How the dotprod set is compiled:
//   baseline   the target already has DotProd (__ARM_FEATURE_DOTPROD)
//   attribute  per-function target attribute, installed after a runtime check
//   none       compiler too old to scope vdotq_s32 to a function: plain set only
#if defined(FISH_NEON_EMULATE) || defined(__ARM_FEATURE_DOTPROD)
#  define FISH_DOTPROD_ATTR
#  define FISH_DOTPROD_BUILT 1
#elif defined(__clang__) && __clang_major__ >= 17
#  define FISH_DOTPROD_ATTR __attribute__((target("dotprod")))
#  define FISH_DOTPROD_BUILT 1
#elif defined(__GNUC__) && !defined(__clang__) && __GNUC__ >= 10
#  define FISH_DOTPROD_ATTR __attribute__((target("arch=armv8.2-a+dotprod")))
#  define FISH_DOTPROD_BUILT 1
#else
#  define FISH_DOTPROD_BUILT 0
#endif

namespace fish {
namespace {

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

// The 12 packed scale bytes of a Q4_K/Q5_K block -> out[0..7] the eight 6-bit
// scales, out[8..15] the eight 6-bit mins (same bit twiddling as the AVX2 set;
// equivalent to scale_min_k4 for j = 0..7).
inline void k4_unpack(const uint8_t* s, uint8_t* out) {
    uint32_t u[4];
    std::memcpy(u, s, 12);
    const uint32_t kmask1 = 0x3f3f3f3fu, kmask2 = 0x0f0f0f0fu, kmask3 = 0x03030303u;
    u[3] = ((u[2] >> 4) & kmask2) | (((u[1] >> 6) & kmask3) << 4);
    const uint32_t uaux = u[1] & kmask1;
    u[1] = (u[2] & kmask2) | (((u[0] >> 6) & kmask3) << 4);
    u[2] = uaux;
    u[0] &= kmask1;
    std::memcpy(out, u, 16);
}

} // namespace

namespace neon_plain {
namespace {
#define FISH_K
#define FISH_K_DOT 0
#include "quant_arm_kernels.inc"
#undef FISH_K
#undef FISH_K_DOT
} // namespace
} // namespace neon_plain

#if FISH_DOTPROD_BUILT
namespace neon_dot {
namespace {
#define FISH_K FISH_DOTPROD_ATTR
#define FISH_K_DOT 1
#include "quant_arm_kernels.inc"
#undef FISH_K
#undef FISH_K_DOT
} // namespace
} // namespace neon_dot
#endif

bool fish_arm_dotprod() {
#if !FISH_DOTPROD_BUILT
    return false;
#elif defined(FISH_NEON_EMULATE) || defined(__ARM_FEATURE_DOTPROD)
    return true;   // the build baseline already requires it
#elif defined(__linux__) || defined(__ANDROID__)
#  ifndef HWCAP_ASIMDDP
#    define HWCAP_ASIMDDP (1UL << 20)
#  endif
    return (getauxval(AT_HWCAP) & HWCAP_ASIMDDP) != 0;
#elif defined(__APPLE__)
    int v = 0;
    size_t len = sizeof(v);
    if (sysctlbyname("hw.optional.arm.FEAT_DotProd", &v, &len, nullptr, 0) == 0) return v != 0;
    return true;   // key missing before macOS 12; every Apple Silicon Mac has DotProd
#elif defined(_WIN32)
#  ifndef PF_ARM_V82_DP_INSTRUCTIONS_AVAILABLE
#    define PF_ARM_V82_DP_INSTRUCTIONS_AVAILABLE 43
#  endif
    return IsProcessorFeaturePresent(PF_ARM_V82_DP_INSTRUCTIONS_AVAILABLE) != 0;
#else
    return false;
#endif
}

bool fish_install_arm(QuantKernels& k,
                      float (**dot_f32)(const float*, const float*, uint64_t),
                      bool allow_dotprod) {
#if FISH_DOTPROD_BUILT
    if (allow_dotprod && fish_arm_dotprod()) {
        neon_dot::fill(k, dot_f32, "neon-dotprod");
        return true;
    }
#endif
    (void)allow_dotprod;
    neon_plain::fill(k, dot_f32, "neon");
    return false;
}

bool fish_arm_dotprod_allowed() {
    const char* e = std::getenv("FISH_NEON_DOTPROD");
    return !(e && e[0] == '0' && e[1] == '\0');
}

} // namespace fish

#endif // arm64
