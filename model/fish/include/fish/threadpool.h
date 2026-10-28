#pragma once
/*
 * FISH — persistent worker pool.
 *
 * Every projection is a loop over independent output rows, so the engine
 * needs one cheap fork/join primitive and nothing else. The pool is created
 * once and reused; spawning threads per matmul would cost more than the
 * matmul itself at 4-bit widths.
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
    // Safe to call from one thread at a time (the generation loop).
    void parallel_for(int n, const std::function<void(int)>& fn);

    int size() const { return static_cast<int>(workers_.size()) + 1; }

private:
    ThreadPool();
    ~ThreadPool();
    ThreadPool(const ThreadPool&) = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    void worker_loop();

    std::vector<std::thread> workers_;
    std::mutex mu_;
    std::condition_variable cv_start_;
    std::condition_variable cv_done_;
    const std::function<void(int)>* job_ = nullptr;
    std::atomic<int> next_{0};
    int total_ = 0;
    int generation_ = 0;
    int active_ = 0;
    bool stop_ = false;
};

} // namespace fish
