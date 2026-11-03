/*
 * FISH — Vulkan compute backend (stage 1: weight-stationary projections).
 *
 * What it does: weight tensors chosen by LlamaModel::place_layers() are copied
 * to device-local memory in their packed GGUF form; matmul() uploads the f32
 * activations, runs a dequantising matmul shader and reads the result back.
 * Everything else (attention, norms, RoPE, KV cache, MoE experts) stays on
 * the CPU. See docs/engine/GPU.md.
 *
 * Lifetime rules (the same caution as the engine's thread pool):
 *   - the backend object is heap-allocated and deliberately leaked, so no
 *     static destructor ever calls into a driver during process exit or
 *     DLL detach;
 *   - the Vulkan loader library is never closed;
 *   - nothing is initialised inside the static initializer: DllMain runs
 *     under the loader lock, and creating a Vulkan instance there (which
 *     loads driver DLLs and starts driver threads) can deadlock. The
 *     initializer only registers the object; the device is opened on the
 *     first query (place_layers' memory_free() at model load). If that fails
 *     the backend unregisters itself, so gpu_backend() becomes nullptr.
 */
#include "vulkan_backend.h"
#include "vk_loader.h"
#include "requant.h"

#include <fish/backend.h>
#include <fish/gguf.h>
#include <fish/quant.h>
#include <fish/threadpool.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace fish {
namespace gpu {

namespace {

// Generated at build time from shaders/*.comp (or the committed prebuilt copy):
// struct FishSpv { const char* name; const uint32_t* code; size_t words; };
// static const FishSpv kFishSpv[]; static const size_t kFishSpvCount;
#include "fish_vk_shaders.inc"

constexpr int kMaxTok = 256;               // tokens per dispatch (engine max batch)
constexpr uint64_t kStagingBytes = 32ull << 20;
constexpr uint64_t kPadBytes = 16;         // straddling 32-bit reads in dequant.glsl
// Headroom kept free on the device heap (live, via VK_EXT_memory_budget):
// max(256 MB, 5% of the heap), FISH_VRAM_HEADROOM_PCT / _MB override. Measured on a
// 4 GB laptop GPU shared with another process: filling the heap to the last
// ~64 MB made WDDM demote part of the weights to system memory and the
// kernels fell from ~60 to ~35 GB/s. Until 2026-11-02 the rule was
// max(256 MB, 10%) plus a separate 85%-of-free ceiling; on a 4 GB card that
// left ~500 MB unused, which is exactly what a fully resident 8B lacks.
// 256 MB held with the whole model resident (GPU.md, "Device formats").
// The rule itself is vram_headroom_bytes() (backend.h), shared with the planner.
constexpr uint64_t kScratchOutBytes = 32ull << 20;   // per-matmul output scratch cap
constexpr uint32_t kGroupOut = 2;          // dev_y_ holds this many max-width outputs (gate|up)

bool env_is(const char* name, const char* v) {
    const char* e = std::getenv(name);
    return e && std::strcmp(e, v) == 0;
}

bool gpu_disabled_by_env() {
    const char* e = std::getenv("FISH_GPU");
    if (!e) return false;
    return std::strcmp(e, "0") == 0 || std::strcmp(e, "off") == 0 ||
           std::strcmp(e, "false") == 0 || std::strcmp(e, "no") == 0;
}

bool verbose() { return env_is("FISH_GPU_VERBOSE", "1"); }

struct TypeKernels {
    GgmlType type;
    const char* suffix;
    VkPipeline matvec = VK_NULL_HANDLE;
    VkPipeline tiled = VK_NULL_HANDLE;
    VkPipeline coop = VK_NULL_HANDLE;    // matmul_coop.comp, null without cooperative matrices
};

struct Buffer {
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    uint64_t size = 0;
    void* map = nullptr;
    bool coherent = true;
    int block = -1;          // >= 0: bound inside weight block `block` (mem not owned)
};

// Weight buffers are bound into shared device-memory blocks. One
// vkAllocateMemory per tensor cost ~1.2 MB each on the RTX 3050 / WDDM
// (measured: 2778 MB of weights in 224 buffers lowered the live budget by
// 3097 MB, scratch and staging included), i.e. ~250 MB on an 8B.
struct WeightBlock {
    VkDeviceMemory mem = VK_NULL_HANDLE;
    uint32_t type = 0;
    uint64_t size = 0, used = 0;
    int live = 0;
};
constexpr uint64_t kWeightBlockBytes = 64ull << 20;

// Internal device-only type id for the re-quantised 3-bit layout (never a GGUF type).
constexpr GgmlType kFQ3 = static_cast<GgmlType>(100);
// FISH 3-bit symmetric non-uniform layout (3.25 bpw, src/gpu/requant.h).
constexpr GgmlType kFQ3S = static_cast<GgmlType>(101);

const char* dev_type_name(GgmlType ty) {
    if (ty == kFQ3) return "FQ3";
    if (ty == kFQ3S) return "FQ3S";
    return ggml_type_name(ty);
}

uint64_t dev_row_bytes(GgmlType ty, uint64_t n_in) {
    if (ty == kFQ3) return n_in / 256 * kFQ3BlockBytes;
    if (ty == kFQ3S) return n_in / 256 * kFQ3SBlockBytes;
    return ggml_row_size(ty, n_in);
}

// On-disk copy of a re-quantised tensor (planner path). Header then payload;
// a file whose header does not match the request is ignored and rewritten.
struct DevCacheHeader {
    char magic[8];          // "FISHDEV1"
    uint32_t dev_type, src_type, n_in, n_out, row_bytes, reserved;
    uint64_t payload;
};

// Opens a cache file whose header matches; the caller reads the payload
// (streamed straight into the staging ring) and closes it.
FILE* dev_cache_open(const std::string& path, const DevCacheHeader& want) {
    if (path.empty()) return nullptr;
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return nullptr;
    // 4 MB stdio buffer: large sequential reads instead of 4 KB ones. Must be
    // set before the first read (setvbuf after I/O drops buffered bytes).
    std::setvbuf(f, nullptr, _IOFBF, 4u << 20);
    DevCacheHeader h{};
    const bool ok = std::fread(&h, sizeof(h), 1, f) == 1 && std::memcmp(h.magic, want.magic, 8) == 0 &&
                    h.dev_type == want.dev_type && h.src_type == want.src_type && h.n_in == want.n_in &&
                    h.n_out == want.n_out && h.row_bytes == want.row_bytes && h.payload == want.payload;
    if (!ok) { std::fclose(f); return nullptr; }
    return f;
}

void dev_cache_write(const std::string& path, const DevCacheHeader& h, const uint8_t* data) {
    if (path.empty()) return;
    const std::string tmp = path + ".tmp";
    FILE* f = std::fopen(tmp.c_str(), "wb");
    if (!f) return;
    bool ok = std::fwrite(&h, sizeof(h), 1, f) == 1 &&
              std::fwrite(data, 1, static_cast<size_t>(h.payload), f) == h.payload;
    ok = (std::fclose(f) == 0) && ok;
    if (!ok) { std::remove(tmp.c_str()); return; }
    std::remove(path.c_str());
    if (std::rename(tmp.c_str(), path.c_str()) != 0) std::remove(tmp.c_str());
}

struct Resident {
    Buffer w;
    VkDescriptorSet set = VK_NULL_HANDLE;
    VkDescriptorPool pool = VK_NULL_HANDLE;
    GgmlType type = GgmlType::F32;       // layout on the device
    GgmlType src_type = GgmlType::F32;   // layout in the GGUF
    uint32_t n_in = 0, n_out = 0, row_bytes = 0;
    const uint8_t* data = nullptr;
    uint64_t nbytes = 0;
};

struct PushConsts { uint32_t n_in, n_out, n_tok, row_bytes, y_off, x_off; };
constexpr uint32_t kPushBytes = 64;          // one push range for every pipeline

// ---- layer span (see span_* shaders and docs/engine/GPU.md)
constexpr uint32_t kNone = 0xFFFFFFFFu;
constexpr int kSpanChunk = 64;               // tokens per pass through the layers

struct SpanTensor {
    const Resident* r = nullptr;
    VkDescriptorSet set = VK_NULL_HANDLE;    // (W, arena, arena)
    VkDescriptorPool pool = VK_NULL_HANDLE;
};

struct SpanLayerDev {
    SpanTensor wq, wk, wv, wqkv, wo, wg, wu, wd;
    uint32_t attn_norm = kNone, ffn_norm = kNone, post_attn = kNone, post_ffn = kNone;
    uint32_t q_norm = kNone, k_norm = kNone;
    uint32_t bq = kNone, bk = kNone, bv = kNone, bo = kNone, bqkv = kNone;
    bool window = false;
};

// Push-constant blocks, field for field the same as the shaders' `PC`.
struct RmsPC  { uint32_t n, rows_per_tok, src_off, src_tok_stride, dst_off, dst_tok_stride, w_off; float eps; };
struct AddPC  { uint32_t n, n_tok, dst_off, dst_stride, src_off, src_stride, src_params; };
struct RopePC { uint32_t off, tok_stride, head_dim, half_rot, pos0, neox, cos_off, sin_off; };
struct KvPC   { uint32_t kvd, k_off, k_stride, v_off, v_stride, ck_off, cv_off, pos0; };
struct AttnPC { uint32_t head_dim, group, kvd, q_off, q_stride, o_off, o_stride, ck_off, cv_off, pos0, window; float scale, softcap; };
// int8 KV (span_kv_store_q8 / span_attention_q8): the same fields + scale offsets.
struct KvQ8PC   { uint32_t kvd, k_off, k_stride, v_off, v_stride, ck_off, cv_off, pos0, cks_off, cvs_off; };
struct AttnQ8PC { uint32_t head_dim, group, kvd, q_off, q_stride, o_off, o_stride, ck_off, cv_off, pos0, window; float scale, softcap;
                  uint32_t cks_off, cvs_off; };
static_assert(sizeof(AttnQ8PC) <= kPushBytes, "push constants");
struct ActPC  { uint32_t n, n_tok, g_off, g_stride, u_off, u_stride, o_off, o_stride, gelu; };
static_assert(sizeof(AttnPC) <= kPushBytes, "push constants");

} // namespace

class VulkanBackend final : public ComputeBackend {
public:
    VulkanBackend() = default;
    ~VulkanBackend() override { shutdown(); }

    const char* name() const override { return "vulkan"; }
    const char* device_name() const override {
        self().ensure_init();
        return device_name_.empty() ? "none" : device_name_.c_str();
    }
    uint64_t memory_total() const override {
        if (!self().ensure_init()) return 0;
        return heap_size_;
    }
    uint64_t memory_free() const override {
        if (!self().ensure_init()) return 0;
        std::lock_guard<std::mutex> lk(mu_);
        return live_free_locked();
    }
    bool supports(GgmlType type) const override {
        if (!self().ensure_init()) return false;
        return kernels_for(type) != nullptr;
    }
    bool upload(const TensorInfo* t) override;
    uint64_t device_bytes(const TensorInfo* t, int fmt) const override;
    bool upload_as(const TensorInfo* t, int fmt, const float* importance,
                   const std::string& cache_file) override;
    bool resident(const TensorInfo* t) const override;
    void release(const TensorInfo* t) override;
    uint64_t bytes_resident() const override {
        std::lock_guard<std::mutex> lk(mu_);
        return bytes_resident_;
    }
    bool matmul(const TensorInfo* t, const float* x, const ActBatch* acts,
                int n_tok, int n_in, int n_out, float* y) override;
    bool matmul_group(const TensorInfo* const* ts, int n, const float* x,
                      const ActBatch* acts, int n_tok, int n_in,
                      const int* n_outs, float* const* ys) override;

    bool span_prepare(const SpanParams& p) override;
    bool span_forward(float* x, int n_tok, int pos0) override;
    void span_kv_truncate(int n_past) override;
    void span_release() override;

    bool ensure_init();
    const char* status() const { return status_.c_str(); }

private:
    // FISH_GPU_REQUANT modes (device copy only; GGUF and CPU path untouched):
    //   1  Q4_K -> FQ3 (3.5 bpw) everywhere, Q5_K/Q6_K -> Q4_K
    //   2  Q5_K/Q6_K -> Q4_K only
    //   3  as 2, plus Q4_K -> FQ3 for ffn_gate / ffn_up (54% of a Llama layer)
    GgmlType requant_target(const TensorInfo* t, uint64_t n_in) const {
        const GgmlType src = t->type;
        if (requant_mode_ == 0 || n_in % 256 != 0) return src;
        if (src == GgmlType::Q5_K || src == GgmlType::Q6_K) return GgmlType::Q4_K;
        if (src == GgmlType::Q4_K) {
            if (requant_mode_ == 1) return kFQ3;
            if (requant_mode_ == 3 && (t->name.find("ffn_gate") != std::string::npos ||
                                       t->name.find("ffn_up") != std::string::npos)) return kFQ3;
        }
        return src;
    }
    bool requantize(const TensorInfo* t, GgmlType dev_type, uint64_t row_bytes, const float* imp);
    // Device type for a planner format, or F32 with ok=false if not possible.
    GgmlType format_type(const TensorInfo* t, int fmt, bool* ok) const;
    bool upload_impl(const TensorInfo* t, GgmlType dev_type, const float* imp, const std::string& cache);
    uint64_t headroom() const { return vram_headroom_bytes(heap_size_); }
public:
    std::string describe() const;
private:
    void span_release_locked();
    bool alloc_raw_set(VkDescriptorSet* set, VkDescriptorPool* pool);
    void write_set3(VkDescriptorSet set, VkBuffer b0, VkBuffer b1, VkBuffer b2);
    bool upload_bytes(Buffer& dst, const void* src, uint64_t bytes);
    VulkanBackend& self() const { return const_cast<VulkanBackend&>(*this); }
    bool init_locked();
    void shutdown();

