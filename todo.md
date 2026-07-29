# Production pilot TODO

Complete these items before starting the real 500-document/1,500-QA pilot.
Do not change a gate merely to make the report pass; supply and verify the
underlying evidence first.

## 1. Set the project contact

Edit `configs/sources.yaml` under `project`:

```yaml
project:
  name: bilingual-aviation-corpus
  contact: aviation-data@example.edu
  user_agent: bilingual-aviation-corpus/0.1 (+mailto:aviation-data@example.edu)
```

Use a monitored institutional or project mailbox. The same address should
appear in `contact` and `user_agent`.

Verify:

```bash
uv run aviation-data rights audit --strict
```

Expected result: no `placeholder_contact` warning. Network acquisition remains
disabled unless `fetch` is explicitly run with `--network`.

## 2. Record the exact vLLM container digest

The model repository revisions are already frozen in
`configs/generation.yaml`. The remaining placeholder is:

```yaml
model:
  container_digest: REPLACE_WITH_SHA256_DIGEST
```

On the RTX 3090 host, pull or build the vLLM 0.19 image that will actually run
the benchmark. Start the container using the command documented in
`containers/README.md`.

For a running container, capture the exact image ID:

```bash
docker inspect --format '{{.Image}}' <container-name-or-id>
```

Alternatively, inspect a locally tagged image:

```bash
docker image inspect --format '{{.Id}}' <image-name:tag>
```

The result must look like `sha256:` followed by 64 lowercase hexadecimal
characters. Copy the complete value into `container_digest`. Also record the
image name, repository digest, build command, CUDA/driver version, and serving
command in the release notes.

Verify the fail-closed configuration without making a model request:

```bash
uv run python -c \
  "import yaml; from pathlib import Path; from aviation_data.qa_generation import _generator_config; c=yaml.safe_load(Path('configs/generation.yaml').read_text()); p=Path('prompts/qa_generation.md').read_text(); print(_generator_config(c, p, 'vllm'))"
```

Then run the isolated model pilots:

```bash
uv run aviation-data qa generate \
  --backend vllm \
  --model-choice primary \
  --run-id primary-pilot \
  --target 400

uv run aviation-data qa generate \
  --backend vllm \
  --model-choice fallback \
  --run-id fallback-pilot \
  --target 400
```

Restart or reconfigure the model server between commands so the requested model
ID is actually served. Do not copy an experiment into the benchmark path until
schema stability, grounding, repetition, and English/Turkish quality have been
compared on the identical task set.

## 3. Install and configure the local pinned tokenizer

Status: completed on 2026-07-29. The tokenizer-only assets are installed at
`/home/goksu/llmodels/aviation/qwen36-tokenizer-e4a111c`, and the production
configuration verifies their SHA-256 checksums before loading them.

Production passages must use the exact tokenizer associated with the served
model. Tests use `configs/passages.fixture.yaml`; production uses the pinned
Hugging Face tokenizer in `configs/passages.yaml`.

Download only tokenizer assets from the frozen primary model revision:

```bash
hf download AxisQuant/Qwen3.6-27b-gptq-int4 \
  --revision e4a111caa43e97606b7a5fa20849bbcc051aa4f0 \
  --include 'tokenizer*' \
  --include '*.jinja' \
  --include 'special_tokens_map.json' \
  --local-dir /home/goksu/llmodels/aviation/qwen36-tokenizer-e4a111c
```

Use a durable path outside the Git repository. Confirm that `tokenizer.json`
and `tokenizer_config.json` exist, then edit `configs/passages.yaml`:

```yaml
tokenizer:
  mode: huggingface_local
  id: AxisQuant/Qwen3.6-27b-gptq-int4
  revision: e4a111caa43e97606b7a5fa20849bbcc051aa4f0
  local_path: /home/goksu/llmodels/aviation/qwen36-tokenizer-e4a111c
  checksums:
    tokenizer.json: f399b3cd12fa270d51457bb749fb30863521e8359b8a27059c71b6c2f7d6dd6c
    tokenizer_config.json: 9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d
```

The runtime containing `transformers` must be available when building
passages. The code loads only local files and requires a fast tokenizer with
offset mappings.

Rebuild passages and all downstream QA after changing tokenization:

```bash
uv run aviation-data passages build
uv run aviation-data qa generate --backend vllm --target 1500
uv run aviation-data qa validate
uv run aviation-data report
```

Expected gate: `production_tokenizer_pinned` passes and the passage report names
the immutable tokenizer revision.

## 4. Freeze the airline cohort

Status: completed for the top-10 pilot cohort. Both rankings contain 10
entries, the fleet source is revision-pinned, the cohort is frozen, and the
`airline_cohort_frozen` report gate passes.

Edit `configs/airline_cohort.yaml`. Obtain citable, dated rankings for:

- the top 10 airlines by annual passenger volume; and
- the top 10 airlines by active fleet size.

For each ranking, replace the placeholder source, set the ranking year and
snapshot date, and add at least 10 entries. Recommended entry structure:

```yaml
ranking_inputs:
  passenger_volume:
    source: https://example.org/citable-ranking
    year: 2025
    metric: annual_passengers
    tie_policy: include_all_ties_at_rank_10
    snapshot_date: 2026-07-29
    top_10:
      - rank: 1
        airline: Example Air
        value: 123456789
        unit: passengers
        tied: false
```

Use the same structure for `fleet_size`, with the appropriate metric and unit.
Include all ties at rank 10, even if that creates more than 10 entries.

Create the final cohort as the union of both rankings plus:

- Turkish Airlines
- SunExpress
- Pegasus Airlines
- AJet
- Corendon Airlines

Document aliases and entity IDs so the same airline is not counted twice.
Every fleet fact needs an `as_of` date and field-level citation. Keep official
fleet pages and annual-report binaries manifest-only unless their exact reuse
terms permit redistribution.

After reviewing the completed file, change:

```yaml
status: frozen
```

Verify:

```bash
uv run aviation-data report
```

Expected gate: `airline_cohort_frozen` passes. The current report checks
`status: frozen` and at least 10 entries in each ranking, so source quality,
ties, aliases, and citations still require human review.

## 5. Implement or configure live-source adapters

The bounded `mediawiki_api` adapter is implemented and approved alongside
`file` and `direct`. Disabled portfolio entries deliberately use unimplemented
adapter names such as `wikimedia_dump`, `wikidata_dump`, `bulk_index`, and
`github_release`.

### Bounded Wikipedia pilot (implemented)

Do not enable `wikipedia_en_dump` or `wikipedia_tr_dump` for this pilot. The
enabled API sources in `configs/sources.yaml` query only:

- at most 150 English articles from `Category:Aircraft engines`,
  `Category:Aircraft aerodynamics`, and two explicit topic pages; and
- at most 40 Turkish articles from
  `Kategori:Türkiye merkezli havayolu şirketleri` and its named list page.

Each article is stored separately as rendered HTML and pinned to its exact
MediaWiki revision ID, timestamp, permanent URL, and history URL. The configured
5 MiB limit applies to each API response/article; `max_pages` is a hard source
cap. English is additionally capped at 256 MiB total and Turkish at 64 MiB.
The adapter uses a contact-bearing user agent, serial requests, a
one-request-per-second rate, `maxlag=5`, retry/backoff, and the existing
content-addressed store. The Action API path follows Wikimedia's API access
policy directly; it does not apply the generic crawler `robots.txt` `/w/`
indexing rule to intentional `api.php` requests.

To disable either scoped source, change only its `enabled` value:

```yaml
- source_id: wikipedia_en_aviation_api
  enabled: false
```

Offline fixture acquisition skips all network entries even when they are
enabled. To acquire the frozen live snapshot explicitly:

```bash
uv run aviation-data rights audit --strict
uv run aviation-data fetch --network --snapshot 2026-07-29
uv run aviation-data extract
```

Review the fetched titles and curation report before passage/QA generation.
Reduce `max_pages`, remove a category, or add explicit `page_titles` if the
English set is still broader than desired. Use the actual acquisition date for
each refresh; the manifest, rather than the mutable category, freezes the exact
revision set that was retrieved.

### Fast path for an exact immutable asset URL

If a reviewed source already has an exact file URL:

1. Change its adapter to `direct`.
2. Put only exact asset URLs in `seed_urls`; do not use a landing page.
3. Set an appropriate `max_bytes`.
4. Confirm the exact publication's license evidence and release flags.
5. Set `enabled: true`.
6. Run the strict rights audit before fetching.

Example:

```yaml
adapter: direct
seed_urls:
  - https://publisher.example/releases/2026-07/document.xml
enabled: true
max_bytes: 104857600
```

### Required path for dump indexes, releases, and sitemaps

For each discovery adapter:

1. Add an adapter module under `src/aviation_data/adapters/`.
2. Resolve the seed/index into version-specific immutable asset URLs.
3. Filter assets using the registry's `bulk_patterns`.
4. Preserve discovery-page URL, asset URL, version, and checksums.
5. Integrate discovery into `Fetcher.run` in
   `src/aviation_data/acquisition.py`.
6. Add the adapter name to `implemented_adapters` in
   `src/aviation_data/registry.py` only after implementation and tests.
7. Add tests using local fixtures or `httpx.MockTransport`; CI must not depend
   on live websites.

Adapter-specific requirements:

- `mediawiki_api`: keep title/category discovery bounded with `max_pages`, use
  namespace-zero category members only, request exact revisions with `oldid`,
  and preserve title, page ID, revision ID/timestamp, permanent URL, and history
  URL. This adapter is implemented.
