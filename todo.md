# Fresh end-to-end aviation pipeline runbook

This file is the copy-and-run checklist for rebuilding the project from an
empty `data/` directory. It fetches new revision-pinned Wikipedia pages,
imports every matching local authority file in `datatoprocess/`, extracts and
curates documents, builds passages, generates QA, runs reviews and reports,
promotes a passing QA run, and creates the rights-filtered public package.

The commands assume the repository is located at:

```text
/home/goksu/projects/datacuration
```

Important behavior:

- `datatoprocess/` is separate from `data/`. The fresh-start procedure below
  does not move or delete the downloaded DHMI, SHGM, EASA, or FAA files.
- Existing `data/` is moved into `data-archive/`, not deleted.
- Corpus language ratios are observations against a 70% English / 30% Turkish
  reference. They do not reject documents.
- QA planning remains 50% English / 50% Turkish.
- FAA, DHMI, and SHGM are `manifest_only`: they produce internal extraction
  artifacts but cannot enter passages, QA, or the public package.
- EASA is open and can enter passages and QA.
- Commands that fetch or generate can be rerun with the same snapshot date or
  run ID to resume from checkpoints.
- Do not mix old QA v1.0 files into a new v1.1 run.

## 1. Open the project and install the environment

```bash
cd /home/goksu/projects/datacuration
pwd
uv sync --extra dev --extra formats --extra evaluation
uv run aviation-data --help
```

The `pwd` output must be:

```text
/home/goksu/projects/datacuration
```

For a long run, `tmux` is useful because the commands continue when the SSH
window closes:

```bash
if command -v tmux >/dev/null; then
  tmux new -s aviation-build
else
  echo "tmux is not installed; continue in the current terminal"
fi
```

Inside `tmux`, press `Ctrl-b` and then `d` to detach. Reconnect later with:

```bash
tmux attach -t aviation-build
```

## 2. Move the previous generated data aside

This is the fresh-start step. It preserves the previous `data/` tree in a
timestamped archive and leaves `datatoprocess/` untouched.

```bash
cd /home/goksu/projects/datacuration

PIPELINE_RUN_LABEL=$(date +%Y%m%d-%H%M%S)
PIPELINE_SNAPSHOT_DATE=$(date +%F)

mkdir -p data-archive
if [ -e data ]; then
  mv data "data-archive/data-before-$PIPELINE_RUN_LABEL"
fi
mkdir -p data
```

Show the archived directory and the new empty data root:

```bash
find data-archive -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort
find data -maxdepth 2 -print
```

Create a run-variable file. It is stored under ignored `data/`, so it will not
be committed:

```bash
cat > data/run.env <<EOF
PIPELINE_RUN_LABEL=$PIPELINE_RUN_LABEL
PIPELINE_SNAPSHOT_DATE=$PIPELINE_SNAPSHOT_DATE
QA_RUN_ID=production-v1.1-$PIPELINE_RUN_LABEL
FIXTURE_RUN_ID=fixture-preflight-v1.1-$PIPELINE_RUN_LABEL
SMOKE_RUN_ID=primary-smoke-v1.1-$PIPELINE_RUN_LABEL
PRIMARY_RUN_ID=primary-comparison-v1.1-$PIPELINE_RUN_LABEL
FALLBACK_RUN_ID=fallback-comparison-v1.1-$PIPELINE_RUN_LABEL
MODEL_CHOICE=primary
VLLM_ENDPOINT=http://127.0.0.1:8000/v1
DENSE_ENDPOINT=http://127.0.0.1:8001/v1
DENSE_MODEL=intfloat/multilingual-e5-base
DENSE_REVISION=d128750597153bb5987e10b1c3493a34e5a4502a
EXTRACTION_REVIEWER_ID=replace-with-your-name
QA_REVIEWER_A=replace-with-first-reviewer
QA_REVIEWER_B=replace-with-second-reviewer
PUBLIC_OUTPUT=release/public-$PIPELINE_RUN_LABEL
EOF

nano data/run.env
```

In `nano`, replace the three `replace-with-...` values. Save with `Ctrl-o`,
press Enter, and exit with `Ctrl-x`.

Load the variables in every new terminal:

```bash
source data/run.env
sed -n '1,30p' data/run.env
```

## 3. Check the local authority files

List the files, sizes, and expected filename groups:

```bash
find datatoprocess -maxdepth 1 -type f -printf '%f\t%s bytes\n' | sort

uv run python - <<'PY'
from pathlib import Path

root = Path("datatoprocess")
patterns = {
    "DHMI": "DHMI*.xlsx",
    "SHGM": "SHGM*.pdf",
    "EASA": "EASA*.pdf",
    "FAA": "FAA*.pdf",
}

missing = []
for authority, pattern in patterns.items():
    files = sorted(root.glob(pattern))
    print(f"{authority}: {len(files)} matching file(s)")
    for path in files:
        print(f"  {path.name} ({path.stat().st_size} bytes)")
    if not files:
        missing.append(pattern)

if missing:
    raise SystemExit(f"Missing local file groups: {', '.join(missing)}")
PY
```

Every matching unique checksum becomes one source/document version. Renaming
an identical file does not create a duplicate; changing its bytes creates a
new version.

## 4. Check configuration and tests before fetching

```bash
uv run aviation-data rights audit --strict
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Expected results:

- the rights audit has no issues;
- all tests pass;
- Ruff reports no lint or formatting failures.

Show the currently enabled sources and their adapters:

```bash
uv run python - <<'PY'
from pathlib import Path
import yaml

config = yaml.safe_load(Path("configs/sources.yaml").read_text(encoding="utf-8"))
for source in config["sources"]:
    if source["enabled"]:
        print(f"{source['source_id']}: {source['adapter']}")
PY
```

Check that the airline cohort used by reporting and promotion is frozen:

```bash
uv run python - <<'PY'
from pathlib import Path
import yaml

path = Path("configs/airline_cohort.yaml")
cohort = yaml.safe_load(path.read_text(encoding="utf-8"))

print("Release:", cohort["release"])
print("Snapshot date:", cohort["snapshot_date"])
print("Status:", cohort["status"])
assert cohort["status"] == "frozen"

for name, ranking in cohort["ranking_inputs"].items():
    rows = ranking["top_10"]
    print(f"{name}: {len(rows)} ranked row(s)")
    assert rows, f"{name} has no ranked rows"
    assert ranking["snapshot_date"], f"{name} has no snapshot date"
PY
```

## 5. Fetch a new Wikipedia and local snapshot

This command imports fixtures and local authority files and, because
`--network` is present, fetches Wikipedia and other enabled HTTP sources:

```bash
source data/run.env

uv run aviation-data fetch \
  --snapshot "$PIPELINE_SNAPSHOT_DATE" \
  --network
```

The Wikipedia collection is rate-limited and can take a long time. If the
terminal disconnects, reconnect, load `data/run.env`, and rerun the same
command with the same snapshot date.

Check fetch errors, source counts, Wikipedia revision pins, local provenance,
and MediaWiki exclusions:

```bash
uv run python - <<'PY'
from collections import Counter
from pathlib import Path
import json

def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

errors = json.loads(
    Path("data/manifests/fetch_errors.json").read_text(encoding="utf-8")
)
records = read_jsonl(Path("data/manifests/source_records.jsonl"))
exclusions = json.loads(
    Path("data/manifests/mediawiki_exclusions.json").read_text(encoding="utf-8")
)

counts = Counter(row["registry_source_id"] for row in records)
print("Fetch errors:", len(errors))
for error in errors[:20]:
    print(" ", error)
print("\nSource counts:")
for source_id, count in sorted(counts.items()):
    print(f"  {source_id}: {count}")
print("\nMediaWiki exclusions:", len(exclusions))

assert not errors, "Resolve fetch errors, then rerun the same fetch command."
assert counts["wikipedia_en_aviation_api"] == 400
assert counts["wikipedia_tr_airlines_api"] == 600

for row in records:
    if row["registry_source_id"].startswith("wikipedia_"):
        assert row["source_version"].startswith("revision:")
    if row["fetch_recipe"].get("adapter") == "local_glob":
        recipe = row["fetch_recipe"]
        assert recipe["configured_glob"]
        assert recipe["concrete_relative_path"].startswith("datatoprocess/")
        assert recipe["checksum"] == row["sha256"]
        assert recipe["extraction_profile"]

print("\nFetch checks passed.")
PY
```

If a fetch error is temporary, rerun:

```bash
uv run aviation-data fetch \
  --snapshot "$PIPELINE_SNAPSHOT_DATE" \
  --network
