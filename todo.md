# QA v1.1 production run TODO

This is the remaining runbook for rebuilding the corpus and completing Task 6
from scratch. Do not mix QA v1.0 and v1.1 records.

## 1. Prepare an empty production data root

- Preserve the current `data/` directory in a durable, read-only archive.
- Confirm the archived v1.0 baseline contains its `READ_ONLY.json` marker and
  the original generated, accepted, rejected, raw-response, and validation
  artifacts.
- Create a new empty `data/` directory for the production rebuild.
- Do not copy old QA JSONL files into the new data root.
- Choose and record:
  - the snapshot date;
  - the production QA run ID;
  - the primary and fallback comparison run IDs;
  - the vLLM and dense-retrieval endpoints;
  - the two independent reviewer identities.

Suggested shell variables:

```bash
SNAPSHOT_DATE=YYYY-MM-DD
QA_RUN_ID=production-v1.1
PRIMARY_RUN_ID=primary-comparison-v1.1
FALLBACK_RUN_ID=fallback-comparison-v1.1
VLLM_ENDPOINT=http://127.0.0.1:8000/v1
DENSE_ENDPOINT=http://127.0.0.1:8001/v1
DENSE_MODEL=intfloat/multilingual-e5-base
DENSE_REVISION=d128750597153bb5987e10b1c3493a34e5a4502a
```

## 2. Verify frozen configuration

- Confirm the project contact and user agent are monitored and current.
- Confirm primary/fallback model revisions, tokenizer revisions, container
  digest, generation seed, and prompt are final.
- Confirm the production tokenizer files and checksums still match
  `configs/passages.yaml`.
- Confirm the English and Turkish bounded MediaWiki sources, one-level
  subcategory limits, page limits, byte limits, and one-request-per-second
  policy.

```bash
uv run aviation-data rights audit --strict
uv run pytest -q
uv run ruff check src tests
```

## 3. Acquire a new revision-pinned corpus snapshot

```bash
uv run aviation-data fetch \
  --network \
  --snapshot "$SNAPSHOT_DATE"

uv run aviation-data extract
uv run aviation-data curate
```

- Inspect `data/manifests/fetch_errors.json`; resolve every material error.
- Inspect `data/extracted/errors.json`; resolve every material extraction
  failure.
- Inspect `data/curated/turkish_snapshot.json`.
- Require `status: frozen` and a Turkish token share between 0.25 and 0.35.
- Check that every frozen Turkish title has an immutable revision ID.
- If the report says `needs_more_pages`, acquire the next deterministic batch
  of 50 pages and repeat extraction and curation.
- If it says `overshoot`, adjust the bounded source mix and rebuild from the
  empty data root.
- Review the accepted/rejected document counts and quality-flag breakdown.

## 4. Complete extraction review

```bash
uv run aviation-data review extraction-sample --rate 0.10
uv run aviation-data review extraction --reviewer-id REVIEWER_ID
```

- Complete every extraction assignment.
- Require unique assigned document IDs, non-empty reviewer IDs, and JSON
  boolean `usable` values.
- Require the manual usable-extraction rate to be at least 0.95.

## 5. Build production passages

```bash
uv run aviation-data passages build --config configs/passages.yaml
```

- Confirm the production tokenizer is reported as pinned and production-ready.
- Confirm passage and canonical offsets reproduce the exact stored text.
- Confirm the passage report has no checksum or tokenizer failures.

## 6. Run the fixture-backed v1.1 preflight

Use an isolated run ID:

```bash
uv run aviation-data qa build \
  --backend fixture \
  --run-id fixture-preflight-v1.1 \
  --target 1500 \
  --max-fill-cycles 8
```

- If it fails, inspect
  `data/qa/experiments/fixture-preflight-v1.1/capacity_report.json`.
- Do not proceed until all language/type strata have sufficient capacity under:
  - at most four uses per passage;
  - at most one item of each type per passage;
  - exact 50/50 question-language allocation;
  - exact 90/10 answerability allocation;
  - the answerable type targets;
  - at least 135 cross-lingual answerable tasks;
  - 150 safe deterministic mutations.
- Confirm the fixture run produces exactly 1,500 accepted records with clean
  quota diagnostics.

## 7. Run the 200-item primary-model smoke pilot

Start vLLM with the pinned primary model and exact container configuration.
The QA command will verify that the configured model ID is actually served.

```bash
uv run aviation-data qa build \
  --backend vllm \
  --model-choice primary \
  --run-id primary-smoke-v1.1 \
  --endpoint "$VLLM_ENDPOINT" \
  --target 200 \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION"
```

Inspect the run artifacts and require:

- JSON-schema success at least 0.99;
- record-construction success at least 0.95;
- evidence-offset validity exactly 1.00;
- automatic validation acceptance at least 0.80;
- duplicate plus near-duplicate rate at most 0.05;
- no translated, calculated, inferred, or reordered closed answers;
- no unanswerable item marked unsupported from dense similarity alone.

