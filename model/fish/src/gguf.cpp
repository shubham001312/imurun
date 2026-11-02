/*
 * FISH — GGUF reader implementation.
 *
 * Reads the header, metadata table and tensor directory with bounds checks
 * on every field, then maps the file read-only. No weight bytes are copied.
 */
#include <fish/gguf.h>

#include <algorithm>
#include <cstring>
#include <cstdio>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace fish {

namespace {

constexpr uint32_t kMagic = 0x46554747u;  // "GGUF"

// Metadata value type ids from the GGUF spec.
enum MetaType : uint32_t {
    MT_UINT8 = 0, MT_INT8 = 1, MT_UINT16 = 2, MT_INT16 = 3,
    MT_UINT32 = 4, MT_INT32 = 5, MT_FLOAT32 = 6, MT_BOOL = 7,
    MT_STRING = 8, MT_ARRAY = 9, MT_UINT64 = 10, MT_INT64 = 11,
    MT_FLOAT64 = 12,
};

constexpr uint64_t kMaxString = 64ull << 20;      // 64 MB per string
constexpr uint64_t kMaxArray  = 1ull << 26;       // 64 M elements
constexpr int      kMaxDepth  = 8;

struct Reader {
    const uint8_t* p = nullptr;
    size_t n = 0;
    size_t off = 0;
    bool ok = true;

    bool need(size_t k) {
        if (!ok || k > n || off > n - k) { ok = false; return false; }
        return true;
    }
    uint8_t  u8()  { if (!need(1)) return 0; return p[off++]; }
    uint16_t u16() { uint16_t v = 0; if (!need(2)) return 0; std::memcpy(&v, p + off, 2); off += 2; return v; }
    uint32_t u32() { uint32_t v = 0; if (!need(4)) return 0; std::memcpy(&v, p + off, 4); off += 4; return v; }
    uint64_t u64() { uint64_t v = 0; if (!need(8)) return 0; std::memcpy(&v, p + off, 8); off += 8; return v; }
    int8_t   i8()  { return static_cast<int8_t>(u8()); }
    int16_t  i16() { return static_cast<int16_t>(u16()); }
    int32_t  i32() { return static_cast<int32_t>(u32()); }
    int64_t  i64() { return static_cast<int64_t>(u64()); }
    float    f32() { uint32_t v = u32(); float f = 0; std::memcpy(&f, &v, 4); return f; }
    double   f64() { uint64_t v = u64(); double d = 0; std::memcpy(&d, &v, 8); return d; }