    const TypeKernels* kernels_for(GgmlType type) const {
        for (const auto& k : kernels_) if (k.type == type && k.matvec && k.tiled) return &k;
        return nullptr;
    }
    uint64_t live_free_locked() const;
    int find_type(uint32_t bits, VkMemoryPropertyFlags want, VkMemoryPropertyFlags avoid) const;
    bool create_buffer(Buffer& b, uint64_t size, VkBufferUsageFlags usage, bool host);
    bool create_weight_buffer(Buffer& b, uint64_t size);
public:
    void reserve_device(uint64_t bytes) override;
    void release_reserve() override;
private:
    std::vector<WeightBlock> blocks_;
    bool create_host_buffer(Buffer& b, uint64_t size, VkBufferUsageFlags usage, bool readback);
    void destroy_buffer(Buffer& b);
    bool ensure_scratch(uint32_t n_in, uint32_t n_out);
    void write_set(const Resident& r);
    bool alloc_set(Resident& r);
    void free_resident(Resident& r);
    bool submit_and_wait();
    // One submit: upload x once, one dispatch per tensor, one readback.
    bool run_locked(const Resident* const* rs, int n, const float* x, int n_tok, float* const* ys);

    mutable std::mutex mu_;
    enum class State { Fresh, Ready, Failed } state_ = State::Fresh;
    std::string status_ = "not initialised";
    bool broken_ = false;

    VkApi vk_;
    VkInstance instance_ = VK_NULL_HANDLE;
    VkPhysicalDevice phys_ = VK_NULL_HANDLE;
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue queue_ = VK_NULL_HANDLE;
    uint32_t queue_family_ = 0;
    VkPhysicalDeviceMemoryProperties memprops_{};
    VkPhysicalDeviceLimits limits_{};
    bool has_budget_ext_ = false;
    bool coop_ = false;              // cooperative-matrix prefill kernels enabled
    uint32_t dev_heap_ = 0;
    uint64_t heap_size_ = 0;
    uint64_t budget_ = 0;            // this backend's own VRAM ceiling
    std::string device_name_;

    VkCommandPool cmd_pool_ = VK_NULL_HANDLE;
    VkCommandBuffer cmd_ = VK_NULL_HANDLE;
    VkFence fence_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout set_layout_ = VK_NULL_HANDLE;
    VkPipelineLayout pipe_layout_ = VK_NULL_HANDLE;
    std::vector<VkDescriptorPool> pools_;
    std::vector<TypeKernels> kernels_;

    // Activations / results: device-local working copies plus persistently
    // mapped host buffers they are copied through.
    Buffer dev_x_, dev_y_, host_x_, host_y_;
    uint32_t cap_in_ = 0, cap_out_ = 0;
    int tok_cap_ = kMaxTok;              // tokens per run_locked submit (ensure_scratch)
    uint64_t scratch_dev_bytes_ = 0;
    Buffer staging_;
    // Upload ring (load time): two staging halves, each with its own command
    // buffer and fence, so the next chunk is read / copied on the CPU while the
    // previous one is still being DMA'd (stream_upload).
    Buffer staging2_;
    VkCommandBuffer cmd_up_[2] = {VK_NULL_HANDLE, VK_NULL_HANDLE};
    VkFence fence_up_[2] = {VK_NULL_HANDLE, VK_NULL_HANDLE};
    bool up_pending_[2] = {false, false};
    bool ensure_upload_ring();
    bool wait_up(int i);
    bool stream_upload(Buffer& dst, uint64_t bytes, const std::function<bool(uint8_t*, uint64_t, uint64_t)>& fill);
    // Load profile (upload_profile()): where upload time went.
    double prof_read_s_ = 0, prof_requant_s_ = 0, prof_copy_s_ = 0;
    int prof_hits_ = 0, prof_misses_ = 0, prof_file_ = 0;
    uint64_t prof_bytes_ = 0;
public:
    std::string upload_profile(bool reset) override;
private:

    // Last activation batch that is still in dev_x_ (so wq/wk/wv share one
    // upload). A shadow copy is compared because xb_ is reused by every layer.
    const float* last_x_ = nullptr;
    size_t last_count_ = 0;
    std::vector<float> shadow_;
    std::vector<uint32_t> group_offs_;   // reserved once; no per-call allocation
    static constexpr int kMaxGroup = 8;

    std::unordered_map<const TensorInfo*, Resident> res_;
    uint64_t bytes_resident_ = 0;
    int matvec_max_tok_ = 4;
    int requant_mode_ = 0;                 // FISH_GPU_REQUANT 0..3, see requant_target()
    std::vector<uint8_t> requant_buf_;     // load time only; freed at the first matmul

