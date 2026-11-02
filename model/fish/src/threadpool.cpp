/*
 * FISH — persistent worker pool implementation.
 */
#include <fish/threadpool.h>
#include <fish/specs.h>

#include <algorithm>
#include <cstdlib>

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
} // namespace

int ThreadPool::default_threads() {
    if (const char* env = std::getenv("FISH_THREADS")) {
        const int n = std::atoi(env);
        if (n > 0) return std::min(n, 128);
    }
    // Quantised matmul is memory-bandwidth bound, not FLOP bound: one thread
    // per physical core saturates the memory system; SMT siblings only add
    // contention. Fall back to logical cores when the topology is unknown.
    int phys = detect_physical_cores();
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    if (phys <= 0) phys = static_cast<int>(hw > 1 ? hw / 2 : 1);
    return std::max(1, std::min(phys, 32));
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
    const int n = default_threads();
    workers_.reserve(static_cast<size_t>(n > 0 ? n - 1 : 0));
    for (int i = 1; i < n; ++i) {
        workers_.emplace_back([this] { worker_loop(); });
    }
}

ThreadPool::~ThreadPool() {
    {
        std::lock_guard<std::mutex> lk(mu_);
        stop_ = true;
        ++generation_;
    }
    cv_start_.notify_all();
    for (auto& t : workers_) {
        if (t.joinable()) t.join();
    }
}

void ThreadPool::worker_loop() {
    lower_priority();
    int seen = 0;
    for (;;) {
        const std::function<void(int, int)>* job = nullptr;
        int total = 0, grain = 1;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_start_.wait(lk, [&] { return stop_ || generation_ != seen; });
            if (stop_) return;
            seen = generation_;
            job = job_;
            total = total_;
            grain = grain_;
        }

        if (job) {
            int begin;
            while ((begin = next_.fetch_add(grain, std::memory_order_relaxed)) < total) {
                (*job)(begin, std::min(total, begin + grain));
            }
        }

        {
            std::lock_guard<std::mutex> lk(mu_);
            if (--active_ == 0) cv_done_.notify_one();
        }
    }
}

void ThreadPool::run_job(const std::function<void(int, int)>& fn, int n, int grain) {
    {
        std::lock_guard<std::mutex> lk(mu_);
        job_ = &fn;
        total_ = n;
        grain_ = grain;
        next_.store(0, std::memory_order_relaxed);
        active_ = static_cast<int>(workers_.size());
        ++generation_;
    }
    cv_start_.notify_all();

    int begin;
    while ((begin = next_.fetch_add(grain, std::memory_order_relaxed)) < n) {
        fn(begin, std::min(n, begin + grain));
    }

    std::unique_lock<std::mutex> lk(mu_);
    cv_done_.wait(lk, [&] { return active_ == 0; });
    job_ = nullptr;
}

void ThreadPool::parallel_range(int n, const std::function<void(int, int)>& fn, int grain) {
    if (n <= 0) return;
    if (grain <= 0) {
        // Aim for ~8 chunks per thread so the tail stays balanced without
        // paying an atomic per item.
        grain = std::max(1, n / (size() * 8));
    }
    if (workers_.empty() || n <= grain) {
        fn(0, n);
        return;
    }
    run_job(fn, n, grain);
}

void ThreadPool::parallel_for(int n, const std::function<void(int)>& fn) {
    if (n <= 0) return;
    if (n == 1 || workers_.empty()) {
        for (int i = 0; i < n; ++i) fn(i);
        return;
    }
    const std::function<void(int, int)> ranged = [&](int b, int e) {
        for (int i = b; i < e; ++i) fn(i);
    };
    run_job(ranged, n, std::max(1, n / (size() * 8)));
}

} // namespace fish
