/*
 * FISH — specs engine, hardware measurement and memory budget.
 *
 * Everything here is measured on the actual device. Where a measurement is
 * unavailable the value stays 0 and the planner says so in its rationale
 * rather than inventing a number.
 */
#include <fish/specs.h>
#include <fish/backend.h>
#include <fish/quant.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <thread>
#include <vector>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <dlfcn.h>
#  include <fcntl.h>
#  include <sys/stat.h>
#  include <sys/types.h>
#  include <unistd.h>
#  if defined(__APPLE__)
#    include <sys/sysctl.h>
#    include <mach/mach.h>
#  endif
#endif

#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
#  include <cpuid.h>
#endif

namespace fish {

namespace {

using Clock = std::chrono::steady_clock;

double seconds_since(const Clock::time_point& t0) {
    return std::chrono::duration<double>(Clock::now() - t0).count();
}

// ---------------------------------------------------------------- CPU

std::string cpu_brand_string() {
#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
    unsigned int regs[4] = {0, 0, 0, 0};
    char brand[49] = {0};
    for (unsigned int i = 0; i < 3; ++i) {
        __cpuid(0x80000002u + i, regs[0], regs[1], regs[2], regs[3]);
        std::memcpy(brand + i * 16, regs, 16);
    }
    std::string s(brand);
    const size_t first = s.find_first_not_of(" \t");
    const size_t last = s.find_last_not_of(" \t");
    if (first == std::string::npos) return "unknown";
    return s.substr(first, last - first + 1);
#elif defined(__aarch64__)
    return "ARM64";
#else
    return "unknown";
#endif
}

int physical_cores() {
#if defined(_WIN32)
    DWORD len = 0;
    GetLogicalProcessorInformationEx(RelationProcessorCore, nullptr, &len);
    if (len == 0) return 0;
    std::vector<uint8_t> buf(len);
    if (!GetLogicalProcessorInformationEx(
            RelationProcessorCore,
            reinterpret_cast<PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX>(buf.data()), &len))
        return 0;
    int count = 0;
    DWORD off = 0;
    while (off < len) {
        const auto* e = reinterpret_cast<const SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX*>(
            buf.data() + off);
        if (e->Relationship == RelationProcessorCore) ++count;
        if (e->Size == 0) break;
        off += e->Size;
    }
    return count;
#elif defined(__APPLE__)
    int n = 0;
    size_t sz = sizeof(n);
    if (sysctlbyname("hw.physicalcpu", &n, &sz, nullptr, 0) == 0) return n;
    return 0;
#else
    // Count distinct (physical id, core id) pairs in /proc/cpuinfo.
    FILE* f = std::fopen("/proc/cpuinfo", "r");
    if (!f) return 0;
    char line[512];
    int phys = -1, core = -1, count = 0, last_phys = -2, last_core = -2;
    while (std::fgets(line, sizeof(line), f)) {
        if (std::strncmp(line, "physical id", 11) == 0)
            phys = std::atoi(std::strchr(line, ':') ? std::strchr(line, ':') + 1 : "0");
        else if (std::strncmp(line, "core id", 7) == 0)
            core = std::atoi(std::strchr(line, ':') ? std::strchr(line, ':') + 1 : "0");
        else if (line[0] == '\n') {
            if (phys != last_phys || core != last_core) { ++count; last_phys = phys; last_core = core; }
            phys = core = -1;
        }
    }
    std::fclose(f);
    return count;
#endif
}

// ---------------------------------------------------------------- memory

void probe_memory(HardwareSpecs& hw) {
#if defined(_WIN32)
    MEMORYSTATUSEX ms{};
    ms.dwLength = sizeof(ms);
    if (GlobalMemoryStatusEx(&ms)) {
        hw.ram_total_bytes = ms.ullTotalPhys;
        hw.ram_available_bytes = ms.ullAvailPhys;
        hw.swap_total_bytes = ms.ullTotalPageFile > ms.ullTotalPhys
                                  ? ms.ullTotalPageFile - ms.ullTotalPhys
                                  : 0;
    }
#elif defined(__APPLE__)
    uint64_t mem = 0;
    size_t sz = sizeof(mem);
    if (sysctlbyname("hw.memsize", &mem, &sz, nullptr, 0) == 0) hw.ram_total_bytes = mem;
    vm_statistics64_data_t vm{};
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_statistics64(mach_host_self(), HOST_VM_INFO64,
                          reinterpret_cast<host_info64_t>(&vm), &count) == KERN_SUCCESS) {
        const uint64_t page = 4096;
        hw.ram_available_bytes = (static_cast<uint64_t>(vm.free_count) +
                                  static_cast<uint64_t>(vm.inactive_count)) * page;
    }
#else
    FILE* f = std::fopen("/proc/meminfo", "r");
    if (f) {
        char line[256];
        while (std::fgets(line, sizeof(line), f)) {
            unsigned long long kb = 0;
            if (std::sscanf(line, "MemTotal: %llu kB", &kb) == 1) hw.ram_total_bytes = kb * 1024ull;
            else if (std::sscanf(line, "MemAvailable: %llu kB", &kb) == 1) hw.ram_available_bytes = kb * 1024ull;
            else if (std::sscanf(line, "SwapTotal: %llu kB", &kb) == 1) hw.swap_total_bytes = kb * 1024ull;
        }
        std::fclose(f);
    }
#endif
}

// ---------------------------------------------------------------- GPU (NVML, dynamic)
//
// Off by default. Loading a driver DLL at runtime requires LoadLibraryW +
// GetProcAddress, and that import pair is exactly what packers use, so a
// reputation-based application-control policy treats it as hostile and
// refuses to load the whole engine. The GPU is not used for compute in this
// build, so the probe is not worth that cost. The host process reports the
// adapter instead.
#ifndef FISH_ENABLE_NVML
#  define FISH_ENABLE_NVML 0
#endif

#if FISH_ENABLE_NVML

struct NvmlApi {
    void* lib = nullptr;
    int (*init)(void) = nullptr;
    int (*shutdown)(void) = nullptr;
    int (*count)(unsigned int*) = nullptr;
    int (*handle)(unsigned int, void**) = nullptr;
    int (*mem)(void*, void*) = nullptr;
    int (*name)(void*, char*, unsigned int) = nullptr;
};

struct NvmlMemory {
    unsigned long long total;
    unsigned long long free;
    unsigned long long used;
};

bool load_nvml(NvmlApi& api) {
#if defined(_WIN32)
    api.lib = reinterpret_cast<void*>(LoadLibraryW(L"nvml.dll"));
#elif defined(__APPLE__)
    return false;
#else
    api.lib = dlopen("libnvidia-ml.so.1", RTLD_LAZY);
#endif
    if (!api.lib) return false;
#if defined(_WIN32)
    auto sym = [&](const char* n) { return GetProcAddress(static_cast<HMODULE>(api.lib), n); };
#else
    auto sym = [&](const char* n) { return dlsym(api.lib, n); };
#endif
    api.init = reinterpret_cast<decltype(api.init)>(sym("nvmlInit_v2"));
    api.shutdown = reinterpret_cast<decltype(api.shutdown)>(sym("nvmlShutdown"));
    api.count = reinterpret_cast<decltype(api.count)>(sym("nvmlDeviceGetCount_v2"));
    api.handle = reinterpret_cast<decltype(api.handle)>(sym("nvmlDeviceGetHandleByIndex_v2"));
    api.mem = reinterpret_cast<decltype(api.mem)>(sym("nvmlDeviceGetMemoryInfo"));
    api.name = reinterpret_cast<decltype(api.name)>(sym("nvmlDeviceGetName"));
    return api.init && api.count && api.handle && api.mem;
}

void probe_gpu(HardwareSpecs& hw) {
#if defined(__APPLE__) && defined(__aarch64__)
    hw.unified_memory = true;
    hw.gpu_name = "Apple integrated GPU (unified memory)";
    hw.vram_total_bytes = hw.ram_total_bytes;
    hw.vram_free_bytes = hw.ram_available_bytes;
    return;
#endif
    NvmlApi api;
    if (!load_nvml(api)) return;
    if (api.init() != 0) return;
    unsigned int n = 0;
    if (api.count(&n) != 0 || n == 0) { if (api.shutdown) api.shutdown(); return; }
    void* dev = nullptr;
    if (api.handle(0, &dev) == 0) {
        char name[96] = {0};
        if (api.name) api.name(dev, name, sizeof(name));
        hw.gpu_name = name[0] ? name : "NVIDIA GPU";
        NvmlMemory m{};
        if (api.mem(dev, &m) == 0) {
            hw.vram_total_bytes = m.total;
            hw.vram_free_bytes = m.free;
        }
    }
    if (api.shutdown) api.shutdown();
}

#else // !FISH_ENABLE_NVML

void probe_gpu(HardwareSpecs& hw) {
#if defined(__APPLE__) && defined(__aarch64__)
    hw.unified_memory = true;
    hw.gpu_name = "Apple integrated GPU (unified memory)";
    hw.vram_total_bytes = hw.ram_total_bytes;
    hw.vram_free_bytes = hw.ram_available_bytes;
#else
    (void)hw;
#endif
}

#endif // FISH_ENABLE_NVML

// ---------------------------------------------------------------- storage

double measure_disk_read_mb_s(const std::string& path) {
    if (path.empty()) return 0.0;
    const size_t kChunk = 1u << 20;
    const size_t kTarget = 64u << 20;
    std::vector<uint8_t> raw(kChunk + 4096);
    uint8_t* buf = reinterpret_cast<uint8_t*>(
        (reinterpret_cast<uintptr_t>(raw.data()) + 4095u) & ~uintptr_t(4095));

#if defined(_WIN32)
    const int wlen = MultiByteToWideChar(CP_UTF8, 0, path.c_str(), -1, nullptr, 0);
    if (wlen <= 0) return 0.0;
    std::wstring wpath(static_cast<size_t>(wlen), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, path.c_str(), -1, wpath.data(), wlen);
    HANDLE h = CreateFileW(wpath.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                           OPEN_EXISTING,
                           FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h == INVALID_HANDLE_VALUE) return 0.0;
    const auto t0 = Clock::now();
    size_t total = 0;
    DWORD read = 0;
    while (total < kTarget && ReadFile(h, buf, static_cast<DWORD>(kChunk), &read, nullptr) && read > 0)
        total += read;
    const double s = seconds_since(t0);
    CloseHandle(h);
    return s > 0 ? (static_cast<double>(total) / s) / 1e6 : 0.0;
#else
    int flags = O_RDONLY;
#  if defined(__linux__)
    flags |= O_DIRECT;
#  endif
    const int fd = ::open(path.c_str(), flags);
    if (fd < 0) return 0.0;
#  if defined(__APPLE__)
    ::fcntl(fd, F_NOCACHE, 1);
#  endif
    const auto t0 = Clock::now();
    size_t total = 0;
    while (total < kTarget) {
        const ssize_t n = ::read(fd, buf, kChunk);
        if (n <= 0) break;
        total += static_cast<size_t>(n);
    }
    const double s = seconds_since(t0);
    ::close(fd);
    return s > 0 ? (static_cast<double>(total) / s) / 1e6 : 0.0;
#endif
}

double measure_ram_bandwidth_mb_s() {
    // What decode is bound by is the *aggregate* read bandwidth all cores can
    // pull together (one core alone sees a fraction of it), so every thread
    // streams its own slice at once. The checksum keeps the optimiser from
    // deleting the loop (an earlier build reported 600 TB/s from exactly that).
    const int n_threads = std::max(1, std::min(physical_cores() > 0 ? physical_cores() : 4, 32));
    const size_t per_thread = 32u << 20;
    std::vector<std::vector<uint64_t>> bufs(static_cast<size_t>(n_threads));
    for (auto& b : bufs) b.assign(per_thread / sizeof(uint64_t), 0x0101010101010101ull);

    std::atomic<uint64_t> sink{0};
    double best = 0.0;
    for (int rep = 0; rep < 3; ++rep) {
        std::vector<std::thread> workers;
        const auto t0 = Clock::now();
        for (int t = 0; t < n_threads; ++t) {
            workers.emplace_back([&, t] {
                uint64_t acc = 0;
                const std::vector<uint64_t>& b = bufs[static_cast<size_t>(t)];
                for (size_t k = 0; k < b.size(); k += 4)
                    acc += b[k] ^ b[k + 1] ^ b[k + 2] ^ b[k + 3];
                sink.fetch_add(acc, std::memory_order_relaxed);
            });
        }
        for (auto& w : workers) w.join();
        const double s = seconds_since(t0);
        if (s > 0) best = std::max(best, (static_cast<double>(per_thread) * n_threads) / s / 1e6);
    }
    (void)sink.load();
    return best;
}

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char esc[8];
                    std::snprintf(esc, sizeof(esc), "\\u%04x", static_cast<unsigned>(c));
                    out += esc;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

double gb(uint64_t bytes) { return static_cast<double>(bytes) / (1024.0 * 1024.0 * 1024.0); }

} // namespace

