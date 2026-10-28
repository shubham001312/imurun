/*
 * FISH — byte-level BPE tokenizer.
 */
#include <fish/tokenizer.h>

#include <algorithm>
#include <array>
#include <climits>
#include <cstring>

namespace fish {

namespace {

// ---------------------------------------------------------------- UTF-8

uint32_t next_cp(const std::string& s, size_t& i) {
    if (i >= s.size()) return 0;
    const unsigned char c = static_cast<unsigned char>(s[i]);
    if (c < 0x80) { ++i; return c; }
    int len = 1;
    if ((c & 0xE0) == 0xC0) len = 2;
    else if ((c & 0xF0) == 0xE0) len = 3;
    else if ((c & 0xF8) == 0xF0) len = 4;
    if (i + static_cast<size_t>(len) > s.size()) { ++i; return c; }
    uint32_t v = c & (0xFFu >> (len + 1));
    for (int k = 1; k < len; ++k)
        v = (v << 6) | (static_cast<unsigned char>(s[i + k]) & 0x3Fu);
    i += static_cast<size_t>(len);
    return v;
}

void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) out += static_cast<char>(cp);
    else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

// ---------------------------------------------------------------- GPT-2 alphabet

struct ByteAlphabet {
    std::array<std::string, 256> enc;
    std::unordered_map<uint32_t, uint8_t> dec;
};

const ByteAlphabet& alphabet() {
    static const ByteAlphabet a = [] {
        ByteAlphabet b;
        std::array<bool, 256> kept{};
        for (int c = '!'; c <= '~'; ++c) kept[static_cast<size_t>(c)] = true;
        for (int c = 0xA1; c <= 0xAC; ++c) kept[static_cast<size_t>(c)] = true;
        for (int c = 0xAE; c <= 0xFF; ++c) kept[static_cast<size_t>(c)] = true;
        int n = 0;
        for (int byte = 0; byte < 256; ++byte) {
            uint32_t cp;
            if (kept[static_cast<size_t>(byte)]) cp = static_cast<uint32_t>(byte);
            else cp = static_cast<uint32_t>(256 + n++);
            append_utf8(b.enc[static_cast<size_t>(byte)], cp);
            b.dec[cp] = static_cast<uint8_t>(byte);
        }
        return b;
    }();
    return a;
}

// ---------------------------------------------------------------- classification

bool is_space_cp(uint32_t c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v' ||
           c == 0x00A0 || (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 ||
           c == 0x202F || c == 0x205F || c == 0x3000;
}

bool is_digit_cp(uint32_t c) { return c >= '0' && c <= '9'; }

// Unicode letter test. ASCII is exact; above ASCII we treat most codepoints as
// letters except the clearly non-letter blocks. This keeps the llama-bpe
// pre-tokenizer's word boundaries right for Latin, Devanagari, Thai and CJK.
bool is_letter_cp(uint32_t c) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) return true;
    if (c < 0x80) return false;
    if (c >= 0x2000 && c <= 0x2BFF) return false;  // punctuation, symbols, arrows, math
    if (c >= 0x3000 && c <= 0x303F) return false;  // CJK punctuation
    if (c >= 0xFE00 && c <= 0xFE0F) return false;  // variation selectors
    return true;
}

bool ieq(char a, char b) {
    const char la = (a >= 'A' && a <= 'Z') ? static_cast<char>(a + 32) : a;
    const char lb = (b >= 'A' && b <= 'Z') ? static_cast<char>(b + 32) : b;
    return la == lb;
}

} // namespace

// ---------------------------------------------------------------- create

Tokenizer::~Tokenizer() = default;

