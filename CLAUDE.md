# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also `AGENTS.md` (repository guidelines) and `todo.md` (the full fresh-rebuild runbook
against real sources).

## Commands

```bash
uv sync --extra dev --extra formats          # add --extra evaluation (bm25/duckdb), --extra release (dvc)
uv run pytest                                # full suite (offline, fixture-only)
uv run pytest tests/test_passages.py::test_name
uv run ruff format --check src tests && uv run ruff check src tests   # what CI runs
uv run aviation-data rights audit            # CI also runs this; fails on registry rights errors
uv run dvc repro                             # fixture pipeline end to end (needs release extra)
```

Offline smoke run of the pipeline (fixture QA backend, no network, no model):

```bash
uv run aviation-data fetch --snapshot 2026-07-29
uv run aviation-data extract && uv run aviation-data curate
uv run aviation-data passages build
uv run aviation-data qa build --backend fixture --run-id fixture-v2 --target 8
uv run aviation-data report --qa-run-id fixture-v2
uv run aviation-data package --public
```

Python is pinned to 3.11–3.12 (CI uses 3.11). All CLI commands accept `--data-dir` (default
`data/`), so tests run the pipeline into `tmp_path`.

## Architecture

A linear, file-based pipeline. Each stage is one module in `src/aviation_data/`, invoked by
a Typer command in `cli.py`, reading the previous stage's JSONL from `data/` and writing its
own (helpers in `io.py`; Parquet only if `pyarrow` is installed). All records are Pydantic
models in `models.py`; `aviation-data schemas export` writes them to `schemas/v<version>/`.
Core records are schema `1.0.0`; QA records are `1.1.0` (`QA_SCHEMA_VERSION`) — don't mix
v1.0 QA artifacts into v1.1 runs.

1. **fetch** (`acquisition.py`, `Fetcher`): reads `configs/sources.yaml` via `registry.py`.
   Adapters: `file`, `direct`, `local_glob` (authority files in ignored `datatoprocess/`,
   e.g. `DHMI*.xlsx`, `SHGM*.pdf`, `EASA*.pdf`, `FAA*.pdf`), `mediawiki_api`
   (`adapters/mediawiki_api.py`, bounded, revision-pinned Wikipedia queries). Bytes go to
   content-addressed `data/raw/sha256/` (immutable); events to `data/manifests/`. HTTP is
   only used with `--network`. `registry.audit_registry` rejects enabled sources whose
   adapter isn't in its `implemented_adapters` set — update it when adding an adapter.
2. **extract** (`extraction.py`): `_extract_payload` dispatches first on the source's
   `extraction.profile` (source-specific parsers in `adapters/`: `dhmi_workbook_v1`,
   `shgm_abbreviations_v1`, `easa_toc_section_v1`, `faa_purpose_applicability_v1`), then on
   MIME/format (PDF via pymupdf4llm with OCR fallback, DOCX, XLSX, HTML, text).
3. **curate** (`curation.py`, `configs/sampling.yaml`): accept/reject documents. Language
   ratio (70% EN / 30% TR reference) is reported, not enforced.
4. **passages build** (`passages.py`): token-bounded passages. `configs/passages.yaml`
   uses a local, checksum-verified tokenizer; tests must use `configs/passages.fixture.yaml`.
5. **qa** (`qa_planning.py` → `qa_generation.py` → `qa_validation.py` → `qa_lifecycle.py`):
   each run is isolated under `data/qa/experiments/<run_id>/`. `qa build` loops
   generate+validate for up to `--max-fill-cycles` until `--target` is reached (exit code 2
   on `CapacityError`). Planning is 50/50 EN/TR. `qa promote` gates a run and copies it to
   the benchmark paths; its exact-count/review-sample-size/correctness/kappa thresholds
   default to the production values (1500 / 0.15 / 0.95 / 0.70) but are parametric via
   `--qa-target`/`--review-sample-rate`/`--min-correct-rate`/`--min-kappa` (same flags on
   `report`) for smaller/faster test runs — see `todo.md` step 2. Runs checkpoint and resume on the same `--run-id`. The `vllm`
   backend refuses to run unless `configs/generation.yaml` has immutable model revision and
   container digest pins (see `containers/README.md`). Prompt: `prompts/qa_generation.md`.
   QA pairs are double-reviewed by two independent *LLM* reviewers, not people:
   `qa review-sample` allocates each sampled QA item to reviewer slots A and B, then
   `qa llm-review --reviewer-slot A` (project's **primary** model) and
   `qa llm-review --reviewer-slot B` (project's **fallback** model) each grade their slot's
   items against `prompts/qa_review.md` (`qa_llm_review.py`). Once both
   `llm_review_A.jsonl` / `llm_review_B.jsonl` exist they are merged into
   `human_reviews.jsonl` automatically — the promotion gate's file name, schema, and Cohen's
   kappa/correctness thresholds are unchanged; only who fills them in changed.
6. **review** (`review.py`: samples; `qa_llm_review.py`: the two QA reviewers), **report**
   (`reporting.py`, includes airline-cohort gate from `configs/airline_cohort.yaml`),
   **evaluate** (`evaluation.py`: bm25/dense retrieval, answer judge, pinned in
   `configs/evaluation.yaml`).
7. **package --public** (`release.py`): fail-closed public release into `release/public/`,
   sharded by license family, with checksums.

### Rights model (central invariant)

Every source and document carries a `RightsState`: `open`, `manifest_only`, or `blocked`.
Blocked sources are never fetched. `manifest_only` sources (currently FAA, DHMI, SHGM) may
produce internal extraction artifacts but must never reach passages, QA, or the public
package. `open` sources must set `release_source`/`release_derived_text`. The key regression
test is `tests/test_end_to_end.py::test_offline_pipeline_and_public_rights_boundary`.

### Local-only directories

`data/`, `data-archive/`, `data-superseded-*/`, `datatoprocess/`, and `release/` are
gitignored (generated or machine-local); `model-cache/` holds local model assets (not
ignored, so don't commit its contents). `fixtures/` holds the tiny CC0 offline source portfolio used by
tests and the fixture sources enabled in `configs/sources.yaml`.
