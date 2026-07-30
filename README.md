# Bilingual Aviation Corpus and RAG QA Benchmark

This repository implements a rights-aware, reproducible pipeline for an
English/Turkish civil-aviation corpus and evidence-linked QA benchmark. It is
designed to begin with a 500-document/1,500-QA pilot and to refuse unsafe public
release when rights or provenance are incomplete.

The repository includes a tiny, openly licensed offline fixture portfolio. It
is a pipeline smoke test, not the research dataset. Live source acquisition and
Qwen generation are always opt-in.

## Quick start

```bash
uv sync --extra dev --extra formats
uv run aviation-data rights audit
uv run aviation-data fetch --snapshot 2026-07-29
uv run aviation-data extract
uv run aviation-data curate
uv run aviation-data passages build
uv run aviation-data qa build --backend fixture --run-id fixture-v2 --target 8
uv run aviation-data qa review-sample --run-id fixture-v2
uv run aviation-data report --qa-run-id fixture-v2
uv run aviation-data package --public
```

By default `fetch` imports local `file:` seeds and matching `local_glob`
sources. Place optional authority files under the ignored `datatoprocess/`
directory using the configured uppercase prefixes (`DHMI*.xlsx`, `SHGM*.pdf`,
`EASA*.pdf`, and `FAA*.pdf`). Each unique checksum becomes one immutable
source/document version. Add `--network` to also enable HTTP sources after
reviewing `configs/sources.yaml`. A blocked source is never fetched. A
manifest-only source may produce internal canonical and structured artifacts,
but its binary, derived text, passages, and QA are excluded from public output.

The enabled Wikipedia sources are bounded Action API queries, not encyclopedia
dumps. They currently cap English aviation coverage at 400 pages/512 MiB and
Turkish aviation coverage at 600 pages/512 MiB, including one bounded
subcategory level. Each page is revision-pinned. Turkish curation
does not trim documents to a language quota: corpus language ratios are
reported against a 70/30 reference with a five-point tolerance. The much
larger `wikipedia_en_dump` and `wikipedia_tr_dump` entries remain disabled. Set
either scoped API source's `enabled` field to `false` to omit it, lower its
`mediawiki.max_pages` value for a smaller snapshot, or increase the existing
400/600 caps for later scaling.

The model backend is configured with:

```bash
uv run aviation-data qa build \
  --backend vllm \
  --run-id production-v2 \
  --endpoint http://127.0.0.1:8000/v1 \
  --target 1500
```

It refuses to run until the endpoint serves the configured model ID and the
model revision and container digest in
`configs/generation.yaml` are immutable pins. Raw responses, parsed records,
rejections, retries, prompt hashes, and passage inputs are checkpointed.

The primary and fallback repository commits are frozen. The container digest
remains host-specific and deliberately blocks model generation until filled.
See [containers/README.md](containers/README.md) for the exact vLLM command and
isolated 400-item comparison runs.

## Data layout

```text
data/
  raw/sha256/           immutable content-addressed source bytes
  manifests/            acquisition events and rights audit
  extracted/            canonical UTF-8 text and document records
  curated/              accepted/rejected documents
  passages/             retrieval-ready passages
  qa/                   raw, accepted, and rejected QA
  reports/              gate and diagnostic reports
release/public/          fail-closed distributable package
```

All public records are Pydantic models. `aviation-data schemas export` emits
versioned JSON Schema. JSONL is always written; Parquet is written when
`pyarrow` is installed, and QA CSV exports are included in public packages.

## Release policy

Rights are recorded per source and per document as `open`, `manifest_only`, or
`blocked`. There is no blanket data license. Public output is sharded by source
license family. The Python code is Apache-2.0; that license does not apply to
third-party data.

Production passage construction uses the locally downloaded fast tokenizer
configured in `configs/passages.yaml`. Its 40-character model commit and local
file checksums are verified before loading. Offline tests use
`configs/passages.fixture.yaml` so CI does not depend on machine-local model
assets.

Retrieval baselines are available with:

```bash
aviation-data evaluate retrieval --backend bm25
aviation-data evaluate retrieval --backend dense
```

The dense checkpoint is pinned in `configs/evaluation.yaml` and is served
through an OpenAI-compatible embeddings endpoint. `aviation-data evaluate
judge` runs the pinned Qwen explanatory-answer judge and refuses a calibrated
report until the stratified double-review sample is complete.

See [docs/curation_protocol.md](docs/curation_protocol.md),
[docs/annotation_guide.md](docs/annotation_guide.md), and
[docs/release.md](docs/release.md).
