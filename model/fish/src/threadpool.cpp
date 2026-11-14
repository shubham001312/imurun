/*
 * FISH — persistent worker pool implementation (spin-then-sleep hand-off,
 * see include/fish/threadpool.h for why).
 */
#include <fish/threadpool.h>
#include <fish/specs.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <pthread.h>
#  include <sched.h>
#  if defined(__linux__)
#    include <sys/resource.h>
#    include <sys/syscall.h>
#    include <unistd.h>
#  endif
#endif

#if defined(__x86_64__) || defined(__i386__) || defined(_M_X64) || defined(_M_IX86)
#  include <immintrin.h>
#  define FISH_CPU_RELAX() _mm_pause()
#elif defined(__aarch64__) || defined(__arm__)
#  define FISH_CPU_RELAX() __asm__ __volatile__("yield" ::: "memory")
#else
#  define FISH_CPU_RELAX() ((void)0)
#endif

namespace fish {

namespace {
// Inference should never make the user's other programs stutter: workers run
// just below normal priority so the UI, the browser and the shell win ties.
// FISH_LOW_PRIORITY=0 turns this off for benchmarking.
void lower_priority() {
    if (const char* v = std::getenv("FISH_LOW_PRIORITY")) {
        if (std::atoi(v) == 0) return;
    }
#ifdef _WIN32
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
#elif defined(__linux__)
    // Per-thread nice: setpriority on the thread id.
    setpriority(PRIO_PROCESS, static_cast<id_t>(syscall(SYS_gettid)), 5);
#else
    (void)0;  // macOS: QoS classes would go here
#endif
}

using clk = std::chrono::steady_clock;

#if defined(__linux__)
// One integer from a sysfs file, or -1.
long read_long(const char* path) {
    FILE* f = std::fopen(path, "r");
    if (!f) return -1;
    long v = -1;
    if (std::fscanf(f, "%ld", &v) != 1) v = -1;
    std::fclose(f);
    return v;
}
#endif
} // namespace

int ThreadPool::fast_cores() {
#if defined(__linux__)
    // Phones mix fast and slow cores (big.LITTLE). A fork/join pool waits for
    // its slowest member at the end of every job, and little cores are 3 to 6
    // times slower, so by default only the fast ones take part. cpu_capacity
    // (EAS, 0..1024) is the best measure; the maximum clock is the fallback.
    // Cores at >= 40% of the top capacity (or >= 70% of the top clock) count
    // as fast: that keeps the mid cores (Cortex-A7x), which add memory
    // bandwidth, and drops the A5x little cores.
    const unsigned hw = std::thread::hardware_concurrency();
    const int n = static_cast<int>(hw == 0 ? 0 : std::min(hw, 256u));
    if (n <= 1) return 0;
    std::vector<long> cap(static_cast<size_t>(n), -1), freq(static_cast<size_t>(n), -1);
    bool all_cap = true, all_freq = true;
    char path[128];
    for (int i = 0; i < n; ++i) {
        std::snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cpu_capacity", i);
        cap[static_cast<size_t>(i)] = read_long(path);
        std::snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq", i);
        freq[static_cast<size_t>(i)] = read_long(path);
        if (cap[static_cast<size_t>(i)] <= 0) all_cap = false;
        if (freq[static_cast<size_t>(i)] <= 0) all_freq = false;
    }
    const std::vector<long>* v = all_cap ? &cap : (all_freq ? &freq : nullptr);
    if (!v) return 0;
    const double frac = all_cap ? 0.40 : 0.70;
    const long hi = *std::max_element(v->begin(), v->end());
    const long lo = *std::min_element(v->begin(), v->end());
    if (hi <= 0 || lo * 100 >= hi * 95) return 0;   // all cores alike
    int fast = 0;
    for (long x : *v) if (static_cast<double>(x) >= frac * static_cast<double>(hi)) ++fast;
    return fast;
#else
    return 0;
#endif
}

int ThreadPool::default_threads() {
    if (const char* env = std::getenv("FISH_THREADS")) {
        const int n = std::atoi(env);
        if (n > 0) return std::min(n, 128);
    }
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    const int fast = fast_cores();
    if (fast > 0) return std::max(1, std::min(fast, 32));
#if defined(__aarch64__) || defined(__arm__)
    // ARM has no SMT, and /proc/cpuinfo on ARM Linux carries no
    // "physical id" / "core id" lines, so the generic count below would
    // report a single core on every phone.
    return std::max(1, std::min(static_cast<int>(hw), 32));
#else
    // Quantised matmul is memory-bandwidth bound, not FLOP bound: one thread
    // per physical core saturates the memory system; SMT siblings only add
    // contention. Fall back to logical cores when the topology is unknown.
    int phys = detect_physical_cores();
    if (phys <= 0) phys = static_cast<int>(hw > 1 ? hw / 2 : 1);
    return std::max(1, std::min(phys, 32));
#endif
}

ThreadPool& ThreadPool::instance() {
    // Deliberately leaked: a process-lifetime pool must not be destroyed
    // during DLL detach, where the worker threads have already been stopped
    // by the loader and join() would block the exit forever. The OS reclaims
    // the threads with the process.
    static ThreadPool* pool = new ThreadPool();
    return *pool;
}

ThreadPool::ThreadPool() {
    if (const char* v = std::getenv("FISH_SPIN_US")) {
        const long us = std::atol(v);
        spin_ns_ = us > 0 ? std::min(us, 1000000L) * 1000L : 0;
    }
    const int n = default_threads();
    workers_.reserve(static_cast<size_t>(n > 0 ? n - 1 : 0));
    limit_.store(std::max(1, n), std::memory_order_relaxed);
    for (int i = 1; i < n; ++i) {
        workers_.emplace_back([this, i] { worker_loop(i); });
    }
}

void ThreadPool::set_active_threads(int n) {
    // Only between jobs: run_job holds the caller until every worker is done,
    // and the generation loop is the only caller.
    {
        std::lock_guard<std::mutex> lk(mu_);
        limit_.store(std::max(1, std::min(n, size())), std::memory_order_seq_cst);
        // A worker that unparks resumes from here: every job issued after
        // this call counts it, so it must not skip any of them.
        park_gen_ = generation_.load();
    }
    cv_park_.notify_all();
    cv_start_.notify_all();
}

ThreadPool::~ThreadPool() {
    {
        std::lock_guard<std::mutex> lk(mu_);
        stop_.store(true);
        generation_.fetch_add(1);
    }
    cv_start_.notify_all();
    cv_park_.notify_all();
    for (auto& t : workers_) {
        if (t.joinable()) t.join();
    }
}

void ThreadPool::worker_loop(int index) {
    lower_priority();
    // Start from 0, not from the current value: on a busy machine the first
    // job can be issued before this thread has run at all, and a worker that
    // started by reading the already-bumped counter would never take part in
    // it while the caller waits for it forever (seen on the emulator).
    int seen = 0;
    for (;;) {
        if (stop_.load(std::memory_order_relaxed)) return;
        if (index >= limit_.load(std::memory_order_relaxed)) {
            // Above the active limit: park until fish_set_threads raises it.
            std::unique_lock<std::mutex> lk(mu_);
            cv_park_.wait(lk, [&] { return stop_.load() || index < limit_.load(); });
            if (stop_.load()) return;
            seen = park_gen_;
            continue;
        }

        // Wait for the next job: spin first, then sleep.
        int g = generation_.load(std::memory_order_acquire);
        if (g == seen && spin_ns_ > 0) {
            const auto t0 = clk::now();
            for (unsigned it = 1;; ++it) {
                FISH_CPU_RELAX();
                g = generation_.load(std::memory_order_acquire);
                if (g != seen) break;
                if ((it & 63) == 0) {
                    if (stop_.load(std::memory_order_relaxed) ||
                        index >= limit_.load(std::memory_order_relaxed)) break;
                    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(clk::now() - t0).count();
                    if (ns > spin_ns_) break;
                    // After the first 50 us give the core away between polls,
                    // so a spinning worker does not starve another process
                    // (the voice, the UI) on a busy phone.
                    if (ns > 50000) std::this_thread::yield();
                }
            }
        }
        if (g == seen) {
            std::unique_lock<std::mutex> lk(mu_);
            sleepers_.fetch_add(1, std::memory_order_seq_cst);
            cv_start_.wait(lk, [&] {
                return stop_.load() || generation_.load(std::memory_order_seq_cst) != seen ||
                       index >= limit_.load();
            });
            sleepers_.fetch_sub(1, std::memory_order_seq_cst);
            continue;   // re-evaluate stop / limit / generation at the top
        }
        seen = g;
        if (stop_.load(std::memory_order_relaxed)) return;
        // run_job counted the workers below the limit; the limit does not
        // change while a job is in flight.
        if (index >= limit_.load(std::memory_order_relaxed)) continue;

        const std::function<void(int, int)>* job = job_;
        const int total = total_, grain = grain_;
        if (job) {
            int begin;
            while ((begin = next_.fetch_add(grain, std::memory_order_relaxed)) < total) {
                (*job)(begin, std::min(total, begin + grain));
            }
        }
        pending_.fetch_sub(1, std::memory_order_acq_rel);
    }
}

void ThreadPool::run_job(const std::function<void(int, int)>& fn, int n, int grain) {
    const int participants = std::min(limit_.load(std::memory_order_relaxed), size()) - 1;
    job_ = &fn;
    total_ = n;
    grain_ = grain;
    next_.store(0, std::memory_order_relaxed);
    pending_.store(participants, std::memory_order_relaxed);
    // seq_cst: pairs with the sleepers_ increment in worker_loop, so either
    // the worker sees the new generation or this thread sees the sleeper.
    generation_.fetch_add(1, std::memory_order_seq_cst);
    if (sleepers_.load(std::memory_order_seq_cst) > 0) {
        std::lock_guard<std::mutex> lk(mu_);
        cv_start_.notify_all();
    }

    int begin;
    while ((begin = next_.fetch_add(grain, std::memory_order_relaxed)) < n) {
        fn(begin, std::min(n, begin + grain));
    }

    for (unsigned it = 1; pending_.load(std::memory_order_acquire) != 0; ++it) {
        FISH_CPU_RELAX();
        if ((it & 1023) == 0) std::this_thread::yield();
    }
    job_ = nullptr;
}

void ThreadPool::parallel_range(int n, const std::function<void(int, int)>& fn, int grain) {
    if (n <= 0) return;
    if (grain <= 0) {
        // Aim for ~8 chunks per thread so the tail stays balanced without
        // paying an atomic per item.
        grain = std::max(1, n / (active_threads() * 8));
    }
    if (workers_.empty() || active_threads() <= 1 || n <= grain) {
        fn(0, n);
        return;
    }
    run_job(fn, n, grain);
}

void ThreadPool::parallel_for(int n, const std::function<void(int)>& fn) {
    if (n <= 0) return;
    if (n == 1 || workers_.empty() || active_threads() <= 1) {
        for (int i = 0; i < n; ++i) fn(i);
        return;
    }
    const std::function<void(int, int)> ranged = [&](int b, int e) {
        for (int i = b; i < e; ++i) fn(i);
    };
    run_job(ranged, n, std::max(1, n / (active_threads() * 8)));
}

} // namespace fish