    // ---- layer span
    VkPipeline sp_rms_ = VK_NULL_HANDLE, sp_add_ = VK_NULL_HANDLE, sp_rope_ = VK_NULL_HANDLE;
    VkPipeline sp_kv_ = VK_NULL_HANDLE, sp_attn_ = VK_NULL_HANDLE, sp_act_ = VK_NULL_HANDLE;
    VkPipeline sp_kv8_ = VK_NULL_HANDLE, sp_attn8_ = VK_NULL_HANDLE;   // int8 KV cache
    bool span_ready_ = false;
    std::vector<SpanLayerDev> span_layers_;
    Buffer span_arena_, span_params_, span_kv_;
    // Host staging of x for span_forward, kMaxTok tokens. Not shared with the
    // per-matmul scratch, which is capped by output width (ensure_scratch):
    // sharing it limited span batches to 32 tokens on a 128k vocabulary.
    Buffer span_hx_, span_hy_;
    VkDescriptorSet span_set_ = VK_NULL_HANDLE;        // (arena, params, kv)
    VkDescriptorPool span_pool_ = VK_NULL_HANDLE;
    uint64_t span_dev_bytes_ = 0;
    int span_kv_len_ = 0;
    // FISH_GPU_PROFILE=2: per-stage GPU time inside span_forward.
    static constexpr uint32_t kStageQueries = 4096;
    bool prof_stages_ = false;
    VkQueryPool stage_pool_ = VK_NULL_HANDLE;
    std::vector<const char*> stage_labels_;
    std::vector<uint64_t> stage_ts_;
    std::vector<std::pair<const char*, double>> stage_ms_;
    long stage_calls_ = 0;
    struct {
        uint32_t E = 0, H = 0, HKV = 0, D = 0, F = 0, Q = 0, KV = 0, n_ctx = 0, half = 0;
        float eps = 0, scale = 0, softcap = 0;
        uint32_t window = 0, gelu = 0, neox = 0, cos_off = 0, sin_off = 0;
        bool kvq8 = false;
        // arena offsets (floats)
        uint32_t X = 0, XB = 0, QKV = 0, ATT = 0, PROJ = 0, GU = 0, ACT = 0, FFN = 0;
    } sp_;

public:
    // FISH_GPU_PROFILE=1: timestamps around the dispatches of every submit.
    bool profile_ = false;
    VkQueryPool qpool_ = VK_NULL_HANDLE;
    double prof_kernel_ms_ = 0, prof_wall_ms_ = 0;
    long prof_submits_ = 0;
};

namespace {
VulkanBackend* g_backend = nullptr;
}

// ---------------------------------------------------------------- init

bool VulkanBackend::ensure_init() {
    std::lock_guard<std::mutex> lk(mu_);
    if (state_ == State::Fresh) {
        const bool ok = init_locked();
        state_ = ok ? State::Ready : State::Failed;
        if (!ok) {
            if (verbose()) std::fprintf(stderr, "[fish/vulkan] disabled: %s\n", status_.c_str());
            // Unregister: from now on the engine sees "no GPU". The object
            // stays alive (callers may still hold the pointer this call).
            if (gpu_backend() == this) set_gpu_backend(nullptr);
        } else if (verbose()) {
            std::fprintf(stderr, "[fish/vulkan] %s, %.0f MB heap, budget %.0f MB, memory_budget ext %s\n",
                         device_name_.c_str(), heap_size_ / 1048576.0, budget_ / 1048576.0,
                         has_budget_ext_ ? "yes" : "no");
        }
    }
    return state_ == State::Ready && !broken_;
}

bool VulkanBackend::init_locked() {
    const char* why = nullptr;
    auto fail = [&](const std::string& msg) { status_ = msg; return false; };

    if (!vk_.load_library(&why)) return fail(why);
    uint32_t api = VK_API_VERSION_1_0;
    if (vk_.vkEnumerateInstanceVersion) vk_.vkEnumerateInstanceVersion(&api);
    if (api < VK_API_VERSION_1_1) return fail("Vulkan loader is 1.0; FISH needs 1.1");

    // Instance extensions: portability enumeration lets MoltenVK (macOS) show up.
    uint32_t n_ext = 0;
    vk_.vkEnumerateInstanceExtensionProperties(nullptr, &n_ext, nullptr);
    std::vector<VkExtensionProperties> exts(n_ext);
    if (n_ext) vk_.vkEnumerateInstanceExtensionProperties(nullptr, &n_ext, exts.data());
    std::vector<const char*> inst_ext;
    VkInstanceCreateFlags inst_flags = 0;
    for (const auto& e : exts) {
        if (std::strcmp(e.extensionName, "VK_KHR_portability_enumeration") == 0) {
            inst_ext.push_back("VK_KHR_portability_enumeration");
            inst_flags |= 0x00000001;  // VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR
        }
    }
    std::vector<const char*> layers;
    if (env_is("FISH_VK_VALIDATE", "1")) layers.push_back("VK_LAYER_KHRONOS_validation");

    VkApplicationInfo app{};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "FISH";
    app.pEngineName = "FISH";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ici{};
    ici.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ici.flags = inst_flags;
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = static_cast<uint32_t>(inst_ext.size());
    ici.ppEnabledExtensionNames = inst_ext.empty() ? nullptr : inst_ext.data();
    ici.enabledLayerCount = static_cast<uint32_t>(layers.size());
    ici.ppEnabledLayerNames = layers.empty() ? nullptr : layers.data();
    VkResult r = vk_.vkCreateInstance(&ici, nullptr, &instance_);
    if (r != VK_SUCCESS && !layers.empty()) {
        ici.enabledLayerCount = 0;   // validation layer not installed
        r = vk_.vkCreateInstance(&ici, nullptr, &instance_);
    }
    if (r != VK_SUCCESS) { instance_ = VK_NULL_HANDLE; return fail("vkCreateInstance failed"); }
    if (!vk_.load_instance(instance_, &why)) return fail(why);

    // ---- device selection: discrete > integrated > virtual > (cpu if allowed)
    uint32_t n_dev = 0;
    vk_.vkEnumeratePhysicalDevices(instance_, &n_dev, nullptr);
    if (n_dev == 0) return fail("no Vulkan physical device");
    std::vector<VkPhysicalDevice> devs(n_dev);
    vk_.vkEnumeratePhysicalDevices(instance_, &n_dev, devs.data());
    const bool allow_cpu = env_is("FISH_GPU", "cpu");
    int forced = -1;
    if (const char* e = std::getenv("FISH_GPU_DEVICE")) forced = std::atoi(e);

    int best = -1, best_score = -1;
    uint32_t best_family = 0;
    for (uint32_t i = 0; i < n_dev; ++i) {
        VkPhysicalDeviceProperties p{};
        vk_.vkGetPhysicalDeviceProperties(devs[i], &p);
        if (p.apiVersion < VK_API_VERSION_1_1) continue;
        int score;
        switch (p.deviceType) {
            case VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU:   score = 40; break;
            case VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU: score = 30; break;
            case VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU:    score = 20; break;
            case VK_PHYSICAL_DEVICE_TYPE_CPU:            score = allow_cpu ? 10 : -1; break;
            default:                                     score = 5; break;
        }
        if (score < 0) continue;
        if (allow_cpu && p.deviceType == VK_PHYSICAL_DEVICE_TYPE_CPU) score = 50;
        // Queue family: a compute-only family first (async compute does not
        // compete with the desktop compositor's graphics queue), else any.
        uint32_t nq = 0;
        vk_.vkGetPhysicalDeviceQueueFamilyProperties(devs[i], &nq, nullptr);
        std::vector<VkQueueFamilyProperties> qf(nq);
        vk_.vkGetPhysicalDeviceQueueFamilyProperties(devs[i], &nq, qf.data());
        int fam = -1;
        const bool want_async = !env_is("FISH_VK_QUEUE", "universal");
        for (uint32_t q = 0; q < nq && want_async; ++q)
            if ((qf[q].queueFlags & VK_QUEUE_COMPUTE_BIT) && !(qf[q].queueFlags & VK_QUEUE_GRAPHICS_BIT) && qf[q].queueCount) { fam = static_cast<int>(q); break; }
        if (fam < 0)
            for (uint32_t q = 0; q < nq; ++q)
                if ((qf[q].queueFlags & VK_QUEUE_COMPUTE_BIT) && qf[q].queueCount) { fam = static_cast<int>(q); break; }
        if (fam < 0) continue;
        if (forced >= 0) score = (static_cast<int>(i) == forced) ? 100 : -1;
        if (score > best_score) { best_score = score; best = static_cast<int>(i); best_family = static_cast<uint32_t>(fam); }
    }
    if (best < 0) return fail(allow_cpu ? "no Vulkan device with a compute queue"
                                        : "no hardware Vulkan GPU with a compute queue (software devices need FISH_GPU=cpu)");
    phys_ = devs[static_cast<size_t>(best)];
    queue_family_ = best_family;
    VkPhysicalDeviceProperties props{};
    vk_.vkGetPhysicalDeviceProperties(phys_, &props);
    device_name_ = props.deviceName;
    limits_ = props.limits;
    vk_.vkGetPhysicalDeviceMemoryProperties(phys_, &memprops_);

    // ---- device extensions
    uint32_t n_dext = 0;
    vk_.vkEnumerateDeviceExtensionProperties(phys_, nullptr, &n_dext, nullptr);
    std::vector<VkExtensionProperties> dexts(n_dext);
    if (n_dext) vk_.vkEnumerateDeviceExtensionProperties(phys_, nullptr, &n_dext, dexts.data());
    std::vector<const char*> dev_ext;
    for (const auto& e : dexts) {
        if (std::strcmp(e.extensionName, "VK_EXT_memory_budget") == 0) {
            has_budget_ext_ = true;
            dev_ext.push_back("VK_EXT_memory_budget");
        } else if (std::strcmp(e.extensionName, "VK_KHR_portability_subset") == 0) {
            dev_ext.push_back("VK_KHR_portability_subset");   // required when present
        }
    }

    // ---- tensor cores for prefill (matmul_coop.comp): VK_KHR_cooperative_matrix
    // with a 16x16x16 f16 x f16 -> f32 subgroup configuration, shaderFloat16,
    // the Vulkan memory model and 32-wide subgroups. FISH_VK_COOPMAT=0 opts out.
    VkPhysicalDeviceCooperativeMatrixFeaturesKHR en_cm{};
    VkPhysicalDeviceShaderFloat16Int8Features en_f16{};
    VkPhysicalDeviceVulkanMemoryModelFeatures en_vmm{};
    const void* dev_pnext = nullptr;
    {
        bool e_cm = false, e_f16 = false, e_vmm = false;
        for (const auto& e : dexts) {
            if (std::strcmp(e.extensionName, "VK_KHR_cooperative_matrix") == 0) e_cm = true;
            if (std::strcmp(e.extensionName, "VK_KHR_shader_float16_int8") == 0) e_f16 = true;
            if (std::strcmp(e.extensionName, "VK_KHR_vulkan_memory_model") == 0) e_vmm = true;
        }
        bool ok = e_cm && e_f16 && e_vmm && !env_is("FISH_VK_COOPMAT", "0");
        if (ok) {
            VkPhysicalDeviceCooperativeMatrixFeaturesKHR cm{};
            cm.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_COOPERATIVE_MATRIX_FEATURES_KHR;
            VkPhysicalDeviceShaderFloat16Int8Features f16{};
            f16.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_FLOAT16_INT8_FEATURES;
            VkPhysicalDeviceVulkanMemoryModelFeatures vmm{};
            vmm.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_MEMORY_MODEL_FEATURES;
            cm.pNext = &f16;
            f16.pNext = &vmm;
            VkPhysicalDeviceFeatures2 f2{};
            f2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
            f2.pNext = &cm;
            vk_.vkGetPhysicalDeviceFeatures2(phys_, &f2);
            ok = cm.cooperativeMatrix && f16.shaderFloat16 && vmm.vulkanMemoryModel;
        }
        if (ok) {
            VkPhysicalDeviceSubgroupProperties sp{};
            sp.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES;
            VkPhysicalDeviceProperties2 p2{};
            p2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
            p2.pNext = &sp;
            vk_.vkGetPhysicalDeviceProperties2(phys_, &p2);
            ok = sp.subgroupSize == 32;
        }
        if (ok) {
            auto fn = reinterpret_cast<PFN_vkGetPhysicalDeviceCooperativeMatrixPropertiesKHR>(
                vk_.vkGetInstanceProcAddr(instance_, "vkGetPhysicalDeviceCooperativeMatrixPropertiesKHR"));
            uint32_t n = 0;
            bool found = false;
            if (fn && fn(phys_, &n, nullptr) == VK_SUCCESS && n > 0) {
                std::vector<VkCooperativeMatrixPropertiesKHR> cps(n);
                for (auto& c : cps) c.sType = VK_STRUCTURE_TYPE_COOPERATIVE_MATRIX_PROPERTIES_KHR;
                if (fn(phys_, &n, cps.data()) == VK_SUCCESS)
                    for (const auto& c : cps)
                        found |= c.MSize == 16 && c.NSize == 16 && c.KSize == 16 &&
                                 c.AType == VK_COMPONENT_TYPE_FLOAT16_KHR && c.BType == VK_COMPONENT_TYPE_FLOAT16_KHR &&
                                 c.CType == VK_COMPONENT_TYPE_FLOAT32_KHR && c.ResultType == VK_COMPONENT_TYPE_FLOAT32_KHR &&
                                 c.scope == VK_SCOPE_SUBGROUP_KHR;
            }
            ok = found;
        }
        if (ok) {
            dev_ext.push_back("VK_KHR_cooperative_matrix");
            dev_ext.push_back("VK_KHR_shader_float16_int8");
            dev_ext.push_back("VK_KHR_vulkan_memory_model");
            en_cm.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_COOPERATIVE_MATRIX_FEATURES_KHR;
            en_cm.cooperativeMatrix = VK_TRUE;
            en_f16.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_FLOAT16_INT8_FEATURES;
            en_f16.shaderFloat16 = VK_TRUE;
            en_vmm.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_MEMORY_MODEL_FEATURES;
            en_vmm.vulkanMemoryModel = VK_TRUE;
            en_cm.pNext = &en_f16;
            en_f16.pNext = &en_vmm;
            dev_pnext = &en_cm;
            coop_ = true;
        }
    }

    const float prio = 1.0f;
    VkDeviceQueueCreateInfo qci{};
    qci.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    qci.queueFamilyIndex = queue_family_;
    qci.queueCount = 1;
    qci.pQueuePriorities = &prio;
    VkDeviceCreateInfo dci{};
    dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = static_cast<uint32_t>(dev_ext.size());
    dci.ppEnabledExtensionNames = dev_ext.empty() ? nullptr : dev_ext.data();
    dci.pNext = dev_pnext;
    if (vk_.vkCreateDevice(phys_, &dci, nullptr, &device_) != VK_SUCCESS) {
        device_ = VK_NULL_HANDLE;
        return fail("vkCreateDevice failed");
    }
    if (!vk_.load_device(device_, &why)) return fail(why);
    vk_.vkGetDeviceQueue(device_, queue_family_, 0, &queue_);

    // ---- the heap weights go to: the largest device-local one
    int best_type = -1;
    for (uint32_t i = 0; i < memprops_.memoryTypeCount; ++i) {
        const auto& mt = memprops_.memoryTypes[i];
        if (!(mt.propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) continue;
        const uint64_t hs = memprops_.memoryHeaps[mt.heapIndex].size;
        if (best_type < 0 || hs > heap_size_) { best_type = static_cast<int>(i); heap_size_ = hs; dev_heap_ = mt.heapIndex; }
    }
    if (best_type < 0) return fail("device has no device-local memory");

    // ---- command buffer, fence
    VkCommandPoolCreateInfo cpi{};
    cpi.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    cpi.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    cpi.queueFamilyIndex = queue_family_;
    if (vk_.vkCreateCommandPool(device_, &cpi, nullptr, &cmd_pool_) != VK_SUCCESS) return fail("vkCreateCommandPool failed");
    VkCommandBufferAllocateInfo cai{};
    cai.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    cai.commandPool = cmd_pool_;
    cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cai.commandBufferCount = 1;
    if (vk_.vkAllocateCommandBuffers(device_, &cai, &cmd_) != VK_SUCCESS) return fail("vkAllocateCommandBuffers failed");
    VkFenceCreateInfo fci{};
    fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    if (vk_.vkCreateFence(device_, &fci, nullptr, &fence_) != VK_SUCCESS) return fail("vkCreateFence failed");

    // ---- pipeline layout: W, X, Y storage buffers + 16 bytes of push constants
    VkDescriptorSetLayoutBinding b[3]{};
    for (uint32_t i = 0; i < 3; ++i) {
        b[i].binding = i;
        b[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        b[i].descriptorCount = 1;
        b[i].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    }
    VkDescriptorSetLayoutCreateInfo dli{};
    dli.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    dli.bindingCount = 3;
    dli.pBindings = b;
    if (vk_.vkCreateDescriptorSetLayout(device_, &dli, nullptr, &set_layout_) != VK_SUCCESS) return fail("descriptor set layout failed");
    VkPushConstantRange pcr{};
    pcr.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    pcr.size = kPushBytes;   // matmul and span kernels share one layout
    VkPipelineLayoutCreateInfo pli{};
    pli.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    pli.setLayoutCount = 1;
    pli.pSetLayouts = &set_layout_;
    pli.pushConstantRangeCount = 1;
    pli.pPushConstantRanges = &pcr;
    if (vk_.vkCreatePipelineLayout(device_, &pli, nullptr, &pipe_layout_) != VK_SUCCESS) return fail("pipeline layout failed");

    // ---- pipelines: one matvec + one tiled kernel per weight type
    kernels_ = {
        {GgmlType::Q4_K, "Q4_K"}, {GgmlType::Q6_K, "Q6_K"}, {GgmlType::Q5_K, "Q5_K"},
        {GgmlType::Q8_0, "Q8_0"}, {GgmlType::Q4_0, "Q4_0"}, {GgmlType::F16, "F16"},
        {GgmlType::F32, "F32"}, {kFQ3, "FQ3"}, {kFQ3S, "FQ3S"},
    };
    auto make_pipeline = [&](const std::string& name) -> VkPipeline {
        const FishSpv* spv = nullptr;
        for (size_t i = 0; i < kFishSpvCount; ++i)
            if (name == kFishSpv[i].name) { spv = &kFishSpv[i]; break; }
        if (!spv) return VK_NULL_HANDLE;
        VkShaderModuleCreateInfo smi{};
        smi.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
        smi.codeSize = spv->words * 4;
        smi.pCode = spv->code;
        VkShaderModule mod = VK_NULL_HANDLE;
        if (vk_.vkCreateShaderModule(device_, &smi, nullptr, &mod) != VK_SUCCESS) return VK_NULL_HANDLE;
        VkComputePipelineCreateInfo cpci{};
        cpci.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
        cpci.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        cpci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        cpci.stage.module = mod;
        cpci.stage.pName = "main";
        cpci.layout = pipe_layout_;
        VkPipeline p = VK_NULL_HANDLE;
        if (vk_.vkCreateComputePipelines(device_, VK_NULL_HANDLE, 1, &cpci, nullptr, &p) != VK_SUCCESS) p = VK_NULL_HANDLE;
        vk_.vkDestroyShaderModule(device_, mod, nullptr);
        return p;
    };
    int n_ok = 0;
    for (auto& k : kernels_) {
        k.matvec = make_pipeline(std::string("matvec_") + k.suffix);
        k.tiled = make_pipeline(std::string("matmul_tiled_") + k.suffix);
        if (coop_) k.coop = make_pipeline(std::string("matmul_coop_") + k.suffix);
        if (k.matvec && k.tiled) ++n_ok;
    }
    if (n_ok == 0) return fail("no compute pipeline could be created");
    // Span kernels are optional: without them span_prepare() declines.
    sp_rms_  = make_pipeline("span_rmsnorm");
    sp_add_  = make_pipeline("span_add");
    sp_rope_ = make_pipeline("span_rope");
    sp_kv_   = make_pipeline("span_kv_store");
    sp_attn_ = make_pipeline("span_attention");
    sp_kv8_  = make_pipeline("span_kv_store_q8");
    sp_attn8_ = make_pipeline("span_attention_q8");
    sp_act_  = make_pipeline("span_act_mul");

    if (const char* e = std::getenv("FISH_VK_MATVEC_MAX")) matvec_max_tok_ = std::max(1, std::atoi(e));
    if (const char* e = std::getenv("FISH_GPU_REQUANT")) requant_mode_ = std::max(0, std::min(3, std::atoi(e)));
    const bool prof2 = env_is("FISH_GPU_PROFILE", "2");
    if ((env_is("FISH_GPU_PROFILE", "1") || prof2) && limits_.timestampPeriod > 0.0f) {
        VkQueryPoolCreateInfo qi{};
        qi.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
        qi.queryType = VK_QUERY_TYPE_TIMESTAMP;
        qi.queryCount = 2;
        profile_ = vk_.vkCreateQueryPool(device_, &qi, nullptr, &qpool_) == VK_SUCCESS;
        if (prof2) {
            qi.queryCount = kStageQueries;
            prof_stages_ = vk_.vkCreateQueryPool(device_, &qi, nullptr, &stage_pool_) == VK_SUCCESS;
            stage_labels_.reserve(kStageQueries);
            stage_ts_.reserve(kStageQueries);
        }
    }

    // ---- this backend's own ceiling (place_layers applies the same rule to
    // the weights; the scratch buffers are counted here too).
    const uint64_t free_now = live_free_locked();
    budget_ = free_now > headroom() ? free_now - headroom() : 0;
    if (const char* e = std::getenv("FISH_VRAM_BUDGET_MB")) {
        const long long mb = std::atoll(e);
        if (mb > 0) budget_ = std::min<uint64_t>(static_cast<uint64_t>(mb) << 20, heap_size_);
    }
    status_ = "ready: " + device_name_;
    return true;
}

void VulkanBackend::shutdown() {
    // Only reached through an explicit delete (tests); the registered global
    // instance is leaked on purpose.
    if (device_) {
        vk_.vkDeviceWaitIdle(device_);
        span_release_locked();
        for (VkPipeline p : {sp_rms_, sp_add_, sp_rope_, sp_kv_, sp_attn_, sp_act_, sp_kv8_, sp_attn8_})
            if (p) vk_.vkDestroyPipeline(device_, p, nullptr);
        for (auto& kv : res_) {
            destroy_buffer(kv.second.w);
        }
        res_.clear();
        destroy_buffer(dev_x_); destroy_buffer(dev_y_);
        destroy_buffer(host_x_); destroy_buffer(host_y_);
        destroy_buffer(staging_);
        destroy_buffer(staging2_);
        for (VkDescriptorPool p : pools_) vk_.vkDestroyDescriptorPool(device_, p, nullptr);
        pools_.clear();
        for (auto& k : kernels_) {
            if (k.matvec) vk_.vkDestroyPipeline(device_, k.matvec, nullptr);
            if (k.tiled) vk_.vkDestroyPipeline(device_, k.tiled, nullptr);
        }
        kernels_.clear();
        if (pipe_layout_) vk_.vkDestroyPipelineLayout(device_, pipe_layout_, nullptr);
        if (set_layout_) vk_.vkDestroyDescriptorSetLayout(device_, set_layout_, nullptr);
        if (fence_) vk_.vkDestroyFence(device_, fence_, nullptr);
        for (VkFence& f : fence_up_) if (f) { vk_.vkDestroyFence(device_, f, nullptr); f = VK_NULL_HANDLE; }
        if (qpool_) vk_.vkDestroyQueryPool(device_, qpool_, nullptr);
        if (stage_pool_) vk_.vkDestroyQueryPool(device_, stage_pool_, nullptr);
        if (cmd_pool_) vk_.vkDestroyCommandPool(device_, cmd_pool_, nullptr);
        vk_.vkDestroyDevice(device_, nullptr);
        device_ = VK_NULL_HANDLE;
    }
    if (instance_ && vk_.vkDestroyInstance) vk_.vkDestroyInstance(instance_, nullptr);
    instance_ = VK_NULL_HANDLE;
}

// ---------------------------------------------------------------- memory

uint64_t VulkanBackend::live_free_locked() const {
    if (!phys_) return 0;
    if (has_budget_ext_) {
        VkPhysicalDeviceMemoryBudgetPropertiesEXT bp{};
        bp.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT;
        VkPhysicalDeviceMemoryProperties2 mp2{};
        mp2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2;
        mp2.pNext = &bp;
        vk_.vkGetPhysicalDeviceMemoryProperties2(phys_, &mp2);
        const uint64_t b = bp.heapBudget[dev_heap_], u = bp.heapUsage[dev_heap_];
        if (b > 0) return b > u ? b - u : 0;
    }
    const uint64_t cap = heap_size_ / 10 * 8;
    const uint64_t mine = bytes_resident_ + scratch_dev_bytes_;
    return cap > mine ? cap - mine : 0;
}

int VulkanBackend::find_type(uint32_t bits, VkMemoryPropertyFlags want, VkMemoryPropertyFlags avoid) const {
    for (uint32_t i = 0; i < memprops_.memoryTypeCount; ++i) {
        if (!(bits & (1u << i))) continue;
        const VkMemoryPropertyFlags f = memprops_.memoryTypes[i].propertyFlags;
        if ((f & want) == want && !(f & avoid)) return static_cast<int>(i);
    }
    return -1;
}

bool VulkanBackend::create_buffer(Buffer& b, uint64_t size, VkBufferUsageFlags usage, bool host) {
    (void)host;
    VkBufferCreateInfo bci{};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = size;
    bci.usage = usage;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vk_.vkCreateBuffer(device_, &bci, nullptr, &b.buf) != VK_SUCCESS) { b.buf = VK_NULL_HANDLE; return false; }
    VkMemoryRequirements req{};
    vk_.vkGetBufferMemoryRequirements(device_, b.buf, &req);
    // Device-local, preferably not host-visible (that is the small BAR window).
    int type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (type < 0) type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0);
    if (type < 0) { destroy_buffer(b); return false; }
    VkMemoryAllocateInfo mai{};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.allocationSize = req.size;
    mai.memoryTypeIndex = static_cast<uint32_t>(type);
    if (vk_.vkAllocateMemory(device_, &mai, nullptr, &b.mem) != VK_SUCCESS) { b.mem = VK_NULL_HANDLE; destroy_buffer(b); return false; }
    if (vk_.vkBindBufferMemory(device_, b.buf, b.mem, 0) != VK_SUCCESS) { destroy_buffer(b); return false; }
    b.size = size;
    return true;
}

bool VulkanBackend::create_host_buffer(Buffer& b, uint64_t size, VkBufferUsageFlags usage, bool readback) {
    VkBufferCreateInfo bci{};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = size;
    bci.usage = usage;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vk_.vkCreateBuffer(device_, &bci, nullptr, &b.buf) != VK_SUCCESS) { b.buf = VK_NULL_HANDLE; return false; }
    VkMemoryRequirements req{};
    vk_.vkGetBufferMemoryRequirements(device_, b.buf, &req);
    const VkMemoryPropertyFlags hv = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    const VkMemoryPropertyFlags hc = VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    const VkMemoryPropertyFlags ca = VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    int type = -1;
    if (readback) {
        // CPU reads results: cached memory, or every read is uncached.
        type = find_type(req.memoryTypeBits, hv | hc | ca, 0);
        if (type < 0) type = find_type(req.memoryTypeBits, hv | ca, 0);
    } else {
        // Upload: plain system memory, not the device-local BAR window.
        type = find_type(req.memoryTypeBits, hv | hc, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    }
    if (type < 0) type = find_type(req.memoryTypeBits, hv | hc, 0);
    if (type < 0) type = find_type(req.memoryTypeBits, hv, 0);
    if (type < 0) { destroy_buffer(b); return false; }
    b.coherent = (memprops_.memoryTypes[type].propertyFlags & hc) != 0;
    VkMemoryAllocateInfo mai{};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.allocationSize = req.size;
    mai.memoryTypeIndex = static_cast<uint32_t>(type);
    if (vk_.vkAllocateMemory(device_, &mai, nullptr, &b.mem) != VK_SUCCESS) { b.mem = VK_NULL_HANDLE; destroy_buffer(b); return false; }
    if (vk_.vkBindBufferMemory(device_, b.buf, b.mem, 0) != VK_SUCCESS ||
        vk_.vkMapMemory(device_, b.mem, 0, VK_WHOLE_SIZE, 0, &b.map) != VK_SUCCESS) {
        b.map = nullptr;
        destroy_buffer(b);
        return false;
    }
    b.size = size;
    return true;
}

void VulkanBackend::destroy_buffer(Buffer& b) {
    if (!device_) { b = Buffer{}; return; }
    if (b.block >= 0) {
        if (b.buf) vk_.vkDestroyBuffer(device_, b.buf, nullptr);
        --blocks_[static_cast<size_t>(b.block)].live;
        // Free every block without a live buffer, including reserved blocks
        // that were never used (planner fallback, unload).
        for (WeightBlock& wb : blocks_) {
            if (wb.mem && wb.live <= 0) {
                vk_.vkFreeMemory(device_, wb.mem, nullptr);
                wb = WeightBlock{};
            }
        }
        b = Buffer{};
        return;
    }
    if (b.map && b.mem) vk_.vkUnmapMemory(device_, b.mem);
    if (b.buf) vk_.vkDestroyBuffer(device_, b.buf, nullptr);
    if (b.mem) vk_.vkFreeMemory(device_, b.mem, nullptr);
    b = Buffer{};
}

// The planner knows its total before uploading: allocate it as a few large
// blocks (first-fit then leaves only small holes) instead of growing 64 MB
// blocks whose tails are wasted when tensor sizes mix (Q6_K 46 MB next to
// FQ3S 23 MB). A block holds no buffer yet, so it is freed with the last one
// bound to it or by release of the whole model.
void VulkanBackend::reserve_device(uint64_t bytes) {
    if (!ensure_init() || bytes == 0) return;
    std::lock_guard<std::mutex> lk(mu_);
    if (broken_) return;
    // Memory type of a device-local storage buffer (probe with a tiny buffer).
    VkBufferCreateInfo bci{};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = 4096;
    bci.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkBuffer probe = VK_NULL_HANDLE;
    if (vk_.vkCreateBuffer(device_, &bci, nullptr, &probe) != VK_SUCCESS) return;
    VkMemoryRequirements req{};
    vk_.vkGetBufferMemoryRequirements(device_, probe, &req);
    vk_.vkDestroyBuffer(device_, probe, nullptr);
    int type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (type < 0) type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0);
    if (type < 0) return;
    constexpr uint64_t kBig = 256ull << 20;
    const uint64_t n = (bytes + kBig - 1) / kBig;
    const uint64_t each = (bytes / n + (4ull << 20) + 0xFFFFF) & ~0xFFFFFull;   // + 4 MB holes slack
    for (uint64_t i = 0; i < n; ++i) {
        WeightBlock wb;
        VkMemoryAllocateInfo mai{};
        mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        mai.allocationSize = std::min<uint64_t>(each, limits_.maxStorageBufferRange);
        mai.memoryTypeIndex = static_cast<uint32_t>(type);
        if (vk_.vkAllocateMemory(device_, &mai, nullptr, &wb.mem) != VK_SUCCESS) return;
        wb.type = static_cast<uint32_t>(type);
        wb.size = mai.allocationSize;
        wb.live = 0;
        bool placed = false;
        for (auto& slot : blocks_) if (!slot.mem) { slot = wb; placed = true; break; }
        if (!placed) blocks_.push_back(wb);
    }
    if (verbose()) std::fprintf(stderr, "[fish/vulkan] reserved %llu x %.0f MB weight blocks; live free now %.0f MB\n",
                                static_cast<unsigned long long>(n), each / 1048576.0, live_free_locked() / 1048576.0);
}

