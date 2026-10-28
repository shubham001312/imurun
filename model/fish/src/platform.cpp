#include <fish/platform.h>
#include <cstdlib>
#include <cstring>
#include <thread>

#ifdef _WIN32
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winnt.h>
#include <intrin.h>
#include <psapi.h>
#include <dxgi.h>
#include <d3d11.h>
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "psapi.lib")
#else
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <linux/perf_event.h>
#include <numa.h>
#include <numaif.h>
#include <sched.h>
#include <fcntl.h>
#include <errno.h>
#endif

namespace fish::platform {

// ============================================================================
// Memory Allocation
// ============================================================================
void* alloc_aligned(size_t size, size_t alignment, AllocFlags flags, int numa_node) noexcept {
    // Round size to page boundary
    size_t page_size = (flags & ALLOC_HUGE_2M) ? PAGE_2M :
                       (flags & ALLOC_HUGE_1G) ? PAGE_1G : PAGE_4K;
    size = (size + page_size - 1) & ~(page_size - 1);
    alignment = (alignment < page_size) ? page_size : alignment;

#ifdef _WIN32
    DWORD alloc_type = MEM_RESERVE | MEM_COMMIT;
    DWORD protect = PAGE_READWRITE;
    if (flags & ALLOC_EXECUTABLE) protect = PAGE_EXECUTE_READWRITE;

    // Windows: large pages need SeLockMemoryPrivilege + "Large Pages" group policy
    if (flags & ALLOC_HUGE_2M) {
        alloc_type |= MEM_LARGE_PAGES;
        // Must be multiple of large page size (2 MB)
        size = (size + PAGE_2M - 1) & ~(PAGE_2M - 1);
    }

    // NUMA allocation
    if (flags & ALLOC_NUMA_SPECIFIC && numa_node >= 0) {
        // Use VirtualAllocExNuma (Windows 10 1803+)
        typedef LPVOID (WINAPI* VirtualAllocExNumaFn)(HANDLE, LPVOID, SIZE_T, DWORD, DWORD, DWORD);
        static VirtualAllocExNumaFn pVirtualAllocExNuma = nullptr;
        if (!pVirtualAllocExNuma) {
            HMODULE h = GetModuleHandleW(L"kernel32.dll");
            pVirtualAllocExNuma = (VirtualAllocExNumaFn)GetProcAddress(h, "VirtualAllocExNuma");
        }
        if (pVirtualAllocExNuma) {
            void* p = pVirtualAllocExNuma(GetCurrentProcess(), nullptr, size, alloc_type, protect, (DWORD)numa_node);
            if (p) return p;
        }
    }

    void* ptr = VirtualAlloc(nullptr, size, alloc_type, protect);
    if (!ptr) return nullptr;

    if (flags & ALLOC_PINNED) {
        // Lock in RAM (requires SeLockMemoryPrivilege)
        VirtualLock(ptr, size);
    }
    if (flags & ALLOC_UNINITIALIZED) {
        // MEM_RESET tells OS contents can be discarded
        VirtualAlloc(ptr, size, MEM_RESET, protect);
    }
    return ptr;

#else // Linux / POSIX
    int prot = PROT_READ | PROT_WRITE;
    if (flags & ALLOC_EXECUTABLE) prot |= PROT_EXEC;

    int map_flags = MAP_PRIVATE | MAP_ANONYMOUS;
    if (flags & ALLOC_HUGE_2M) map_flags |= MAP_HUGETLB | (21 << MAP_HUGE_SHIFT); // 2 MB
    if (flags & ALLOC_HUGE_1G) map_flags |= MAP_HUGETLB | (30 << MAP_HUGE_SHIFT); // 1 GB

    void* ptr = mmap(nullptr, size, prot, map_flags, -1, 0);
    if (ptr == MAP_FAILED) return nullptr;

    // NUMA binding
    if ((flags & (ALLOC_NUMA_LOCAL | ALLOC_NUMA_SPECIFIC)) && numa_available() >= 0) {
        int target_node = (flags & ALLOC_NUMA_SPECIFIC) ? numa_node : numa_node_of_cpu(sched_getcpu());
        if (target_node >= 0) {
            struct bitmask* nodes = numa_allocate_nodemask();
            numa_bitmask_setbit(nodes, target_node);
            mbind(ptr, size, MPOL_BIND, nodes->maskp, nodes->size + 1, 0);
            numa_free_nodemask(nodes);
        }
    }

    if (flags & ALLOC_PINNED) {
        mlock(ptr, size);
    }
    if (flags & ALLOC_UNINITIALIZED) {
        madvise(ptr, size, MADV_FREE);
    }
    return ptr;
#endif
}

void free_aligned(void* ptr, size_t size) noexcept {
    if (!ptr) return;
#ifdef _WIN32
    VirtualFree(ptr, 0, MEM_RELEASE);
#else
    munmap(ptr, size);
#endif
}

bool huge_pages_2m_available() noexcept {
#ifdef _WIN32
    // Check SeLockMemoryPrivilege
    HANDLE token;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
    TOKEN_PRIVILEGES tp;
    DWORD len;
    GetTokenInformation(token, TokenPrivileges, &tp, sizeof(tp), &len);
    CloseHandle(token);
    for (DWORD i = 0; i < tp.PrivilegeCount; ++i) {
        if (tp.Privileges[i].Luid.LowPart == 67 /* SE_LOCK_MEMORY_PRIVILEGE */) return true;
    }
    return false;
#else
    // Check /proc/meminfo for HugePages_Total
    FILE* f = fopen("/proc/meminfo", "r");
    if (!f) return false;
    char line[256];
    bool ok = false;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "HugePages_Total:", 16) == 0) {
            long val = strtol(line + 16, nullptr, 10);
            ok = val > 0;
            break;
        }
    }
    fclose(f);
    return ok;