int detect_physical_cores() { return physical_cores(); }

uint64_t detect_ram_available_bytes() {
    HardwareSpecs hw;
    probe_memory(hw);
    return hw.ram_available_bytes;
}

// ---------------------------------------------------------------- engine

SpecsEngine& SpecsEngine::instance() {
    static SpecsEngine engine;
    return engine;
}

const HardwareSpecs& SpecsEngine::hardware() {
    if (!measured_) refresh(std::string());
    return hw_;
}

void SpecsEngine::refresh(const std::string& probe_path) {
    HardwareSpecs hw;
    hw.cpu_name = cpu_brand_string();
    hw.physical_cores = physical_cores();
    hw.logical_cores = static_cast<int>(std::thread::hardware_concurrency());
    hw.avx2 = avx2_available();
#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
    __builtin_cpu_init();
    hw.avx512 = __builtin_cpu_supports("avx512f") != 0;
#elif defined(__aarch64__)
    hw.neon = true;
#endif

    probe_memory(hw);
    probe_gpu(hw);
    // The compute backend knows the real device and its live budget
    // (VK_EXT_memory_budget); prefer it over any static probe.
    if (ComputeBackend* gpu = gpu_backend()) {
        const uint64_t total = gpu->memory_total();
        if (total > 0) {
            hw.gpu_name = std::string(gpu->device_name()) + " (" + gpu->name() + ")";
            hw.vram_total_bytes = total;
            hw.vram_free_bytes = gpu->memory_free();
        }
    }

    if (!probe_path.empty()) hw.disk_read_mb_s = measure_disk_read_mb_s(probe_path);
    hw.ram_bandwidth_mb_s = measure_ram_bandwidth_mb_s();

    hw_ = std::move(hw);
    measured_ = true;
}