void VulkanBackend::release_reserve() {
    if (!ensure_init()) return;
    std::lock_guard<std::mutex> lk(mu_);
    for (WeightBlock& wb : blocks_) {
        if (wb.mem && wb.live <= 0) {
            vk_.vkFreeMemory(device_, wb.mem, nullptr);
            wb = WeightBlock{};
        }
    }
}

// A device-local buffer bound into a shared block (bump allocation; a block
// is freed when its last buffer goes). Larger than a block: dedicated memory.
bool VulkanBackend::create_weight_buffer(Buffer& b, uint64_t size) {
    VkBufferCreateInfo bci{};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = size;
    bci.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vk_.vkCreateBuffer(device_, &bci, nullptr, &b.buf) != VK_SUCCESS) { b.buf = VK_NULL_HANDLE; return false; }
    VkMemoryRequirements req{};
    vk_.vkGetBufferMemoryRequirements(device_, b.buf, &req);
    int type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (type < 0) type = find_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0);
    if (type < 0) {
        vk_.vkDestroyBuffer(device_, b.buf, nullptr);
        b = Buffer{};
        return false;
    }
    const uint64_t align = std::max<uint64_t>(req.alignment, 256);
    int bi = -1;
    for (size_t i = 0; i < blocks_.size(); ++i) {
        const WeightBlock& wb = blocks_[i];
        if (!wb.mem || wb.type != static_cast<uint32_t>(type)) continue;
        const uint64_t off = (wb.used + align - 1) / align * align;
        if (off + req.size <= wb.size) { bi = static_cast<int>(i); break; }
    }
    if (bi < 0 && req.size > kWeightBlockBytes) {
        vk_.vkDestroyBuffer(device_, b.buf, nullptr);
        b = Buffer{};
        return create_buffer(b, size, bci.usage, false);
    }
    if (bi < 0) {
        WeightBlock wb;
        VkMemoryAllocateInfo mai{};
        mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        mai.allocationSize = kWeightBlockBytes;
        mai.memoryTypeIndex = static_cast<uint32_t>(type);
        if (vk_.vkAllocateMemory(device_, &mai, nullptr, &wb.mem) != VK_SUCCESS) {
            vk_.vkDestroyBuffer(device_, b.buf, nullptr);
            b = Buffer{};
            return false;
        }
        wb.type = static_cast<uint32_t>(type);
        wb.size = kWeightBlockBytes;
        for (size_t i = 0; i < blocks_.size() && bi < 0; ++i) if (!blocks_[i].mem) { blocks_[i] = wb; bi = static_cast<int>(i); }
        if (bi < 0) { blocks_.push_back(wb); bi = static_cast<int>(blocks_.size() - 1); }
    }
    WeightBlock& wb = blocks_[static_cast<size_t>(bi)];
    const uint64_t off = (wb.used + align - 1) / align * align;
    if (vk_.vkBindBufferMemory(device_, b.buf, wb.mem, off) != VK_SUCCESS) {
        vk_.vkDestroyBuffer(device_, b.buf, nullptr);
        b = Buffer{};
        if (wb.live == 0) { vk_.vkFreeMemory(device_, wb.mem, nullptr); wb = WeightBlock{}; }
        return false;
    }
    wb.used = off + req.size;
    ++wb.live;
    b.block = bi;
    b.size = size;
    return true;
}

// Grow the activation/result buffers so a kMaxTok batch of this tensor fits.
// Called at upload time only (never during decode).
bool VulkanBackend::ensure_scratch(uint32_t n_in, uint32_t n_out) {
    const uint32_t want_in = std::max(cap_in_, n_in);
    const uint32_t want_out = std::max(cap_out_, n_out);
    if (want_in == cap_in_ && want_out == cap_out_ && dev_x_.buf) return true;
    // Tokens per submit: up to kMaxTok, but the output scratch is capped at
    // kScratchOutBytes. Sized for 256 tokens it was 256 x vocab x 2 floats:
    // 262 MB of VRAM (and as much pinned host memory) for a 128k vocabulary,
    // which is what a fully resident 8B was short of. Wide outputs only see
    // many tokens in verification / tests; run_locked() chunks those.
    const uint64_t per_tok_out = static_cast<uint64_t>(want_out) * kGroupOut * 4;
    const int tok_cap = static_cast<int>(std::max<uint64_t>(8, std::min<uint64_t>(kMaxTok, kScratchOutBytes / per_tok_out)));
    const uint64_t xb = static_cast<uint64_t>(tok_cap) * want_in * 4;
    const uint64_t yb = static_cast<uint64_t>(tok_cap) * per_tok_out;
    if (bytes_resident_ + xb + yb > budget_) return false;

    Buffer nx, ny, hx, hy;
    const VkBufferUsageFlags st = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    if (!create_buffer(nx, xb, st | VK_BUFFER_USAGE_TRANSFER_DST_BIT, false) ||
        !create_buffer(ny, yb, st | VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false) ||
        !create_host_buffer(hx, xb, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false) ||
        !create_host_buffer(hy, yb, VK_BUFFER_USAGE_TRANSFER_DST_BIT, true)) {
        destroy_buffer(nx); destroy_buffer(ny); destroy_buffer(hx); destroy_buffer(hy);
        return false;
    }
    destroy_buffer(dev_x_); destroy_buffer(dev_y_); destroy_buffer(host_x_); destroy_buffer(host_y_);
    dev_x_ = nx; dev_y_ = ny; host_x_ = hx; host_y_ = hy;
    cap_in_ = want_in;
    cap_out_ = want_out;
    scratch_dev_bytes_ = xb + yb;
    tok_cap_ = tok_cap;
    shadow_.assign(static_cast<size_t>(tok_cap) * want_in, 0.0f);
    last_x_ = nullptr;
    last_count_ = 0;
    for (auto& kv : res_) write_set(kv.second);
    return true;
}

