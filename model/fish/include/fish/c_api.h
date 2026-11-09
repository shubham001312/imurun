#pragma once
/*
 * FISH — stable C ABI.
 *
 * The product loads FISH as a shared library (fish.dll / libfish.so /
 * libfish.dylib) inside the server process. No unsigned executable is ever
 * launched, which is what keeps Windows Application Control and macOS
 * Gatekeeper out of the way, and it lets the weights stay in one address
 * space instead of being copied between processes.
 *
 * ABI rule: structs only grow at the end; new functions are added, never
 * changed. Python binds by name and checks `fish_abi_version`.
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

#define FISH_ABI_VERSION 4

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
    /* ABI 2 */
    int    truncated;          /* 1 when the prompt was cut to fit the context */
    int    cached_tokens;      /* prompt tokens served from the KV prefix cache */
    int    n_threads;
    int    streaming;          /* 1 when layers are prefetched from disk */
    double prefill_tokens_per_second;
    /* placement (ABI 2) */
    int    gpu_layers;         /* layers whose projections run on the GPU backend */
    int    ram_layers;         /* layers kept resident in RAM */
    int    disk_layers;        /* layers streamed through the resident window */
    unsigned long long gpu_bytes;
    char   gpu_name[64];       /* "" when no GPU backend is active */
    /* speculative decoding (ABI 2) */
    int    draft_proposed;     /* tokens the draft model proposed in the last generation */
    int    draft_accepted;     /* of those, accepted by the target model */
    int    draft_active;       /* 1 when a draft model is loaded */
    /* what the GPU holds, human readable: layers, re-quantised mix, span KV */
    char   gpu_mix[160];
    /* ABI 4 */
    char   placement[320];     /* the residency planner's decision, or why it was not used */
    char   load_profile[400];  /* wall-clock per load phase (open, bind, calibration, upload, span, KV) */
    unsigned long long vram_total;     /* device heap bytes (any Vulkan vendor), 0 without a GPU */
    unsigned long long vram_free;      /* live free VRAM at this stats call (VK_EXT_memory_budget) */
    unsigned long long vram_headroom;  /* VRAM the engine keeps free (FISH_VRAM_HEADROOM_PCT / _MB) */
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
    /* ABI 2 */
    int    n_experts;
    int    n_experts_per_tok;
    int    n_ctx_train;
    char   chat_format[16];
    char   tokenizer[8];       /* "bpe" or "spm" */
} fish_model_info;

FISH_API const char* fish_version(void);
FISH_API int         fish_abi_version(void);

FISH_API fish_ctx* fish_create(void);
FISH_API void      fish_destroy(fish_ctx* ctx);

/* Load a GGUF file. n_ctx is the KV budget to use; 0 lets the specs engine
 * choose from the measured machine. Returns 0 on success, negative on failure. */
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
 * small, or -1 on error. For display; generation should use
 * fish_generate_chat so user text can never inject control tokens. */
FISH_API int fish_chat_prompt(fish_ctx* ctx,
                              const char* const* roles,
                              const char* const* contents,
                              int n_messages,
                              char* out, int cap);
FISH_API const char* fish_chat_format(fish_ctx* ctx);

/* Generate from a raw prompt. `prompt` is tokenised as-is (special-token
 * strings are honoured); add_bos prepends the model BOS.
 * Streams decoded text through `cb` when it is non-null.
 * Returns the number of tokens generated, or -1 on error. */
FISH_API int fish_generate(fish_ctx* ctx, const char* prompt, int add_bos,
                           int max_tokens, float temperature, int top_k,
                           float top_p, float repeat_penalty,
                           fish_token_cb cb, void* user);

/* Generate from a conversation. The template is applied structurally: message
 * contents are tokenised as plain text, so "<|eot_id|>" typed by a user is
 * just text. BOS is added when the model expects one. */
FISH_API int fish_generate_chat(fish_ctx* ctx,
                                const char* const* roles,
                                const char* const* contents,
                                int n_messages,
                                int max_tokens, float temperature, int top_k,
                                float top_p, float repeat_penalty,
                                fish_token_cb cb, void* user);

/* Same as fish_generate, but collects the answer into `out`. Returns bytes
 * written, or the required size, or -1. */
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

