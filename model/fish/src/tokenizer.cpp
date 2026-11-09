/*
 * FISH — tokenizer implementation (byte-level BPE and SentencePiece).
 */
#include <fish/tokenizer.h>

#include <algorithm>
#include <array>
#include <climits>
#include <cstdio>
#include <cstring>
#include <queue>

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
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
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
           c == 0x0085 || c == 0x00A0 || c == 0x1680 ||
           (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 ||
           c == 0x202F || c == 0x205F || c == 0x3000;
}

bool is_digit_cp(uint32_t c) {
    if (c >= '0' && c <= '9') return true;
    // Common non-ASCII decimal digit blocks (Arabic-Indic, Devanagari, Bengali,
    // fullwidth) and superscripts/fractions in Latin-1.
    if (c >= 0x0660 && c <= 0x0669) return true;
    if (c >= 0x06F0 && c <= 0x06F9) return true;
    if (c >= 0x0966 && c <= 0x096F) return true;
    if (c >= 0x09E6 && c <= 0x09EF) return true;
    if (c >= 0xFF10 && c <= 0xFF19) return true;
    if (c == 0xB2 || c == 0xB3 || c == 0xB9 || (c >= 0xBC && c <= 0xBE)) return true;
    return false;
}

// Unicode letter test. ASCII is exact; above ASCII we treat most codepoints as
// letters except the clearly non-letter blocks: Latin-1 punctuation and
// symbols, general punctuation, symbols, arrows, math, box drawing, CJK
// punctuation, emoji. This keeps the pre-tokenizer's word boundaries right for
// Latin, Devanagari, Thai, CJK and emoji-laden chat.
bool is_letter_cp(uint32_t c) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) return true;
    if (c < 0x80) return false;
    if (c < 0xC0) return c == 0xAA || c == 0xB5 || c == 0xBA;  // ª µ º
    if (c == 0xD7 || c == 0xF7) return false;                   // × ÷
    if (is_digit_cp(c)) return false;
    if (c >= 0x2000 && c <= 0x2BFF) return false;  // punctuation, symbols, arrows, math, box
    if (c >= 0x3000 && c <= 0x303F) return false;  // CJK punctuation
    if (c >= 0xFE00 && c <= 0xFE0F) return false;  // variation selectors
    if (c >= 0xFF00 && c <= 0xFF20) return false;  // fullwidth punctuation
    if (c >= 0x1F000 && c <= 0x1FAFF) return false; // emoji & pictographs
    if (c >= 0xE000 && c <= 0xF8FF) return false;  // private use
    return true;
}

bool ieq(char a, char b) {
    const char la = (a >= 'A' && a <= 'Z') ? static_cast<char>(a + 32) : a;
    const char lb = (b >= 'A' && b <= 'Z') ? static_cast<char>(b + 32) : b;
    return la == lb;
}

bool contains(const std::string& hay, const char* needle) {
    return hay.find(needle) != std::string::npos;
}

const char* kSpmSpace = "\xE2\x96\x81";  // U+2581 "▁"

} // namespace

const char* chat_format_name(ChatFormat f) {
    switch (f) {
        case ChatFormat::Llama3:   return "llama3";
        case ChatFormat::ChatML:   return "chatml";
        case ChatFormat::Phi3:     return "phi3";
        case ChatFormat::Gemma:    return "gemma";
        case ChatFormat::Mistral:  return "mistral";
        case ChatFormat::DeepSeek: return "deepseek";
        default:                   return "plain";
    }
}

// ---------------------------------------------------------------- create

Tokenizer::~Tokenizer() = default;