```

## 6. Extract all fetched source versions

```bash
uv run aviation-data extract
```

Check extraction errors, document counts, and all four local structured
artifact types:

```bash
uv run python - <<'PY'
from collections import Counter
from pathlib import Path
import json

def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

errors = json.loads(Path("data/extracted/errors.json").read_text(encoding="utf-8"))
sources = {
    row["source_record_id"]: row
    for row in read_jsonl(Path("data/manifests/source_records.jsonl"))
}
documents = read_jsonl(Path("data/extracted/documents.jsonl"))
counts = Counter(
    sources[row["source_record_id"]]["registry_source_id"]
    for row in documents
)

print("Extraction errors:", len(errors))
for error in errors[:20]:
    print(" ", error)
print("Extracted documents:", len(documents))
print("Documents by source:")
for source_id, count in sorted(counts.items()):
    print(f"  {source_id}: {count}")

required = {
    "dhmi_statistics_manifest": "dhmi_workbook_json",
    "shgm_legislation_manifest": "shgm_abbreviations_json",
    "easa_easy_access_rules": "easa_section_json",
    "faa_handbooks": "faa_sections_json",
}

assert not errors, "Resolve extraction errors, then rerun extraction."
for source_id, artifact_key in required.items():
    matching = [
        row
        for row in documents
        if sources[row["source_record_id"]]["registry_source_id"] == source_id
    ]
    assert matching, f"No extracted document for {source_id}"
    assert all(artifact_key in row["artifact_paths"] for row in matching)
    print(f"{source_id}: {len(matching)} document(s), {artifact_key} present")

print("Extraction checks passed.")
PY
```

View the beginning of each local structured artifact:

```bash
for artifact in \
  dhmi_workbook.json \
  shgm_abbreviations.json \
  easa_section.json \
  faa_sections.json
do
  artifact_path=$(find data/extracted/artifacts -name "$artifact" -print -quit)
  echo "===== $artifact_path ====="
  sed -n '1,60p' "$artifact_path"
done
```

## 7. Curate documents and view ratio observations

```bash
uv run aviation-data curate
```

Show accepted/rejected totals, language observations, real diversity-gate
issues, and rejection flags:

```bash
uv run python - <<'PY'
from collections import Counter
from pathlib import Path
import json

def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

report = json.loads(
    Path("data/curated/curation_report.json").read_text(encoding="utf-8")
)
accepted = read_jsonl(Path("data/curated/accepted_documents.jsonl"))
rejected = read_jsonl(Path("data/curated/rejected_documents.jsonl"))
flags = Counter(
    flag
    for row in rejected
    for flag in row.get("quality_flags", [])
)

print("Accepted documents:", len(accepted))
print("Rejected documents:", len(rejected))
print("\nLanguage observation:")
print(json.dumps(report["language_observation"], indent=2, ensure_ascii=False))
print("\nTopic/source-family gate issues:")
print(json.dumps(report["quota_issues"], indent=2, ensure_ascii=False))
print("\nRejection flags:")
for flag, count in flags.most_common():
    print(f"  {flag}: {count}")

assert report["language_observation"]["blocking"] is False
assert "turkish_snapshot" not in report
assert all(
    "turkish_snapshot_quota_overflow" not in row.get("quality_flags", [])
    for row in rejected
)
PY
```

An `outside_tolerance` language status is informational. Topic-minimum and
QA-eligible source-family issues are real release gates and should be resolved
before publication, but they do not delete otherwise valid documents.

## 8. Create and complete the extraction review

Create the review sample once for this new curated corpus:

```bash
uv run aviation-data review extraction-sample --rate 0.10
```

Show the sample size and first assignment:

```bash
uv run python - <<'PY'
from pathlib import Path
import json