- `wikimedia_dump`: read `dumpstatus.json`, freeze the dump date, select the
  multistream article XML and matching index files, and retain revision IDs.
- `wikidata_dump`: freeze the dated JSON dump and checksum.
- `bulk_index`: extract only expected file links, reject unexpected hosts or
  MIME types, and preserve publication/amendment metadata.
- `github_release`: resolve a tag or commit to its immutable commit SHA and
  verify every selected asset.
- `sitemap`: obey robots and published crawl limits, and use it only when no
  bulk/API source exists.

Important limitation: very large Wikimedia/Wikidata dumps must not be enabled
until acquisition finalization hashes and moves the partial file without
loading the entire completed payload into memory. The current implementation
streams downloads with byte limits but calls `read_bytes()` before
content-addressed finalization.

For every source, review these fields before enabling it:

```yaml
rights:
  state: open | manifest_only | blocked
  license_id: ...
  license_url: ...
  terms_url: ...
  reviewed_on: YYYY-MM-DD
  attribution: ...
  release_source: true | false
  release_derived_text: true | false
  release_qa: true | false
```

Never promote a source to `open` solely because it is publicly accessible.

Verify each enabled batch:

```bash
uv run aviation-data rights audit --strict
uv run aviation-data fetch --snapshot YYYY-MM-DD --network
uv run aviation-data extract
uv run aviation-data curate
```

Inspect `data/manifests/fetch_errors.json`,
`data/extracted/errors.json`, and `data/curated/curation_report.json` before
continuing.

## 6. Supply extraction and QA human reviews

### Extraction review

Generate a stratified extraction sample:

```bash
uv run aviation-data review extraction-sample --rate 0.10
```

This creates:

```text
data/reports/extraction_review_sample.jsonl
```

Assign reviewers and complete every row. `usable` must be a JSON boolean, not a
string:

```json
{
  "document_id": "doc_...",
  "reviewer_id": "reviewer-01",
  "usable": true,
  "format": "pdf",
  "language": "tr",
  "topic": ["safety"],
  "notes": "Headings and table are complete."
}
```

After completion and quality control, save the finalized rows as:

```text
data/reports/extraction_reviews.jsonl
```

The report uses this finalized file, not the assignment template.

### QA double review

After automatic validation, generate the deterministic 15% stratified sample:

```bash
uv run aviation-data qa review-sample --rate 0.15
```

This creates two assignments (`reviewer_slot` A and B) per selected QA item:

```text
data/qa/review_sample.jsonl
```

Assign two different independent reviewers to every QA item. Complete all four
dimensions using JSON booleans:

```json
{
  "qa_id": "qa_...",
  "reviewer_slot": "A",
  "reviewer_id": "reviewer-01",
  "clarity": true,
  "correctness": true,
  "evidence_sufficiency": true,
  "language_quality": true,
  "notes": ""
}
```

Save the finalized assignments as:

```text
data/qa/human_reviews.jsonl
```

Keep both reviewer rows for each QA item. Adjudicate disagreements and preserve
the original independent decisions. Before the research release, add an
explicit adjudication-import command and adjudicated-label file; the current
report computes correctness and Cohen's kappa from the independent review rows
but does not update `QARecord.review_status` from adjudications.

For the 1,500-QA pilot, target approximately 225 double-reviewed QA items. For
the final 20,000-QA benchmark, review 3,000 items, producing 6,000 independent
assignment rows before adjudication.

Verify:

```bash
uv run aviation-data report
```

Expected gates:

- `usable_extraction_manual_sample` is at least 0.95;
- `human_correctness_and_grounding` is at least 0.95; and
- `reviewer_agreement_kappa` is at least 0.70.

## 7. Final pilot sequence

Run the full frozen pilot from an empty data directory:

```bash
uv run aviation-data rights audit --strict
uv run aviation-data fetch --snapshot YYYY-MM-DD --network
uv run aviation-data extract
uv run aviation-data curate
uv run aviation-data passages build
uv run aviation-data qa generate --backend vllm --target 1500
uv run aviation-data qa validate \
  --dense-endpoint http://127.0.0.1:8001/v1 \
  --dense-model intfloat/multilingual-e5-base \
  --dense-revision d128750597153bb5987e10b1c3493a34e5a4502a
uv run aviation-data review extraction-sample --rate 0.10
uv run aviation-data qa review-sample --rate 0.15
# Complete and finalize both review files here.
uv run aviation-data evaluate retrieval --backend bm25
uv run aviation-data evaluate retrieval --backend dense
uv run aviation-data report
uv run aviation-data package --public
```

Do not begin the 5,000-document/20,000-QA collection or publish to GitHub,
Hugging Face, or Zenodo until every pilot gate reports `pass` and the generated
public package's `rights_boundary_verified` field is `true`.
