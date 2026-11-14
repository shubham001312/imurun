/*
 * Thread pool hand-off check: many tiny jobs (the decode pattern), jobs
 * after pauses longer than the spin window (sleep / wake path), changes of
 * the active thread count between jobs (park / unpark), and a job issued
 * the moment the pool exists (workers that have not started yet). Every job
 * checks that each index ran exactly once. A watchdog fails the test if no
 * job completes for 20 s, so a lost wake-up shows as a failure, not a hang.
 */
#include <fish/threadpool.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

using namespace fish;

static std::atomic<long> g_done{0};
static int failures = 0;

static bool one_job(ThreadPool& pool, int n) {
    static std::vector<std::atomic<int>> hits(4096);
    for (int i = 0; i < n; ++i) hits[static_cast<size_t>(i)].store(0, std::memory_order_relaxed);
    pool.parallel_range(n, [&](int b, int e) {
        for (int i = b; i < e; ++i) hits[static_cast<size_t>(i)].fetch_add(1, std::memory_order_relaxed);
    }, 1);
    for (int i = 0; i < n; ++i)
        if (hits[static_cast<size_t>(i)].load() != 1) return false;
    g_done.fetch_add(1);
    return true;
}

int main() {
#ifdef _WIN32
    _putenv_s("FISH_THREADS", "6");
#else
    setenv("FISH_THREADS", "6", 1);
#endif
    std::thread([] {
        long last = -1;
        for (;;) {
            std::this_thread::sleep_for(std::chrono::seconds(20));
            const long now = g_done.load();
            if (now == last) {
                std::printf("  [FAIL] no job finished for 20 s (lost wake-up?) after %ld jobs\n", now);
                std::fflush(stdout);
                std::_Exit(1);
            }
            last = now;
        }
    }).detach();

    ThreadPool& pool = ThreadPool::instance();
    std::printf("pool threads %d\n", pool.size());
    auto check = [](bool ok, const char* what) {
        std::printf("  [%s] %s\n", ok ? "PASS" : "FAIL", what);
        if (!ok) ++failures;
    };

    check(one_job(pool, 64), "first job right after the pool is created");

    bool ok = true;
    for (int i = 0; i < 100000 && ok; ++i) ok = one_job(pool, 1 + (i % 97));
    check(ok, "100000 back-to-back small jobs");

    ok = true;
    for (int i = 0; i < 200 && ok; ++i) {
        std::this_thread::sleep_for(std::chrono::microseconds(i % 2 ? 3000 : 200));
        ok = one_job(pool, 256);
    }
    check(ok, "jobs after pauses shorter and longer than the spin window");

    ok = true;
    for (int i = 0; i < 20000 && ok; ++i) {
        if (i % 50 == 0) pool.set_active_threads(1 + (i / 50) % pool.size());
        if (i % 500 == 0) std::this_thread::sleep_for(std::chrono::milliseconds(3));
        ok = one_job(pool, 512);
    }
    pool.set_active_threads(pool.size());
    check(ok, "active thread count changed between jobs (park / unpark)");

    std::printf("\n%s (%d failure%s)\n", failures ? "THREADPOOL FAILED" : "THREADPOOL PASSED",
                failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