/* Comma-separated list of GGUF architectures this build can run. */
FISH_API const char* fish_supported_archs(void);

/* Speculative decoding: a small "draft" model with the same vocabulary
 * proposes `k` tokens per step; the loaded (target) model verifies them in one
 * batched forward and keeps the longest accepted prefix. Output distribution is
 * identical to decoding with the target alone; only speed changes. Returns 0,
 * -1 on a bad argument, -2 if the draft fails to load, -3 if the vocabularies
 * differ, -4 for hybrid (DeltaNet) models, whose recurrent state cannot
 * rewind rejected drafts. k <= 0 picks a default (5). */
/* Threads that take part in the next generations: 1..pool size (extra
 * workers park). The pool itself is created once (FISH_THREADS). Returns 0 on
 * success, -1 without a context. fish_stats.n_threads reports the active count. */
FISH_API int  fish_set_threads(fish_ctx* ctx, int n);

FISH_API int  fish_set_draft(fish_ctx* ctx, const char* path, int k);
FISH_API void fish_clear_draft(fish_ctx* ctx);

/* Number of tokens a conversation occupies once the chat template is applied
 * (what the context budget is spent on). -1 on error. Role "tool" is rendered
 * with each family's tool-result convention. */
FISH_API int fish_count_chat_tokens(fish_ctx* ctx, const char* const* roles,
                                    const char* const* contents, int n_messages);

/* ---- ABI 3: LoRA adapters, merged into the weights at load ----------------
 *
 * The adapter is the GGUF llama.cpp's convert_lora_to_gguf.py writes
 * (general.type "adapter", adapter.type "lora", adapter.lora.alpha, tensors
 * <weight>.lora_a / .lora_b). Every targeted weight is dequantised row by row,
 * delta = scale * alpha / rank * (B A) is added (llama.cpp's convention; alpha
 * 0 means scale as is), and the row is re-quantised to the tensor's own type
 * before placement, so GPU upload / spans / decode speed are unchanged.
 * Writable types: F32 F16 BF16 Q8_0 Q4_0 Q4_K Q5_K Q6_K; an adapter that
 * targets any other type (or a MoE expert tensor, or a tensor the model does
 * not have) is refused with an error, never partially applied.
 *
 * fish_set_adapter: remembers (path, scale) for every following fish_load. If
 * a model is loaded it is reloaded with the adapter right away. Returns 0;
 * -1 bad argument; -2 the adapter was rejected (fish_last_error says why; the
 * model is reloaded without an adapter and the setting is cleared); -3 the
 * model could not be reloaded at all (nothing is loaded now).
 * fish_load with an adapter set returns -4 when the adapter is rejected (the
 * setting is cleared, nothing is loaded).
 * fish_clear_adapter: forget the adapter; reloads the plain model if one was
 * applied. Returns 0, or -3 if the reload failed. */
typedef struct fish_adapter_info {
    int    active;             /* 1 when an adapter is merged into the loaded weights */
    int    pending;            /* 1 when one is set but no model is loaded yet */
    char   name[128];          /* adapter file name */
    int    rank;               /* LoRA rank (the largest, if tensors differ) */
    int    rank_min;
    float  alpha;              /* adapter.lora.alpha (0 = absent) */
    float  scale;              /* user scale */
    float  effective_scale;    /* scale * alpha / rank */
    int    tensors;            /* weight tensors merged */
    unsigned long long merged_bytes;   /* RAM held by the merged copies */
    double merge_seconds;
    double retention;          /* <merged - base, delta> / |delta|^2: share of the delta kept, 1 = all */
    double residual;           /* |merged - (base + delta)| / |delta|: 0 = exact, 1 = as if unmerged */
    unsigned long long blocks;          /* quant blocks of the merged tensors */
    unsigned long long blocks_refit;    /* of those, freshly re-quantised (the rest kept their scales) */
} fish_adapter_info;

FISH_API int  fish_set_adapter(fish_ctx* ctx, const char* path, float scale);
FISH_API int  fish_clear_adapter(fish_ctx* ctx);
FISH_API int  fish_adapter_info_of(fish_ctx* ctx, fish_adapter_info* out);

#ifdef __cplusplus
}
#endif