#endif
}

bool huge_pages_1g_available() noexcept {
#ifdef _WIN32
    return false; // Windows doesn't support 1 GB pages for user-mode
#else
    FILE* f = fopen("/proc/meminfo", "r");
    if (!f) return false;
    char line[256];
    bool ok = false;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "HugePages_Total:", 16) == 0) {
            // 1 GB pages need separate mount; check hugetlbfs
            ok = (access("/dev/hugepages1G", F_OK) == 0);
            break;
        }
    }
    fclose(f);
    return ok;
#endif
}

int current_numa_node() noexcept {
#ifdef _WIN32
    PROCESSOR_NUMBER pn;
    GetCurrentProcessorNumberEx(&pn);
    USHORT node = 0;
    GetNumaProcessorNodeEx(&pn, &node);
    return (int)node;
#else
    if (numa_available() < 0) return 0;
    return numa_node_of_cpu(sched_getcpu());
#endif
}

int numa_node_count() noexcept {
#ifdef _WIN32
    ULONG highest = 0;
    GetNumaHighestNodeNumber(&highest);
    return (int)highest + 1;
#else
    return numa_max_node() + 1;
#endif
}

void pin_thread_to_core(int core_id) noexcept {
#ifdef _WIN32
    GROUP_AFFINITY ga = {};
    ga.Mask = KAFFINITY(1) << core_id;
    ga.Group = 0;
    SetThreadGroupAffinity(GetCurrentThread(), &ga, nullptr);
#else
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(core_id, &cpuset);
    pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
#endif
}

// ============================================================================
// Timing
// ============================================================================
uint64_t rdtsc() noexcept {
#if FISH_ARCH_X64
    return __rdtsc();
#elif FISH_ARCH_ARM64
    uint64_t val;
    __asm__ volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
#endif
}

// Calibrated ns conversion (run once at startup)
static double g_tsc_to_ns = 0.0;

uint64_t monotonic_ns() noexcept {
#ifdef _WIN32
    LARGE_INTEGER qpc, freq;
    QueryPerformanceCounter(&qpc);
    QueryPerformanceFrequency(&freq);
    return (uint64_t)((unsigned __int128)qpc.QuadPart * 1000000000 / freq.QuadPart);
#else
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
#endif
}

double tscto_ns(uint64_t tsc) noexcept {
    if (g_tsc_to_ns == 0.0) {
        // Quick calibration: 10 ms sleep
        uint64_t t0 = rdtsc();
        uint64_t n0 = monotonic_ns();
#ifdef _WIN32
        Sleep(10);
#else
        usleep(10000);
#endif
        uint64_t t1 = rdtsc();
        uint64_t n1 = monotonic_ns();
        g_tsc_to_ns = double(n1 - n0) / double(t1 - t0);
    }
    return tsc * g_tsc_to_ns;
}

// ============================================================================
// PMU (Performance Monitoring Unit)
// ============================================================================
PmuSession::PmuSession() noexcept {
#ifdef __linux__
    perf_event_attr attr = {};
    attr.type = PERF_TYPE_HARDWARE;
    attr.size = sizeof(attr);
    attr.disabled = 0;
    attr.exclude_kernel = 1;
    attr.exclude_hv = 1;

    // Cycles
    attr.config = PERF_COUNT_HW_CPU_CYCLES;
    fd_ = syscall(__NR_perf_event_open, &attr, 0, -1, -1, 0);
    // In real impl: open multiple fds for each counter, use PERF_FORMAT_GROUP
#else
    fd_ = -1;
#endif
}

PmuSession::~PmuSession() noexcept {
#ifdef __linux__
    if (fd_ >= 0) close(fd_);
#endif
}

PmuCounters PmuSession::snapshot() noexcept {
    PmuCounters c{};
#ifdef __linux__
    if (fd_ >= 0) {
        uint64_t val;
        read(fd_, &val, sizeof(val));
        c.cycles = val;
        // Read other fds...
    }
#endif
    return c;
}

void PmuSession::reset() noexcept {
#ifdef __linux__
    if (fd_ >= 0) ioctl(fd_, PERF_EVENT_IOC_RESET, 0);
#endif
}

} // namespace fish::platform