std::unique_ptr<Tokenizer> Tokenizer::create(const GgufFile& g, std::string* err) {
    auto fail = [&](const std::string& why) -> std::unique_ptr<Tokenizer> {
        if (err) *err = why;
        return nullptr;
    };

    const MetaValue* toks = g.meta("tokenizer.ggml.tokens");
    if (!toks || toks->sarr.empty()) return fail("tokenizer.ggml.tokens missing");

    std::unique_ptr<Tokenizer> t(new Tokenizer());
    t->id_to_token_ = toks->sarr;
    t->token_to_id_.reserve(t->id_to_token_.size() * 2);
    for (size_t i = 0; i < t->id_to_token_.size(); ++i)
        t->token_to_id_.emplace(t->id_to_token_[i], static_cast<int32_t>(i));

    t->bos_ = static_cast<int32_t>(g.i64("tokenizer.ggml.bos_token_id", 1));
    t->eos_ = static_cast<int32_t>(g.i64("tokenizer.ggml.eos_token_id", 2));

    // ---- merges
    if (const MetaValue* m = g.meta("tokenizer.ggml.merges")) {
        t->merges_.reserve(m->sarr.size() * 2);
        for (size_t rank = 0; rank < m->sarr.size(); ++rank) {
            const std::string& line = m->sarr[rank];
            const size_t sp = line.find(' ');
            if (sp == std::string::npos) continue;
            const std::string a = line.substr(0, sp);
            const std::string b = line.substr(sp + 1);
            auto ia = t->token_to_id_.find(a);
            auto ib = t->token_to_id_.find(b);
            auto iab = t->token_to_id_.find(a + b);
            if (ia == t->token_to_id_.end() || ib == t->token_to_id_.end() ||
                iab == t->token_to_id_.end())
                continue;
            const uint64_t key = (static_cast<uint64_t>(static_cast<uint32_t>(ia->second)) << 32) |
                                 static_cast<uint32_t>(ib->second);
            t->merges_.emplace(key, Merge{static_cast<int>(rank), iab->second});
        }
    }

    // ---- special tokens: control (3) and user-defined (4)
    const MetaValue* types = g.meta("tokenizer.ggml.token_type");
    for (size_t i = 0; i < t->id_to_token_.size(); ++i) {
        bool special = false;
        if (types && i < types->iarr.size()) {
            const int32_t ty = types->iarr[i];
            special = (ty == 3 || ty == 4);
        }
        if (!special) {
            const std::string& s = t->id_to_token_[i];
            if (s.size() >= 4 && s[0] == '<' && s[1] == '|' && s[s.size() - 1] == '>' &&
                s[s.size() - 2] == '|')
                special = true;
        }
        if (special) t->specials_.emplace_back(t->id_to_token_[i], static_cast<int32_t>(i));
    }
    std::sort(t->specials_.begin(), t->specials_.end(),
              [](const auto& a, const auto& b) { return a.first.size() > b.first.size(); });

    // ---- chat format
    auto has = [&](const char* s) { return t->token_to_id_.count(s) != 0; };
    if (has("<|start_header_id|>") && has("<|end_header_id|>")) t->format_ = ChatFormat::Llama3;
    else if (has("<|im_start|>")) t->format_ = ChatFormat::ChatML;
    else if (has("<|user|>") && has("<|end|>")) t->format_ = ChatFormat::Phi3;
    else if (has("<start_of_turn>")) t->format_ = ChatFormat::Gemma;
    else t->format_ = ChatFormat::Plain;

    return t;
}

// ---------------------------------------------------------------- pre-tokenizer

