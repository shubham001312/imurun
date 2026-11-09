// FISH - load-time calibration support for device re-quantisation.
//
// The planner (LlamaModel::place_layers) may re-quantise device copies to a
// lower-bit format so a model fits the GPU entirely (docs/engine/GPU.md,
// "Device formats"). The re-quantiser is importance-weighted: per input
// column j of every projection it weights the error by E[x_j^2], measured by
// running the CPU forward over the built-in text below. This file holds that
// text, the on-disk cache location/key and the (de)serialisation of the
// measured importance, so the second load of the same model skips both the
// calibration pass and the re-quantisation.
#include "fish/calib.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <system_error>

namespace fish {

// Mixed on purpose: narrative, explanation, code, arithmetic, dialogue,
// lists and a few non-English sentences, so every channel that a chat
// session drives is exercised. ~600 tokens with a Llama-3 vocabulary.
const char* calibration_text() {
    return
        "The lighthouse keeper climbed the spiral stairs every evening at dusk. "
        "From the lantern room he could see the whole bay: the fishing boats "
        "returning with their nets, the gulls circling above the harbour, and "
        "the dark line of the cliffs where the old road ran north towards the "
        "village. One winter night a storm broke the glass of the east window, "
        "and in the morning he found a green bottle wedged between the rocks.\n\n"
        "Photosynthesis converts light energy into chemical energy. In the "
        "chloroplast, chlorophyll absorbs mostly red and blue light; water is "
        "split, oxygen is released, and the energy is stored in ATP and NADPH, "
        "which the Calvin cycle then uses to fix carbon dioxide into sugars.\n\n"
        "def merge_sorted(a, b):\n"
        "    \"\"\"Merge two sorted lists into one sorted list.\"\"\"\n"
        "    i, j, out = 0, 0, []\n"
        "    while i < len(a) and j < len(b):\n"
        "        if a[i] <= b[j]:\n"
        "            out.append(a[i]); i += 1\n"
        "        else:\n"
        "            out.append(b[j]); j += 1\n"
        "    return out + a[i:] + b[j:]\n\n"
        "for (int k = 0; k < n; ++k) { sum += values[k] * weights[k]; }\n\n"
        "Question: A train leaves at 14:35 and arrives at 18:10. How long is the "
        "journey? Answer: from 14:35 to 18:35 is 4 hours, minus 25 minutes, so "
        "3 hours and 35 minutes. Check: 14:35 + 3:35 = 18:10. If 12 apples cost "
        "3.60 dollars, one apple costs 0.30 dollars and 7 apples cost 2.10.\n\n"
        "User: Can you help me write a polite email asking my landlord to fix "
        "the heating?\nAssistant: Of course. Subject: Heating repair request. "
        "Dear Ms. Patel, I hope you are well. Since Monday the radiators in the "
        "flat have not been warming up, even with the thermostat at 22 degrees. "
        "Could you arrange for someone to look at the boiler this week? I am at "
        "home most afternoons. Thank you for your help. Kind regards, Sam.\n\n"
        "Key points of the meeting:\n- Launch moved to 14 March.\n- Budget "
        "approved at 120,000 EUR.\n- Next review: Friday, 10:00, room B2.\n\n"
        "La ciudad despertaba lentamente mientras el sol iluminaba los tejados. "
        "Die Forschung zeigt, dass regelmaessige Bewegung das Gedaechtnis "
        "verbessert. Le chat dormait sur le rebord de la fenetre.\n\n"
        "In 1969 the Apollo 11 mission landed on the Moon. Neil Armstrong and "
        "Buzz Aldrin spent about two and a half hours outside the lunar module, "
        "collecting 21.5 kilograms of samples, while Michael Collins orbited "
        "above in the command module Columbia.\n\n"
        "To reverse a linked list, keep three pointers: previous, current and "
        "next. At each step save current.next, point current.next at previous, "
        "then advance previous and current. When current becomes null, previous "
        "is the new head. The algorithm runs in O(n) time and O(1) space.\n";
}

namespace {

uint64_t fnv1a(const uint8_t* p, size_t n, uint64_t h) {
    for (size_t i = 0; i < n; ++i) { h ^= p[i]; h *= 1099511628211ull; }
    return h;
}

std::string home_dir() {
#ifdef _WIN32
    if (const char* u = std::getenv("USERPROFILE")) if (*u) return u;
#endif
    if (const char* h = std::getenv("HOME")) if (*h) return h;
    return std::string();
}

} // namespace

std::string device_cache_dir(const GgufFile& g, const std::string& variant) {
    if (const char* e = std::getenv("FISH_GPU_CACHE")) {
        if (std::strcmp(e, "0") == 0 || std::strcmp(e, "off") == 0) return std::string();
    }
    std::filesystem::path root;
    if (const char* e = std::getenv("FISH_CACHE_DIR")) {
        if (*e) root = std::filesystem::path(e);
    }
    if (root.empty()) {
        const std::string home = home_dir();
        if (home.empty()) return std::string();
        root = std::filesystem::path(home) / ".imurun" / "cache" / "fish";
    }
    // Key: file size + a hash of the header region (metadata and tensor table)
    // and of the last MiB (the tail of the weights) + the variant string
    // (adapter, format version). A re-downloaded identical file maps to the
    // same directory; a different quantisation does not.
    const uint8_t* base = g.data_base() ? g.data_base() - g.data_offset() : nullptr;
    const size_t size = g.file_size();
    uint64_t h = 1469598103934665603ull;
    if (base && size > 0) {
        const size_t head = std::min<size_t>(size, 1u << 20);
        h = fnv1a(base, head, h);
        const size_t tail = std::min<size_t>(size, 1u << 20);
        h = fnv1a(base + size - tail, tail, h);
    }
    h = fnv1a(reinterpret_cast<const uint8_t*>(variant.data()), variant.size(), h);
    std::string stem = std::filesystem::path(g.path()).stem().string();
    if (stem.size() > 64) stem.resize(64);
    for (char& c : stem) if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_' || c == '.')) c = '_';
    char key[64];
    std::snprintf(key, sizeof(key), "-%zu-%016llx", size, static_cast<unsigned long long>(h));
    const std::filesystem::path dir = root / (stem + key);
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    if (ec) return std::string();
    return dir.string();
}

std::string cache_file_name(const std::string& tensor, const char* fmt) {
    std::string s;
    for (char c : tensor) s += (std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '_') ? c : '_';
    return s + "." + fmt + ".dev";
}

// File: "FISHIMX1", u32 version, u32 tokens, u32 count, then per entry
// u32 name length, name bytes, u32 n, n floats.
bool importance_save(const std::string& path, const Importance& imp, int tokens) {
    if (path.empty()) return false;
    const std::string tmp = path + ".tmp";
    FILE* f = std::fopen(tmp.c_str(), "wb");
    if (!f) return false;
    bool ok = std::fwrite("FISHIMX1", 1, 8, f) == 8;
    const uint32_t hdr[3] = {1u, static_cast<uint32_t>(tokens), static_cast<uint32_t>(imp.size())};
    ok = ok && std::fwrite(hdr, sizeof(hdr), 1, f) == 1;
    for (const auto& kv : imp) {
        if (!ok) break;
        const uint32_t ln = static_cast<uint32_t>(kv.first.size()), n = static_cast<uint32_t>(kv.second.size());
        ok = std::fwrite(&ln, 4, 1, f) == 1 && std::fwrite(kv.first.data(), 1, ln, f) == ln &&
             std::fwrite(&n, 4, 1, f) == 1 && std::fwrite(kv.second.data(), sizeof(float), n, f) == n;
    }
    ok = (std::fclose(f) == 0) && ok;
    if (!ok) { std::remove(tmp.c_str()); return false; }
    std::remove(path.c_str());
    if (std::rename(tmp.c_str(), path.c_str()) != 0) { std::remove(tmp.c_str()); return false; }
    return true;
}

bool importance_load(const std::string& path, Importance* imp, int* tokens) {
    if (path.empty()) return false;
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    char magic[8];
    uint32_t hdr[3];
    bool ok = std::fread(magic, 1, 8, f) == 8 && std::memcmp(magic, "FISHIMX1", 8) == 0 &&
              std::fread(hdr, sizeof(hdr), 1, f) == 1 && hdr[0] == 1u && hdr[2] < 100000u;
    Importance out;
    for (uint32_t i = 0; ok && i < hdr[2]; ++i) {
        uint32_t ln = 0, n = 0;
        ok = std::fread(&ln, 4, 1, f) == 1 && ln < 4096;
        std::string name(ok ? ln : 0, '\0');
        ok = ok && std::fread(&name[0], 1, ln, f) == ln && std::fread(&n, 4, 1, f) == 1 && n <= (1u << 20);
        std::vector<float> v(ok ? n : 0);
        ok = ok && std::fread(v.data(), sizeof(float), n, f) == n;
        if (ok) out.emplace(std::move(name), std::move(v));
    }
    std::fclose(f);
    if (!ok) return false;
    *imp = std::move(out);
    if (tokens) *tokens = static_cast<int>(hdr[1]);
    return true;
}

} // namespace fish