## 8. Compare primary and fallback on the same 400-task manifest

Run the primary model:

```bash
uv run aviation-data qa generate \
  --backend vllm \
  --model-choice primary \
  --run-id "$PRIMARY_RUN_ID" \
  --endpoint "$VLLM_ENDPOINT" \
  --target 400

uv run aviation-data qa validate \
  --run-id "$PRIMARY_RUN_ID" \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION"
```

Restart or reconfigure vLLM with the pinned fallback model, then run:

```bash
uv run aviation-data qa generate \
  --backend vllm \
  --model-choice fallback \
  --run-id "$FALLBACK_RUN_ID" \
  --endpoint "$VLLM_ENDPOINT" \
  --target 400

uv run aviation-data qa validate \
  --run-id "$FALLBACK_RUN_ID" \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION"
```

- Verify both task manifests contain exactly 200 English and 200 Turkish
  questions.
- Verify their task-manifest hashes and task rows are identical.
- Generate the deterministic 15% review sample for each run:

```bash
uv run aviation-data qa review-sample --run-id "$PRIMARY_RUN_ID" --rate 0.15
uv run aviation-data qa review-sample --run-id "$FALLBACK_RUN_ID" --rate 0.15
```

- Blind-review the same strata from both runs.
- Select the model with the higher four-dimension human pass rate.
- Break ties using higher automatic acceptance, then lower duplication.
- Record the selection decision and supporting metrics.

## 9. Build the final 1,500-item QA run

Start vLLM with the selected pinned model.

For a primary-model selection:

```bash
uv run aviation-data qa build \
  --backend vllm \
  --model-choice primary \
  --run-id "$QA_RUN_ID" \
  --endpoint "$VLLM_ENDPOINT" \
  --target 1500 \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION" \
  --max-fill-cycles 8
```

Use `--model-choice fallback` instead if the fallback won the controlled
comparison.

Require:

- exactly 1,500 accepted records;
- exactly 750 English and 750 Turkish questions;
- exactly 1,350 answerable and 150 corpus-unanswerable questions;
- answerable type counts of 540 factual, 405 definition, 203 list/table,
  135 comparison, and 67 temporal;
- at least 135 cross-lingual answerable questions;
- clean quota diagnostics;
- zero evidence-offset failures;
- no schema-invalid rows in the accepted set;
- no unsafe or jointly supported mutations;
- separate non-empty valid-pool, rejected, and quota-overflow diagnostics as
  applicable.

## 10. Create and complete the final QA review sample

```bash
uv run aviation-data qa review-sample \
  --run-id "$QA_RUN_ID" \
  --rate 0.15
```

- Confirm exactly 225 unique QA items and 450 assignment rows.
- Confirm each QA item has reviewer slots A and B.
- Assign two different independent reviewer IDs to every QA item.
- Complete `clarity`, `correctness`, `evidence_sufficiency`, and
  `language_quality` with JSON booleans.
- Save the finalized rows at:

```text
data/qa/experiments/<QA_RUN_ID>/human_reviews.jsonl
```

- Do not overwrite the independent decisions during adjudication.

## 11. Run the Task 6 report

```bash
uv run aviation-data report --qa-run-id "$QA_RUN_ID"
```

Require:

- accepted QA count exactly 1,500;
- QA balance diagnostics clean;
- review sample exactly 225 unique items and 450 rows;
- completed independent double review;
- human correctness/grounding at least 0.95;
- Cohen's kappa at least 0.70;
- exact evidence-offset validity of 1.00;
- immutable model, tokenizer, prompt, planner, passage, and container
  provenance in the run manifest.

## 12. Promote the passing run

Do not promote a smoke, comparison, incomplete, or fixture run.

```bash
uv run aviation-data qa promote --run-id "$QA_RUN_ID"
```

- Confirm the previous benchmark artifacts were archived.
- Confirm `data/qa/current_run.json` names the selected v1.1 run.
- Confirm the promoted legacy QA files all use `schema_version: "1.1.0"`.
- Confirm no v1.0 QA record was migrated or mixed into v1.1.

## 13. Build and inspect the public package

```bash
uv run aviation-data package \
  --public \
  --qa-run-id benchmark \
  --output release/public-v1.1
```

- Inspect the package manifest, license shards, schema files, model/prompt
  manifest, and checksums.
- Confirm `rights_boundary_verified: true`.
- Confirm internal mutation provenance, parent answers, raw model responses,
  review identities, and restricted source bytes are absent from the public
  package.
- Run the final report once more against the promoted benchmark:

```bash
uv run aviation-data report --qa-run-id benchmark
```

## 14. Release decision

- Do not publish until every Task 6 gate passes.
- Record the snapshot date, selected model, all immutable revisions/digests,
  run ID, manifest hashes, review metrics, and promotion archive.
- Keep Wikipedia source-family diversification outside Task 6 as a separately
  tracked follow-up; do not weaken the v1.1 QA gates to compensate for it.
