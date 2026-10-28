#include <fish/engine.h>

namespace fish {

QuantPolicy choose_quant(const TensorDesc& tensor, const HardwareCaps& caps) {
    // Heuristic: prefer Q4_K_M for most tensors on constrained VRAM
    if (caps.vram_bytes > 0 && caps.vram_bytes < 6ull * 1024 * 1024 * 1024) {
        // < 6 GB VRAM: aggressive quantization
        if (tensor.qtype == QuantType::F16 || tensor.qtype == QuantType::F32) {
            return QuantPolicy::Q4_K_M;
        }
    } else if (caps.vram_bytes > 0 && caps.vram_bytes < 12ull * 1024 * 1024 * 1024) {
        // 6-12 GB: Q8_0 for attention, Q4_K_M for FFN
        if (tensor.name.find("attn") != std::string::npos ||
            tensor.name.find("qkv") != std::string::npos) {
            return QuantPolicy::Q8_0;
        }
        return QuantPolicy::Q4_K_M;
    }
    // High VRAM: keep original or Q8_0
    return QuantPolicy::Q8_0;
}

std::optional<QuantPolicy> requantize_tensor(const std::string& model_path,
                                             TensorDesc& tensor,
                                             QuantPolicy target) {
    // Stub: in real impl, call llama_model_quantize on a single tensor
    // using the slab loop with max_buf_size
    tensor.qtype = static_cast<QuantType>(target);
    return target;
}

} // namespace fish