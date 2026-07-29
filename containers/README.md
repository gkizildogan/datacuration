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
vllm serve AxisQuant/Qwen3.6-27b-gptq-int4 \
  --revision e4a111caa43e97606b7a5fa20849bbcc051aa4f0 \
  --tokenizer-revision e4a111caa43e97606b7a5fa20849bbcc051aa4f0 \
  --language-model-only \
  --gpu-memory-utilization 0.90 \
  --max-model-len 8192 \
  --max-num-seqs 1 \
  --reasoning-parser qwen3
```

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

The fallback is frozen at
`3dec12a2e68033ba440364493c074f6b3c6995f6`. Do not copy an experiment file into
the benchmark path until its schema stability, grounding, repetition, and
English/Turkish quality review is recorded.