path = Path("data/reports/extraction_review_sample.jsonl")
rows = [
    json.loads(line)
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
print("Extraction review assignments:", len(rows))
print(json.dumps(rows[0], indent=2, ensure_ascii=False))
PY
```

Run the interactive reviewer:

```bash
source data/run.env
uv run aviation-data review extraction \
  --reviewer-id "$EXTRACTION_REVIEWER_ID"
```

The pager shows one canonical document. Press `q` to leave the pager, then:

- enter `t` or `true` when the extraction is usable;
- enter `f` or `false` when it is not usable;
- enter `q` or `quit` at the decision prompt to save progress and stop.

Resume with the same command and same reviewer ID. Use `--no-pager` if printing
the document directly is easier:

```bash
uv run aviation-data review extraction \
  --reviewer-id "$EXTRACTION_REVIEWER_ID" \
  --no-pager
```

After every assignment is complete, check the finalized review:

```bash
uv run python - <<'PY'
from pathlib import Path
import json

sample_path = Path("data/reports/extraction_review_sample.jsonl")
review_path = Path("data/reports/extraction_reviews.jsonl")
sample = [
    json.loads(line)
    for line in sample_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
reviews = [
    json.loads(line)
    for line in review_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]

assert len(reviews) == len(sample)
assert len({row["document_id"] for row in reviews}) == len(reviews)
assert all(row["reviewer_id"].strip() for row in reviews)
assert all(isinstance(row["usable"], bool) for row in reviews)

usable_rate = sum(row["usable"] for row in reviews) / len(reviews)
print("Completed extraction reviews:", len(reviews))
print("Usable extraction rate:", round(usable_rate, 6))
assert usable_rate >= 0.95
PY
```

## 9. Check the pinned production tokenizer and build passages

Verify the configured tokenizer files and checksums:

```bash
uv run python - <<'PY'
from hashlib import sha256
from pathlib import Path
import yaml

config = yaml.safe_load(Path("configs/passages.yaml").read_text(encoding="utf-8"))
tokenizer = config["tokenizer"]
root = Path(tokenizer["local_path"])

print("Tokenizer directory:", root)
assert root.is_dir(), f"Tokenizer directory does not exist: {root}"

for filename, expected in tokenizer["checksums"].items():
    path = root / filename
    assert path.is_file(), f"Missing tokenizer file: {path}"
    actual = sha256(path.read_bytes()).hexdigest()
    print(f"{filename}: {actual}")
    assert actual == expected, f"Checksum mismatch for {filename}"

print("Tokenizer checks passed.")
PY
```

Build production passages:

```bash
uv run aviation-data passages build --config configs/passages.yaml
```

Check the passage report, exact rights boundary, and tokenizer status:

```bash
uv run python - <<'PY'
from pathlib import Path
import json

def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

report = json.loads(Path("data/passages/report.json").read_text(encoding="utf-8"))
documents = {
    row["document_id"]: row
    for row in read_jsonl(Path("data/curated/accepted_documents.jsonl"))
}
passages = read_jsonl(Path("data/passages/passages.jsonl"))
restricted = [
    row["passage_id"]
    for row in passages
    if not documents[row["document_id"]]["release_derived_text"]
    or not documents[row["document_id"]]["release_qa"]
]

print(json.dumps(report, indent=2, ensure_ascii=False))
print("Restricted passages:", len(restricted))

assert report["tokenizer"]["production_ready"] is True
assert not restricted
assert report["passages"] == len(passages)
PY
```

## 10. Run the fixture-backed 1,500-item QA preflight

This checks planning capacity and the complete QA lifecycle without calling a
model server:

```bash
source data/run.env

uv run aviation-data qa build \
  --backend fixture \
  --run-id "$FIXTURE_RUN_ID" \
  --target 1500 \
  --max-fill-cycles 8
```

Check the fixture validation:

```bash
uv run python - "$FIXTURE_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
run_dir = Path("data/qa/experiments") / run_id
validation = json.loads(
    (run_dir / "validation_report.json").read_text(encoding="utf-8")
)
build = json.loads((run_dir / "build_report.json").read_text(encoding="utf-8"))

print(json.dumps(build, indent=2))
print(json.dumps(validation["quota_diagnostics"], indent=2))
print(json.dumps(validation["accepted_qa_language_balance"], indent=2))

assert build["status"] == "complete"
assert validation["accepted"] == 1500
assert validation["quota_diagnostics"]["clean"] is True
assert validation["accepted_qa_language_balance"]["counts"] == {
    "en": 750,
    "tr": 750,
}
PY
```

If capacity is insufficient, show the capacity report:

```bash
sed -n '1,240p' \
  "data/qa/experiments/$FIXTURE_RUN_ID/capacity_report.json"
```

Do not start model-backed QA until the fixture preflight completes.

## 11. Start the primary model and dense-retrieval services

Check Docker and the GPU:

```bash
sudo systemctl enable --now docker
nvidia-smi
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

If `aviation-vllm` and `aviation-tei` already exist, start them:

```bash
docker start aviation-vllm aviation-tei
```

If `aviation-vllm` does not exist, create the pinned primary container:

```bash
docker volume create aviation-vllm-cache

docker run -d \
  --name aviation-vllm \
  --restart unless-stopped \
  --gpus all \
  --ipc=host \
  -p 8000:8000 \
  -v aviation-vllm-cache:/root/.cache/huggingface \
  --entrypoint vllm \
  vllm/vllm-openai@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089 \
  serve AxisQuant/Qwen3.6-27b-gptq-int4 \
  --revision e4a111caa43e97606b7a5fa20849bbcc051aa4f0 \
  --tokenizer-revision e4a111caa43e97606b7a5fa20849bbcc051aa4f0 \
  --language-model-only \
  --gpu-memory-utilization 0.8 \
  --max-model-len 8192 \
  --max-num-seqs 2 \
  --enforce-eager \
  --reasoning-parser qwen3
```

If `aviation-tei` does not exist, create the embedding container:

```bash
docker volume create aviation-tei-cache

docker run -d \
  --name aviation-tei \
  --restart unless-stopped \
  --gpus all \
  --shm-size 1g \
  -p 8001:80 \
  -v aviation-tei-cache:/data \
  ghcr.io/huggingface/text-embeddings-inference:86-1.9 \
  --model-id intfloat/multilingual-e5-base \
  --revision d128750597153bb5987e10b1c3493a34e5a4502a \
  --served-model-name intfloat/multilingual-e5-base
```

Wait for both readiness commands to succeed:

```bash
curl -fsS http://127.0.0.1:8000/v1/models
curl -fsS http://127.0.0.1:8001/health
```

If either command fails, show the latest service logs:

```bash
docker logs --tail 100 aviation-vllm
docker logs --tail 100 aviation-tei
```

## 12. Run a 200-item primary-model smoke build

```bash
source data/run.env

uv run aviation-data qa build \
  --backend vllm \
  --model-choice primary \
  --run-id "$SMOKE_RUN_ID" \
  --endpoint "$VLLM_ENDPOINT" \
  --target 200 \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION" \
  --max-fill-cycles 8
```

Show the smoke metrics:

```bash
uv run python - "$SMOKE_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
run_dir = Path("data/qa/experiments") / run_id
generation = json.loads(
    (run_dir / "generation_report.json").read_text(encoding="utf-8")
)
validation = json.loads(
    (run_dir / "validation_report.json").read_text(encoding="utf-8")
)
build = json.loads((run_dir / "build_report.json").read_text(encoding="utf-8"))

print("Build:", json.dumps(build, indent=2))
print("JSON-schema success:", generation["json_schema_success_rate"])
print(
    "Record-construction success:",
    generation["record_construction_success_rate"],
)
print("Accepted:", validation["accepted"])
print(
    "Duplicate/near-duplicate rate:",
    validation["duplicate_and_near_duplicate_rate"],
)
print("Evidence offsets valid:", validation["evidence_offsets_valid"])
print(
    "Language balance:",
    json.dumps(validation["accepted_qa_language_balance"], indent=2),
)

assert build["status"] == "complete"
assert generation["json_schema_success_rate"] >= 0.99
assert generation["record_construction_success_rate"] >= 0.95
assert validation["evidence_offsets_valid"] is True
assert validation["quota_diagnostics"]["clean"] is True
PY
```

## 13. Optional but recommended: compare primary and fallback

Generate and validate 400 tasks with the primary model:

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

To serve the fallback model on the same port, stop the primary container:

```bash
docker stop aviation-vllm
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

If `aviation-vllm-fallback` does not exist, create it:

```bash
docker run -d \
  --name aviation-vllm-fallback \
  --gpus all \
  --ipc=host \
  -p 8000:8000 \
  -v aviation-vllm-cache:/root/.cache/huggingface \
  --entrypoint vllm \
  vllm/vllm-openai@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089 \
  serve glenic/Qwen3.6-27B-AWQ \
  --revision 3dec12a2e68033ba440364493c074f6b3c6995f6 \
  --tokenizer-revision 3dec12a2e68033ba440364493c074f6b3c6995f6 \
  --language-model-only \
  --gpu-memory-utilization 0.85 \
  --max-model-len 8192 \
  --max-num-seqs 8 \
  --enforce-eager \
  --reasoning-parser qwen3
```

If it already exists:

```bash
docker start aviation-vllm-fallback
```

Check that the fallback is ready:

```bash
curl -fsS http://127.0.0.1:8000/v1/models
```

Generate and validate the fallback comparison:

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

Prove that both runs used the same task rows:

```bash
uv run python - "$PRIMARY_RUN_ID" "$FALLBACK_RUN_ID" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

primary, fallback = sys.argv[1:3]
root = Path("data/qa/experiments")
primary_bytes = (root / primary / "task_manifest.jsonl").read_bytes()
fallback_bytes = (root / fallback / "task_manifest.jsonl").read_bytes()

print("Primary task hash:", sha256(primary_bytes).hexdigest())
print("Fallback task hash:", sha256(fallback_bytes).hexdigest())
assert primary_bytes == fallback_bytes
PY
```

Create review samples for the comparison runs:

```bash
uv run aviation-data qa review-sample \
  --run-id "$PRIMARY_RUN_ID" \
  --rate 0.15

uv run aviation-data qa review-sample \
  --run-id "$FALLBACK_RUN_ID" \
  --rate 0.15
```

Show the two automatic validation summaries:

```bash
uv run python - "$PRIMARY_RUN_ID" "$FALLBACK_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

root = Path("data/qa/experiments")
for run_id in sys.argv[1:]:
    generation = json.loads(
        (root / run_id / "generation_report.json").read_text(encoding="utf-8")
    )
    validation = json.loads(
        (root / run_id / "validation_report.json").read_text(encoding="utf-8")
    )
    print(f"\n{run_id}")
    print("  schema success:", generation["json_schema_success_rate"])
    print(
        "  construction success:",
        generation["record_construction_success_rate"],
    )
    print("  accepted:", validation["accepted"])
    print(
        "  duplicate rate:",
        validation["duplicate_and_near_duplicate_rate"],
    )
    print("  rejection reasons:", validation["rejection_reasons"])
PY
```

Use independent human review plus these metrics to choose `primary` or
`fallback`. Update `MODEL_CHOICE` in `data/run.env`, then reload it:

```bash
nano data/run.env
source data/run.env
echo "$MODEL_CHOICE"
```

Start the selected server before the final run. For primary:

```bash
docker stop aviation-vllm-fallback
docker start aviation-vllm
curl -fsS http://127.0.0.1:8000/v1/models
```

For fallback:

```bash
docker stop aviation-vllm
docker start aviation-vllm-fallback
curl -fsS http://127.0.0.1:8000/v1/models
```

## 14. Build the final 1,500-item QA run

```bash
source data/run.env

uv run aviation-data qa build \
  --backend vllm \
  --model-choice "$MODEL_CHOICE" \
  --run-id "$QA_RUN_ID" \
  --endpoint "$VLLM_ENDPOINT" \
  --target 1500 \
  --dense-endpoint "$DENSE_ENDPOINT" \
  --dense-model "$DENSE_MODEL" \
  --dense-revision "$DENSE_REVISION" \
  --max-fill-cycles 8
```

The build is checkpointed. Rerun the exact command if it is interrupted.

Check final counts and balance:

```bash
uv run python - "$QA_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
run_dir = Path("data/qa/experiments") / run_id
validation = json.loads(
    (run_dir / "validation_report.json").read_text(encoding="utf-8")
)
build = json.loads((run_dir / "build_report.json").read_text(encoding="utf-8"))

print(json.dumps(build, indent=2))
print(json.dumps(validation["quota_diagnostics"], indent=2))
print(json.dumps(validation["accepted_qa_language_balance"], indent=2))
print("Rejection reasons:", validation["rejection_reasons"])

actual = validation["quota_diagnostics"]["actual"]
assert build["status"] == "complete"
assert validation["accepted"] == 1500
assert validation["quota_diagnostics"]["clean"] is True
assert actual["question_language"] == {"en": 750, "tr": 750}
assert actual["answerability"] == {
    "answerable": 1350,
    "corpus_unanswerable": 150,
}
assert validation["evidence_offsets_valid"] is True
PY
```

## 15. Create and complete the final QA review

Create exactly 225 unique review items and 450 A/B assignments:

```bash
uv run aviation-data qa review-sample \
  --run-id "$QA_RUN_ID" \
  --rate 0.15
```

Check the assignment counts and show the first row:

```bash
uv run python - "$QA_RUN_ID" <<'PY'
from collections import Counter
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
path = Path("data/qa/experiments") / run_id / "review_sample.jsonl"
rows = [
    json.loads(line)
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
qa_ids = {row["qa_id"] for row in rows}
slots = Counter(row["reviewer_slot"] for row in rows)

print("Unique QA items:", len(qa_ids))
print("Assignment rows:", len(rows))
print("Slots:", dict(slots))
print(json.dumps(rows[0], indent=2, ensure_ascii=False))

assert len(qa_ids) == 225
assert len(rows) == 450
assert slots == {"A": 225, "B": 225}
PY
```

Two genuinely independent people are required for the promotion gate. Split
the assignments into reviewer files:

```bash
uv run python - "$QA_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
run_dir = Path("data/qa/experiments") / run_id
rows = [
    json.loads(line)
    for line in (run_dir / "review_sample.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]

for slot in ("A", "B"):
    output = run_dir / f"reviewer_{slot}.jsonl"
    selected = [row for row in rows if row["reviewer_slot"] == slot]
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in selected) + "\n",
        encoding="utf-8",
    )
    print(output, len(selected))
PY
```

Reviewer A edits:

```bash
nano "data/qa/experiments/$QA_RUN_ID/reviewer_A.jsonl"
```

Reviewer B edits:

```bash
nano "data/qa/experiments/$QA_RUN_ID/reviewer_B.jsonl"
```

For every row, each reviewer must:

- set `reviewer_id` to their own non-empty identity;
- replace `clarity: null` with `clarity: true` or `clarity: false`;
- replace `correctness: null` with a JSON boolean;
- replace `evidence_sufficiency: null` with a JSON boolean;
- replace `language_quality: null` with a JSON boolean;
- add a short `notes` value when something fails.

Do not copy one reviewer's decisions into the other file.

Merge and validate the independent review files:

```bash
uv run python - "$QA_RUN_ID" <<'PY'
from collections import defaultdict
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
run_dir = Path("data/qa/experiments") / run_id

def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

sample = read_jsonl(run_dir / "review_sample.jsonl")
reviews = [
    *read_jsonl(run_dir / "reviewer_A.jsonl"),
    *read_jsonl(run_dir / "reviewer_B.jsonl"),
]
dimensions = (
    "clarity",
    "correctness",
    "evidence_sufficiency",
    "language_quality",
)

sample_keys = {(row["qa_id"], row["reviewer_slot"]) for row in sample}
review_keys = {(row["qa_id"], row["reviewer_slot"]) for row in reviews}
assert len(reviews) == 450
assert review_keys == sample_keys
assert all(row["reviewer_id"].strip() for row in reviews)
assert all(
    all(isinstance(row[field], bool) for field in dimensions)
    for row in reviews
)

reviewers_by_qa = defaultdict(set)
for row in reviews:
    reviewers_by_qa[row["qa_id"]].add(row["reviewer_id"])
assert all(len(reviewers) == 2 for reviewers in reviewers_by_qa.values())

reviews.sort(key=lambda row: (row["qa_id"], row["reviewer_slot"]))
(run_dir / "human_reviews.jsonl").write_text(
    "\n".join(json.dumps(row, ensure_ascii=False) for row in reviews) + "\n",
    encoding="utf-8",
)
print("Wrote", run_dir / "human_reviews.jsonl")
PY
```

## 16. Build the pilot report and show every failing gate

```bash
uv run aviation-data report --qa-run-id "$QA_RUN_ID"
```

Show the report status, QA balance, human metrics, and non-passing gates:

```bash
uv run python - "$QA_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys

run_id = sys.argv[1]
path = Path("data/qa/experiments") / run_id / "pilot_report.json"
report = json.loads(path.read_text(encoding="utf-8"))

print("Overall status:", report["overall_status"])
print(
    "Accepted QA language balance:",
    json.dumps(report["accepted_qa_language_balance"], indent=2),
)
print("Human review:", json.dumps(report["human_review"], indent=2))
print("\nNon-passing gates:")
for gate in report["gates"]:
    if gate["status"] != "pass":
        print(
            f"  {gate['name']}: status={gate['status']} "
            f"actual={gate['actual']} threshold={gate['threshold']}"
        )
PY
```

Promotion specifically requires these gates to pass:

- accepted QA count;
- exact QA planning balance;
- completed independent double review;
- human correctness and grounding of at least 0.95;
- Cohen's kappa of at least 0.70.

Do not change honest review decisions merely to make a gate pass.

## 17. Promote the passing final QA run

Only run this after the required report gates pass:

```bash
uv run aviation-data qa promote --run-id "$QA_RUN_ID"
```

Check the promoted pointer and schema versions:

```bash
uv run python - <<'PY'
from pathlib import Path
import json

pointer = json.loads(Path("data/qa/current_run.json").read_text(encoding="utf-8"))
accepted = [
    json.loads(line)
    for line in Path("data/qa/accepted.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]

print(json.dumps(pointer, indent=2))
print("Promoted accepted QA:", len(accepted))
print("Schema versions:", sorted({row["schema_version"] for row in accepted}))

assert len(accepted) == 1500
assert {row["schema_version"] for row in accepted} == {"1.1.0"}
PY
```

Run the report against the promoted benchmark:

```bash
uv run aviation-data report --qa-run-id benchmark
```

## 18. Run retrieval evaluation

Run the local BM25 baseline:

```bash
uv run aviation-data evaluate retrieval --backend bm25
```

Run dense retrieval through TEI:

```bash
uv run aviation-data evaluate retrieval \
  --backend dense \
  --dense-endpoint "$DENSE_ENDPOINT"
```

Show generated evaluation reports:

```bash
find data/reports -maxdepth 2 -type f -printf '%p\n' | sort
```

`evaluate answers` and `evaluate judge` require an external predictions JSONL
file. View their exact required options with:

```bash
uv run aviation-data evaluate answers --help
uv run aviation-data evaluate judge --help
```

## 19. Export schemas and build the public package

Export record schemas:

```bash
uv run aviation-data schemas export --output schemas/v1.1.0
```

Build a new rights-filtered package at the unique output path from
`data/run.env`:

```bash
uv run aviation-data package \
  --public \
  --qa-run-id benchmark \
  --output "$PUBLIC_OUTPUT"
```

Check the package manifest and every checksum:

```bash
uv run python - "$PUBLIC_OUTPUT" <<'PY'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1])
manifest = json.loads(
    (root / "package_manifest.json").read_text(encoding="utf-8")
)
print(json.dumps(manifest, indent=2, ensure_ascii=False))

assert manifest["rights_boundary_verified"] is True
assert manifest["qa"] == 1500
assert manifest["fixture_qa"] == 0
PY

(cd "$PUBLIC_OUTPUT" && sha256sum -c checksums.sha256)
```

List every package file:

```bash
find "$PUBLIC_OUTPUT" -type f -printf '%P\n' | sort
```

## 20. Final completion check

Run the repository checks one last time:

```bash
uv run aviation-data rights audit --strict
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run aviation-data report --qa-run-id benchmark
```

Show the final run identifiers and package location:

```bash
source data/run.env
printf 'Snapshot: %s\n' "$PIPELINE_SNAPSHOT_DATE"
printf 'QA run: %s\n' "$QA_RUN_ID"
printf 'Model choice: %s\n' "$MODEL_CHOICE"
printf 'Public package: %s\n' "$PUBLIC_OUTPUT"
```

The end-to-end run is complete when:

- fetch and extraction error files are empty;
- all four local source profiles produced their JSON artifacts;
- extraction review is complete;
- production passages use the pinned tokenizer and contain no restricted data;
- the final QA run contains exactly 1,500 accepted v1.1 records;
- accepted QA contains 750 English and 750 Turkish questions;
- the independent human review and required promotion gates pass;
- the benchmark is promoted;
- retrieval evaluation has run;
- the public package reports `rights_boundary_verified: true`;
- package checksums pass; and
- tests and Ruff checks pass.

## 21. Safe stopping and resuming

At any point, record the current files:

```bash
source data/run.env
find data -maxdepth 3 -type f -printf '%TY-%Tm-%Td %TH:%TM\t%p\n' | sort | tail -40
```

Safe resume rules:

- interrupted fetch: rerun the same `fetch` command and snapshot date;
- interrupted extraction or curation: rerun that command;
- interrupted extraction review: rerun with the same reviewer ID;
- interrupted QA build: rerun the exact command with the same run ID and
  configuration;
- new terminal: run `source data/run.env`;
- stopped containers: restart them with `docker start`;
- never copy QA files between experiment run directories.

Stop the model services when finished:

```bash
docker stop aviation-vllm aviation-tei
docker stop aviation-vllm-fallback 2>/dev/null || true
```
