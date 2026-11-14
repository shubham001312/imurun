#pragma once
/*
 * FISH — persistent worker pool.
 *
 * Every projection is a loop over independent output rows, so the engine
 * needs one cheap fork/join primitive and nothing else. The pool is created
 * once and reused; spawning threads per matmul would cost more than the
 * matmul itself at 4-bit widths.
 *
 * Work is handed out in chunks (not one index per atomic increment): a 2048
 * row matmul with 8 threads must not pay 2048 cache-line bounces.
 *
 * Hand-off is spin-then-sleep. One decoded token is ~200 small jobs; waking
 * sleeping workers through the kernel for each of them costs a futex round
 * trip per job (tens of microseconds on a desktop, hundreds inside a VM or
 * on a phone core that has to leave a low-power state). Workers therefore
 * spin on an atomic job counter for a short while after each job
 * (FISH_SPIN_US, default 2000 us; 0 = always sleep), and only then park on
 * a condition variable. The caller waits for the workers by spinning too.
 */
#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>

namespace fish {

class ThreadPool {
public:
    static ThreadPool& instance();

    // Runs fn(i) for i in [0, n). Blocks until every index is done.
    // Safe to call from one thread at a time (the generation loop); the
    // callback must not call parallel_for itself.
    void parallel_for(int n, const std::function<void(int)>& fn);

    // Same, but fn(begin, end) receives a contiguous range. Fewer dispatches
    // when the per-item work is tiny.
    void parallel_range(int n, const std::function<void(int, int)>& fn, int grain = 0);

    int size() const { return static_cast<int>(workers_.size()) + 1; }
    // Threads that take part in the next jobs (1..size(), the caller counts
    // as one). Workers above the limit park until the limit is raised again.
    // fish_set_threads (REQUESTS.md R1).
    int active_threads() const { return limit_.load(std::memory_order_relaxed); }
    void set_active_threads(int n);

    // How many threads FISH would use by default on this machine:
    // FISH_THREADS if set; else, on Linux/Android with cores of different
    // speeds (big.LITTLE), the number of fast cores; else physical cores.
    static int default_threads();
    // Cores whose capacity (or max frequency) is close to the fastest one.
    // 0 when unknown or when all cores are the same (desktop, emulator).
    static int fast_cores();

private:
    ThreadPool();
    ~ThreadPool();
    ThreadPool(const ThreadPool&) = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    void worker_loop(int index);
    void run_job(const std::function<void(int, int)>& fn, int n, int grain);

    std::vector<std::thread> workers_;
    std::mutex mu_;
    std::condition_variable cv_start_;   // sleeping workers (between jobs)
    std::condition_variable cv_park_;    // workers above the active limit
    const std::function<void(int, int)>* job_ = nullptr;
    int total_ = 0;
    int grain_ = 1;
    std::atomic<int> next_{0};
    std::atomic<int> generation_{0};     // bumped once per job
    std::atomic<int> pending_{0};        // participating workers not yet done
    std::atomic<int> sleepers_{0};       // workers waiting on cv_start_
    std::atomic<bool> stop_{false};
    std::atomic<int> limit_{1};
    long spin_ns_ = 2000000;
};

} // namespace fish
