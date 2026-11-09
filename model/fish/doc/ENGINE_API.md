# Engine API — `engine.h` / `engine.cpp`

## Purpose
**Public C++17 interface for the embedded server.** Single class `Engine` with factory `create()`. Used by `/fish/*` HTTP routes.

## Factory
```cpp
static unique_ptr<Engine> Engine::create(
    const string& model_path,
    const LayerPlan& plan,
    const HardwareCaps& caps
);
```
**Does:**
1. `inspect_gguf(model_path)` → `TensorTable`
2. `ResidentWindow(plan)` → allocates VRAM arena + staging ring
3. `Prefetcher(tensors, window)` → starts background thread
4. `ExpertStreamer(tensors, window)` → builds expert table
5. `llama_backend_init()`, `llama_model_load_from_file()` with `load_mode=MMAP`, `lazy_mode=AUTO`
6. `llama_init_from_model()` with `context_params` (n_ctx, flash_attn, moe_cache_size)
7. Returns `EngineReal` instance

## Core Methods

### `GenerateHandle start_gen(Prompt, SampleParams)`
```cpp
struct Prompt {
    string text;              // Raw text (tokenized internally)
    vector<int32_t> tokens;   // Pre-tokenized (optional, for speed)
};

struct SampleParams {
    float temperature = 0.7f;
    float top_p = 0.9f;
    float repeat_penalty = 1.1f;
    int top_k = 40;
    int64_t seed = -1;
    int max_tokens = 2048;
};

using GenerateHandle = uint64_t;  // Opaque, increments from 1
```
**Returns:** Handle for `next_token` / `cancel`. **Non-blocking** — starts background generation.

### `int next_token(GenerateHandle, int32_t& out_token)`
```cpp
// Returns:
//   >0 : token ID (valid)
//    0 : end of stream (EOS or max_tokens)
//   -1 : error (check logs)
```
**Blocking** — waits for next token from generation thread.

### `void cancel(GenerateHandle)`
- Signals generation thread to stop
- `Prefetcher::cancel_all()`
- Cleans up partial state
- Handle becomes invalid

### `EngineStats stats() const`
```cpp
struct EngineStats {
    double ttft_ms = 0;              // Time to first token (from start_gen)
    double prefill_tok_s = 0;        // Prefill throughput
    double decode_tok_s = 0;         // Decode throughput
    size_t peak_ram_bytes = 0;       // Process RSS peak
    size_t peak_vram_bytes = 0;      // GPU memory peak (via nvml/ROCm)
    double disk_mb_per_token = 0;    // Cold→Warm bytes / generated token
    size_t hot_layers_count = 0;
    size_t warm_layers_count = 0;
    size_t cold_layers_count = 0;
    steady_clock::time_point start_time;
};
```

### Adapter Control
```cpp
void set_adapters(const AdapterSet&);  // Before next start_gen
void clear_adapters();                 // Reset to base model
```

### Model Info
```cpp
int n_layers() const;        // From plan
int n_ctx() const;           // Context window
size_t vocab_size() const;   // Tokenizer vocab size
```

## Internal: Generation Thread
```cpp
// EngineReal::Impl::generation_thread(GenerateHandle h)
void generation_thread(GenerateHandle h) {
    // 1. Tokenize prompt (if not pre-tokenized)
    // 2. Build first batch (batch_size=1 for streaming)
    // 3. Loop:
    //    a. prefetcher.schedule_prefetch(next_layers)
    //    b. window.ensure_hot(current_layers)
    //    c. llama_decode(ctx, batch)
    //    d. sample next token (llama_sample_*)
    //    e. push token to SPSC ring (handle → token)
    //    f. if EOS or max_tokens: break
    // 4. Mark handle complete
}
```

## Thread Safety
| Method | Thread | Notes |
|--------|--------|-------|
| `create` | Main | Single call at startup |
| `start_gen` | HTTP handler | Lock-free, returns immediately |
| `next_token` | HTTP handler (SSE) | Blocks on ring pop |
| `cancel` | HTTP handler | Atomic flag + thread join |
| `stats` | HTTP handler / monitor | Atomic snapshot |
| `set_adapters` | HTTP handler | Before next `start_gen` |

