#pragma once
/*
 * FISH — GGUF container reader.
 *
 * GGUF is an open file format (magic "GGUF"), not part of any inference
 * engine. This reader is FISH's own: it parses the header, the metadata
 * key/value table and the tensor directory, then maps the file read-only
 * so no weight is ever copied unless the caller asks for it.
 *
 * Design rules:
 *   - every read is bounds checked; a corrupt file yields an error, never UB
 *   - nothing is allocated in proportion to a value we have not validated
 *   - the mapping is read-only: FISH never writes to a model file
 *   - parsing is streaming and lazy; the tensor table is small (a few MB)
 */
#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>
#include <memory>

namespace fish {

// ggml tensor types, values from the GGUF specification.
enum class GgmlType : uint32_t {
    F32 = 0, F16 = 1, Q4_0 = 2, Q4_1 = 3,
    Q5_0 = 6, Q5_1 = 7, Q8_0 = 8, Q8_1 = 9,
    Q2_K = 10, Q3_K = 11, Q4_K = 12, Q5_K = 13, Q6_K = 14, Q8_K = 15,
    IQ2_XXS = 16, IQ2_XS = 17, IQ3_XXS = 18, IQ1_S = 19, IQ4_NL = 20,
    IQ3_S = 21, IQ2_S = 22, IQ4_XS = 23, I8 = 24, I16 = 25, I32 = 26,
    I64 = 27, F64 = 28, IQ1_M = 29, BF16 = 30, TQ1_0 = 34, TQ2_0 = 35,
    MXFP4 = 39,
};

const char* ggml_type_name(GgmlType t);
size_t      ggml_type_size(GgmlType t);   // bytes per block
int         ggml_blck_size(GgmlType t);   // elements per block
size_t      ggml_row_size(GgmlType t, uint64_t n_elements);

// One metadata value. Arrays of strings and arrays of small ints get their
// own storage because the tokenizer tables are the only large ones and
// nesting a full MetaValue per token would waste ~12 MB.
struct MetaValue {
    enum Kind : uint8_t { UINT, INT, FLOAT, BOOL, STRING, ARRAY } kind = UINT;
    uint64_t u = 0;
    int64_t  i = 0;
    double   f = 0;
    bool     b = false;
    std::string s;
    std::vector<MetaValue>   arr;    // ARRAY of mixed values
    std::vector<std::string> sarr;   // ARRAY of STRING
    std::vector<int32_t>     iarr;   // ARRAY of INT32 / UINT32

    uint64_t    as_u64(uint64_t def = 0) const;
    int64_t     as_i64(int64_t def = 0) const;
    double      as_f64(double def = 0) const;
    bool        as_bool(bool def = false) const;
    std::string as_str(const std::string& def = std::string()) const;
};

struct TensorInfo {
    std::string name;
    GgmlType type = GgmlType::F32;
    std::vector<uint64_t> ne;      // ne[0] is the fastest-varying dimension
    uint64_t offset = 0;           // byte offset from the data section start
    uint64_t nbytes = 0;
    const uint8_t* data = nullptr; // mapped pointer, nullptr if not mapped
    int  layer = -1;               // transformer block index, -1 = non-layer
    bool is_expert = false;
    int  expert = -1;

    uint64_t n_elements() const;
    size_t   row_bytes() const;    // bytes of one ne[0] row
};

class GgufFile {
public:
    // Returns nullptr and fills *err on failure. Never throws.
    static std::unique_ptr<GgufFile> open(const std::string& path,
                                          std::string* err = nullptr);
    ~GgufFile();
    GgufFile(const GgufFile&) = delete;
    GgufFile& operator=(const GgufFile&) = delete;

    const std::vector<TensorInfo>& tensors() const { return tensors_; }
    const TensorInfo* find(const std::string& name) const;

    bool has(const std::string& key) const;
    const MetaValue* meta(const std::string& key) const;

    uint64_t    u64(const std::string& key, uint64_t def = 0) const;
    int64_t     i64(const std::string& key, int64_t def = 0) const;
    double      f64(const std::string& key, double def = 0) const;
    bool        flag(const std::string& key, bool def = false) const;
    std::string str(const std::string& key, const std::string& def = std::string()) const;

    uint32_t alignment() const { return alignment_; }
    const std::string& path() const { return path_; }
    size_t file_size() const { return file_size_; }
    const uint8_t* data_base() const { return data_; }
    uint64_t data_offset() const { return data_offset_; }

private:
    GgufFile() = default;
    bool parse(std::string* err);

    std::string path_;
    size_t file_size_ = 0;
    uint32_t alignment_ = 32;
    uint64_t data_offset_ = 0;
    const uint8_t* map_ = nullptr;   // whole-file read-only mapping
    const uint8_t* data_ = nullptr;  // map_ + data_offset_
    void* os_file_ = nullptr;        // HANDLE / fd
    void* os_map_ = nullptr;         // mapping HANDLE
    std::vector<TensorInfo> tensors_;
    std::vector<std::pair<std::string, MetaValue>> kv_;
};

} // namespace fish