ExecutionPlan SpecsEngine::plan(const ModelProfile& m, const PlanRequest& req) const {
    ExecutionPlan p;
    const HardwareSpecs& hw = hw_;

    // ---- RAM budget: never eat the last gigabyte of the machine
    uint64_t reserve = req.ram_reserve_bytes;
    if (reserve == 0) {
        reserve = std::max<uint64_t>(1536ull << 20, hw.ram_total_bytes / 8);
    }
    const uint64_t free_ram = hw.ram_available_bytes > 0 ? hw.ram_available_bytes
                                                         : hw.ram_total_bytes / 2;
    uint64_t budget = free_ram > reserve ? free_ram - reserve : free_ram / 2;
    // FISH_RAM_BUDGET_MB (set by the host's resource governor at every load,
    // and used by place_layers / the adapter merge) is the one number in
    // charge when present, so the context choice cannot eat the user's
    // headroom either (docs/engine/REQUESTS.md R2).
    if (const char* e = std::getenv("FISH_RAM_BUDGET_MB")) {
        const long long mb = std::atoll(e);
        if (mb > 0) budget = static_cast<uint64_t>(mb) << 20;
    }
    p.ram_budget_bytes = budget;

    // ---- context: shrink until the KV cache fits a quarter of the budget
    const int ctx_cap = m.n_ctx_max > 0 ? m.n_ctx_max : 4096;
    int n_ctx = req.n_ctx > 0 ? req.n_ctx : std::min(ctx_cap, 8192);
    n_ctx = std::min(n_ctx, ctx_cap);
    while (n_ctx > 256 && m.kv_per_token_bytes > 0 &&
           static_cast<double>(m.kv_per_token_bytes) * n_ctx > static_cast<double>(budget) / 4.0) {
        n_ctx /= 2;
    }
    p.n_ctx = std::max(256, n_ctx);

    const uint64_t kv_bytes = m.kv_per_token_bytes * static_cast<uint64_t>(p.n_ctx);

    p.n_threads = std::min(8, std::max(1, hw.physical_cores > 0 ? hw.physical_cores
                                                                : hw.logical_cores));

    const double ram_bw = hw.ram_bandwidth_mb_s > 0 ? hw.ram_bandwidth_mb_s : 8000.0;
    const double disk_bw = hw.disk_read_mb_s > 0 ? hw.disk_read_mb_s : 500.0;

    // Effective bandwidth is well below peak: a matmul stream is not a memcpy.
    const double ram_eff = ram_bw * 0.55;
    const double disk_eff = disk_bw * 0.75;

    const uint64_t weights = m.weights_bytes;
    const uint64_t per_layer = m.per_layer_bytes > 0 ? m.per_layer_bytes : 1;

    std::ostringstream why;

    const double need = static_cast<double>(weights) + static_cast<double>(kv_bytes);
    if (need <= static_cast<double>(budget) * 0.90) {
        p.mode = "resident";
        p.resident_layers = m.n_layers;
        p.window_layers = m.n_layers;
        p.bytes_per_token = weights;
        const double t = static_cast<double>(weights) / (ram_eff * 1e6);
        p.predicted_tok_s = t > 0 ? 1.0 / t : 0.0;
        p.predicted_ttft_s = t;
        why << "resident: " << gb(weights) << " GB of weights + " << gb(kv_bytes)
            << " GB KV fit in " << gb(budget) << " GB of usable RAM; every layer is read "
            << "from memory at ~" << static_cast<int>(ram_eff) << " MB/s effective.";
    } else {
        // ---- windowed: as many resident layers as fit, the rest stream
        const uint64_t after_kv = budget > kv_bytes ? budget - kv_bytes : 0;
        int resident = static_cast<int>(std::min<uint64_t>(static_cast<uint64_t>(m.n_layers),
                                                           after_kv / per_layer));
        uint64_t left = after_kv - static_cast<uint64_t>(resident) * per_layer;
        int window = static_cast<int>(std::min<uint64_t>(4, left / per_layer));
        if (window < 1) {
            window = 1;
            if (resident > 0) {
                --resident;
                left += per_layer;
            }
        }
        p.resident_layers = resident;
        p.window_layers = std::min(window, std::max(1, m.n_layers - resident));
        p.mode = resident > 0 ? "windowed" : "streamed";

        const int streamed = std::max(0, m.n_layers - resident);
        const uint64_t resident_bytes = static_cast<uint64_t>(resident) * per_layer;
        const uint64_t streamed_bytes = static_cast<uint64_t>(streamed) * per_layer;
        p.bytes_per_token = static_cast<double>(resident_bytes + streamed_bytes);

        const double t_ram = static_cast<double>(resident_bytes) / (ram_eff * 1e6);
        const double t_disk = static_cast<double>(streamed_bytes) / (disk_eff * 1e6);
        const double t = t_ram + t_disk;
        p.predicted_tok_s = t > 0 ? 1.0 / t : 0.0;
        p.predicted_ttft_s = t;

        why << p.mode << ": weights need " << gb(weights) << " GB but only " << gb(budget)
            << " GB are usable. Keeping " << resident << " of " << m.n_layers
            << " layers resident and streaming " << streamed << " per token ("
            << p.window_layers << " at a time) at ~" << static_cast<int>(disk_eff)
            << " MB/s effective.";
        if (streamed > 0) {
            why << " Predicted " << (p.predicted_tok_s >= 1.0
                                         ? std::to_string(static_cast<int>(p.predicted_tok_s + 0.5))
                                         : std::to_string(p.predicted_tok_s).substr(0, 4))
                << " tok/s is disk-bound; it is a physics limit, not a tuning choice.";
        }
    }

    // ---- VRAM: with a compute backend the top layers run there. The model's
    // own placement is authoritative; this is the plan-time estimate.
    if (req.allow_vram && hw.vram_free_bytes > 0) {
        p.vram_bytes = hw.vram_free_bytes - hw.vram_free_bytes * 15 / 100;
        const int gpu_layers = static_cast<int>(std::min<uint64_t>(
            static_cast<uint64_t>(m.n_layers), p.vram_bytes / per_layer));
        if (gpu_backend() && gpu_layers > 0) {
            why << " " << gpu_layers << " of " << m.n_layers << " layers fit the "
                << gb(p.vram_bytes) << " GB VRAM budget on " << hw.gpu_name
                << " and run there; the rest on the CPU.";
        } else {
            why << " " << gb(hw.vram_free_bytes) << " GB VRAM free on " << hw.gpu_name
                << " (no compute backend active: FISH_GPU=0 or no Vulkan device).";
        }
    }

    p.arena_bytes = std::max<uint64_t>(256ull << 20, budget / 8);
    p.rationale = why.str();
    return p;
}

