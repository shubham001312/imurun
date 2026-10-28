// Fix for MinGW GCC 16.2 broken xopintrin.h / fma4intrin.h
// This header must be included FIRST (via -include) to disable problematic AMD intrinsics

#pragma once

// Disable XOP and FMA4 intrinsics which have broken __m128i definitions in MinGW GCC 16.2
#ifndef __XOP__
#define __XOP__ 0
#endif

#ifndef __FMA4__
#define __FMA4__ 0
#endif

// Also disable AVX512 intrinsics that may cause issues
#ifndef __AVX512F__
#define __AVX512F__ 0
#endif

#ifndef __AVX512BW__
#define __AVX512BW__ 0
#endif

#ifndef __AVX512VL__
#define __AVX512VL__ 0
#endif

#ifndef __AVX512DQ__
#define __AVX512DQ__ 0
#endif

#ifndef __AVX512CD__
#define __AVX512CD__ 0
#endif

#ifndef __AVX512ER__
#define __AVX512ER__ 0
#endif

#ifndef __AVX512PF__
#define __AVX512PF__ 0
#endif

// Disable F16C which may also have issues
#ifndef __F16C__
#define __F16C__ 0
#endif