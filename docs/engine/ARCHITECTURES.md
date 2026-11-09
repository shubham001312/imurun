# Supported model architectures

FISH reads `general.architecture` from the GGUF and refuses anything not in
this table with a message that names the supported set. Everything here runs
through the single `LlamaModel::forward()`; the columns are the flags that
differ.

| GGUF arch | Families | Tokenizer | RoPE pairing | Special handling | Chat template detection | Status |
|---|---|---|---|---|---|---|
| `llama` | Llama 2/3.x, Mistral 7B/Nemo, TinyLlama, SmolLM, DeepSeek-Coder, DeepSeek-R1-Distill-Llama, Mixtral (MoE) | BPE (Llama 3) or SPM (Llama 2, Mistral) | normal (adjacent pairs) | Llama-3 `rope_freqs` factors; MoE via `ffn_gate_inp` + 3-D `ffn_*_exps` | `<\|start_header_id\|>` → llama3; `[INST]` → mistral; DeepSeek `<｜User｜>` → deepseek | **Tested** (Llama 3.2 1B: M1/M2/ABI pass) |
| `qwen2`, `qwen2moe` | Qwen 2 / 2.5, QwQ, DeepSeek-R1-Distill-Qwen, Qwen2-MoE | BPE (qwen2 pre-tokenizer: single digits) | NeoX | q/k/v biases; MoE shared expert with sigmoid gate (`ffn_gate_inp_shexp`); `expert_weights_norm` default false for qwen2moe | `<\|im_start\|>` → chatml | Implemented, **not yet run** (no file on this machine) |
| `qwen3`, `qwen3moe` | Qwen 3 | BPE | NeoX | per-head `attn_q_norm` / `attn_k_norm`; `head_dim × n_head ≠ n_embd` handled (`q_dim`) | chatml | Run: Qwen3-1.7B Q4_K_M M1/M2/C-ABI pass, ~60 tok/s CPU |
| `qwen35` | Qwen 3.5 (hybrid) | BPE, no BOS | NeoX, partial (first 64 of 256 dims), θ 1e7 | 3:1 Gated DeltaNet : full attention (`full_attention_interval`); DeltaNet = fused q|k|v → causal depthwise conv (k=4) + SiLU → L2-norm q/k → per-head delta rule with decay `ssm_a·softplus(alpha+dt_bias)` → gated RMSNorm × SiLU(z) → `ssm_out`; full-attn layers fuse per-head `q|gate` (sigmoid gate before `attn_output`); fp32 recurrent state, no prefix rewind, no speculative decoding; matmuls on the GPU per-matmul (no spans), conv/recurrence on the CPU | chatml | Run: Qwen3.5-2B Q4_K_M M1/M2/C-ABI pass; GPU ~32-36 tok/s decode, 149 tok/s prefill (2026-11-09) |
| `phi3` | Phi-3 mini/medium, Phi-3.5 mini | SPM | NeoX | fused `attn_qkv`; fused `ffn_up` = gate\|up; longrope `rope_factors_short/long` + `attn_factor` | `<\|user\|>`/`<\|end\|>` → phi3 | Implemented, not yet run |
| `gemma2` | Gemma 2 2B/9B/27B | SPM (256k vocab) | NeoX | RMSNorm weight = 1+w; GELU; embedding × √n_embd; post-attention and post-FFN norms; attention soft-cap 50, final logit soft-cap 30; sliding window 4096 on even layers; no system role (folded into first user turn) | `<start_of_turn>` → gemma | Implemented, not yet run |

Quantisation types accepted in weights: F32, F16, BF16, Q4_0, Q4_1, Q5_0, Q5_1,
Q8_0, Q2_K, Q3_K, Q4_K, Q5_K, Q6_K, Q8_K, IQ4_NL, IQ4_XS, MXFP4. Fast int8
kernels exist for Q4_0, Q8_0, Q4_K, Q5_K, Q6_K (which covers all `*_K_M`/`*_K_S`
files); the others use the f32 path (slower, still correct). IQ2/IQ3/IQ1 and
TQ types are rejected at load.

RoPE scaling read from metadata: `rope.scaling.type` = `linear` (θ/factor),
`yarn` (full YaRN ramp + attention scale), `longrope` (per-pair factor tensors),
plus Llama-3 `rope_freqs.weight`. `rope.dimension_count` < head_dim (partial
rotary) is honoured.

Chat formats implemented: llama3, chatml, phi3, gemma, mistral (`[INST]`),
deepseek (`<｜User｜>`/`<｜Assistant｜>`), plain fallback. Detection prefers the
GGUF's own `tokenizer.chat_template` text, then token presence.

End-of-generation tokens: `eos`, `eot`, `eom` ids from metadata plus the known
strings (`<|eot_id|>`, `<|im_end|>`, `<|end|>`, `<end_of_turn>`,
`<|endoftext|>`, DeepSeek `<｜end▁of▁sentence｜>`).

## Not supported (fails at load with a clear message)

`deepseek2` (MLA attention), `gemma3`, `llama4`, `phi2`, `starcoder2`, `olmo2`,
`granite`, `command-r`, `glm4`, `falcon`, `stablelm`, `mamba`, any encoder or
multimodal model, and the IQ1/IQ2/IQ3 quants. Each is a contained addition —
see "Adding an architecture" in `ARCHITECTURE.md`.

## Verification matrix

| Check | How | Last result |
|---|---|---|
| GGUF parse + block layouts + AVX2 == scalar | `fish_test_m1 <gguf>` | PASS (Llama-3.2-1B Q4_K_M, 2026-10-31) |
| Forward pass sanity, tokenizer round trips, injection safety, BOS once | `fish_test_m2 <gguf>` | PASS |
| C ABI: specs, model info, structural chat, prefix cache, 2-turn dialogue | `python model/fish/test/test_fish.py <gguf>` | PASS (Paris / Berlin; 30 of 44 prompt tokens cached) |
| Vulkan GPU backend: Q4_K/Q5_K/Q6_K/Q8_0/Q4_0/F16/F32 GPU matmul vs CPU f32 (n_tok 1/3/37/256, unaligned rows), `matmul_group`, auto placement, Paris with GPU layers, VRAM freed on unload | `fish_test_gpu <gguf>` (see `docs/engine/GPU.md`) | PASS on RTX 3050 Laptop (NVIDIA only; AMD/Intel not run): Llama-3.2-1B 16/16 layers on GPU, DeepSeek-R1-Distill-Llama-8B partial; max error < 1% of the bound, 2026-10-31 |
| Vulkan layer spans (device KV, attention, norms on GPU) ≡ per-matmul GPU path; re-quantised kernels (FQ3, Q4_K) ≡ CPU decode of the same bytes; requant quality vs CPU | `fish_test_gpu <gguf> --no-kernels`; `FISH_GPU_REQUANT=1 fish_test_gpu <gguf> --no-e2e`; `FISH_GPU_REQUANT=<m> fish_test_gpu <8B> --quality 200` | PASS 1B and 8B: 33/33 argmax, \|Δlogit\| 0.02–0.03%; quality top-1: mode 2 96.5%, mode 3 95.0%, mode 1 88.1% (fails, stays opt-in), 2026-11-01 |

Add a row (and a tiny GGUF) for every new family you bring up.