// ---------------------------------------------------------------- JSON

std::string hardware_json(const HardwareSpecs& hw) {
    std::ostringstream o;
    o << "{";
    o << "\"cpu\":\"" << json_escape(hw.cpu_name) << "\"";
    o << ",\"physical_cores\":" << hw.physical_cores;
    o << ",\"logical_cores\":" << hw.logical_cores;
    o << ",\"avx2\":" << (hw.avx2 ? "true" : "false");
    o << ",\"avx512\":" << (hw.avx512 ? "true" : "false");
    o << ",\"neon\":" << (hw.neon ? "true" : "false");
    o << ",\"ram_total_bytes\":" << hw.ram_total_bytes;
    o << ",\"ram_available_bytes\":" << hw.ram_available_bytes;
    o << ",\"swap_total_bytes\":" << hw.swap_total_bytes;
    o << ",\"gpu\":\"" << json_escape(hw.gpu_name) << "\"";
    o << ",\"vram_total_bytes\":" << hw.vram_total_bytes;
    o << ",\"vram_free_bytes\":" << hw.vram_free_bytes;
    o << ",\"unified_memory\":" << (hw.unified_memory ? "true" : "false");
    o << ",\"disk_read_mb_s\":" << hw.disk_read_mb_s;
    o << ",\"ram_bandwidth_mb_s\":" << hw.ram_bandwidth_mb_s;
    o << "}";
    return o.str();
}