std::vector<std::string> Tokenizer::split_pre_tokens(const std::string& text) const {
    std::vector<std::string> out;
    const size_t n = text.size();
    size_t i = 0;

    auto emit = [&](size_t a, size_t b) {
        if (b > a) out.push_back(text.substr(a, b - a));
    };

    while (i < n) {
        size_t t = i;
        const uint32_t c = next_cp(text, t);

        // 1. contractions
        if (c == '\'') {
            static const char* kContr[] = {"'s", "'t", "'re", "'ve", "'m", "'ll", "'d"};
            bool matched = false;
            for (const char* contr : kContr) {
                const size_t L = std::strlen(contr);
                if (i + L > n) continue;
                bool eq = true;
                for (size_t k = 0; k < L; ++k)
                    if (!ieq(text[i + k], contr[k])) { eq = false; break; }
                if (eq) { emit(i, i + L); i += L; matched = true; break; }
            }
            if (matched) continue;
        }

        // 2. [^\r\n L N]? L+
        {
            size_t after = i;
            const uint32_t c0 = next_cp(text, after);
            if (is_letter_cp(c0)) {
                size_t end = i;
                while (end < n) {
                    size_t r = end;
                    if (!is_letter_cp(next_cp(text, r))) break;
                    end = r;
                }
                emit(i, end);
                i = end;
                continue;
            }
            if (c0 != '\r' && c0 != '\n' && !is_digit_cp(c0) && after < n) {
                size_t after2 = after;
                if (is_letter_cp(next_cp(text, after2))) {
                    size_t end = after;
                    while (end < n) {
                        size_t r = end;
                        if (!is_letter_cp(next_cp(text, r))) break;
                        end = r;
                    }
                    emit(i, end);
                    i = end;
                    continue;
                }
            }
        }

        // 3. digits, at most three
        if (is_digit_cp(c)) {
            size_t end = i;
            int count = 0;
            while (end < n && count < 3) {
                size_t r = end;
                if (!is_digit_cp(next_cp(text, r))) break;
                end = r;
                ++count;
            }
            emit(i, end);
            i = end;
            continue;
        }

        // 4. optional space + punctuation run + trailing newlines
        {
            size_t start = i;
            size_t after = i;
            if (next_cp(text, after) == ' ') start = after;
            if (start < n) {
                size_t r = start;
                const uint32_t cc = next_cp(text, r);
                if (!is_space_cp(cc) && !is_letter_cp(cc) && !is_digit_cp(cc)) {
                    size_t end = start;
                    while (end < n) {
                        size_t s = end;
                        const uint32_t x = next_cp(text, s);
                        if (is_space_cp(x) || is_letter_cp(x) || is_digit_cp(x)) break;
                        end = s;
                    }
                    while (end < n) {
                        size_t s = end;
                        const uint32_t x = next_cp(text, s);
                        if (x != '\r' && x != '\n') break;
                        end = s;
                    }
                    emit(i, end);
                    i = end;
                    continue;
                }
            }
        }

        // 5-7. whitespace
        if (is_space_cp(c)) {
            size_t end = i;
            bool has_newline = false;
            while (end < n) {
                size_t s = end;
                const uint32_t x = next_cp(text, s);
                if (!is_space_cp(x)) break;
                if (x == '\n') has_newline = true;
                end = s;
            }
            if (has_newline) {
                size_t last_nl = i;
                size_t p = i;
                while (p < end) {
                    size_t s = p;
                    if (next_cp(text, s) == '\n') last_nl = s;
                    p = s;
                }
                emit(i, last_nl);
                i = last_nl;
            } else {
                emit(i, end);
                i = end;
            }
            continue;
        }

        // fallback: one codepoint
        emit(i, t);
        i = t;
    }
    return out;
}

// ---------------------------------------------------------------- BPE

std::vector<int32_t> Tokenizer::encode_plain(const std::string& text) const {
    std::vector<int32_t> ids;
    const ByteAlphabet& ab = alphabet();

    for (const std::string& pre : split_pre_tokens(text)) {
        std::string enc;
        enc.reserve(pre.size() * 2);
        for (unsigned char b : pre) enc += ab.enc[b];

        std::vector<int32_t> syms;
        size_t i = 0;
        while (i < enc.size()) {
            size_t j = i;
            next_cp(enc, j);
            auto it = token_to_id_.find(enc.substr(i, j - i));
            if (it != token_to_id_.end()) {
                syms.push_back(it->second);
            } else {
                // Unknown symbol: emit each byte token separately.
                for (size_t k = i; k < j; ++k) {
                    auto bt = token_to_id_.find(ab.enc[static_cast<unsigned char>(enc[k])]);
                    if (bt != token_to_id_.end()) syms.push_back(bt->second);
                }
            }
            i = j;
        }

        while (syms.size() > 1) {
            int best_rank = INT_MAX;
            size_t best_pos = syms.size();
            int32_t best_id = -1;
            for (size_t k = 0; k + 1 < syms.size(); ++k) {
                const uint64_t key =
                    (static_cast<uint64_t>(static_cast<uint32_t>(syms[k])) << 32) |
                    static_cast<uint32_t>(syms[k + 1]);
                auto m = merges_.find(key);
                if (m != merges_.end() && m->second.rank < best_rank) {
                    best_rank = m->second.rank;
                    best_pos = k;
                    best_id = m->second.id;
                }
            }
            if (best_pos == syms.size()) break;
            syms[best_pos] = best_id;
            syms.erase(syms.begin() + static_cast<long>(best_pos) + 1);
        }
        ids.insert(ids.end(), syms.begin(), syms.end());
    }
    return ids;
}

