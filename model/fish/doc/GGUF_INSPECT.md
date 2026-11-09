# GGUF Inspector — `gguf_inspect.cpp`

## Purpose
**Read-only GGUF header + tensor table parser.** Zero model loading, zero ggml dependency. Returns `TensorTable` (vector of `TensorDesc`) used by `LayerPlanner`, `ResidentWindow`, `Prefetcher`, `ExpertStreamer`.

## Why Not Use llama.cpp's Loader?
- llama.cpp's `llama_model_loader` *allocates and fills* all tensors (load_tensors)
- We need **only metadata** (offset, size, quant, layer index) to plan residency
- Loading 70B model just to inspect = 30+ seconds, 40+ GB RAM
- Inspector runs in **< 100 ms**, **< 10 MB RAM**

## GGUF v3 Format (subset we parse)
```
Magic: "GGUF" (4 bytes)
Version: uint32 (3)
Tensor count: uint64
Metadata KV count: uint64
KV pairs: [key:string, type:uint32, value...]
Tensor infos: [name:string, ndims:uint32, dims[4]:uint64, qtype:uint32, offset:uint64]
Tensor data: starts at align(offset, 32) after last tensor info
```

## Output: `TensorDesc`
```cpp
struct TensorDesc {
    std::string name;           // "blk.12.attn_q.weight"
    size_t offset = 0;          // Byte offset in file (from tensor info)
    size_t size = 0;            // Computed: elements × bytes_per_element(qtype)
    QuantType qtype = UNKNOWN;  // From GGUF quantization enum
    int layer_idx = -1;         // Extracted from name; -1 = non-layer
    bool is_expert = false;     // Heuristic: name contains "expert" or "ffn_gate"
    int expert_id = -1;         // If is_expert, parsed from name suffix
};
```

## Layer Index Extraction (Heuristic)
```cpp
// Patterns tried in order:
1. "blk.{N}."           → LLaMA, Mistral, etc.
2. "layers.{N}."        → HuggingFace transformers
3. "model.layers.{N}."  → Some quantized exports
4. "h{N}."              → GPT-2 style
```
First match wins. Non-layer tensors (embedding, output, norms) keep `layer_idx = -1`.

## Expert Detection (Heuristic)
```cpp
if (name.find("expert") != npos ||
    name.find("ffn_gate") != npos ||
    name.find("ffn_up_exps") != npos) {
    is_expert = true;
    // Try to extract expert ID from last numeric suffix
}
```
**Limitation**: Not all MoE architectures use standard naming. May need per-architecture overrides.

## QuantType Mapping (GGUF → Fish)
| GGUF Enum | Fish QuantType | Approx bits/weight |
|-----------|----------------|-------------------|
| 0 | F32 | 32 |
| 1 | F16 | 16 |
| 2 | Q4_0 | 4 |
| 3 | Q4_1 | 4 |
| 6 | Q5_0 | 5 |
| 7 | Q5_1 | 5 |
| 8 | Q8_0 | 8 |
| 10-16 | Q2_K..Q8_K | 2-8 (K-quant) |
| 17-22 | IQ2_XXS..IQ4_XS | 2-4 (Importance) |

## Size Calculation
```cpp
// elements = dims[0] * dims[1] * dims[2] * dims[3]
// bytes_per_element = lookup_table[qtype]
size = elements * bytes_per_element;
// Note: K-quants have variable block size; this is approximate.
// Real size from ggml_nbytes(tensor) would be exact but requires ggml.
```

## Connection to Downstream Modules

```
inspect_gguf(model_path)
        │
        ▼
TensorTable (vector<TensorDesc>)
        │
        ├─▶ LayerPlanner::make_layer_plan()
        │       Groups by layer_idx, sums sizes → per-layer VRAM cost
        │
        ├─▶ ResidentWindow::ensure_hot(layer_idx)
        │       Looks up tensors for layer → knows exact byte ranges to promote
        │
        ├─▶ Prefetcher::schedule_prefetch(layers)
        │       For each layer, issues pread for tensor ranges [offset, offset+size)
        │
        ├─▶ ExpertStreamer
        │       Filters is_expert tensors → builds layer→expert_id map
        │
        └─▶ CompressionPolicy::choose_quant()
                Uses qtype + layer_idx + name to pick target quantization
```

## Invariants
1. **No allocation proportional to model size** — only `TensorDesc` per tensor (~100 bytes each)
2. **File handle closed before return** — no leaked FDs
3. **Offset/size valid for `pread`/`ReadFile`** — can stream any tensor independently
4. **Idempotent** — multiple calls return identical tables
5. **Throws no exceptions** — returns empty table on error

## Testing
```cpp
TEST(GGUFInspector, KnownModel) {
    TensorTable t = inspect_gguf("testdata/7b-q4_k_m.gguf");
    EXPECT_GT(t.size(), 200);           // ~300 tensors for 7B
    int layers = 0, experts = 0;
    for (auto& d : t) {
        layers = std::max(layers, d.layer_idx + 1);
        if (d.is_expert) ++experts;
    }
    EXPECT_EQ(layers, 32);              // 7B = 32 layers
    EXPECT_EQ(experts, 0);              // Dense model
}

TEST(GGUFInspector, MoEModel) {
    TensorTable t = inspect_gguf("testdata/mixtral-8x7b-q4_k_m.gguf");
    int experts = 0;
    for (auto& d : t) if (d.is_expert) ++experts;
    EXPECT_GT(experts, 100);            // 8 experts × 32 layers × ~3 tensors
}
```

## Future Improvements
- Exact size via `ggml_nbytes` (link ggml statically, no runtime init)
- Architecture-specific name parsers (registry by `general.architecture` metadata)
- Tensor dependency graph (which tensors used together in attention/FFN)
- Memory-map the header only (first 1 MB) for instant inspect of huge files