    std::string str() {
        uint64_t len = u64();
        if (!ok || len > kMaxString || !need(static_cast<size_t>(len))) { ok = false; return {}; }
        std::string s(reinterpret_cast<const char*>(p + off), static_cast<size_t>(len));
        off += static_cast<size_t>(len);
        return s;
    }
};

bool parse_value(Reader& r, MetaValue& out, int depth) {
    if (depth > kMaxDepth) return false;
    uint32_t t = r.u32();
    if (!r.ok) return false;

    switch (t) {
        case MT_UINT8:  out.kind = MetaValue::UINT;  out.u = r.u8();  break;
        case MT_UINT16: out.kind = MetaValue::UINT;  out.u = r.u16(); break;
        case MT_UINT32: out.kind = MetaValue::UINT;  out.u = r.u32(); break;
        case MT_UINT64: out.kind = MetaValue::UINT;  out.u = r.u64(); break;
        case MT_INT8:   out.kind = MetaValue::INT;   out.i = r.i8();  break;
        case MT_INT16:  out.kind = MetaValue::INT;   out.i = r.i16(); break;
        case MT_INT32:  out.kind = MetaValue::INT;   out.i = r.i32(); break;
        case MT_INT64:  out.kind = MetaValue::INT;   out.i = r.i64(); break;
        case MT_FLOAT32:out.kind = MetaValue::FLOAT; out.f = r.f32(); break;
        case MT_FLOAT64:out.kind = MetaValue::FLOAT; out.f = r.f64(); break;
        case MT_BOOL:   out.kind = MetaValue::BOOL;  out.b = r.u8() != 0; break;
        case MT_STRING: out.kind = MetaValue::STRING; out.s = r.str(); break;
        case MT_ARRAY: {
            uint32_t et = r.u32();
            uint64_t count = r.u64();
            if (!r.ok || count > kMaxArray) return false;
            out.kind = MetaValue::ARRAY;

            // Token tables are the only large arrays; store them compactly.
            if (et == MT_STRING) {
                out.sarr.reserve(static_cast<size_t>(count));
                for (uint64_t i = 0; i < count && r.ok; ++i) out.sarr.push_back(r.str());
                break;
            }
            if (et == MT_INT32 || et == MT_UINT32) {
                out.iarr.reserve(static_cast<size_t>(count));
                for (uint64_t i = 0; i < count && r.ok; ++i)
                    out.iarr.push_back(static_cast<int32_t>(r.u32()));
                break;
            }
            if (et == MT_FLOAT32) {
                out.farr.reserve(static_cast<size_t>(count));
                for (uint64_t i = 0; i < count && r.ok; ++i)
                    out.farr.push_back(r.f32());
                break;
            }
            if (et == MT_FLOAT64) {
                out.farr.reserve(static_cast<size_t>(count));
                for (uint64_t i = 0; i < count && r.ok; ++i)
                    out.farr.push_back(r.f64());
                break;
            }
            // Sanity check: ensure enough bytes remain for the array elements.
            // Element size depends on the element type.
            size_t elem_size = 1;
            if (et == MT_UINT8 || et == MT_INT8) elem_size = 1;
            else if (et == MT_UINT16 || et == MT_INT16) elem_size = 2;
            else if (et == MT_UINT32 || et == MT_INT32 || et == MT_FLOAT32) elem_size = 4;
            else if (et == MT_UINT64 || et == MT_INT64 || et == MT_FLOAT64) elem_size = 8;
            // STRING and ARRAY are variable-size; skip precise check for them.
            if (et != MT_STRING && et != MT_ARRAY) {
                size_t remaining = (r.off <= r.n) ? (r.n - r.off) : 0;
                if (count > 0 && count * elem_size > remaining) return false;
            }
            out.arr.reserve(static_cast<size_t>(count));
            for (uint64_t i = 0; i < count && r.ok; ++i) {
                MetaValue v;
                if (!parse_value(r, v, depth + 1)) return false;
                out.arr.push_back(std::move(v));
            }
            break;
        }
        default:
            return false;
    }
    return r.ok;
}

std::string infer_name_only(const std::string& name) { return name; }

void classify_tensor(TensorInfo& t) {
    // "blk.12.attn_q.weight" -> layer 12
    if (t.name.rfind("blk.", 0) == 0) {
        size_t dot = t.name.find('.', 4);
        if (dot != std::string::npos) {
            int idx = 0;
            bool digits = dot > 4;
            for (size_t i = 4; i < dot; ++i) {
                if (t.name[i] < '0' || t.name[i] > '9') { digits = false; break; }
                idx = idx * 10 + (t.name[i] - '0');
            }
            if (digits) t.layer = idx;
        }
    }
    if (t.name.find("_exps") != std::string::npos) t.is_expert = true;
    (void)infer_name_only;
}

uint64_t align_up(uint64_t v, uint64_t a) {
    return a ? ((v + a - 1) / a) * a : v;
}

#ifdef _WIN32
std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    if (n <= 0) return std::wstring();
    std::wstring w(static_cast<size_t>(n - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
    return w;
}
#endif

} // namespace

// ---------------------------------------------------------------- type table

const char* ggml_type_name(GgmlType t) {
    switch (t) {
        case GgmlType::F32: return "F32";
        case GgmlType::F16: return "F16";
        case GgmlType::Q4_0: return "Q4_0";
        case GgmlType::Q4_1: return "Q4_1";
        case GgmlType::Q5_0: return "Q5_0";
        case GgmlType::Q5_1: return "Q5_1";
        case GgmlType::Q8_0: return "Q8_0";
        case GgmlType::Q8_1: return "Q8_1";
        case GgmlType::Q2_K: return "Q2_K";
        case GgmlType::Q3_K: return "Q3_K";
        case GgmlType::Q4_K: return "Q4_K";
        case GgmlType::Q5_K: return "Q5_K";
        case GgmlType::Q6_K: return "Q6_K";
        case GgmlType::Q8_K: return "Q8_K";
        case GgmlType::I8: return "I8";
        case GgmlType::I16: return "I16";
        case GgmlType::I32: return "I32";
        case GgmlType::I64: return "I64";
        case GgmlType::F64: return "F64";
        case GgmlType::BF16: return "BF16";
        case GgmlType::IQ4_NL: return "IQ4_NL";
        case GgmlType::MXFP4: return "MXFP4";
        default: return "UNSUPPORTED";
    }
}

size_t ggml_type_size(GgmlType t) {
    switch (t) {
        case GgmlType::F32:  return 4;
        case GgmlType::F16:  return 2;
        case GgmlType::BF16: return 2;
        case GgmlType::Q4_0: return 18;
        case GgmlType::Q4_1: return 20;
        case GgmlType::Q5_0: return 22;
        case GgmlType::Q5_1: return 24;
        case GgmlType::Q8_0: return 34;
        case GgmlType::Q8_1: return 36;
        case GgmlType::Q2_K: return 84;
        case GgmlType::Q3_K: return 110;
        case GgmlType::Q4_K: return 144;
        case GgmlType::Q5_K: return 176;
        case GgmlType::Q6_K: return 210;
        case GgmlType::Q8_K: return 292;
        case GgmlType::I8:   return 1;
        case GgmlType::I16:  return 2;
        case GgmlType::I32:  return 4;
        case GgmlType::I64:  return 8;
        case GgmlType::F64:  return 8;
        case GgmlType::IQ4_NL: return 18;
        case GgmlType::MXFP4:  return 17;
        default: return 0;
    }
}

int ggml_blck_size(GgmlType t) {
    switch (t) {
        case GgmlType::F32: case GgmlType::F16: case GgmlType::BF16:
        case GgmlType::I8: case GgmlType::I16: case GgmlType::I32:
        case GgmlType::I64: case GgmlType::F64:
            return 1;
        case GgmlType::Q2_K: case GgmlType::Q3_K: case GgmlType::Q4_K:
        case GgmlType::Q5_K: case GgmlType::Q6_K: case GgmlType::Q8_K:
            return 256;
        default:
            return 32;
    }
}

size_t ggml_row_size(GgmlType t, uint64_t n) {
    int blck = ggml_blck_size(t);
    size_t ts = ggml_type_size(t);
    if (!blck || !ts) return 0;
    return static_cast<size_t>(n / static_cast<uint64_t>(blck)) * ts;
}

// ---------------------------------------------------------------- MetaValue

uint64_t MetaValue::as_u64(uint64_t def) const {
    switch (kind) {
        case UINT:   return u;
        case INT:    return i < 0 ? def : static_cast<uint64_t>(i);
        case FLOAT:  return f < 0 ? def : static_cast<uint64_t>(f);
        case BOOL:   return b ? 1u : 0u;
        default:     return def;
    }
}

int64_t MetaValue::as_i64(int64_t def) const {
    switch (kind) {
        case UINT:  return static_cast<int64_t>(u);
        case INT:   return i;
        case FLOAT: return static_cast<int64_t>(f);
        case BOOL:  return b ? 1 : 0;
        default:    return def;
    }
}

double MetaValue::as_f64(double def) const {
    switch (kind) {
        case UINT:  return static_cast<double>(u);
        case INT:   return static_cast<double>(i);
        case FLOAT: return f;
        case BOOL:  return b ? 1.0 : 0.0;
        default:    return def;
    }
}

bool MetaValue::as_bool(bool def) const {
    switch (kind) {
        case BOOL:  return b;
        case UINT:  return u != 0;
        case INT:   return i != 0;
        case FLOAT: return f != 0;
        default:    return def;
    }
}

std::string MetaValue::as_str(const std::string& def) const {
    return kind == STRING ? s : def;
}

// ---------------------------------------------------------------- TensorInfo

uint64_t TensorInfo::n_elements() const {
    uint64_t n = 1;
    for (uint64_t d : ne) n *= d;
    return n;
}

size_t TensorInfo::row_bytes() const {
    return ne.empty() ? 0 : ggml_row_size(type, ne[0]);
}

// ---------------------------------------------------------------- GgufFile

GgufFile::~GgufFile() {
#ifdef _WIN32
    if (map_) UnmapViewOfFile(map_);
    if (os_map_) CloseHandle(static_cast<HANDLE>(os_map_));
    if (os_file_) CloseHandle(static_cast<HANDLE>(os_file_));
#else
    if (map_) munmap(const_cast<uint8_t*>(map_), file_size_);
    if (os_file_) close(static_cast<int>(reinterpret_cast<intptr_t>(os_file_)));
#endif
}

std::unique_ptr<GgufFile> GgufFile::open(const std::string& path, std::string* err) {
    auto fail = [&](const std::string& why) -> std::unique_ptr<GgufFile> {
        if (err) *err = why;
        return nullptr;
    };

    auto f = std::unique_ptr<GgufFile>(new GgufFile());
    f->path_ = path;

#ifdef _WIN32
    std::wstring w = widen(path);
    HANDLE h = CreateFileW(w.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return fail("cannot open " + path);
    LARGE_INTEGER sz{};
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart <= 0) {
        CloseHandle(h);
        return fail("cannot size " + path);
    }
    HANDLE m = CreateFileMappingW(h, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (!m) { CloseHandle(h); return fail("cannot map " + path); }
    void* base = MapViewOfFile(m, FILE_MAP_READ, 0, 0, 0);
    if (!base) { CloseHandle(m); CloseHandle(h); return fail("cannot view " + path); }
    f->os_file_ = h;
    f->os_map_ = m;
    f->map_ = static_cast<const uint8_t*>(base);
    f->file_size_ = static_cast<size_t>(sz.QuadPart);
#else
    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) return fail("cannot open " + path);
    struct stat st{};
    if (fstat(fd, &st) != 0 || st.st_size <= 0) { ::close(fd); return fail("cannot size " + path); }
    void* base = mmap(nullptr, static_cast<size_t>(st.st_size), PROT_READ, MAP_PRIVATE, fd, 0);
    if (base == MAP_FAILED) { ::close(fd); return fail("cannot map " + path); }
    f->os_file_ = reinterpret_cast<void*>(static_cast<intptr_t>(fd));
    f->map_ = static_cast<const uint8_t*>(base);
    f->file_size_ = static_cast<size_t>(st.st_size);
#endif

    if (!f->parse(err)) {
        return nullptr;  // destructor unmaps
    }
    return f;
}

bool GgufFile::parse(std::string* err) {
    auto fail = [&](const std::string& why) {
        if (err) *err = why;
        return false;
    };

    Reader r{map_, file_size_};
    uint32_t magic = r.u32();
    if (!r.ok || magic != kMagic) return fail("not a GGUF file");
    uint32_t version = r.u32();
    if (version < 2 || version > 3) return fail("unsupported GGUF version " + std::to_string(version));

    uint64_t n_tensors = r.u64();
    uint64_t n_kv = r.u64();
    if (!r.ok) return fail("truncated header");
    if (n_tensors > 1ull << 20) return fail("implausible tensor count");
    if (n_kv > 1ull << 20) return fail("implausible metadata count");

    kv_.reserve(static_cast<size_t>(n_kv));
    for (uint64_t i = 0; i < n_kv; ++i) {
        std::string key = r.str();
        if (!r.ok) return fail("truncated metadata key");
        MetaValue v;
        if (!parse_value(r, v, 0)) return fail("bad metadata value for " + key);
        kv_.emplace_back(std::move(key), std::move(v));
    }

    // Alignment is declared in metadata; 32 is the specification default.
    if (const MetaValue* a = meta("general.alignment")) {
        uint64_t v = a->as_u64(32);
        if (v >= 1 && v <= (1u << 20) && (v & (v - 1)) == 0) alignment_ = static_cast<uint32_t>(v);
    }

    tensors_.reserve(static_cast<size_t>(n_tensors));
    for (uint64_t i = 0; i < n_tensors; ++i) {
        TensorInfo t;
        t.name = r.str();
        if (!r.ok) return fail("truncated tensor name");
        uint32_t n_dims = r.u32();
        if (!r.ok || n_dims == 0 || n_dims > 4) return fail("bad tensor rank for " + t.name);
        t.ne.resize(n_dims);
        for (uint32_t d = 0; d < n_dims; ++d) {
            t.ne[d] = r.u64();
            if (!r.ok || t.ne[d] == 0 || t.ne[d] > (1ull << 40)) return fail("bad tensor dim for " + t.name);
        }
        t.type = static_cast<GgmlType>(r.u32());
        t.offset = r.u64();
        if (!r.ok) return fail("truncated tensor info for " + t.name);

        int blck = ggml_blck_size(t.type);
        size_t ts = ggml_type_size(t.type);
        if (!blck || !ts) return fail("unsupported tensor type for " + t.name);
        if (t.ne[0] % static_cast<uint64_t>(blck) != 0)
            return fail("tensor " + t.name + " row is not a whole number of blocks");

        size_t row = ggml_row_size(t.type, t.ne[0]);
        uint64_t rows = 1;
        for (size_t d = 1; d < t.ne.size(); ++d) rows *= t.ne[d];
        t.nbytes = row * rows;
        classify_tensor(t);
        tensors_.push_back(std::move(t));
    }

    data_offset_ = align_up(r.off, alignment_);
    if (data_offset_ > file_size_) return fail("data section beyond end of file");
    data_ = map_ + data_offset_;

    for (auto& t : tensors_) {
        if (t.offset > file_size_ - data_offset_ ||
            t.nbytes > file_size_ - data_offset_ - t.offset) {
            return fail("tensor " + t.name + " extends past end of file");
        }
        t.data = data_ + t.offset;
    }
    return true;
}

const TensorInfo* GgufFile::find(const std::string& name) const {
    for (const auto& t : tensors_) if (t.name == name) return &t;
    return nullptr;
}

bool GgufFile::has(const std::string& key) const { return meta(key) != nullptr; }

const MetaValue* GgufFile::meta(const std::string& key) const {
    for (const auto& kv : kv_) if (kv.first == key) return &kv.second;
    return nullptr;
}

uint64_t GgufFile::u64(const std::string& key, uint64_t def) const {
    const MetaValue* v = meta(key);
    return v ? v->as_u64(def) : def;
}

int64_t GgufFile::i64(const std::string& key, int64_t def) const {
    const MetaValue* v = meta(key);
    return v ? v->as_i64(def) : def;
}

double GgufFile::f64(const std::string& key, double def) const {
    const MetaValue* v = meta(key);
    return v ? v->as_f64(def) : def;
}

bool GgufFile::flag(const std::string& key, bool def) const {
    const MetaValue* v = meta(key);
    return v ? v->as_bool(def) : def;
}

std::string GgufFile::str(const std::string& key, const std::string& def) const {
    const MetaValue* v = meta(key);
    return v ? v->as_str(def) : def;
}

} // namespace fish