int32_t Tokenizer::lookup(const std::string& s) const {
    auto it = token_to_id_.find(s);
    return it == token_to_id_.end() ? -1 : it->second;
}

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

    const std::string model = g.str("tokenizer.ggml.model", "gpt2");
    if (model == "llama") t->kind_ = VocabKind::SPM;
    else if (model == "gpt2" || model == "bpe") t->kind_ = VocabKind::BPE;
    else return fail("unsupported tokenizer model: " + model);

    const int64_t n_vocab = static_cast<int64_t>(t->id_to_token_.size());
    auto id_or = [&](const char* key, int32_t def) {
        const int64_t v = g.i64(key, def);
        return (v >= 0 && v < n_vocab) ? static_cast<int32_t>(v) : def;
    };
    t->bos_ = id_or("tokenizer.ggml.bos_token_id", t->kind_ == VocabKind::SPM ? 1 : -1);
    t->eos_ = id_or("tokenizer.ggml.eos_token_id", t->kind_ == VocabKind::SPM ? 2 : -1);
    t->unk_ = id_or("tokenizer.ggml.unknown_token_id", 0);
    t->add_bos_ = g.flag("tokenizer.ggml.add_bos_token", t->bos_ >= 0);
    if (t->bos_ < 0) t->add_bos_ = false;
    t->add_space_prefix_ = g.flag("tokenizer.ggml.add_space_prefix", true);

    // ---- end-of-generation set
    if (t->eos_ >= 0) t->eog_.insert(t->eos_);
    for (const char* key : {"tokenizer.ggml.eot_token_id", "tokenizer.ggml.eom_token_id"}) {
        const int32_t id = id_or(key, -1);
        if (id >= 0) t->eog_.insert(id);
    }
    for (const char* s : {"<|eot_id|>", "<|end_of_text|>", "<|im_end|>", "<|end|>",
                          "<|endoftext|>", "<end_of_turn>", "<|eom_id|>",
                          "<\xEF\xBD\x9C" "end\xE2\x96\x81" "of\xE2\x96\x81" "sentence\xEF\xBD\x9C>"}) {
        const int32_t id = t->lookup(s);
        if (id >= 0) t->eog_.insert(id);
    }

    // ---- pre-tokenizer flavour (BPE only)
    const std::string pre = g.str("tokenizer.ggml.pre", "");
    if (pre == "llama3" || pre == "llama-bpe" || pre == "smollm" || pre == "tekken" ||
        pre == "phi-4" || pre == "falcon3" || pre == "exaone")
        t->digits_per_token_ = 3;
    else if (pre == "qwen2" || pre == "qwen35" || pre == "deepseek-llm" || pre == "deepseek-coder" ||
             pre == "deepseek-v3" || pre == "stablelm2" || pre == "command-r" ||
             pre == "olmo" || pre == "dbrx" || pre == "chatglm-bpe" || pre == "glm4")
        t->digits_per_token_ = 1;
    else if (pre == "gpt-2" || pre == "starcoder" || pre == "refact" || pre == "jina-v2-en")
        t->digits_per_token_ = 0;  // \p{N}+
    else
        t->digits_per_token_ = 3;

    // ---- merges (BPE)
    if (const MetaValue* m = g.meta("tokenizer.ggml.merges")) {
        t->merges_.reserve(m->sarr.size() * 2);
        for (size_t rank = 0; rank < m->sarr.size(); ++rank) {
            const std::string& line = m->sarr[rank];
            const size_t sp = line.find(' ');
            if (sp == std::string::npos) continue;
            const std::string a = line.substr(0, sp);
            const std::string b = line.substr(sp + 1);
            const int32_t ia = t->lookup(a), ib = t->lookup(b), iab = t->lookup(a + b);
            if (ia < 0 || ib < 0 || iab < 0) continue;
            const uint64_t key = (static_cast<uint64_t>(static_cast<uint32_t>(ia)) << 32) |
                                 static_cast<uint32_t>(ib);
            t->merges_.emplace(key, Merge{static_cast<int>(rank), iab});
        }
    }
    if (t->kind_ == VocabKind::BPE && t->merges_.empty())
        return fail("tokenizer.ggml.merges missing for a BPE vocabulary");

    // ---- scores (SPM)
    if (const MetaValue* scores = g.meta("tokenizer.ggml.scores")) {
        if (!scores->farr.empty()) t->scores_ = scores->farr;
    }
    if (t->kind_ == VocabKind::SPM && t->scores_.size() != t->id_to_token_.size())
        t->scores_.assign(t->id_to_token_.size(), 0.0f);

    // ---- token types; special tokens: control (3) and user-defined (4)
    const MetaValue* types = g.meta("tokenizer.ggml.token_type");
    t->token_type_.assign(t->id_to_token_.size(), 1);
    if (types) {
        for (size_t i = 0; i < t->id_to_token_.size() && i < types->iarr.size(); ++i)
            t->token_type_[i] = types->iarr[i];
    }
    for (size_t i = 0; i < t->id_to_token_.size(); ++i) {
        bool special = t->token_type_[i] == 3 || t->token_type_[i] == 4;
        if (!special && !types) {
            const std::string& s = t->id_to_token_[i];
            if (s.size() >= 4 && s[0] == '<' && s[1] == '|' && s[s.size() - 1] == '>' &&
                s[s.size() - 2] == '|')
                special = true;
        }
        if (special && !t->id_to_token_[i].empty())
            t->specials_.emplace_back(t->id_to_token_[i], static_cast<int32_t>(i));
    }
    std::sort(t->specials_.begin(), t->specials_.end(),
              [](const auto& a, const auto& b) { return a.first.size() > b.first.size(); });

    // ---- SPM byte fallback tokens
    if (t->kind_ == VocabKind::SPM) {
        t->byte_tokens_.assign(256, -1);
        char name[8];
        for (int b = 0; b < 256; ++b) {
            std::snprintf(name, sizeof(name), "<0x%02X>", b);
            t->byte_tokens_[static_cast<size_t>(b)] = t->lookup(name);
        }
    }

    // ---- chat format: the model's own template is the ground truth; token
    // presence is the fallback for files that do not ship one.
    const std::string tmpl = g.str("tokenizer.chat_template", "");
    auto has = [&](const char* s) { return t->lookup(s) >= 0; };
    if (contains(tmpl, "<|start_header_id|>")) t->format_ = ChatFormat::Llama3;
    else if (contains(tmpl, "<|im_start|>")) t->format_ = ChatFormat::ChatML;
    else if (contains(tmpl, "<start_of_turn>")) t->format_ = ChatFormat::Gemma;
    else if (contains(tmpl, "<|user|>") && contains(tmpl, "<|end|>")) t->format_ = ChatFormat::Phi3;
    else if (contains(tmpl, "User\xEF\xBD\x9C>")) t->format_ = ChatFormat::DeepSeek;
    else if (contains(tmpl, "[INST]")) t->format_ = ChatFormat::Mistral;
    else if (has("<|start_header_id|>") && has("<|end_header_id|>")) t->format_ = ChatFormat::Llama3;
    else if (has("<|im_start|>")) t->format_ = ChatFormat::ChatML;
    else if (has("<|user|>") && has("<|end|>")) t->format_ = ChatFormat::Phi3;
    else if (has("<start_of_turn>")) t->format_ = ChatFormat::Gemma;
    else if (has("<\xEF\xBD\x9C" "User\xEF\xBD\x9C>")) t->format_ = ChatFormat::DeepSeek;
    else if (has("[INST]")) t->format_ = ChatFormat::Mistral;
    else t->format_ = ChatFormat::Plain;

    return t;
}

