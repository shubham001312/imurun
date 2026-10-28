#pragma once
/*
 * FISH Platform Abstraction — Zero-overhead OS/hardware primitives
 * Every function here maps to 1-2 CPU instructions or a single syscall.
 * No heap allocation, no locks, no exceptions.
 */

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <atomic>

namespace fish::platform {

// ============================================================================
// Compiler / Architecture
// ============================================================================
#if defined(_MSC_VER)
  #define FISH_FORCE_INLINE __forceinline
  #define FISH_NOINLINE __declspec(noinline)
  #define FISH_ALIGN(x) __declspec(align(x))
  #define FISH_ASSUME(x) __assume(x)
  #define FISH_UNREACHABLE() __assume(false)
  #define FISH_PREFETCH(addr, rw, locality) _mm_prefetch((const char*)(addr), (locality))
#else
  #define FISH_FORCE_INLINE __attribute__((always_inline)) inline
  #define FISH_NOINLINE __attribute__((noinline))
  #define FISH_ALIGN(x) __attribute__((aligned(x)))
  #define FISH_ASSUME(x) if(!(x)) __builtin_unreachable()
  #define FISH_UNREACHABLE() __builtin_unreachable()
  #define FISH_PREFETCH(addr, rw, locality) __builtin_prefetch((addr), (rw), (locality))
#endif

#if defined(__x86_64__) || defined(_M_X64)
  #define FISH_ARCH_X64 1
  #include <immintrin.h>
#elif defined(__aarch64__)
  #define FISH_ARCH_ARM64 1
  #include <arm_neon.h>
#else
  #error "Unsupported architecture"
#endif

// ============================================================================
// Cache Line & Page Constants
// ============================================================================
constexpr size_t CACHE_LINE = 64;
constexpr size_t PAGE_4K = 4096;
constexpr size_t PAGE_2M = 2 * 1024 * 1024;
constexpr size_t PAGE_1G = 1024 * 1024 * 1024;

// ============================================================================
// Atomic Intrinsics (lock-free primitives)
// ============================================================================
template<typename T>
FISH_FORCE_INLINE T atomic_load_relaxed(const std::atomic<T>* p) noexcept {
    return p->load(std::memory_order_relaxed);
}
template<typename T>
FISH_FORCE_INLINE void atomic_store_relaxed(std::atomic<T>* p, T v) noexcept {
    p->store(v, std::memory_order_relaxed);
}
template<typename T>
FISH_FORCE_INLINE bool cas_acq_rel(std::atomic<T>* p, T& expected, T desired) noexcept {
    return p->compare_exchange_strong(expected, desired,
                                       std::memory_order_acq_rel,
                                       std::memory_order_relaxed);
}
template<typename T>
FISH_FORCE_INLINE T atomic_fetch_add_acq_rel(std::atomic<T>* p, T v) noexcept {
    return p->fetch_add(v, std::memory_order_acq_rel);
}

// ============================================================================
// Memory Allocation — Huge-page, NUMA-aware, zero-overhead
// ============================================================================
// Use plain enum (not enum class) to allow bitwise operations without casts
enum AllocFlags : uint32_t {
    ALLOC_NONE          = 0,
    ALLOC_HUGE_2M       = 1u << 0,   // 2 MB pages
    ALLOC_HUGE_1G       = 1u << 1,   // 1 GB pages (Linux only, needs hugetlbfs)
    ALLOC_NUMA_LOCAL    = 1u << 2,   // Allocate on current NUMA node
    ALLOC_NUMA_SPECIFIC = 1u << 3,   // Use numa_node parameter
    ALLOC_PINNED        = 1u << 4,   // mlock / VirtualLock — never swapped
    ALLOC_UNINITIALIZED = 1u << 5,   // Skip zeroing (MADV_FREE / MEM_RESET)
    ALLOC_EXECUTABLE    = 1u << 6,   // RWX for JIT
};

// Bitwise operators for AllocFlags
FISH_FORCE_INLINE AllocFlags operator|(AllocFlags a, AllocFlags b) noexcept {
    return static_cast<AllocFlags>(static_cast<uint32_t>(a) | static_cast<uint32_t>(b));
}
FISH_FORCE_INLINE AllocFlags operator&(AllocFlags a, AllocFlags b) noexcept {
    return static_cast<AllocFlags>(static_cast<uint32_t>(a) & static_cast<uint32_t>(b));
}
FISH_FORCE_INLINE AllocFlags operator^(AllocFlags a, AllocFlags b) noexcept {
    return static_cast<AllocFlags>(static_cast<uint32_t>(a) ^ static_cast<uint32_t>(b));
}
FISH_FORCE_INLINE AllocFlags operator~(AllocFlags a) noexcept {
    return static_cast<AllocFlags>(~static_cast<uint32_t>(a));
}
FISH_FORCE_INLINE AllocFlags& operator|=(AllocFlags& a, AllocFlags b) noexcept {
    a = a | b; return a;
}
FISH_FORCE_INLINE AllocFlags& operator&=(AllocFlags& a, AllocFlags b) noexcept {
    a = a & b; return a;
}

// Allocate `size` bytes, aligned to `alignment` (must be power of 2, >= 64).
// Returns nullptr on failure.  `numa_node` ignored unless NUMA_SPECIFIC set.
void* alloc_aligned(size_t size, size_t alignment, AllocFlags flags = AllocFlags::NONE, int numa_node = -1) noexcept;
void  free_aligned(void* ptr, size_t size) noexcept;

// Query huge-page availability
bool huge_pages_2m_available() noexcept;
bool huge_pages_1g_available() noexcept;

// Current NUMA node of calling thread
int current_numa_node() noexcept;
int numa_node_count() noexcept;

// Pin current thread to a CPU core (Linux: sched_setaffinity, Windows: SetThreadAffinityMask)
void pin_thread_to_core(int core_id) noexcept;

// ============================================================================
// High-Resolution Timing (TSC / QPC / CNTVCT)
// ============================================================================
uint64_t rdtsc() noexcept;                    // Raw TSC (x86) / CNTVCT (ARM)
uint64_t monotonic_ns() noexcept;             // Calibrated nanoseconds
double   tscto_ns(uint64_t tsc) noexcept;     // Convert TSC delta → ns

// ============================================================================
// SIMD Helpers (compile to single instructions)
// ============================================================================
// Prefetch: locality 0=T0, 1=T1, 2=T2, 3=NTA
FISH_FORCE_INLINE void prefetch_read(const void* addr, int locality = 3) noexcept {
#if defined(_MSC_VER)
    _mm_prefetch((const char*)addr, locality);
#elif defined(__GNUC__)
    __builtin_prefetch(addr, 0, locality);
#endif
}
FISH_FORCE_INLINE void prefetch_write(const void* addr, int locality = 3) noexcept {
#if defined(_MSC_VER)
    _mm_prefetch((const char*)addr, locality | _MM_HINT_NTA);
#elif defined(__GNUC__)
    __builtin_prefetch(addr, 1, locality);
#endif
}

// Memory fence
FISH_FORCE_INLINE void sfence() noexcept {
#if FISH_ARCH_X64
    _mm_sfence();
#elif FISH_ARCH_ARM64
    __asm__ volatile("dsb sy" ::: "memory");
#endif
}
FISH_FORCE_INLINE void lfence() noexcept {
#if FISH_ARCH_X64
    _mm_lfence();
#elif FISH_ARCH_ARM64
    __asm__ volatile("dsb ld" ::: "memory");
#endif
}
FISH_FORCE_INLINE void mfence() noexcept {
#if FISH_ARCH_X64
    _mm_mfence();
#elif FISH_ARCH_ARM64
    __asm__ volatile("dsb sy" ::: "memory");
#endif
}

// ============================================================================
// Hardware Performance Counters (PMU) — zero-overhead sampling
// ============================================================================
struct PmuCounters {
    uint64_t cycles = 0;
    uint64_t instructions = 0;
    uint64_t l1_misses = 0;
    uint64_t llc_misses = 0;
    uint64_t branch_mispredicts = 0;
    uint64_t memory_bytes = 0;   // Estimated from LLC misses * line size
};

class PmuSession {
public:
    PmuSession() noexcept;
    ~PmuSession() noexcept;
    PmuCounters snapshot() noexcept;
    void reset() noexcept;
private:
    int fd_ = -1; // perf_event_open fd (Linux) / NULL (Windows stub)
};

// ============================================================================
// Lock-Free MPSC Ring Buffer (single-producer, multi-consumer)
// Used for: token stream, prefetch completions, stats
// ============================================================================
template<typename T, size_t CAPACITY>
requires ((CAPACITY & (CAPACITY - 1)) == 0) // power of 2
class MpscRing {
    FISH_ALIGN(CACHE_LINE) std::atomic<size_t> head_{0}; // producer index
    FISH_ALIGN(CACHE_LINE) std::atomic<size_t> tail_{0}; // consumer index
    FISH_ALIGN(CACHE_LINE) T slots_[CAPACITY];

public:
    // Returns false if full
    bool try_push(const T& item) noexcept {
        size_t h = head_.load(std::memory_order_relaxed);
        size_t next = (h + 1) & (CAPACITY - 1);
        if (next == tail_.load(std::memory_order_acquire)) return false;
        slots_[h] = item;
        head_.store(next, std::memory_order_release);
        return true;
    }

