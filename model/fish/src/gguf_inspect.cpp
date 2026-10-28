#include <fish/engine.h>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <vector>
#include <string>
#include <algorithm>
#include <memory>

namespace fish {

// Minimal GGUF reader (header + tensor info only, no data loading)
// Based on llama.cpp's gguf.h but self-contained for inspection.

#pragma pack(push, 1)
struct GGUFHeader {
    char magic[4];           // "GGUF"
    uint32_t version;        // 3
    uint64_t tensor_count;
    uint64_t metadata_kv_count;
};
#pragma pack(pop)

struct GGUFMetadataValue {
    enum Type : uint32_t {
        UINT8   = 0,
        INT8    = 1,
        UINT16  = 2,
        INT16   = 3,
        UINT32  = 4,
        INT32   = 5,
        FLOAT32 = 6,
        BOOL    = 7,
        STRING  = 8,
        ARRAY   = 9,
        UINT64  = 10,
        INT64   = 11,
        FLOAT64 = 12
    };
    Type type;
    std::vector<uint8_t> data;
};

static std::string read_string(std::ifstream& f) {
    uint64_t len;
    f.read(reinterpret_cast<char*>(&len), 8);
    std::string s(len, '\0');
    f.read(&s[0], len);
    return s;
}

static void skip_metadata_value(std::ifstream& f, GGUFMetadataValue::Type type) {
    switch (type) {
        case GGUFMetadataValue::UINT8:
        case GGUFMetadataValue::INT8:
        case GGUFMetadataValue::BOOL: f.seekg(1, std::ios::cur); break;
        case GGUFMetadataValue::UINT16:
        case GGUFMetadataValue::INT16: f.seekg(2, std::ios::cur); break;
        case GGUFMetadataValue::UINT32:
        case GGUFMetadataValue::INT32:
        case GGUFMetadataValue::FLOAT32: f.seekg(4, std::ios::cur); break;
        case GGUFMetadataValue::UINT64:
        case GGUFMetadataValue::INT64:
        case GGUFMetadataValue::FLOAT64: f.seekg(8, std::ios::cur); break;
        case GGUFMetadataValue::STRING: {
            uint64_t len; f.read(reinterpret_cast<char*>(&len), 8);
            f.seekg(len, std::ios::cur);
            break;
        }
        case GGUFMetadataValue::ARRAY: {
            uint32_t arr_type; f.read(reinterpret_cast<char*>(&arr_type), 4);
            uint64_t count; f.read(reinterpret_cast<char*>(&count), 8);
            for (uint64_t i = 0; i < count; ++i) skip_metadata_value(f, static_cast<GGUFMetadataValue::Type>(arr_type));
            break;
        }
    }
}

TensorTable inspect_gguf(const std::string& model_path) {
    TensorTable table;
    std::ifstream f(model_path, std::ios::binary);
    if (!f) return table;

    // Read header
    GGUFHeader hdr;
    f.read(reinterpret_cast<char*>(&hdr), sizeof(hdr));
    if (std::strncmp(hdr.magic, "GGUF", 4) != 0) return table;
    if (hdr.version != 3) return table; // Only support GGUF v3

    // Skip metadata KV pairs
    for (uint64_t i = 0; i < hdr.metadata_kv_count; ++i) {
        std::string key = read_string(f);
        uint32_t type; f.read(reinterpret_cast<char*>(&type), 4);
        skip_metadata_value(f, static_cast<GGUFMetadataValue::Type>(type));
    }

    // Read tensor infos
    for (uint64_t i = 0; i < hdr.tensor_count; ++i) {
        TensorDesc desc;
        desc.name = read_string(f);

        uint32_t n_dims; f.read(reinterpret_cast<char*>(&n_dims), 4);
        uint64_t dims[4] = {1,1,1,1};
        for (uint32_t d = 0; d < n_dims; ++d) {
            f.read(reinterpret_cast<char*>(&dims[d]), 8);
        }
        uint32_t qtype; f.read(reinterpret_cast<char*>(&qtype), 4);
        uint64_t offset; f.read(reinterpret_cast<char*>(&offset), 8);

        desc.offset = static_cast<size_t>(offset);
        desc.qtype = static_cast<QuantType>(qtype);

        // Compute size from dims and quantization type (approximate)
        // In reality we'd use ggml_nbytes for exact size, but we avoid linking ggml here.
        // This is a placeholder; real impl would map quant type to block size.
        uint64_t nelements = dims[0] * dims[1] * dims[2] * dims[3];
        size_t type_size = 1; // fallback
        switch (desc.qtype) {
            case QuantType::F32: type_size = 4; break;
            case QuantType::F16: case QuantType::BF16: type_size = 2; break;
            case QuantType::Q4_0: case QuantType::Q4_1: case QuantType::Q4_K: type_size = 0.5; break; // bits per weight ~4
            case QuantType::Q5_0: case QuantType::Q5_1: case QuantType::Q5_K: type_size = 0.625; break;
            case QuantType::Q8_0: case QuantType::Q8_K: type_size = 1; break;
            default: type_size = 1; break;
        }
        desc.size = static_cast<size_t>(nelements * type_size);

        // Heuristic: extract layer index from name (e.g. "blk.12.attn_q.weight")
        // Common patterns: "blk.N.", "layers.N.", "model.layers.N."
        size_t pos = desc.name.find("blk.");
        if (pos == std::string::npos) pos = desc.name.find("layers.");
        if (pos == std::string::npos) pos = desc.name.find("model.layers.");
        if (pos != std::string::npos) {
            pos += 4; // skip "blk." or "layers."
            size_t end = desc.name.find('.', pos);
            if (end != std::string::npos) {
                std::string num_str = desc.name.substr(pos, end - pos);
                try {
                    desc.layer_idx = std::stoi(num_str);
                } catch (...) {}
            }
        }

        // Heuristic: detect MoE expert tensors
        if (desc.name.find("expert") != std::string::npos ||
            desc.name.find("ffn_gate") != std::string::npos) {
            desc.is_expert = true;
            // Try to extract expert id
            size_t epos = desc.name.rfind('.');
            if (epos != std::string::npos && epos + 1 < desc.name.size()) {
                std::string suffix = desc.name.substr(epos + 1);
                try { desc.expert_id = std::stoi(suffix); } catch (...) {}
            }
        }

        table.push_back(std::move(desc));
    }

    return table;
}

} // namespace fish