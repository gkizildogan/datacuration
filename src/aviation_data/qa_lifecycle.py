from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aviation_data.io import read_json, read_jsonl, write_json
from aviation_data.models import QARecord
from aviation_data.qa_planning import file_sha256, qa_run_dir
from aviation_data.reporting import build_report

LEGACY_ARTIFACTS = (
    "accepted.jsonl",
    "accepted.parquet",
    "build_report.json",
    "capacity_report.json",
    "evidence_candidates.jsonl",
    "generated.jsonl",
    "generation_manifest.json",
    "generation_rejections.jsonl",
    "generation_report.json",
    "human_reviews.jsonl",
    "mutation_provenance.jsonl",
    "passage_snapshot.jsonl",
    "pilot_report.json",
    "pilot_report.md",
    "quota_overflow.jsonl",
    "raw_responses.jsonl",
    "rejected.jsonl",
    "review_sample.jsonl",
    "run_manifest.json",
    "task_manifest.jsonl",
    "valid_pool.jsonl",
    "validation_rejections.jsonl",
    "validation_report.json",
)


def preserve_legacy_baseline(
    data_dir: Path,
    *,
    baseline_id: str = "baseline-v1",
) -> dict[str, Any]:
    legacy_dir = data_dir / "qa"
    baseline_dir = qa_run_dir(data_dir, baseline_id)
    marker = baseline_dir / "READ_ONLY.json"
    if marker.exists():
        return read_json(marker)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in LEGACY_ARTIFACTS:
        source = legacy_dir / name
        if not source.is_file():
            continue
        destination = baseline_dir / name
        shutil.copy2(source, destination)
        copied.append(name)
    manifest = {
        "baseline_id": baseline_id,
        "read_only": True,
        "created_at": datetime.now(UTC).isoformat(),
        "artifacts": {
            name: file_sha256(baseline_dir / name)
            for name in sorted(copied)
        },
    }
    write_json(marker, manifest)
    return manifest


def _validate_reviews(
    sample: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[str]:
    issues = []
    sample_keys = Counter(
        (str(row.get("qa_id")), str(row.get("reviewer_slot"))) for row in sample
    )
    review_keys = Counter(
        (str(row.get("qa_id")), str(row.get("reviewer_slot"))) for row in reviews
    )
    if sample_keys != review_keys:
        issues.append("human review rows do not exactly match review assignments")
    sample_slots: dict[str, set[str]] = defaultdict(set)
    for row in sample:
        sample_slots[str(row.get("qa_id"))].add(str(row.get("reviewer_slot")))
    if any(slots != {"A", "B"} for slots in sample_slots.values()):
        issues.append("each sampled QA item must contain reviewer slots A and B")
    reviewers: dict[str, set[str]] = defaultdict(set)
    for row in reviews:
        reviewer_id = str(row.get("reviewer_id", "")).strip()
        if not reviewer_id:
            issues.append("human review has an empty reviewer_id")
        reviewers[str(row.get("qa_id"))].add(reviewer_id)
        if not all(
            isinstance(row.get(dimension), bool)
            for dimension in (
                "clarity",
                "correctness",
                "evidence_sufficiency",
                "language_quality",
            )
        ):
            issues.append("human review has incomplete boolean dimensions")
    if any(len(values) != 2 for values in reviewers.values()):
        issues.append("each sampled QA item must have two independent reviewer IDs")
    return sorted(set(issues))


def promote_qa_run(
    data_dir: Path,
    run_id: str,
    *,
    airline_cohort_path: Path,
) -> dict[str, Any]:
    if run_id == "benchmark":
        raise ValueError("benchmark is already the promoted legacy path")
    run_dir = qa_run_dir(data_dir, run_id)
    accepted = read_jsonl(run_dir / "accepted.jsonl", QARecord)
    validation = read_json(run_dir / "validation_report.json")
    sample = read_jsonl(run_dir / "review_sample.jsonl")
    reviews = read_jsonl(run_dir / "human_reviews.jsonl")
    unique_sample = {str(row.get("qa_id")) for row in sample if row.get("qa_id")}
    issues = []
    if len(accepted) != 1_500:
        issues.append(f"accepted QA count is {len(accepted)}, expected exactly 1500")
    if not validation.get("quota_diagnostics", {}).get("clean"):
        issues.append("QA quota diagnostics are not clean")
    if len(unique_sample) != 225 or len(sample) != 450:
        issues.append(
            f"review sample has {len(unique_sample)} unique items/{len(sample)} rows; "
            "expected 225/450"
        )
    issues.extend(_validate_reviews(sample, reviews))
    report = build_report(
        data_dir,
        airline_cohort_path,
        qa_run_id=run_id,
    )
    required_gates = {
        "accepted_qa_count",
        "qa_balance_quotas",
        "double_review_complete",
        "human_correctness_and_grounding",
        "reviewer_agreement_kappa",
    }
    failed = [
        gate["name"]
        for gate in report["gates"]
        if gate["name"] in required_gates and gate["status"] != "pass"
    ]
    if failed:
        issues.append(f"Task 6 gates are not passing: {', '.join(sorted(failed))}")
    if issues:
        raise ValueError("run cannot be promoted: " + "; ".join(issues))

    baseline = preserve_legacy_baseline(data_dir)
    legacy_dir = data_dir / "qa"
    current_files = [
        legacy_dir / name for name in LEGACY_ARTIFACTS if (legacy_dir / name).is_file()
    ]
    archive_key = (
        file_sha256(legacy_dir / "current_run.json")[:12]
        if (legacy_dir / "current_run.json").is_file()
        else file_sha256(run_dir / "run_manifest.json")[:12]
    )
    archive_dir = legacy_dir / "archive" / f"benchmark-{archive_key}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for source in current_files:
        shutil.copy2(source, archive_dir / source.name)

    staging = Path(tempfile.mkdtemp(prefix=f".promote-{run_id}-", dir=legacy_dir))
    promoted = []
    try:
        for name in LEGACY_ARTIFACTS:
            source = run_dir / name
            if not source.is_file():
                continue
            shutil.copy2(source, staging / name)
            promoted.append(name)
        pointer = {
            "run_id": run_id,
            "promoted_at": datetime.now(UTC).isoformat(),
            "run_manifest_sha256": file_sha256(run_dir / "run_manifest.json"),
            "archived_previous_benchmark": str(archive_dir),
        }
        write_json(staging / "current_run.json", pointer)
        promoted_set = set(promoted)
        for source in current_files:
            if source.name not in promoted_set:
                source.unlink()
        for name in promoted:
            os.replace(staging / name, legacy_dir / name)
        # The pointer is the commit marker and is replaced last.
        os.replace(staging / "current_run.json", legacy_dir / "current_run.json")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = {
        "status": "promoted",
        "run_id": run_id,
        "artifacts": promoted,
        "archive": str(archive_dir),
        "baseline": baseline,
    }
    write_json(run_dir / "promotion.json", result)
    return result
