# Pinned vLLM runtime

Use vLLM 0.19 or later on the RTX 3090 host and record the exact image
repository digest in `configs/generation.yaml`. A mutable tag is not sufficient.
The current release environment is pinned to:

```text
vllm/vllm-openai@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089
```

That immutable image was verified to contain vLLM 0.25.1.

Serve the primary checkpoint at its frozen revision:

```bash
vllm serve RedHatAI/Qwen3.8-27B-INT4 \
  --revision c063053e004e9783631651df95cf55d0bbf88b32 \
  --tokenizer-revision c063053e004e9783631651df95cf55d0bbf88b32 \
  --language-model-only \
  --gpu-memory-utilization 0.80 \
  --max-model-len 4096 \
  --max-num-seqs 2 \
  --enforce-eager \
  --reasoning-parser qwen3
```

Primary weights (`model.safetensors`) are 18,603,387,656 bytes = 17.32 GiB
(not 18.6 GiB -- that's the decimal-GB figure; `--gpu-memory-utilization` and
`nvidia-smi` both use binary GiB). With the TEI embedding container holding
~3.35 GiB on the same 24 GiB 3090, `--gpu-memory-utilization` was raised from
0.75 to 0.80 (19.2 GiB) on 2026-09-28 for a ~1.9 GiB KV-cache margin above the
weights. This applies to both the primary and fallback serve commands, since
the fallback (Qwen3.5-9B-AWQ-4bit) is far smaller and fits easily either way.

Thinking is disabled per request with `chat_template_kwargs.enable_thinking=false`.
Generation uses temperature 0.2, a task-derived fixed seed, bounded output, and
JSON Schema response formatting.

Run the same frozen 400-item comparison set in isolated experiment directories:

```bash
aviation-data qa generate --backend vllm --target 400 \
  --model-choice primary --run-id primary-pilot
aviation-data qa generate --backend vllm --target 400 \
  --model-choice fallback --run-id fallback-pilot
```

The fallback checkpoint is `cyankiwi/Qwen3.5-9B-AWQ-4bit` at the frozen
repository revision `156edc4bbeb8d1910ee7be9196bafaf1bc052156`. The fallback
uses its own tokenizer at the same revision and disables thinking through
`chat_template_kwargs.enable_thinking=false`.

Serve the fallback checkpoint with:

```bash
vllm serve cyankiwi/Qwen3.5-9B-AWQ-4bit \
  --revision 156edc4bbeb8d1910ee7be9196bafaf1bc052156 \
  --tokenizer cyankiwi/Qwen3.5-9B-AWQ-4bit \
  --tokenizer-revision 156edc4bbeb8d1910ee7be9196bafaf1bc052156 \
  --served-model-name cyankiwi/Qwen3.5-9B-AWQ-4bit \
  --language-model-only \
  --gpu-memory-utilization 0.80 \
  --max-model-len 4096 \
  --max-num-seqs 2 \
  --enforce-eager \
  --reasoning-parser qwen3
```

Do not copy an experiment file into the benchmark path until its schema
stability, grounding, repetition, and English/Turkish quality review is
recorded.