void VulkanBackend::write_set(const Resident& r) {
    VkDescriptorBufferInfo bi[3]{};
    bi[0].buffer = r.w.buf;  bi[0].range = VK_WHOLE_SIZE;
    bi[1].buffer = dev_x_.buf; bi[1].range = VK_WHOLE_SIZE;
    bi[2].buffer = dev_y_.buf; bi[2].range = VK_WHOLE_SIZE;
    VkWriteDescriptorSet wr[3]{};
    for (uint32_t i = 0; i < 3; ++i) {
        wr[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        wr[i].dstSet = r.set;
        wr[i].dstBinding = i;
        wr[i].descriptorCount = 1;
        wr[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        wr[i].pBufferInfo = &bi[i];
    }
    vk_.vkUpdateDescriptorSets(device_, 3, wr, 0, nullptr);
}

bool VulkanBackend::alloc_set(Resident& r) {
    auto try_alloc = [&](VkDescriptorPool p) {
        VkDescriptorSetAllocateInfo ai{};
        ai.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        ai.descriptorPool = p;
        ai.descriptorSetCount = 1;
        ai.pSetLayouts = &set_layout_;
        if (vk_.vkAllocateDescriptorSets(device_, &ai, &r.set) == VK_SUCCESS) { r.pool = p; return true; }
        r.set = VK_NULL_HANDLE;
        return false;
    };
    for (VkDescriptorPool p : pools_) if (try_alloc(p)) return true;
    VkDescriptorPoolSize ps{};
    ps.type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    ps.descriptorCount = 3 * 512;
    VkDescriptorPoolCreateInfo pci{};
    pci.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    pci.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    pci.maxSets = 512;
    pci.poolSizeCount = 1;
    pci.pPoolSizes = &ps;
    VkDescriptorPool p = VK_NULL_HANDLE;
    if (vk_.vkCreateDescriptorPool(device_, &pci, nullptr, &p) != VK_SUCCESS) return false;
    pools_.push_back(p);
    return try_alloc(p);
}

void VulkanBackend::free_resident(Resident& r) {
    if (r.set && r.pool) vk_.vkFreeDescriptorSets(device_, r.pool, 1, &r.set);
    r.set = VK_NULL_HANDLE;
    bytes_resident_ -= std::min(bytes_resident_, r.w.size);
    destroy_buffer(r.w);
}

bool VulkanBackend::submit_and_wait() {
    VkSubmitInfo si{};
    si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cmd_;
    if (vk_.vkQueueSubmit(queue_, 1, &si, fence_) != VK_SUCCESS) { broken_ = true; return false; }
    // Blocking wait in the driver (no spinning here). A GPU that has not
    // finished after 20 s is treated as lost: the backend stops accepting
    // work and the engine falls back to the CPU for every later call.
    for (int i = 0; i < 20; ++i) {
        const VkResult r = vk_.vkWaitForFences(device_, 1, &fence_, VK_TRUE, 1000000000ull);
        if (r == VK_SUCCESS) { vk_.vkResetFences(device_, 1, &fence_); return true; }
        if (r != VK_TIMEOUT) break;
    }
    broken_ = true;
    status_ = "device stopped responding; GPU disabled for this process";
    return false;
}

bool VulkanBackend::ensure_upload_ring() {
    if (cmd_up_[0] && fence_up_[0] && fence_up_[1] && staging_.buf && staging2_.buf) return true;
    if (!staging_.buf && !create_host_buffer(staging_, kStagingBytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false)) return false;
    if (!staging2_.buf && !create_host_buffer(staging2_, kStagingBytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false)) return false;
    if (!cmd_up_[0]) {
        VkCommandBufferAllocateInfo cai{};
        cai.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        cai.commandPool = cmd_pool_;
        cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        cai.commandBufferCount = 2;
        if (vk_.vkAllocateCommandBuffers(device_, &cai, cmd_up_) != VK_SUCCESS) { cmd_up_[0] = cmd_up_[1] = VK_NULL_HANDLE; return false; }
    }
    for (VkFence& f : fence_up_) {
        if (f) continue;
        VkFenceCreateInfo fci{};
        fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        if (vk_.vkCreateFence(device_, &fci, nullptr, &f) != VK_SUCCESS) { f = VK_NULL_HANDLE; return false; }
    }
    return true;
}

bool VulkanBackend::wait_up(int i) {
    if (!up_pending_[i]) return true;
    up_pending_[i] = false;
    for (int k = 0; k < 20; ++k) {
        const VkResult r = vk_.vkWaitForFences(device_, 1, &fence_up_[i], VK_TRUE, 1000000000ull);
        if (r == VK_SUCCESS) { vk_.vkResetFences(device_, 1, &fence_up_[i]); return true; }
        if (r != VK_TIMEOUT) break;
    }
    broken_ = true;
    status_ = "device stopped responding; GPU disabled for this process";
    return false;
}

// Copies `bytes` into dst through the two staging halves: fill(staging, off,
// n) produces chunk k on the CPU while chunk k-1 is still in flight.
bool VulkanBackend::stream_upload(Buffer& dst, uint64_t bytes,
                                  const std::function<bool(uint8_t*, uint64_t, uint64_t)>& fill) {
    if (!ensure_upload_ring()) return false;
    Buffer* st[2] = {&staging_, &staging2_};
    int k = 0;
    bool ok = true;
    for (uint64_t off = 0; off < bytes; off += kStagingBytes, k ^= 1) {
        const uint64_t n = std::min<uint64_t>(kStagingBytes, bytes - off);
        if (!wait_up(k)) return false;
        if (!fill(static_cast<uint8_t*>(st[k]->map), off, n)) { ok = false; break; }
        vk_.vkResetCommandBuffer(cmd_up_[k], 0);
        VkCommandBufferBeginInfo bi{};
        bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_.vkBeginCommandBuffer(cmd_up_[k], &bi);
        VkBufferCopy c{0, off, (n + 3) & ~3ull};
        if (c.size > st[k]->size) c.size = n;
        if (c.dstOffset + c.size > dst.size) c.size = dst.size - c.dstOffset;
        vk_.vkCmdCopyBuffer(cmd_up_[k], st[k]->buf, dst.buf, 1, &c);
        vk_.vkEndCommandBuffer(cmd_up_[k]);
        VkSubmitInfo si{};
        si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        si.commandBufferCount = 1;
        si.pCommandBuffers = &cmd_up_[k];
        if (vk_.vkQueueSubmit(queue_, 1, &si, fence_up_[k]) != VK_SUCCESS) { broken_ = true; return false; }
        up_pending_[k] = true;
    }
    const bool w0 = wait_up(0), w1 = wait_up(1);
    return ok && w0 && w1;
}

std::string VulkanBackend::upload_profile(bool reset) {
    std::lock_guard<std::mutex> lk(mu_);
    char b[256];
    std::snprintf(b, sizeof(b), "%.0f MB: %d from cache %.1f s, %d re-quantised %.1f s, %d file tensors, copy %.1f s",
                  prof_bytes_ / 1048576.0, prof_hits_, prof_read_s_, prof_misses_, prof_requant_s_, prof_file_, prof_copy_s_);
    if (reset) { prof_read_s_ = prof_requant_s_ = prof_copy_s_ = 0; prof_hits_ = prof_misses_ = prof_file_ = 0; prof_bytes_ = 0; }
    return b;
}

// ---------------------------------------------------------------- upload

bool VulkanBackend::upload(const TensorInfo* t) {
    if (!t || !t->data || !ensure_init()) return false;
    // Online re-quantisation (FISH_GPU_REQUANT legacy modes): the device gets
    // a smaller copy; the GGUF and the CPU path are untouched.
    const uint64_t n_in = t->ne.empty() ? 0 : t->ne[0];
    return upload_impl(t, requant_target(t, n_in), nullptr, std::string());
}

// Planner formats (backend.h DeviceFormat) -> device layout. Re-quantised
// formats need whole 256-blocks per row and a source dequant_row() knows.
GgmlType VulkanBackend::format_type(const TensorInfo* t, int fmt, bool* ok) const {
    *ok = false;
    if (!t || t->ne.size() < 2) return GgmlType::F32;
    const uint64_t n_in = t->ne[0];
    GgmlType ty = t->type;
    switch (fmt) {
        case kDevFile: break;
        case kDevQ4K:  ty = GgmlType::Q4_K; break;
        case kDevFQ3:  ty = kFQ3; break;
        case kDevFQ3S: ty = kFQ3S; break;
        default: return GgmlType::F32;
    }
    if (ty != t->type && (n_in % 256 != 0 || !quant_supported(t->type))) return GgmlType::F32;
    if (!kernels_for(ty)) return GgmlType::F32;
    *ok = true;
    return ty;
}

uint64_t VulkanBackend::device_bytes(const TensorInfo* t, int fmt) const {
    if (!self().ensure_init()) return 0;
    bool ok = false;
    const GgmlType ty = format_type(t, fmt, &ok);
    if (!ok) return 0;
    const uint64_t bytes = dev_row_bytes(ty, t->ne[0]) * t->ne[1];
    return ((bytes + 3) & ~3ull) + kPadBytes;
}

bool VulkanBackend::upload_as(const TensorInfo* t, int fmt, const float* importance,
                              const std::string& cache_file) {
    if (!t || !t->data || !ensure_init()) return false;
    bool ok = false;
    const GgmlType ty = format_type(t, fmt, &ok);
    if (!ok) return false;
    return upload_impl(t, ty, importance, cache_file);
}

bool VulkanBackend::upload_impl(const TensorInfo* t, GgmlType dev_type, const float* imp,
                                const std::string& cache) {
    std::lock_guard<std::mutex> lk(mu_);
    if (broken_) return false;

    auto it = res_.find(t);
    if (it != res_.end()) {
        // ~LlamaModel releases its tensors, so a live entry is this tensor.
        // Still refuse to trust it if the descriptor visibly changed.
        if (it->second.data == t->data && it->second.nbytes == t->nbytes && it->second.src_type == t->type &&
            it->second.type == dev_type) return true;
        if (span_ready_) span_release_locked();
        free_resident(it->second);
        res_.erase(it);
    }
    if (!kernels_for(dev_type)) return false;
    // 2-D projections only; rows must be whole 32-element chunks for the tiled kernel.
    if (t->ne.size() < 2) return false;
    for (size_t d = 2; d < t->ne.size(); ++d) if (t->ne[d] != 1) return false;
    const uint64_t n_in = t->ne[0], n_out = t->ne[1];
    if (n_in % 32 != 0 || n_in > (1u << 20) || n_out > (1u << 24)) return false;
    const uint64_t src_row_bytes = ggml_row_size(t->type, n_in);
    if (src_row_bytes == 0 || src_row_bytes * n_out != t->nbytes) return false;
    if (dev_type != t->type && n_in % 256 != 0) return false;

    const uint64_t row_bytes = dev_row_bytes(dev_type, n_in);
    const uint64_t dev_bytes = row_bytes * n_out;
    const uint64_t size = ((dev_bytes + 3) & ~3ull) + kPadBytes;
    if (size > limits_.maxStorageBufferRange) return false;

    if (!ensure_scratch(static_cast<uint32_t>(n_in), static_cast<uint32_t>(n_out))) return false;
    // Own ceiling plus what the driver says is free right now (other programs
    // may have taken VRAM since load started).
    if (bytes_resident_ + scratch_dev_bytes_ + size > budget_) return false;
    // Blocks reserved up front by the planner (reserve_device) are already
    // ours: a tensor that lands in one takes no new VRAM, so only a fresh
    // allocation is checked against the live budget. WDDM lowers the
    // reported budget as this process allocates, so after reserving 2.3 GB
    // the "live free" figure read 0 MB although every tensor still fitted its
    // block (8B, 2026-11-03: the first upload after the reservation failed).
    bool in_block = false;
    for (const WeightBlock& wb : blocks_) {
        if (!wb.mem) continue;
        const uint64_t off = (wb.used + 255) / 256 * 256;
        if (off + size + 4096 <= wb.size) { in_block = true; break; }
    }
    const uint64_t live = live_free_locked();
    if (has_budget_ext_ && !in_block && size + headroom() > live) {
        if (verbose()) std::fprintf(stderr, "[fish/vulkan] upload of %s (%.1f MB) refused: live free %.0f MB < size + headroom %.0f MB (resident %.0f MB, scratch %.0f MB)\n",
                                    t->name.c_str(), size / 1048576.0, live / 1048576.0, headroom() / 1048576.0,
                                    bytes_resident_ / 1048576.0, scratch_dev_bytes_ / 1048576.0);
        return false;
    }

    Resident r;
    if (!create_weight_buffer(r.w, size)) return false;
    if (!staging_.buf && !create_host_buffer(staging_, kStagingBytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false)) {
        destroy_buffer(r.w);
        return false;
    }
    using clk = std::chrono::steady_clock;
    const auto tp0 = clk::now();
    bool streamed = false;
    if (dev_type != t->type) {
        DevCacheHeader h{};
        std::memcpy(h.magic, "FISHDEV1", 8);
        h.dev_type = static_cast<uint32_t>(dev_type);
        h.src_type = static_cast<uint32_t>(t->type);
        h.n_in = static_cast<uint32_t>(n_in);
        h.n_out = static_cast<uint32_t>(n_out);
        h.row_bytes = static_cast<uint32_t>(row_bytes);
        h.payload = dev_bytes;
        // Warm path: the cached device bytes go from the file straight into
        // the staging ring (no host copy of the tensor, no re-quantisation,
        // the GGUF pages are never touched).
        if (FILE* f = dev_cache_open(cache, h)) {
            const bool ok = stream_upload(r.w, dev_bytes, [&](uint8_t* dst, uint64_t, uint64_t n) {
                return std::fread(dst, 1, static_cast<size_t>(n), f) == n;
            });
            std::fclose(f);
            if (!ok && broken_) { destroy_buffer(r.w); return false; }
            streamed = ok;   // a short / corrupt file falls through and is rewritten
            if (ok) { ++prof_hits_; prof_read_s_ += std::chrono::duration<double>(clk::now() - tp0).count(); }
        }
        if (!streamed) {
            const auto tq = clk::now();
            if (!requantize(t, dev_type, row_bytes, imp)) { destroy_buffer(r.w); return false; }
            dev_cache_write(cache, h, requant_buf_.data());
            prof_requant_s_ += std::chrono::duration<double>(clk::now() - tq).count();
            ++prof_misses_;
            const auto tc = clk::now();
            const uint8_t* src = requant_buf_.data();
            if (!stream_upload(r.w, dev_bytes, [&](uint8_t* dst, uint64_t off, uint64_t n) {
                    std::memcpy(dst, src + off, static_cast<size_t>(n)); return true; })) {
                destroy_buffer(r.w);
                return false;
            }
            prof_copy_s_ += std::chrono::duration<double>(clk::now() - tc).count();
        }
    } else {
        // File bytes: memcpy from the mapping (the page faults read the GGUF)
        // overlaps with the DMA of the previous chunk.
        const uint8_t* src = t->data;
        if (!stream_upload(r.w, dev_bytes, [&](uint8_t* dst, uint64_t off, uint64_t n) {
                std::memcpy(dst, src + off, static_cast<size_t>(n)); return true; })) {
            destroy_buffer(r.w);
            return false;
        }
        ++prof_file_;
        prof_copy_s_ += std::chrono::duration<double>(clk::now() - tp0).count();
    }
    prof_bytes_ += dev_bytes;
    if (!requant_buf_.empty() && requant_buf_.capacity() > (64u << 20)) {
        std::vector<uint8_t>().swap(requant_buf_);   // do not keep a large host buffer around
    }
    if (!alloc_set(r)) { destroy_buffer(r.w); return false; }
    r.type = dev_type;
    r.src_type = t->type;
    r.n_in = static_cast<uint32_t>(n_in);
    r.n_out = static_cast<uint32_t>(n_out);
    r.row_bytes = static_cast<uint32_t>(row_bytes);
    r.data = t->data;
    r.nbytes = t->nbytes;
    write_set(r);
    bytes_resident_ += r.w.size;
    if (env_is("FISH_GPU_VERBOSE", "2")) {
        int nb = 0; for (const auto& wb : blocks_) nb += wb.mem != VK_NULL_HANDLE;
        std::fprintf(stderr, "[fish/vulkan] upload %s %s %.1f MB: resident %.0f MB, %d blocks, live free %.0f MB\n", t->name.c_str(), dev_type_name(dev_type), r.w.size / 1048576.0, bytes_resident_ / 1048576.0, nb, live_free_locked() / 1048576.0);
    }
    res_.emplace(t, r);
    return true;
}

// Rows are dequantised with the engine's dequant_row() and re-quantised in
// parallel on the engine's thread pool (load time only).
bool VulkanBackend::requantize(const TensorInfo* t, GgmlType dev_type, uint64_t row_bytes, const float* imp) {
    const int n_in = static_cast<int>(t->ne[0]);
    const int n_out = static_cast<int>(t->ne[1]);
    const size_t src_rb = t->row_bytes();
    try {
        requant_buf_.resize(static_cast<size_t>(row_bytes) * n_out);
    } catch (const std::bad_alloc&) {
        return false;
    }
    bool ok = true;
    uint8_t* out = requant_buf_.data();
    ThreadPool::instance().parallel_range(n_out, [&](int r0, int r1) {
        thread_local std::vector<float> row;
        if (static_cast<int>(row.size()) < n_in) row.resize(static_cast<size_t>(n_in));
        for (int i = r0; i < r1; ++i) {
            if (!dequant_row(t->type, t->data + static_cast<size_t>(i) * src_rb, row.data(), static_cast<uint64_t>(n_in))) {
                ok = false;
                return;
            }
            uint8_t* dst = out + static_cast<size_t>(i) * row_bytes;
            if (dev_type == kFQ3S) quantize_row_fq3s(row.data(), n_in, dst, imp);
            else if (imp && dev_type == kFQ3) quantize_row_fq3_w(row.data(), n_in, dst, imp);
            else if (imp) quantize_row_q4k_w(row.data(), n_in, dst, imp);
            else if (dev_type == kFQ3) quantize_row_fq3(row.data(), n_in, dst);
            else quantize_row_q4k(row.data(), n_in, dst);
        }
    });
    return ok;
}

std::string VulkanBackend::describe() const {
    std::lock_guard<std::mutex> lk(mu_);
    struct Agg { GgmlType src, dev; int n; uint64_t bytes, src_bytes; };
    std::vector<Agg> agg;
    for (const auto& kv : res_) {
        const Resident& r = kv.second;
        auto it = std::find_if(agg.begin(), agg.end(), [&](const Agg& a) { return a.src == r.src_type && a.dev == r.type; });
        if (it == agg.end()) agg.push_back({r.src_type, r.type, 1, r.w.size, r.nbytes});
        else { ++it->n; it->bytes += r.w.size; it->src_bytes += r.nbytes; }
    }
    auto tname = [](GgmlType ty) { return dev_type_name(ty); };
    char buf[256];
    std::snprintf(buf, sizeof(buf), "vulkan %s: %.0f MB weights", device_name_.c_str(), bytes_resident_ / 1048576.0);
    std::string s = buf;
    for (const auto& a : agg) {
        if (a.src == a.dev) std::snprintf(buf, sizeof(buf), ", %d x %s %.0f MB", a.n, tname(a.dev), a.bytes / 1048576.0);
        else std::snprintf(buf, sizeof(buf), ", %d x %s->%s %.0f MB (from %.0f)", a.n, tname(a.src), tname(a.dev),
                           a.bytes / 1048576.0, a.src_bytes / 1048576.0);
        s += buf;
    }
    if (span_ready_) {
        std::snprintf(buf, sizeof(buf), "; span %zu layers, KV %.0f MB %s", span_layers_.size(),
                      span_kv_.size / 1048576.0, sp_.kvq8 ? "int8" : "f16");
        s += buf;
    }
    return s;
}

bool VulkanBackend::resident(const TensorInfo* t) const {
    std::lock_guard<std::mutex> lk(mu_);
    if (state_ != State::Ready || broken_ || res_.empty()) return false;
    return res_.find(t) != res_.end();
}

void VulkanBackend::release(const TensorInfo* t) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = res_.find(t);
    if (it == res_.end()) return;
    // The span holds pointers to resident tensors: any release ends it.
    if (span_ready_) span_release_locked();
    free_resident(it->second);
    res_.erase(it);
}

// ---------------------------------------------------------------- matmul

bool VulkanBackend::run_locked(const Resident* const* rs, int n, const float* x, int n_tok,
                               float* const* ys) {
    const uint32_t n_in = rs[0]->n_in;
    uint64_t total_out = 0;
    for (int i = 0; i < n; ++i) total_out += rs[i]->n_out;
    if (total_out > static_cast<uint64_t>(cap_out_) * kGroupOut) return false;   // caller splits

    // Load is over once decoding starts: give the staging memory back.
    if (staging_.buf) destroy_buffer(staging_);
    if (staging2_.buf) destroy_buffer(staging2_);
    if (requant_buf_.capacity()) std::vector<uint8_t>().swap(requant_buf_);

    for (int t0 = 0; t0 < n_tok; t0 += tok_cap_) {
        const int nt = std::min(tok_cap_, n_tok - t0);
        const float* xs = x + static_cast<size_t>(t0) * n_in;
        const size_t count = static_cast<size_t>(nt) * n_in;
        const size_t xbytes = count * sizeof(float);
        // xb_ is one buffer reused by every layer, so pointer equality alone
        // proves nothing: compare against the CPU shadow of what dev_x_ holds.
        const bool same = xs == last_x_ && count == last_count_ &&
                          std::memcmp(shadow_.data(), xs, xbytes) == 0;
        if (!same) {
            std::memcpy(host_x_.map, xs, xbytes);
            std::memcpy(shadow_.data(), xs, xbytes);
        }

        vk_.vkResetCommandBuffer(cmd_, 0);
        VkCommandBufferBeginInfo bi{};
        bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_.vkBeginCommandBuffer(cmd_, &bi);
        if (!same) {
            VkBufferCopy c{0, 0, xbytes};
            vk_.vkCmdCopyBuffer(cmd_, host_x_.buf, dev_x_.buf, 1, &c);
            VkMemoryBarrier mb{};
            mb.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
            mb.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            mb.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            vk_.vkCmdPipelineBarrier(cmd_, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                     0, 1, &mb, 0, nullptr, 0, nullptr);
        }
        const bool use_matvec = nt <= matvec_max_tok_;
        if (profile_) {
            vk_.vkCmdResetQueryPool(cmd_, qpool_, 0, 2);
            vk_.vkCmdWriteTimestamp(cmd_, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, qpool_, 0);
        }
        // Outputs are packed back to back in dev_y_: tensor i at y_off[i].
        uint32_t y_off = 0;
        std::vector<uint32_t>& offs = group_offs_;
        offs.resize(static_cast<size_t>(n));
        for (int i = 0; i < n; ++i) {
            const Resident& r = *rs[i];
            const TypeKernels* k = kernels_for(r.type);
            vk_.vkCmdBindPipeline(cmd_, VK_PIPELINE_BIND_POINT_COMPUTE,
                                  use_matvec ? k->matvec : (k->coop ? k->coop : k->tiled));
            vk_.vkCmdBindDescriptorSets(cmd_, VK_PIPELINE_BIND_POINT_COMPUTE, pipe_layout_, 0, 1, &r.set, 0, nullptr);
            const PushConsts pc{r.n_in, r.n_out, static_cast<uint32_t>(nt), r.row_bytes, y_off, 0};
            vk_.vkCmdPushConstants(cmd_, pipe_layout_, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(pc), &pc);
            // matvec.comp: 8 lane groups x 4 rows = 32 rows per workgroup.
            if (use_matvec) vk_.vkCmdDispatch(cmd_, (r.n_out + 31) / 32, static_cast<uint32_t>(nt), 1);
            else vk_.vkCmdDispatch(cmd_, (r.n_out + 63) / 64, (static_cast<uint32_t>(nt) + 63) / 64, 1);
            offs[static_cast<size_t>(i)] = y_off;
            y_off += static_cast<uint32_t>(nt) * r.n_out;
        }
        if (profile_) vk_.vkCmdWriteTimestamp(cmd_, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, qpool_, 1);
        {
            VkMemoryBarrier mb{};
            mb.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
            mb.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            mb.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            vk_.vkCmdPipelineBarrier(cmd_, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                                     0, 1, &mb, 0, nullptr, 0, nullptr);
        }
        VkBufferCopy c{0, 0, static_cast<VkDeviceSize>(y_off) * sizeof(float)};
        vk_.vkCmdCopyBuffer(cmd_, dev_y_.buf, host_y_.buf, 1, &c);
        {
            VkMemoryBarrier mb{};
            mb.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
            mb.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            mb.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
            vk_.vkCmdPipelineBarrier(cmd_, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
                                     0, 1, &mb, 0, nullptr, 0, nullptr);
        }
        vk_.vkEndCommandBuffer(cmd_);
        const auto w0 = std::chrono::steady_clock::now();
        if (!submit_and_wait()) { last_x_ = nullptr; last_count_ = 0; return false; }
        if (profile_) {
            uint64_t ts[2] = {0, 0};
            if (vk_.vkGetQueryPoolResults(device_, qpool_, 0, 2, sizeof(ts), ts, sizeof(uint64_t),
                                          VK_QUERY_RESULT_64_BIT) == VK_SUCCESS && ts[1] >= ts[0])
                prof_kernel_ms_ += (ts[1] - ts[0]) * static_cast<double>(limits_.timestampPeriod) * 1e-6;
            prof_wall_ms_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - w0).count();
            ++prof_submits_;
        }
        last_x_ = xs;
        last_count_ = count;
        if (!host_y_.coherent) {
            VkMappedMemoryRange mr{};
            mr.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;
            mr.memory = host_y_.mem;
            mr.size = VK_WHOLE_SIZE;
            vk_.vkInvalidateMappedMemoryRanges(device_, 1, &mr);
        }
        const float* hy = static_cast<const float*>(host_y_.map);
        for (int i = 0; i < n; ++i) {
            const size_t no = rs[i]->n_out;
            std::memcpy(ys[i] + static_cast<size_t>(t0) * no, hy + offs[static_cast<size_t>(i)],
                        static_cast<size_t>(nt) * no * sizeof(float));
        }
    }
    return true;
}

