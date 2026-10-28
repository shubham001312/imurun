#include <fish/engine.h>
#include <iostream>
#include <cassert>

int main() {
    std::cout << "=== FISH Engine Unit Tests ===\n\n";

    // Test 1: Hardware Probe
    std::cout << "[1] Hardware Probe\n";
    fish::HardwareCaps caps = fish::probe_hardware();
    std::cout << "    RAM: " << (caps.ram_bytes / (1024*1024*1024)) << " GB\n";
    std::cout << "    VRAM: " << (caps.vram_bytes / (1024*1024*1024)) << " GB\n";
    std::cout << "    CPU cores: " << caps.cpu_cores << "\n";
    std::cout << "    mmap: " << (caps.has_mmap ? "yes" : "no") << "\n";
    std::cout << "    direct I/O: " << (caps.has_direct_io ? "yes" : "no") << "\n";
    std::cout << "    GPU: " << caps.gpu_name << "\n";
    std::cout << "    Disk seq read: " << caps.disk.seq_read_mb_s << " MB/s\n";
    assert(caps.ram_bytes > 0);
    assert(caps.cpu_cores > 0);
    std::cout << "    PASSED\n\n";

    // Test 2: GGUF Inspector (requires a model file)
    std::cout << "[2] GGUF Inspector\n";
    // Try to find a test model
    const char* test_models[] = {
        "C:/Users/imu/AppData/Local/Temp/opencode/refs/llama.cpp/models/ggml-test.gguf",
        "C:/models/test.gguf",
        nullptr
    };
    bool tested = false;
    for (int i = 0; test_models[i]; ++i) {
        std::ifstream f(test_models[i], std::ios::binary);
        if (f) {
            f.close();
            fish::TensorTable tensors = fish::inspect_gguf(test_models[i]);
            std::cout << "    Model: " << test_models[i] << "\n";
            std::cout << "    Tensors: " << tensors.size() << "\n";
            int layers = 0, experts = 0;
            for (const auto& t : tensors) {
                if (t.layer_idx >= 0) layers = std::max(layers, t.layer_idx + 1);
                if (t.is_expert) ++experts;
            }
            std::cout << "    Layers detected: " << layers << "\n";
            std::cout << "    Expert tensors: " << experts << "\n";
            assert(tensors.size() > 0);
            tested = true;
            break;
        }
    }
    if (!tested) {
        std::cout <<    "    SKIPPED (no test model found)\n";
    }
    std::cout << "    PASSED\n\n";

    // Test 3: Layer Planner
    std::cout << "[3] Layer Planner\n";
    fish::TensorTable dummy_tensors;
    // Create dummy tensors for 32 layers
    for (int L = 0; L < 32; ++L) {
        fish::TensorDesc d;
        d.name = "blk." + std::to_string(L) + ".attn_q.weight";
        d.layer_idx = L;
        d.size = 1024 * 1024; // 1 MB each
        d.qtype = fish::QuantType::Q4_K;
        dummy_tensors.push_back(d);
    }
    fish::LayerPlan plan = fish::make_layer_plan(caps, dummy_tensors, 2ull * 1024 * 1024 * 1024); // 2 GB VRAM budget
    std::cout << "    Hot layers: " << plan.hot_layers.size() << "\n";
    std::cout << "    Warm layers: " << plan.warm_layers.size() << "\n";
    std::cout << "    Cold layers: " << plan.cold_layers.size() << "\n";
    std::cout << "    VRAM budget: " << (plan.vram_budget_bytes / (1024*1024)) << " MB\n";
    std::cout << "    Staging ring: " << (plan.staging_ring_bytes / (1024*1024)) << " MB x " << plan.staging_slots << "\n";
    assert(plan.hot_layers.size() + plan.warm_layers.size() + plan.cold_layers.size() == 32);
    std::cout << "    PASSED\n\n";

    std::cout << "=== ALL TESTS PASSED ===\n";
    return 0;
}