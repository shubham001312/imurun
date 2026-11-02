#pragma once
/*
 * FISH — tokenizer and chat templates.
 *
 * Two vocabularies are understood, both straight from the GGUF metadata:
 *
 *   byte-level BPE ("gpt2")  Llama 3, Qwen, DeepSeek, Phi-4, SmolLM ...
 *       Text is pre-split with the model's regex (llama-bpe / qwen2 / gpt2
 *       flavours), each piece is mapped to the GPT-2 byte alphabet and merged
 *       by rank from tokenizer.ggml.merges.
 *
 *   SentencePiece ("llama")  Llama 2, Mistral, Phi-3, Gemma, TinyLlama ...
 *       Spaces become U+2581, the whole text is merged by tokenizer.ggml.scores
 *       (highest score first) and unknown bytes fall back to <0xNN> tokens.
 *
 * Special tokens (control/user-defined) are matched literally before BPE,
 * longest match first — but only when the caller asks (`parse_special`).
 * Chat templates are applied structurally by encode_chat(): template text is
 * tokenised with specials enabled, message content with specials disabled,
 * so a user cannot type "<|eot_id|>" and break out of their turn.
 */
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "gguf.h"

namespace fish {

enum class ChatFormat { Llama3, ChatML, Phi3, Gemma, Mistral, DeepSeek, Plain };
enum class VocabKind { BPE, SPM };

class Tokenizer {
public:
    static std::unique_ptr<Tokenizer> create(const GgufFile& g, std::string* err);
    ~Tokenizer();

    // Tokenise text. `parse_special` lets literal special-token strings in
    // the text become control tokens; it must be false for user content.
    std::vector<int32_t> encode(const std::string& text, bool add_bos,
                                bool parse_special = true) const;

    // Tokenise a conversation with the model's own template. BOS is added
    // when the model expects one; the result ends ready for the assistant.
    std::vector<int32_t> encode_chat(
        const std::vector<std::pair<std::string, std::string>>& messages) const;

    // Raw bytes of one token, as emitted while streaming.
    std::string token_text(int32_t id) const;

    // Decode a sequence to text.
    std::string decode(const std::vector<int32_t>& ids) const;

    // Render a conversation into the model's prompt format (text form, for
    // display and for callers that tokenise themselves).
    std::string apply_chat(const std::vector<std::pair<std::string, std::string>>& messages,
                           ChatFormat format) const;

    ChatFormat detect_format() const { return format_; }
    VocabKind kind() const { return kind_; }

    int32_t bos() const { return bos_; }
    int32_t eos() const { return eos_; }
    bool add_bos_default() const { return add_bos_; }
    bool is_eog(int32_t id) const;
    size_t vocab_size() const { return id_to_token_.size(); }

private:
    Tokenizer() = default;

    struct Segment { std::string text; bool special; };
    std::vector<Segment> chat_segments(
        const std::vector<std::pair<std::string, std::string>>& messages,
        ChatFormat format) const;

    std::vector<int32_t> encode_plain(const std::string& text) const;
    std::vector<int32_t> encode_bpe_piece(const std::string& piece) const;
    std::vector<int32_t> encode_spm(const std::string& text) const;
    std::vector<std::string> split_pre_tokens(const std::string& text) const;
    int32_t lookup(const std::string& s) const;

    VocabKind kind_ = VocabKind::BPE;
    std::vector<std::string> id_to_token_;
    std::vector<int32_t> token_type_;
    std::unordered_map<std::string, int32_t> token_to_id_;
    // (a << 32 | b) -> (rank, merged id)
    struct Merge { int rank; int32_t id; };
    std::unordered_map<uint64_t, Merge> merges_;
    std::vector<std::pair<std::string, int32_t>> specials_;  // longest first
    std::vector<float> scores_;
    std::unordered_set<int32_t> eog_;
    std::vector<int32_t> byte_tokens_;   // SPM: id of <0xNN>, -1 if absent

    int32_t bos_ = 1;
    int32_t eos_ = 2;
    int32_t unk_ = 0;
    bool add_bos_ = true;
    bool add_space_prefix_ = true;
    int digits_per_token_ = 3;      // pre-tokenizer flavour
    ChatFormat format_ = ChatFormat::Plain;
};

const char* chat_format_name(ChatFormat f);

} // namespace fish