## Embedded Server Routes
```cpp
// In server bootstrap (replicates server.cpp:627-680):
server_http_context ctx_http;
ctx_http.init(params);

// Built-in routes from llama-server-impl:
routes.init_routes();  // /v1/completions, /health, /v1/models, etc.

// FISH routes:
ctx_http.get("/fish/health", [](auto req, auto res) {
    res->set_content(R"({"status":"ok"})", "application/json");
});

ctx_http.post("/fish/load", [&](auto req, auto res) {
    auto json = parse_json(req->body);
    string model = json["model_path"];
    size_t vram_mb = json.value("vram_limit_mb", 0);
    engine = Engine::create(model, make_plan(...), probe_hardware());
    res->set_content(R"({"status":"loaded"})", "application/json");
});

ctx_http.post("/fish/stream", [&](auto req, auto res) {
    auto json = parse_json(req->body);
    Prompt p{json["prompt"]};
    SampleParams sp = parse_sampling(json);
    auto handle = engine->start_gen(p, sp);
    // SSE streaming:
    res->set_header("Content-Type", "text/event-stream");
    res->set_header("Cache-Control", "no-cache");
    while (true) {
        int32_t tok;
        int r = engine->next_token(handle, tok);
        if (r <= 0) break;
        string piece = tokenizer_decode(tok);
        res->write("data: " + json_encode({{"token", piece}}) + "\n\n");
    }
    res->done();
});

ctx_http.get("/fish/stats", [&](auto req, auto res) {
    res->set_content(json_encode(engine->stats()), "application/json");
});

ctx_http.post("/fish/adapters", [&](auto req, auto res) {
    AdapterSet a = parse_adapters(req->body);
    engine->set_adapters(a);
    res->set_content(R"({"status":"ok"})", "application/json");
});

ctx_http.post("/fish/cancel", [&](auto req, auto res) {
    auto json = parse_json(req->body);
    engine->cancel(json["handle"]);
    res->set_content(R"({"status":"cancelled"})", "application/json");
});

ctx_http.start();
ctx_server.load_model(params);
ctx_server.start_loop();
```

## Error Handling
| Error | HTTP Code | Response |
|-------|-----------|----------|
| Model not loaded | 503 | `{"error":"model_not_loaded"}` |
| Invalid handle | 404 | `{"error":"invalid_handle"}` |
| OOM / VRAM full | 507 | `{"error":"out_of_memory"}` |
| Generation cancelled | 499 | `{"error":"cancelled"}` |
| Internal error | 500 | `{"error":"internal"}` |

## Invariants
1. **Single model per Engine instance** — multi-model = multiple processes
2. **`start_gen` / `next_token` / `cancel` serialized per handle** — no concurrent calls on same handle
3. **`stats()` lock-free** — atomic reads only
4. **Generation thread cleans up on cancel** — no leaked buffers
5. **Tokenizer thread-safe** — `llama_tokenize` called from generation thread only

## Testing
```cpp
TEST(Engine, LoadAndGenerate) {
    auto engine = Engine::create("test.gguf", plan, caps);
    auto h = engine->start_gen({"Hello"}, {});
    int32_t tok;
    int count = 0;
    while (engine->next_token(h, tok) > 0 && count < 10) ++count;
    EXPECT_GT(count, 0);
    auto s = engine->stats();
    EXPECT_GT(s.decode_tok_s, 0);
}

TEST(Engine, CancelMidStream) {
    auto h = engine->start_gen({"Long prompt..."}, {.max_tokens=1000});
    engine->cancel(h);
    int32_t tok;
    EXPECT_EQ(engine->next_token(h, tok), -1);  // Cancelled
}

TEST(Engine, AdapterSwitch) {
    engine->set_adapters({{lora: {"style", "style.gguf", 1.0}}});
    auto h1 = engine->start_gen({"Hello"}, {});
    // ... generate ...
    engine->clear_adapters();
    auto h2 = engine->start_gen({"Hello"}, {});
    // Different adapter set → separate batch
}
```

## Performance Targets (RTX 3050 4 GB, 7B Q4_K_M)
| Metric | Target |
|--------|--------|
| `create()` latency | < 5 s (mmap load) |
| `start_gen()` latency | < 1 ms |
| TTFT | < 800 ms |
| Decode throughput | > 40 tok/s |
| Peak VRAM | ≤ 3.5 GB |
| Peak RSS | ≤ 6 GB |

## Future
- **Batched `start_gen`** — multiple prompts per call
- **Streaming `next_token` callback** — push instead of pull
- **Speculative decoding** — draft model via `ctx_other`
- **Multi-GPU** — `split_mode=LAYER`, `tensor_split`
- **WebSocket transport** — lower latency than SSE