// ---------------------------------------------------------------- pre-tokenizer (BPE)

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

        // 3. digits: \p{N}{1,3} (llama3), \p{N} (qwen2), \p{N}+ (gpt2)
        if (is_digit_cp(c)) {
            size_t end = i;
            int count = 0;
            while (end < n && (digits_per_token_ == 0 || count < digits_per_token_)) {
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

        // 5-7. whitespace:  \s*[\r\n]+  |  \s+(?!\S)  |  \s+
        if (is_space_cp(c)) {
            size_t end = i;
            size_t last_ws_start = i;   // start of the final whitespace char
            size_t last_nl_end = 0;     // one past the last newline, 0 = none
            while (end < n) {
                size_t s = end;
                const uint32_t x = next_cp(text, s);
                if (!is_space_cp(x)) break;
                if (x == '\n' || x == '\r') last_nl_end = s;
                last_ws_start = end;
                end = s;
            }
            if (last_nl_end > 0) {
                // \s*[\r\n]+ : up to and including the last newline; any
                // spaces after it start the next piece.
                emit(i, last_nl_end);
                i = last_nl_end;
            } else if (end < n && last_ws_start > i) {
                // \s+(?!\S): leave the last space to attach to the next word.
                emit(i, last_ws_start);
                i = last_ws_start;
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

std::vector<int32_t> Tokenizer::encode_bpe_piece(const std::string& pre) const {
    const ByteAlphabet& ab = alphabet();
    std::string enc;
    enc.reserve(pre.size() * 2);
    for (unsigned char b : pre) enc += ab.enc[b];

    std::vector<int32_t> syms;
    size_t i = 0;
    while (i < enc.size()) {
        size_t j = i;
        next_cp(enc, j);
        const int32_t id = lookup(enc.substr(i, j - i));
        if (id >= 0) {
            syms.push_back(id);
        } else {
            // Unknown symbol: emit each byte token separately.
            for (size_t k = i; k < j; ++k) {
                const int32_t bt = lookup(ab.enc[static_cast<unsigned char>(enc[k])]);
                if (bt >= 0) syms.push_back(bt);
            }
        }
        i = j;
    }

    // Merge by rank. A pre-token is short (one word), so the quadratic
    // scan is cheaper than a heap.
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
    return syms;
}

// ---------------------------------------------------------------- SentencePiece

std::vector<int32_t> Tokenizer::encode_spm(const std::string& raw) const {
    // Spaces become U+2581; a leading one marks the start of a word.
    std::string text;
    text.reserve(raw.size() + 8);
    if (add_space_prefix_ && !raw.empty()) text += kSpmSpace;
    for (char ch : raw) {
        if (ch == ' ') text += kSpmSpace;
        else text += ch;
    }

    struct Sym { int prev, next; size_t off, len; };
    std::vector<Sym> syms;
    {
        size_t i = 0;
        while (i < text.size()) {
            size_t j = i;
            next_cp(text, j);
            Sym s;
            s.off = i;
            s.len = j - i;
            s.prev = static_cast<int>(syms.size()) - 1;
            s.next = (j < text.size()) ? static_cast<int>(syms.size()) + 1 : -1;
            syms.push_back(s);
            i = j;
        }
    }
    std::vector<int32_t> out;
    if (syms.empty()) return out;

    struct Bigram { int left, right; float score; size_t size; int32_t id; };
    struct Cmp {
        bool operator()(const Bigram& a, const Bigram& b) const {
            if (a.score != b.score) return a.score < b.score;   // higher score first
            return a.left > b.left;                             // then leftmost
        }
    };
    std::priority_queue<Bigram, std::vector<Bigram>, Cmp> heap;

    auto try_add = [&](int left, int right) {
        if (left < 0 || right < 0) return;
        const Sym& a = syms[static_cast<size_t>(left)];
        const Sym& b = syms[static_cast<size_t>(right)];
        const std::string merged = text.substr(a.off, a.len + b.len);
        const int32_t id = lookup(merged);
        if (id < 0 || static_cast<size_t>(id) >= scores_.size()) return;
        heap.push(Bigram{left, right, scores_[static_cast<size_t>(id)], a.len + b.len, id});
    };
    for (size_t i = 1; i < syms.size(); ++i) try_add(static_cast<int>(i) - 1, static_cast<int>(i));

    while (!heap.empty()) {
        const Bigram bg = heap.top();
        heap.pop();
        Sym& l = syms[static_cast<size_t>(bg.left)];
        Sym& r = syms[static_cast<size_t>(bg.right)];
        if (l.len == 0 || r.len == 0 || l.len + r.len != bg.size) continue;  // stale
        l.len += r.len;
        r.len = 0;
        l.next = r.next;
        if (r.next >= 0) syms[static_cast<size_t>(r.next)].prev = bg.left;
        try_add(l.prev, bg.left);
        try_add(bg.left, l.next);
    }

    for (int i = 0; i >= 0; i = syms[static_cast<size_t>(i)].next) {
        const Sym& s = syms[static_cast<size_t>(i)];
        if (s.len == 0) continue;
        const std::string piece = text.substr(s.off, s.len);
        const int32_t id = lookup(piece);
        if (id >= 0) { out.push_back(id); continue; }
        // byte fallback
        bool any = false;
        for (unsigned char b : piece) {
            const int32_t bt = byte_tokens_[b];
            if (bt >= 0) { out.push_back(bt); any = true; }
        }
        if (!any) out.push_back(unk_);
    }
    return out;
}

// ---------------------------------------------------------------- encode

std::vector<int32_t> Tokenizer::encode_plain(const std::string& text) const {
    if (text.empty()) return {};
    if (kind_ == VocabKind::SPM) return encode_spm(text);
    std::vector<int32_t> ids;
    for (const std::string& pre : split_pre_tokens(text)) {
        const std::vector<int32_t> part = encode_bpe_piece(pre);
        ids.insert(ids.end(), part.begin(), part.end());
    }
    return ids;
}

std::vector<int32_t> Tokenizer::encode(const std::string& text, bool add_bos,
                                       bool parse_special) const {
    std::vector<int32_t> ids;
    if (add_bos && bos_ >= 0) ids.push_back(bos_);
    if (!parse_special) {
        const std::vector<int32_t> part = encode_plain(text);
        ids.insert(ids.end(), part.begin(), part.end());
        return ids;
    }

    size_t i = 0;
    size_t plain_from = 0;
    while (i < text.size()) {
        bool matched = false;
        for (const auto& sp : specials_) {
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
        next_cp(text, i);
    }
    if (plain_from < text.size()) {
        auto part = encode_plain(text.substr(plain_from));
        ids.insert(ids.end(), part.begin(), part.end());
    }
    return ids;
}

// ---------------------------------------------------------------- decode

std::string Tokenizer::token_text(int32_t id) const {
    if (id < 0 || static_cast<size_t>(id) >= id_to_token_.size()) return std::string();
    const std::string& tok = id_to_token_[static_cast<size_t>(id)];
    const int ty = token_type_[static_cast<size_t>(id)];

    if (kind_ == VocabKind::SPM) {
        if (ty == 6) {  // BYTE: "<0xNN>"
            if (tok.size() == 6 && tok[0] == '<' && tok[1] == '0' && tok[2] == 'x') {
                const int b = static_cast<int>(std::strtol(tok.c_str() + 3, nullptr, 16));
                return std::string(1, static_cast<char>(b));
            }
        }
        if (ty == 3 || ty == 4) return tok;  // control / user-defined: literal
        std::string out;
        out.reserve(tok.size());
        for (size_t i = 0; i < tok.size();) {
            if (tok.compare(i, 3, kSpmSpace) == 0) { out += ' '; i += 3; }
            else out += tok[i++];
        }
        return out;
    }

    // Byte-level BPE: reverse the alphabet.
    if (ty == 3 || ty == 4) return tok;
    const ByteAlphabet& ab = alphabet();
    std::string out;
    size_t i = 0;
    while (i < tok.size()) {
        const size_t start = i;
        const uint32_t cp = next_cp(tok, i);
        auto it = ab.dec.find(cp);
        if (it != ab.dec.end()) out += static_cast<char>(it->second);
        else out.append(tok, start, i - start);
    }
    return out;
}

std::string Tokenizer::decode(const std::vector<int32_t>& ids) const {
    std::string out;
    for (int32_t id : ids) out += token_text(id);
    // SentencePiece adds a leading word marker on encode; a full decode must
    // not show it. Streaming (token_text) keeps it because there it is real.
    if (kind_ == VocabKind::SPM && add_space_prefix_ && !out.empty() && out[0] == ' ')
        out.erase(0, 1);
    return out;
}

bool Tokenizer::is_eog(int32_t id) const {
    return eog_.count(id) != 0;
}

// ---------------------------------------------------------------- chat

std::vector<Tokenizer::Segment> Tokenizer::chat_segments(
        const std::vector<std::pair<std::string, std::string>>& messages,
        ChatFormat format) const {
    std::vector<Segment> seg;
    auto T = [&](const std::string& s) { if (!s.empty()) seg.push_back({s, true}); };
    auto C = [&](const std::string& s) { if (!s.empty()) seg.push_back({s, false}); };

    // Tool results arrive with role "tool". Each family has its own
    // convention; where there is none, they are presented as a user message.
    switch (format) {
        case ChatFormat::Llama3:
            for (const auto& m : messages) {
                const std::string role = (m.first == "tool") ? "ipython" : m.first;
                T("<|start_header_id|>" + role + "<|end_header_id|>\n\n");
                C(m.second);
                T("<|eot_id|>");
            }
            T("<|start_header_id|>assistant<|end_header_id|>\n\n");
            break;

        case ChatFormat::ChatML:
            for (const auto& m : messages) {
                if (m.first == "tool") {
                    T("<|im_start|>user\n");
                    C("<tool_response>\n" + m.second + "\n</tool_response>");
                    T("<|im_end|>\n");
                    continue;
                }
                T("<|im_start|>" + m.first + "\n");
                C(m.second);
                T("<|im_end|>\n");
            }
            T("<|im_start|>assistant\n");
            break;

        case ChatFormat::Phi3:
            for (const auto& m : messages) {
                const std::string role = (m.first == "tool") ? "user" : m.first;
                T("<|" + role + "|>\n");
                C(m.second);
                T("<|end|>\n");
            }
            T("<|assistant|>\n");
            break;

        case ChatFormat::Gemma: {
            // Gemma has no system role: it is folded into the first user turn.
            std::string pending_system;
            for (const auto& m : messages) {
                if (m.first == "system") {
                    pending_system += (pending_system.empty() ? "" : "\n") + m.second;
                    continue;
                }
                const std::string role = (m.first == "assistant") ? "model" : "user";  // tool -> user
                T("<start_of_turn>" + role + "\n");
                if (!pending_system.empty() && role == "user") {
                    C(pending_system + "\n\n");
                    pending_system.clear();
                }
                C(m.second);
                T("<end_of_turn>\n");
            }
            T("<start_of_turn>model\n");
            break;
        }

        case ChatFormat::Mistral: {
            // [INST] system\n\nuser [/INST] answer</s>[INST] user [/INST]
            std::string pending_system;
            for (const auto& m : messages) {
                if (m.first == "system") {
                    pending_system += (pending_system.empty() ? "" : "\n") + m.second;
                } else if (m.first == "assistant") {
                    C(" " + m.second);
                    T(eos_ >= 0 ? id_to_token_[static_cast<size_t>(eos_)] : "</s>");
                } else {
                    T("[INST]");
                    C(" " + (pending_system.empty() ? "" : pending_system + "\n\n") + m.second + " ");
                    pending_system.clear();
                    T("[/INST]");
                }
            }
            break;
        }

        case ChatFormat::DeepSeek: {
            const std::string user_tag = "<\xEF\xBD\x9C" "User\xEF\xBD\x9C>";
            const std::string asst_tag = "<\xEF\xBD\x9C" "Assistant\xEF\xBD\x9C>";
            for (const auto& m : messages) {
                if (m.first == "system") { C(m.second); continue; }
                if (m.first == "assistant") {
                    T(asst_tag);
                    C(m.second);
                    T(eos_ >= 0 ? id_to_token_[static_cast<size_t>(eos_)] : "");
                } else {
                    T(user_tag);
                    C(m.second);
                }
            }
            T(asst_tag);
            break;
        }

        case ChatFormat::Plain:
        default:
            for (const auto& m : messages) {
                std::string role = m.first;
                if (!role.empty() && role[0] >= 'a' && role[0] <= 'z')
                    role[0] = static_cast<char>(role[0] - 32);
                C(role + ": " + m.second + "\n\n");
            }
            C("Assistant:");
            break;
    }
    return seg;
}

std::vector<int32_t> Tokenizer::encode_chat(
        const std::vector<std::pair<std::string, std::string>>& messages) const {
    std::vector<int32_t> ids;
    if (add_bos_ && bos_ >= 0) ids.push_back(bos_);
    for (const Segment& s : chat_segments(messages, format_)) {
        const std::vector<int32_t> part = encode(s.text, false, s.special);
        ids.insert(ids.end(), part.begin(), part.end());
    }
    return ids;
}

std::string Tokenizer::apply_chat(
        const std::vector<std::pair<std::string, std::string>>& messages,
        ChatFormat format) const {
    std::string out;
    if (add_bos_ && bos_ >= 0 && format != ChatFormat::Plain)
        out += id_to_token_[static_cast<size_t>(bos_)];
    for (const Segment& s : chat_segments(messages, format)) out += s.text;
    return out;
}

} // namespace fish