bool VulkanBackend::matmul(const TensorInfo* t, const float* x, const ActBatch* /*acts*/,
                           int n_tok, int n_in, int n_out, float* y) {
    return matmul_group(&t, 1, x, nullptr, n_tok, n_in, &n_out, &y);
}

bool VulkanBackend::matmul_group(const TensorInfo* const* ts, int n, const float* x,
                                 const ActBatch* /*acts*/, int n_tok, int n_in,
                                 const int* n_outs, float* const* ys) {
    std::lock_guard<std::mutex> lk(mu_);
    if (state_ != State::Ready || broken_ || n_tok <= 0 || n <= 0 || n > kMaxGroup) return false;
    const Resident* rs[kMaxGroup];
    for (int i = 0; i < n; ++i) {
        auto it = res_.find(ts[i]);
        if (it == res_.end()) return false;
        const Resident& r = it->second;
        if (static_cast<uint32_t>(n_in) != r.n_in || static_cast<uint32_t>(n_outs[i]) != r.n_out) return false;
        if (!kernels_for(r.type)) return false;
        rs[i] = &r;
    }
    // Split only if the outputs do not fit dev_y_ together (never for q/k/v
    // or gate/up, whose sum is <= 2 x the widest tensor).
    int i0 = 0;
    while (i0 < n) {
        int i1 = i0;
        uint64_t sum = 0;
        while (i1 < n && sum + rs[i1]->n_out <= static_cast<uint64_t>(cap_out_) * kGroupOut) sum += rs[i1++]->n_out;
        if (i1 == i0) return false;
        if (!run_locked(rs + i0, i1 - i0, x, n_tok, ys + i0)) return false;
        i0 = i1;
    }
    return true;
}

// ---------------------------------------------------------------- layer span
//
// A contiguous range of dense layers runs entirely on the device in ONE
// command buffer per span_forward(): x is uploaded once, every layer's norms,
// projections (the same matmul kernels as above, addressing the arena via
// x_off/y_off), RoPE, KV append, attention, activation and residual adds are
// chained with compute->compute barriers, and x is read back once. Prefill
// walks the tokens in chunks of kSpanChunk (each chunk sees the KV written by
// the previous ones, so causality holds) inside that same command buffer.
//
// Device memory: arena (x for kMaxTok tokens + per-chunk work buffers),
// params (norm weights, biases, RoPE cos/sin), KV cache (f16, n_ctx positions
// per span layer). All counted against the same budget and live headroom as
// the weights; if they do not fit, span_prepare() declines and the model keeps
// per-matmul routing.

bool VulkanBackend::alloc_raw_set(VkDescriptorSet* set, VkDescriptorPool* pool) {
    Resident tmp;
    if (!alloc_set(tmp)) return false;
    *set = tmp.set;
    *pool = tmp.pool;
    return true;
}