std::vector<int32_t> Tokenizer::encode(const std::string& text, bool add_bos) const {
    std::vector<int32_t> ids;
    if (add_bos) ids.push_back(bos_);

    size_t i = 0;
    size_t plain_from = 0;
    while (i < text.size()) {
        bool matched = false;
        for (const auto& sp : specials_) {
            if (sp.first.empty()) continue;
            if (i + sp.first.size() > text.size()) continue;
            if (text.compare(i, sp.first.size(), sp.first) != 0) continue;
            if (i > plain_from) {
                auto part = encode_plain(text.substr(plain_from, i - plain_from));
                ids.insert(ids.end(), part.begin(), part.end());
            }
            ids.push_back(sp.second);
            i += sp.first.size();
            plain_from = i;
            matched = true;
            break;
        }
        if (matched) continue;
        // advance one codepoint
        next_cp(text, i);
    }
    if (plain_from < text.size()) {
        auto part = encode_plain(text.substr(plain_from));
        ids.insert(ids.end(), part.begin(), part.end());
    }
    return ids;
}

// ---------------------------------------------------------------- decode

std::string Tokenizer::decode(const std::vector<int32_t>& ids) const {
    std::string raw;
    for (int32_t id : ids) {
        if (id >= 0 && static_cast<size_t>(id) < id_to_token_.size())
            raw += id_to_token_[static_cast<size_t>(id)];
    }
    const ByteAlphabet& ab = alphabet();
    std::string out;
    size_t i = 0;
    while (i < raw.size()) {
        const size_t start = i;
        const uint32_t cp = next_cp(raw, i);
        auto it = ab.dec.find(cp);
        if (it != ab.dec.end()) out += static_cast<char>(it->second);
        else out.append(raw, start, i - start);
    }
    return out;
}

std::string Tokenizer::token_text(int32_t id) const {
    if (id < 0 || static_cast<size_t>(id) >= id_to_token_.size()) return std::string();
    return decode(std::vector<int32_t>{id});
}

bool Tokenizer::is_eog(int32_t id) const {
    if (id == eos_) return true;
    if (id < 0 || static_cast<size_t>(id) >= id_to_token_.size()) return false;
    const std::string& s = id_to_token_[static_cast<size_t>(id)];
    return s == "<|eot_id|>" || s == "<|end_of_text|>" || s == "<|im_end|>" ||
           s == "<|end|>" || s == "<end_of_turn>";
}

// ---------------------------------------------------------------- chat

std::string Tokenizer::apply_chat(
        const std::vector<std::pair<std::string, std::string>>& messages,
        ChatFormat format) const {
    std::string out;
    switch (format) {
        case ChatFormat::Llama3:
            out += "<|begin_of_text|>";
            for (const auto& m : messages) {
                out += "<|start_header_id|>" + m.first + "<|end_header_id|>\n\n" +
                       m.second + "<|eot_id|>";
            }
            out += "<|start_header_id|>assistant<|end_header_id|>\n\n";
            break;

        case ChatFormat::ChatML:
            for (const auto& m : messages)
                out += "<|im_start|>" + m.first + "\n" + m.second + "<|im_end|>\n";
            out += "<|im_start|>assistant\n";
            break;

        case ChatFormat::Phi3:
            for (const auto& m : messages)
                out += "<|" + m.first + "|>\n" + m.second + "<|end|>\n";
            out += "<|assistant|>\n";
            break;

        case ChatFormat::Gemma:
            for (const auto& m : messages) {
                const std::string role = (m.first == "assistant") ? "model" : "user";
                out += "<start_of_turn>" + role + "\n" + m.second + "<end_of_turn>\n";
            }
            out += "<start_of_turn>model\n";
            break;

        case ChatFormat::Plain:
        default:
            for (const auto& m : messages) {
                std::string role = m.first;
                if (!role.empty() && role[0] >= 'a' && role[0] <= 'z')
                    role[0] = static_cast<char>(role[0] - 32);
                out += role + ": " + m.second + "\n\n";
            }
            out += "Assistant:";
            break;
    }
    return out;
}

} // namespace fish