    // Returns false if empty
    bool try_pop(T& out) noexcept {
        size_t t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;
        out = slots_[t];
        tail_.store((t + 1) & (CAPACITY - 1), std::memory_order_release);
        return true;
    }

    size_t size() const noexcept {
        return (head_.load(std::memory_order_acquire) - tail_.load(std::memory_order_acquire)) & (CAPACITY - 1);
    }
    bool empty() const noexcept { return size() == 0; }
    bool full() const noexcept  { return size() == CAPACITY - 1; }
};

// ============================================================================
// Lock-Free SPSC Ring Buffer (single-producer, single-consumer)
// Lower latency than MPSC, used for hot paths (prefetch → compute)
// ============================================================================
template<typename T, size_t CAPACITY>
requires ((CAPACITY & (CAPACITY - 1)) == 0)
class SpscRing {
    FISH_ALIGN(CACHE_LINE) std::atomic<size_t> head_{0};
    FISH_ALIGN(CACHE_LINE) std::atomic<size_t> tail_{0};
    FISH_ALIGN(CACHE_LINE) T slots_[CAPACITY];

public:
    bool try_push(const T& item) noexcept {
        size_t h = head_.load(std::memory_order_relaxed);
        size_t next = (h + 1) & (CAPACITY - 1);
        if (next == tail_.load(std::memory_order_acquire)) return false;
        slots_[h] = item;
        head_.store(next, std::memory_order_release);
        return true;
    }
    bool try_pop(T& out) noexcept {
        size_t t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;
        out = slots_[t];
        tail_.store((t + 1) & (CAPACITY - 1), std::memory_order_release);
        return true;
    }
    size_t size() const noexcept {
        return (head_.load(std::memory_order_acquire) - tail_.load(std::memory_order_acquire)) & (CAPACITY - 1);
    }
};

} // namespace fish::platform