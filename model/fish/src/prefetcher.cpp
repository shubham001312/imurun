#include <fish/engine.h>
#include <thread>
#include <future>
#include <queue>
#include <mutex>
#include <condition_variable>

namespace fish {

struct Prefetcher::Impl {
    const TensorTable& tensors;
    ResidentWindow& window;
    std::thread worker;
    std::queue<std::vector<int>> prefetch_queue;
    std::mutex mtx;
    std::condition_variable cv;
    bool stop = false;

    Impl(const TensorTable& t, ResidentWindow& w) : tensors(t), window(w) {
        worker = std::thread([this] { run(); });
    }
    ~Impl() {
        {
            std::lock_guard<std::mutex> lk(mtx);
            stop = true;
        }
        cv.notify_all();
        if (worker.joinable()) worker.join();
    }

    void run() {
        while (true) {
            std::vector<int> layers;
            {
                std::unique_lock<std::mutex> lk(mtx);
                cv.wait(lk, [this] { return stop || !prefetch_queue.empty(); });
                if (stop && prefetch_queue.empty()) return;
                layers = std::move(prefetch_queue.front());
                prefetch_queue.pop();
            }
            // Stub: in real impl, issue async pread/io_uring for cold layers,
            // then H2D copy via staging ring for warm->hot
            for (int idx : layers) {
                window.ensure_hot(idx);
            }
        }
    }
};

Prefetcher::Prefetcher(const TensorTable& tensors, ResidentWindow& window)
    : pimpl_(std::make_unique<Impl>(tensors, window)) {}
Prefetcher::~Prefetcher() = default;

void Prefetcher::schedule_prefetch(const std::vector<int>& next_layer_indices) {
    std::lock_guard<std::mutex> lk(pimpl_->mtx);
    pimpl_->prefetch_queue.push(next_layer_indices);
    pimpl_->cv.notify_one();
}

void Prefetcher::wait_ready(int layer_idx) {
    // Stub: in real impl, wait on future for this layer
    // For now, just ensure it's hot (blocking)
    pimpl_->window.ensure_hot(layer_idx);
}

void Prefetcher::cancel_all() {
    std::lock_guard<std::mutex> lk(pimpl_->mtx);
    while (!pimpl_->prefetch_queue.empty()) pimpl_->prefetch_queue.pop();
}

} // namespace fish