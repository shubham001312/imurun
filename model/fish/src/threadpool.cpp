/*
 * FISH — persistent worker pool implementation.
 */
#include <fish/threadpool.h>

#include <algorithm>
#include <cstdlib>

namespace fish {

namespace {

int default_thread_count() {
    if (const char* env = std::getenv("FISH_THREADS")) {
        const int n = std::atoi(env);
        if (n > 0) return std::min(n, 64);
    }
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    // Quantised matmul is memory-bandwidth bound, not FLOP bound: beyond the
    // physical core count extra threads only add contention. 8 is the cap for
    // this class of machine; the specs engine can lower it per device.
    return static_cast<int>(std::min<unsigned>(hw, 8u));
}

} // namespace

ThreadPool& ThreadPool::instance() {
    // Deliberately leaked: a process-lifetime pool must not be destroyed
    // during DLL detach, where the worker threads have already been stopped
    // by the loader and join() would block the exit forever. The OS reclaims
    // the threads with the process.
    static ThreadPool* pool = new ThreadPool();
    return *pool;
}

ThreadPool::ThreadPool() {
    const int n = default_thread_count();
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
    int seen = 0;
    for (;;) {
        const std::function<void(int)>* job = nullptr;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_start_.wait(lk, [&] { return stop_ || generation_ != seen; });
            if (stop_) return;
            seen = generation_;
            job = job_;
        }

        if (job) {
            int i;
            while ((i = next_.fetch_add(1, std::memory_order_relaxed)) < total_) {
                (*job)(i);
            }
        }

        {
            std::lock_guard<std::mutex> lk(mu_);
            if (--active_ == 0) cv_done_.notify_one();
        }
    }
}

void ThreadPool::parallel_for(int n, const std::function<void(int)>& fn) {
    if (n <= 0) return;
    if (n == 1 || workers_.empty()) {
        for (int i = 0; i < n; ++i) fn(i);
        return;
    }

    {
        std::lock_guard<std::mutex> lk(mu_);
        job_ = &fn;
        total_ = n;
        next_.store(0, std::memory_order_relaxed);
        active_ = static_cast<int>(workers_.size());
        ++generation_;
    }
    cv_start_.notify_all();

    int i;
    while ((i = next_.fetch_add(1, std::memory_order_relaxed)) < n) {
        fn(i);
    }

    std::unique_lock<std::mutex> lk(mu_);
    cv_done_.wait(lk, [&] { return active_ == 0; });
    job_ = nullptr;
}

} // namespace fish
