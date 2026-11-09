# Compression Policy — `compression_policy.cpp` / `compression_policy.h`

## Purpose
**Quantization as a streaming knob.** Two modes:
1. **Offline** — whole-model re-quant (llama.cpp quantizer, slab loop)
2. **Online** — single-tensor re-quant when VRAM pressure spikes

## QuantPolicy Enum
```cpp
enum class QuantPolicy {
    Q4_K_M,   // Default: balanced quality/size
    Q8_0,     // High precision for attention
    IQ4_NL,   // Importance-quantized (needs imatrix)
    // BF16, F16, Q5_K, Q6_K, etc. as needed
};
```

## Offline: Whole-Model Quantization
```cpp
// Uses llama.cpp's quantizer slab loop (llama-quant.cpp:1299-1388)
// FISH invokes via:
llama_model_quantize_params qparams = {};
qparams.max_buf_size = 1_GB;        // Slab buffer cap
qparams.allow_requantize = true;    // Can quantize already-quantized
qparams.imatrix_path = "imatrix.dat"; // For IQ4_NL
llama_model_quantize(src_gguf, dst_gguf, QuantPolicy::Q4_K_M, &qparams);
```

### Slab Loop (Key to Bounded Memory)
```cpp
// Per tensor, processes in slabs:
const size_t bytes_per_row = row_size_src + row_size_dst + (F32?0:n_per_row*sizeof(float));
const int64_t nrows_slab = max(1, min(nrows_total, max_buf_size / bytes_per_row));
for (int64_t ir = 0; ir < nrows_total; ir += nrows_slab) {
    load_range(src, ir, nrows_slab);      // mmap or pread
    quantize_rows(src_slab, dst_slab);    // CPU vectorized
    write_range(dst, ir, nrows_slab);     // pwrite
    unmap_range(src, ir, nrows_slab);     // Release immediately
}
```
**Peak RAM = `max_buf_size` (default 8 GB, FISH sets 1-2 GB)**

## Online: Single-Tensor Re-Quant
```cpp
// Triggered by VRAM pressure monitor:
optional<QuantPolicy> requantize_tensor(model_path, tensor_desc, target_policy);

// Steps:
1. Load tensor from disk (pread tensor_desc.offset, tensor_desc.size)
2. Dequantize to FP32 (CPU, vectorized)
3. Quantize to target (Q8_0 → Q4_K_M, etc.)
4. Write back to disk (pwrite same offset) — **in-place in GGUF**
5. Update TensorDesc.qtype, size
6. If tensor currently Hot: re-promote with new quantization
```

### Why In-Place GGUF Works
- GGUF tensor data is **contiguous per tensor** (offset + size)
- Quantization changes size but **not offset of subsequent tensors** if we:
  - Only down-quantize (size decreases)
  - Rewrite entire file periodically (compaction)
- **Simpler**: write to new file, atomic rename (safer, supports up-quantize)

## Per-Tensor Policy Selection
```cpp
QuantPolicy choose_quant(const TensorDesc& tensor, const HardwareCaps& caps) {
    if (caps.vram_bytes < 6_GB) {
        // Aggressive: everything Q4_K_M
        return QuantPolicy::Q4_K_M;
    }
    if (caps.vram_bytes < 12_GB) {
        // Hybrid: attention Q8_0, FFN Q4_K_M
        if (tensor.name.find("attn") != npos ||
            tensor.name.find("qkv") != npos)
            return QuantPolicy::Q8_0;
        return QuantPolicy::Q4_K_M;
    }
    // High VRAM: keep original or Q8_0
    return QuantPolicy::Q8_0;
}
```

### Policy Rules (Configurable)
| Tensor Pattern | VRAM < 6 GB | VRAM 6-12 GB | VRAM > 12 GB |
|----------------|-------------|--------------|--------------|
| `*attn*`, `*qkv*`, `*attn_out*` | Q4_K_M | **Q8_0** | Q8_0 / original |
| `*ffn*`, `*gate*`, `*up*`, `*down*` | **Q4_K_M** | Q4_K_M | Q8_0 |
| `*norm*`, `*embd*`, `output` | F16 | F16 | F16 |
| MoE experts | Q4_K_M | Q4_K_M | Q8_0 |

## Importance Matrix (IQ4_NL)
- Requires calibration run (`llama-imatrix` tool)
- Produces `imatrix.dat` (per-tensor importance scores)
- IQ4_NL uses this to allocate more bits to important weights
- **FISH integration**: Store imatrix alongside model, pass to quantizer

## VRAM Pressure → Online Re-Quant Flow
```
1. EngineStats.peak_vram_bytes > 0.9 * vram_budget
2. Engine::on_vram_pressure()
3. For each Hot tensor (LRU order):
     if tensor.qtype > Q4_K_M:
         target = next_lower_quant(tensor.qtype)
         requantize_tensor(model_path, tensor, target)
         if success:
             window.demote(tensor.layer)  // Will re-promote with new quant
             break
4. Resume generation
```

## Invariants
1. **Offline quant never exceeds `max_buf_size` RAM**
2. **Online quant is atomic per tensor** — no partial state visible
3. **GGUF file remains valid** after each re-quant (or new file written)
4. **TensorDesc updated** before re-promotion
5. **No re-quant during active generation** — only at ubatch boundary

## Testing
```cpp
TEST(CompressionPolicy, OfflineQuantBoundedRAM) {
    // Quantize 70B model with max_buf_size=2GB
    // Verify peak RSS < 3 GB
}

TEST(CompressionPolicy, OnlineReQuantSingleTensor) {
    // Load model, promote layer to Hot
    // Trigger re-quant Q8_0→Q4_K_M
    // Verify: new size < old, layer re-promoted, generation continues
}

TEST(CompressionPolicy, PolicySelection) {
    HardwareCaps caps{.vram_bytes=4_GB};
    TensorDesc attn{"blk.0.attn_q.weight", ..., QuantType::Q8_0, 0};
    EXPECT_EQ(choose_quant(attn, caps), QuantPolicy::Q4_K_M);
    // etc.
}
```

## Future
- **Per-channel quantization** (AWQ/GPTQ style) via calibration
- **Dynamic precision** — attention heads with low entropy → lower bits
- **Quantization-aware training** integration (export directly to GGUF)
- **Hardware-specific** — Hopper FP8, AMD RDNA3 INT4