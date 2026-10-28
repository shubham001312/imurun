#pragma once
/*
 * FISH — byte-level BPE tokenizer and chat templates.
 *
 * GGUF stores the vocabulary already in the GPT-2 byte-level alphabet, so
 * FISH never needs a SentencePiece library: encode text to bytes, map each
 * byte to its alphabet character, merge by rank.
 *
 * Special tokens (control/user-defined) are matched literally before BPE,
 * longest match first, which is what makes <|start_header_id|> a single
 * token rather than eleven.
 */
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "gguf.h"

namespace fish {

enum class ChatFormat { Llama3, ChatML, Phi3, Gemma, Plain };

class Tokenizer {
public:
    static std::unique_ptr<Tokenizer> create(const GgufFile& g, std::string* err);
    ~Tokenizer();

    std::vector<int32_t> encode(const std::string& text, bool add_bos) const;

    // Raw bytes of one token (byte-level alphabet already reversed).
    std::string token_text(int32_t id) const;

    // Concatenate and reverse the byte-level alphabet.
    std::string decode(const std::vector<int32_t>& ids) const;

    // Render a conversation into the model's prompt format.
    std::string apply_chat(const std::vector<std::pair<std::string, std::string>>& messages,
                           ChatFormat format) const;

    ChatFormat detect_format() const { return format_; }

    int32_t bos() const { return bos_; }
    int32_t eos() const { return eos_; }
    bool is_eog(int32_t id) const;
    size_t vocab_size() const { return id_to_token_.size(); }

private:
    Tokenizer() = default;

    std::vector<int32_t> encode_plain(const std::string& text) const;
    std::vector<std::string> split_pre_tokens(const std::string& text) const;

    std::vector<std::string> id_to_token_;
    std::unordered_map<std::string, int32_t> token_to_id_;
    // (a << 32 | b) -> (rank, merged id)
    struct Merge { int rank; int32_t id; };
    std::unordered_map<uint64_t, Merge> merges_;
    std::vector<std::pair<std::string, int32_t>> specials_;  // longest first

    int32_t bos_ = 1;
    int32_t eos_ = 2;
    ChatFormat format_ = ChatFormat::Plain;
};

} // namespace fish