void VulkanBackend::write_set3(VkDescriptorSet set, VkBuffer b0, VkBuffer b1, VkBuffer b2) {
    VkDescriptorBufferInfo bi[3]{};
    const VkBuffer bs[3] = {b0, b1, b2};
    VkWriteDescriptorSet wr[3]{};
    for (uint32_t i = 0; i < 3; ++i) {
        bi[i].buffer = bs[i];
        bi[i].range = VK_WHOLE_SIZE;
        wr[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        wr[i].dstSet = set;
        wr[i].dstBinding = i;
        wr[i].descriptorCount = 1;
        wr[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        wr[i].pBufferInfo = &bi[i];
    }
    vk_.vkUpdateDescriptorSets(device_, 3, wr, 0, nullptr);
}

// Synchronous host -> device copy through a temporary staging buffer (load time only).
bool VulkanBackend::upload_bytes(Buffer& dst, const void* src, uint64_t bytes) {
    Buffer st;
    const uint64_t chunk = std::min<uint64_t>(bytes, kStagingBytes);
    if (!create_host_buffer(st, chunk, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false)) return false;
    bool ok = true;
    for (uint64_t off = 0; off < bytes && ok; off += chunk) {
        const uint64_t n = std::min<uint64_t>(chunk, bytes - off);
        std::memcpy(st.map, static_cast<const uint8_t*>(src) + off, static_cast<size_t>(n));
        vk_.vkResetCommandBuffer(cmd_, 0);
        VkCommandBufferBeginInfo bi{};
        bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_.vkBeginCommandBuffer(cmd_, &bi);
        VkBufferCopy c{0, off, n};
        vk_.vkCmdCopyBuffer(cmd_, st.buf, dst.buf, 1, &c);
        vk_.vkEndCommandBuffer(cmd_);
        ok = submit_and_wait();
    }
    destroy_buffer(st);
    return ok;
}

void VulkanBackend::span_release_locked() {
    if (!device_) return;
    destroy_buffer(span_hx_);
    destroy_buffer(span_hy_);
    if (prof_stages_ && stage_calls_ > 0) {
        double total = 0;
        for (const auto& e : stage_ms_) total += e.second;
        std::fprintf(stderr, "[fish/vulkan] span stage profile over %ld span_forward calls (GPU ms per call):\n", stage_calls_);
        for (const auto& e : stage_ms_)
            std::fprintf(stderr, "    %-14s %8.3f ms  %5.1f%%\n", e.first, e.second / stage_calls_, 100.0 * e.second / std::max(total, 1e-9));
        stage_ms_.clear();
        stage_calls_ = 0;
    }
    auto free_set = [&](VkDescriptorSet& s, VkDescriptorPool& p) {
        if (s && p) vk_.vkFreeDescriptorSets(device_, p, 1, &s);
        s = VK_NULL_HANDLE;
        p = VK_NULL_HANDLE;
    };
    for (auto& L : span_layers_)
        for (SpanTensor* st : {&L.wq, &L.wk, &L.wv, &L.wqkv, &L.wo, &L.wg, &L.wu, &L.wd})
            free_set(st->set, st->pool);
    span_layers_.clear();
    free_set(span_set_, span_pool_);
    destroy_buffer(span_arena_);
    destroy_buffer(span_params_);
    destroy_buffer(span_kv_);
    span_dev_bytes_ = 0;
    span_ready_ = false;
    span_kv_len_ = 0;
}

void VulkanBackend::span_release() {
    std::lock_guard<std::mutex> lk(mu_);
    span_release_locked();
}

void VulkanBackend::span_kv_truncate(int n_past) {
    std::lock_guard<std::mutex> lk(mu_);
    // Positions < n_past that the device already holds stay valid; nothing
    // beyond what it holds can be claimed (span_forward refuses gaps).
    span_kv_len_ = std::max(0, std::min(span_kv_len_, n_past));
}

bool VulkanBackend::span_prepare(const SpanParams& p) {
    if (!ensure_init()) return false;
    std::lock_guard<std::mutex> lk(mu_);
    span_release_locked();
    if (broken_ || env_is("FISH_GPU_SPAN", "0")) return false;
    // Uploads are over: the 32 MB staging buffer and the re-quantisation
    // buffer would otherwise still hold VRAM / RAM while the span's KV cache
    // is sized against what is live-free.
    if (staging_.buf) destroy_buffer(staging_);
    if (staging2_.buf) destroy_buffer(staging2_);
    if (requant_buf_.capacity()) std::vector<uint8_t>().swap(requant_buf_);
    if (!sp_rms_ || !sp_add_ || !sp_rope_ || !sp_kv_ || !sp_attn_ || !sp_act_) return false;
    const int nl = p.layer_end - p.layer_begin;
    if (nl <= 0 || !p.layers || p.n_embd <= 0 || p.n_head <= 0 || p.n_head_kv <= 0 ||
        p.n_head % p.n_head_kv != 0 || p.head_dim <= 0 || p.head_dim > 256 || (p.head_dim & 1) ||
        p.n_ff <= 0 || p.n_ctx <= 0 || (p.rot_dim & 1) || p.rot_dim > p.head_dim)
        return false;
    if (p.rot_dim > 0 && (!p.rope_cos || !p.rope_sin)) return false;

    auto& s = sp_;
    s.E = static_cast<uint32_t>(p.n_embd);
    s.H = static_cast<uint32_t>(p.n_head);
    s.HKV = static_cast<uint32_t>(p.n_head_kv);
    s.D = static_cast<uint32_t>(p.head_dim);
    s.F = static_cast<uint32_t>(p.n_ff);
    s.Q = s.H * s.D;
    s.KV = s.HKV * s.D;
    s.n_ctx = static_cast<uint32_t>(p.n_ctx);
    s.half = static_cast<uint32_t>(p.rot_dim / 2);
    s.eps = p.eps;
    s.scale = p.attn_scale > 0.0f ? p.attn_scale : 1.0f / std::sqrt(static_cast<float>(p.head_dim));
    s.softcap = p.attn_softcap;
    s.window = static_cast<uint32_t>(std::max(0, p.window));
    s.gelu = p.gelu ? 1u : 0u;
    s.neox = p.rope_neox ? 1u : 0u;
    if (s.E % 4 || s.Q % 4 || s.F % 4) return false;   // vec4 activation reads
    s.kvq8 = p.kv_q8 && sp_kv8_ && sp_attn8_ && s.KV % 32 == 0 && s.D % 32 == 0;

    // ---- params: norm vectors, biases, RoPE table
    std::vector<float> prm;
    auto put = [&](const float* v, size_t n) -> uint32_t {
        if (!v) return kNone;
        while (prm.size() % 4) prm.push_back(0.0f);
        const uint32_t off = static_cast<uint32_t>(prm.size());
        prm.insert(prm.end(), v, v + n);
        return off;
    };
    auto find = [&](const TensorInfo* t, uint32_t n_in, uint32_t n_out, SpanTensor& out) -> bool {
        if (!t) return true;
        auto it = res_.find(t);
        if (it == res_.end()) return false;
        if (it->second.n_in != n_in || it->second.n_out != n_out || !kernels_for(it->second.type)) return false;
        out.r = &it->second;
        return true;
    };
    span_layers_.assign(static_cast<size_t>(nl), SpanLayerDev{});
    for (int i = 0; i < nl; ++i) {
        const SpanLayer& L = p.layers[i];
        SpanLayerDev& d = span_layers_[static_cast<size_t>(i)];
        const bool fused_qkv = L.wqkv != nullptr;
        if (!L.wo || !L.wu || !L.wd || !L.attn_norm || !L.ffn_norm) { span_layers_.clear(); return false; }
        if (!fused_qkv && (!L.wq || !L.wk || !L.wv)) { span_layers_.clear(); return false; }
        const bool ok =
            find(L.wq, s.E, s.Q, d.wq) && find(L.wk, s.E, s.KV, d.wk) && find(L.wv, s.E, s.KV, d.wv) &&
            find(L.wqkv, s.E, s.Q + 2 * s.KV, d.wqkv) && find(L.wo, s.Q, s.E, d.wo) &&
            find(L.wg, s.E, s.F, d.wg) && find(L.wu, s.E, L.wg ? s.F : 2 * s.F, d.wu) &&
            find(L.wd, s.F, s.E, d.wd);
        if (!ok) { span_layers_.clear(); return false; }
        d.attn_norm = put(L.attn_norm, s.E);
        d.ffn_norm = put(L.ffn_norm, s.E);
        d.post_attn = put(L.post_attn_norm, s.E);
        d.post_ffn = put(L.post_ffn_norm, s.E);
        d.q_norm = put(L.q_norm, s.D);
        d.k_norm = put(L.k_norm, s.D);
        d.bq = put(L.bias_q, s.Q);
        d.bk = put(L.bias_k, s.KV);
        d.bv = put(L.bias_v, s.KV);
        d.bo = put(L.bias_o, s.E);
        d.bqkv = put(L.bias_qkv, s.Q + 2 * s.KV);
        d.window = L.sliding_window && s.window > 0;
    }
    const size_t rope_n = static_cast<size_t>(s.n_ctx) * s.half;
    s.cos_off = s.half ? put(p.rope_cos, rope_n) : 0;
    s.sin_off = s.half ? put(p.rope_sin, rope_n) : 0;
    if (prm.empty()) prm.push_back(0.0f);

    // ---- arena layout (floats, 64-aligned regions)
    uint64_t at = 0;
    auto region = [&](uint64_t n) { const uint64_t o = at; at += (n + 63) & ~63ull; return static_cast<uint32_t>(o); };
    const uint64_t C = kSpanChunk;
    s.X = region(static_cast<uint64_t>(kMaxTok) * s.E);
    s.XB = region(C * s.E);
    s.QKV = region(C * (s.Q + 2ull * s.KV));
    s.ATT = region(C * s.Q);
    s.PROJ = region(C * s.E);
    s.GU = region(C * 2ull * s.F);
    s.ACT = region(C * s.F);
    s.FFN = region(C * s.E);
    const uint64_t arena_bytes = at * 4;
    const uint64_t params_bytes = prm.size() * 4;
    // KV per layer: f16 K and V = n_ctx x kv_dim / 2 words each; int8: codes
    // n_ctx x kv_dim / 4 words each + one f32 scale per 32 values each.
    const uint64_t kv_words = s.kvq8
        ? static_cast<uint64_t>(nl) * s.n_ctx * (s.KV / 2 + s.KV / 16)
        : static_cast<uint64_t>(nl) * s.n_ctx * s.KV;
    const uint64_t kv_bytes = kv_words * 4;
    if (at >= (1ull << 32) || kv_words >= (1ull << 32)) { span_layers_.clear(); return false; }
    const uint64_t need = arena_bytes + params_bytes + kv_bytes;
    const uint64_t max_range = limits_.maxStorageBufferRange;
    if (arena_bytes > max_range || kv_bytes > max_range || params_bytes > max_range ||
        bytes_resident_ + scratch_dev_bytes_ + need > budget_) {
        if (verbose()) std::fprintf(stderr, "[fish/vulkan] span declined: needs %.0f MB (KV %.0f MB), budget left %.0f MB\n",
                                    need / 1048576.0, kv_bytes / 1048576.0,
                                    (budget_ > bytes_resident_ + scratch_dev_bytes_ ? budget_ - bytes_resident_ - scratch_dev_bytes_ : 0) / 1048576.0);
        span_layers_.clear();
        return false;
    }
    if (has_budget_ext_ && need + headroom() > live_free_locked()) {
        if (verbose()) std::fprintf(stderr, "[fish/vulkan] span declined: needs %.0f MB + %.0f MB headroom, live free %.0f MB\n",
                                    need / 1048576.0, headroom() / 1048576.0, live_free_locked() / 1048576.0);
        span_layers_.clear();
        return false;
    }

    const VkBufferUsageFlags st = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    const uint64_t hx_bytes = static_cast<uint64_t>(kMaxTok) * s.E * sizeof(float);
    bool ok = create_host_buffer(span_hx_, hx_bytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false) &&
              create_host_buffer(span_hy_, hx_bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT, true) &&
              create_buffer(span_arena_, arena_bytes, st | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT, false) &&
              create_buffer(span_params_, params_bytes, st | VK_BUFFER_USAGE_TRANSFER_DST_BIT, false) &&
              create_buffer(span_kv_, kv_bytes, st, false) &&
              upload_bytes(span_params_, prm.data(), params_bytes) &&
              alloc_raw_set(&span_set_, &span_pool_);
    if (ok) {
        write_set3(span_set_, span_arena_.buf, span_params_.buf, span_kv_.buf);
        // The arena is bound as both the matmul input (readonly) and output
        // (writeonly); regions never overlap within one dispatch. Measured:
        // binding the output through a separate aliasing VkBuffer is no faster.
        const VkBuffer out_buf = span_arena_.buf;
        for (auto& L : span_layers_) {
            for (SpanTensor* t : {&L.wq, &L.wk, &L.wv, &L.wqkv, &L.wo, &L.wg, &L.wu, &L.wd}) {
                if (!t->r) continue;
                if (!alloc_raw_set(&t->set, &t->pool)) { ok = false; break; }
                write_set3(t->set, t->r->w.buf, span_arena_.buf, out_buf);
            }
            if (!ok) break;
        }
    }
    if (!ok) { span_release_locked(); return false; }
    span_dev_bytes_ = need;
    span_kv_len_ = 0;
    span_ready_ = true;
    if (verbose())
        std::fprintf(stderr, "[fish/vulkan] span ready: layers %d..%d, arena %.1f MB, KV %.1f MB (%s, %u positions)\n",
                     p.layer_begin, p.layer_end - 1, arena_bytes / 1048576.0, kv_bytes / 1048576.0,
                     s.kvq8 ? "int8" : "f16", s.n_ctx);
    return true;
}

bool VulkanBackend::span_forward(float* x, int n_tok, int pos0) {
    std::lock_guard<std::mutex> lk(mu_);
    if (!span_ready_ || broken_ || !x || n_tok <= 0 || n_tok > kMaxTok || pos0 < 0) return false;
    const auto& s = sp_;
    if (static_cast<uint32_t>(pos0 + n_tok) > s.n_ctx) return false;
    if (pos0 > span_kv_len_) return false;   // device KV has a gap: never guess
    if (staging_.buf) destroy_buffer(staging_);
    if (staging2_.buf) destroy_buffer(staging2_);

    const size_t xbytes = static_cast<size_t>(n_tok) * s.E * sizeof(float);
    if (xbytes > span_hx_.size || xbytes > span_hy_.size) return false;
    std::memcpy(span_hx_.map, x, xbytes);

    VkCommandBuffer cb = cmd_;
    vk_.vkResetCommandBuffer(cb, 0);
    VkCommandBufferBeginInfo bi{};
    bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vk_.vkBeginCommandBuffer(cb, &bi);

    auto barrier = [&](VkPipelineStageFlags src, VkAccessFlags sa, VkPipelineStageFlags dst, VkAccessFlags da) {
        VkMemoryBarrier mb{};
        mb.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        mb.srcAccessMask = sa;
        mb.dstAccessMask = da;
        vk_.vkCmdPipelineBarrier(cb, src, dst, 0, 1, &mb, 0, nullptr, 0, nullptr);
    };
    const VkPipelineStageFlags CS = VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    // FISH_GPU_PROFILE=2: a timestamp before every barrier, labelled by the
    // kind of the last dispatch, summed per kind (printed at span_release).
    const char* last_kind = "misc";
    uint32_t qi = 0;
    stage_labels_.clear();
    if (prof_stages_) {
        vk_.vkCmdResetQueryPool(cb, stage_pool_, 0, kStageQueries);
        vk_.vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, stage_pool_, qi++);
        stage_labels_.push_back("start");
    }
    auto bar = [&] {
        if (prof_stages_ && qi < kStageQueries) {
            vk_.vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, stage_pool_, qi++);
            stage_labels_.push_back(last_kind);
        }
        barrier(CS, VK_ACCESS_SHADER_WRITE_BIT, CS, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
    };
    auto kind_of = [&](VkPipeline pl) -> const char* {
        if (pl == sp_rms_) return "rmsnorm";
        if (pl == sp_add_) return "add";
        if (pl == sp_rope_) return "rope";
        if (pl == sp_kv_ || pl == sp_kv8_) return "kv_store";
        if (pl == sp_attn_ || pl == sp_attn8_) return "attention";
        if (pl == sp_act_) return "act_mul";
        return "misc";
    };
    auto aux = [&](VkPipeline pl, const void* pc, uint32_t size, uint32_t gx, uint32_t gy) {
        last_kind = kind_of(pl);
        vk_.vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pl);
        vk_.vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipe_layout_, 0, 1, &span_set_, 0, nullptr);
        vk_.vkCmdPushConstants(cb, pipe_layout_, VK_SHADER_STAGE_COMPUTE_BIT, 0, size, pc);
        vk_.vkCmdDispatch(cb, gx, gy, 1);
    };
    auto mm = [&](const SpanTensor& t, uint32_t nt, uint32_t x_off, uint32_t y_off) {
        const Resident& r = *t.r;
        const TypeKernels* k = kernels_for(r.type);
        const bool mv = static_cast<int>(nt) <= matvec_max_tok_;
        last_kind = mv ? "matvec" : (k->coop ? "matmul_coop" : "matmul_tiled");
        vk_.vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mv ? k->matvec : (k->coop ? k->coop : k->tiled));
        vk_.vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipe_layout_, 0, 1, &t.set, 0, nullptr);
        const PushConsts pc{r.n_in, r.n_out, nt, r.row_bytes, y_off, x_off};
        vk_.vkCmdPushConstants(cb, pipe_layout_, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(pc), &pc);
        if (mv) vk_.vkCmdDispatch(cb, (r.n_out + 31) / 32, nt, 1);
        else vk_.vkCmdDispatch(cb, (r.n_out + 63) / 64, (nt + 63) / 64, 1);
    };
    auto rms = [&](uint32_t n, uint32_t rpt, uint32_t src, uint32_t sst, uint32_t dst, uint32_t dstr,
                   uint32_t w, uint32_t rows) {
        const RmsPC pc{n, rpt, src, sst, dst, dstr, w, s.eps};
        aux(sp_rms_, &pc, sizeof(pc), rows, 1);
    };
    auto add = [&](uint32_t n, uint32_t nt, uint32_t dst, uint32_t dstr, uint32_t src, uint32_t sstr, bool params) {
        const AddPC pc{n, nt, dst, dstr, src, sstr, params ? 1u : 0u};
        aux(sp_add_, &pc, sizeof(pc), (n * nt + 255) / 256, 1);
    };

    // x in
    {
        VkBufferCopy c{0, static_cast<VkDeviceSize>(s.X) * 4, xbytes};
        vk_.vkCmdCopyBuffer(cb, span_hx_.buf, span_arena_.buf, 1, &c);
        barrier(VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, CS,
                VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
    }
    if (profile_) {
        vk_.vkCmdResetQueryPool(cb, qpool_, 0, 2);
        vk_.vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, qpool_, 0);
    }

    const uint32_t E = s.E, Q = s.Q, KVD = s.KV, F = s.F, H = s.H, HKV = s.HKV, D = s.D;
    // K + V of one layer, in words (codes, then for int8 the two scale arrays).
    const uint32_t layer_words = s.kvq8 ? s.n_ctx * (KVD / 2 + KVD / 16) : s.n_ctx * KVD;
    const uint32_t code_words = s.kvq8 ? s.n_ctx * KVD / 4 : s.n_ctx * KVD / 2;   // one of K / V
    for (int t0 = 0; t0 < n_tok; t0 += kSpanChunk) {
        const uint32_t nt = static_cast<uint32_t>(std::min(kSpanChunk, n_tok - t0));
        const uint32_t posc = static_cast<uint32_t>(pos0 + t0);
        const uint32_t xoff = s.X + static_cast<uint32_t>(t0) * E;
        for (size_t li = 0; li < span_layers_.size(); ++li) {
            const SpanLayerDev& L = span_layers_[li];
            const uint32_t ck = static_cast<uint32_t>(li) * layer_words;
            const uint32_t cv = ck + code_words;
            const uint32_t cks = cv + code_words, cvs = cks + s.n_ctx * KVD / 32;   // int8 only

            // ---- attention block
            rms(E, 1, xoff, E, s.XB, E, L.attn_norm, nt);
            bar();
            uint32_t qo, qs, ko, ks, vo, vs;
            if (L.wqkv.r) {
                const uint32_t W = Q + 2 * KVD;
                mm(L.wqkv, nt, s.XB, s.QKV);
                bar();
                if (L.bqkv != kNone) { add(W, nt, s.QKV, W, L.bqkv, 0, true); bar(); }
                qo = s.QKV; ko = s.QKV + Q; vo = s.QKV + Q + KVD;
                qs = ks = vs = W;
            } else {
                qo = s.QKV; ko = s.QKV + nt * Q; vo = ko + nt * KVD;
                qs = Q; ks = vs = KVD;
                mm(L.wq, nt, s.XB, qo);
                mm(L.wk, nt, s.XB, ko);
                mm(L.wv, nt, s.XB, vo);
                bar();
                const bool any_b = L.bq != kNone || L.bk != kNone || L.bv != kNone;
                if (L.bq != kNone) add(Q, nt, qo, qs, L.bq, 0, true);
                if (L.bk != kNone) add(KVD, nt, ko, ks, L.bk, 0, true);
                if (L.bv != kNone) add(KVD, nt, vo, vs, L.bv, 0, true);
                if (any_b) bar();
            }
            if (L.q_norm != kNone || L.k_norm != kNone) {
                if (L.q_norm != kNone) rms(D, H, qo, qs, qo, qs, L.q_norm, nt * H);
                if (L.k_norm != kNone) rms(D, HKV, ko, ks, ko, ks, L.k_norm, nt * HKV);
                bar();
            }
            if (s.half) {
                const RopePC rq{qo, qs, D, s.half, posc, s.neox, s.cos_off, s.sin_off};
                aux(sp_rope_, &rq, sizeof(rq), nt, H);
                const RopePC rk{ko, ks, D, s.half, posc, s.neox, s.cos_off, s.sin_off};
                aux(sp_rope_, &rk, sizeof(rk), nt, HKV);
                bar();
            }
            if (s.kvq8) {
                const KvQ8PC kp{KVD, ko, ks, vo, vs, ck, cv, posc, cks, cvs};
                aux(sp_kv8_, &kp, sizeof(kp), nt, 1);
                bar();
                const AttnQ8PC ap{D, H / HKV, KVD, qo, qs, s.ATT, Q, ck, cv, posc,
                                  L.window ? s.window : 0u, s.scale, s.softcap, cks, cvs};
                aux(sp_attn8_, &ap, sizeof(ap), H, nt);
                bar();
            } else {
                {
                    const KvPC kp{KVD, ko, ks, vo, vs, ck, cv, posc};
                    aux(sp_kv_, &kp, sizeof(kp), nt, 1);
                    bar();
                }
                const AttnPC ap{D, H / HKV, KVD, qo, qs, s.ATT, Q, ck, cv, posc,
                                L.window ? s.window : 0u, s.scale, s.softcap};
                aux(sp_attn_, &ap, sizeof(ap), H, nt);
                bar();
            }
            mm(L.wo, nt, s.ATT, s.PROJ);
            bar();
            if (L.bo != kNone) { add(E, nt, s.PROJ, E, L.bo, 0, true); bar(); }
            if (L.post_attn != kNone) { rms(E, 1, s.PROJ, E, s.PROJ, E, L.post_attn, nt); bar(); }
            add(E, nt, xoff, E, s.PROJ, E, false);
            bar();

            // ---- feed-forward block
            rms(E, 1, xoff, E, s.XB, E, L.ffn_norm, nt);
            bar();
            uint32_t down_in;
            if (L.wg.r) {
                mm(L.wg, nt, s.XB, s.GU);
                mm(L.wu, nt, s.XB, s.GU + nt * F);
                bar();
                const ActPC ac{F, nt, s.GU, F, s.GU + nt * F, F, s.GU, F, s.gelu};
                aux(sp_act_, &ac, sizeof(ac), (F * nt + 255) / 256, 1);
                down_in = s.GU;
            } else {
                mm(L.wu, nt, s.XB, s.GU);   // fused gate|up rows: [nt][2F]
                bar();
                const ActPC ac{F, nt, s.GU, 2 * F, s.GU + F, 2 * F, s.ACT, F, s.gelu};
                aux(sp_act_, &ac, sizeof(ac), (F * nt + 255) / 256, 1);
                down_in = s.ACT;
            }
            bar();
            mm(L.wd, nt, down_in, s.FFN);
            bar();
            if (L.post_ffn != kNone) { rms(E, 1, s.FFN, E, s.FFN, E, L.post_ffn, nt); bar(); }
            add(E, nt, xoff, E, s.FFN, E, false);
            bar();
        }
    }

    if (profile_) vk_.vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, qpool_, 1);
    barrier(CS, VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    {
        VkBufferCopy c{static_cast<VkDeviceSize>(s.X) * 4, 0, xbytes};
        vk_.vkCmdCopyBuffer(cb, span_arena_.buf, span_hy_.buf, 1, &c);
    }
    barrier(VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_READ_BIT);
    vk_.vkEndCommandBuffer(cb);

    const auto w0 = std::chrono::steady_clock::now();
    if (!submit_and_wait()) return false;      // x untouched
    if (profile_) {
        uint64_t ts[2] = {0, 0};
        if (vk_.vkGetQueryPoolResults(device_, qpool_, 0, 2, sizeof(ts), ts, sizeof(uint64_t),
                                      VK_QUERY_RESULT_64_BIT) == VK_SUCCESS && ts[1] >= ts[0])
            prof_kernel_ms_ += (ts[1] - ts[0]) * static_cast<double>(limits_.timestampPeriod) * 1e-6;
        prof_wall_ms_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - w0).count();
        ++prof_submits_;
    }
    if (prof_stages_ && qi > 1 && n_tok == 1) {   // decode steps only
        stage_ts_.resize(qi);
        if (vk_.vkGetQueryPoolResults(device_, stage_pool_, 0, qi, qi * sizeof(uint64_t), stage_ts_.data(),
                                      sizeof(uint64_t), VK_QUERY_RESULT_64_BIT) == VK_SUCCESS) {
            for (uint32_t i = 1; i < qi; ++i) {
                const double ms = (stage_ts_[i] - stage_ts_[i - 1]) * static_cast<double>(limits_.timestampPeriod) * 1e-6;
                const char* lab = stage_labels_[i];
                auto it = std::find_if(stage_ms_.begin(), stage_ms_.end(), [&](const auto& e) { return e.first == lab; });
                if (it == stage_ms_.end()) stage_ms_.push_back({lab, ms}); else it->second += ms;
            }
            ++stage_calls_;
        }
    }
    if (!span_hy_.coherent) {
        VkMappedMemoryRange mr{};
        mr.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;
        mr.memory = span_hy_.mem;
        mr.size = VK_WHOLE_SIZE;
        vk_.vkInvalidateMappedMemoryRanges(device_, 1, &mr);
    }
    std::memcpy(x, span_hy_.map, xbytes);
    // Positions written now replace pos0..; anything the device held beyond
    // them belonged to a different continuation and is no longer valid.
    span_kv_len_ = pos0 + n_tok;
    return true;
}

// ---------------------------------------------------------------- registration

const char* vulkan_status() {
    return g_backend ? g_backend->status() : (gpu_disabled_by_env() ? "disabled by FISH_GPU=0" : "not registered");
}

bool vulkan_init_now() { return g_backend && g_backend->ensure_init(); }

std::string vulkan_describe() { return g_backend ? g_backend->describe() : std::string("vulkan: not registered"); }

bool vulkan_profile(double* kernel_ms, double* wall_ms, long* submits, bool reset) {
    if (!g_backend || !g_backend->profile_) return false;
    if (kernel_ms) *kernel_ms = g_backend->prof_kernel_ms_;
    if (wall_ms) *wall_ms = g_backend->prof_wall_ms_;
    if (submits) *submits = g_backend->prof_submits_;
    if (reset) { g_backend->prof_kernel_ms_ = 0; g_backend->prof_wall_ms_ = 0; g_backend->prof_submits_ = 0; }
    return true;
}

namespace {
struct Registrar {
    Registrar() {
        if (gpu_disabled_by_env()) return;
        g_backend = new VulkanBackend();   // leaked on purpose: see the file header
        set_gpu_backend(g_backend);
    }
};
Registrar g_registrar;
} // namespace

} // namespace gpu
} // namespace fish
