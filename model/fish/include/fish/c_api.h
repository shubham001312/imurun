#pragma once
/*
 * FISH — stable C ABI.
 *
 * The product loads FISH as a shared library (fish.dll / libfish.so /
 * libfish.dylib) inside the server process. No unsigned executable is ever
 * launched, which is what keeps Windows Application Control and macOS
 * Gatekeeper out of the way, and it lets the weights stay in one address
 * space instead of being copied between processes.
 */
#include <stdint.h>

#ifdef _WIN32
#  define FISH_API __declspec(dllexport)
#else
#  define FISH_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct fish_ctx fish_ctx;

/* Called once per streamed text chunk. `token_id` is the token that produced
 * the chunk, or -1 when several tokens were merged to keep UTF-8 valid. */
typedef void (*fish_token_cb)(const char* text, int token_id, void* user);

typedef struct fish_stats {
    double load_seconds;
    double prefill_seconds;
    double decode_seconds;
    double tokens_per_second;
    int    prompt_tokens;
    int    generated_tokens;
    int    context_used;
    int    context_size;
    unsigned long long weights_bytes;
    unsigned long long kv_bytes;
    double peak_rss_mb;
} fish_stats;

typedef struct fish_model_info {
    char   arch[32];
    int    n_layers;
    int    n_embd;
    int    n_ff;
    int    n_head;
    int    n_head_kv;
    int    head_dim;
    int    n_ctx;
    int    vocab;
    int    tied_output;
    char   quant[64];
} fish_model_info;

FISH_API const char* fish_version(void);

FISH_API fish_ctx* fish_create(void);
FISH_API void      fish_destroy(fish_ctx* ctx);

/* Load a GGUF file. n_ctx is the KV budget to use; 0 means "the model's own
 * limit, capped by FISH". Returns 0 on success, negative on failure. */
FISH_API int fish_load(fish_ctx* ctx, const char* path, int n_ctx);
FISH_API int fish_loaded(fish_ctx* ctx);
FISH_API const char* fish_last_error(fish_ctx* ctx);

FISH_API int fish_model_info_of(fish_ctx* ctx, fish_model_info* out);

/* Tokenisation. Returns the number of ids written, or the required size when
 * `out` is too small, or -1 on error. */
FISH_API int fish_tokenize(fish_ctx* ctx, const char* text, int add_bos,
                           int32_t* out, int cap);
FISH_API int fish_detokenize(fish_ctx* ctx, const int32_t* ids, int n,
                             char* out, int cap);

/* Render a conversation using the model's own chat format. Returns the number
 * of bytes written (excluding NUL), or the required size when out is too
 * small, or -1 on error. */
FISH_API int fish_chat_prompt(fish_ctx* ctx,
                              const char* const* roles,
                              const char* const* contents,
                              int n_messages,
                              char* out, int cap);
FISH_API const char* fish_chat_format(fish_ctx* ctx);

/* Generate. `prompt` is tokenised as-is; add_bos prepends the model BOS.
 * Streams decoded text through `cb` when it is non-null.
 * Returns the number of tokens generated, or -1 on error. */
FISH_API int fish_generate(fish_ctx* ctx, const char* prompt, int add_bos,
                           int max_tokens, float temperature, int top_k,
                           float top_p, float repeat_penalty,
                           fish_token_cb cb, void* user);

/* Same, but collects the answer into `out`. Returns bytes written, or the
 * required size, or -1. */
FISH_API int fish_generate_to(fish_ctx* ctx, const char* prompt, int add_bos,
                              int max_tokens, float temperature, int top_k,
                              float top_p, float repeat_penalty,
                              char* out, int cap);

FISH_API void fish_cancel(fish_ctx* ctx);
FISH_API void fish_reset(fish_ctx* ctx);
FISH_API void fish_get_stats(fish_ctx* ctx, fish_stats* out);

/* Which kernel path is active: "scalar", "avx2" or "neon". */
FISH_API const char* fish_kernel_name(void);

/* Measured hardware and the plan FISH derived from it, as one JSON object.
 * The pointer is valid until the next call on the same thread. */
FISH_API const char* fish_specs_json(fish_ctx* ctx);

#ifdef __cplusplus
}
#endif