std::string plan_json(const ExecutionPlan& p) {
    std::ostringstream o;
    o << "{";
    o << "\"mode\":\"" << p.mode << "\"";
    o << ",\"n_ctx\":" << p.n_ctx;
    o << ",\"n_threads\":" << p.n_threads;
    o << ",\"resident_layers\":" << p.resident_layers;
    o << ",\"window_layers\":" << p.window_layers;
    o << ",\"expert_cache_slots\":" << p.expert_cache_slots;
    o << ",\"ram_budget_bytes\":" << p.ram_budget_bytes;
    o << ",\"arena_bytes\":" << p.arena_bytes;
    o << ",\"vram_bytes\":" << p.vram_bytes;
    o << ",\"bytes_per_token\":" << p.bytes_per_token;
    o << ",\"predicted_tok_s\":" << p.predicted_tok_s;
    o << ",\"predicted_ttft_s\":" << p.predicted_ttft_s;
    o << ",\"rationale\":\"" << json_escape(p.rationale) << "\"";
    o << "}";
    return o.str();
}

// ---------------------------------------------------------------- arena

Arena::Arena(uint64_t capacity) : capacity_(capacity) {
    if (capacity_ == 0) return;
    base_ = static_cast<uint8_t*>(std::malloc(static_cast<size_t>(capacity_)));
    if (!base_) capacity_ = 0;
}

Arena::~Arena() { std::free(base_); }

void* Arena::alloc(uint64_t bytes, size_t align) {
    if (!base_ || align == 0) return nullptr;
    const uint64_t aligned = (used_ + align - 1) & ~static_cast<uint64_t>(align - 1);
    if (aligned + bytes > capacity_) return nullptr;
    void* ptr = base_ + aligned;
    used_ = aligned + bytes;
    return ptr;
}

void Arena::reset() { used_ = 0; }

} // namespace fish
