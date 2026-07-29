from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from aviation_data.io import read_json, read_jsonl, write_json
from aviation_data.models import DocumentRecord, PassageRecord, QARecord, SourceRecord

AIRLINE_RANKING_SIZE = 10


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _human_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "not_evaluated",
            "reviewed_items": 0,
            "double_reviewed_items": 0,
            "correct_and_grounded_rate": None,
            "error_95ci": None,
            "cohens_kappa": None,
        }
    by_qa: dict[str, list[bool]] = defaultdict(list)
    scores = []
    for row in rows:
        accepted = all(
            bool(row.get(dimension))
            for dimension in ("clarity", "correctness", "evidence_sufficiency", "language_quality")
        )
        by_qa[str(row["qa_id"])].append(accepted)
        scores.append(accepted)
    n = len(scores)
    successes = sum(scores)
    rate = successes / n
    z = 1.96
    denominator = 1 + z * z / n
    center = (rate + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
    pairs = [values[:2] for values in by_qa.values() if len(values) >= 2]
    kappa = None
    if pairs:
        observed = sum(left == right for left, right in pairs) / len(pairs)
        first_true = sum(left for left, _ in pairs) / len(pairs)
        second_true = sum(right for _, right in pairs) / len(pairs)
        expected = first_true * second_true + (1 - first_true) * (1 - second_true)
        kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "status": "evaluated",
        "review_rows": n,
        "reviewed_items": len(by_qa),
        "double_reviewed_items": len(pairs),
        "correct_and_grounded_rate": round(rate, 6),
        "error_95ci": [round(1 - center - margin, 6), round(1 - center + margin, 6)],
        "cohens_kappa": round(kappa, 6) if kappa is not None else None,
    }


def _gate(name: str, actual: Any, threshold: Any, passed: bool | None) -> dict[str, Any]:
    return {
        "name": name,
        "actual": actual,
        "threshold": threshold,
        "status": "not_evaluated" if passed is None else ("pass" if passed else "fail"),
    }


def build_report(
    data_dir: Path,
    airline_cohort_path: Path = Path("configs/airline_cohort.yaml"),
) -> dict[str, Any]:
    source_records = read_jsonl(data_dir / "manifests" / "source_records.jsonl", SourceRecord)
    extracted = read_jsonl(data_dir / "extracted" / "documents.jsonl", DocumentRecord)
    accepted_documents = read_jsonl(
        data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
    )
    passages = read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    generated_qa = read_jsonl(data_dir / "qa" / "generated.jsonl", QARecord)
    accepted_qa = read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
    rejected_qa = read_jsonl(data_dir / "qa" / "rejected.jsonl", QARecord)
    qa_validation = (
        read_json(data_dir / "qa" / "validation_report.json")
        if (data_dir / "qa" / "validation_report.json").exists()
        else {}
    )
    curation = (
        read_json(data_dir / "curated" / "curation_report.json")
        if (data_dir / "curated" / "curation_report.json").exists()
        else {}
    )
    passage_report = (
        read_json(data_dir / "passages" / "report.json")
        if (data_dir / "passages" / "report.json").exists()
        else {}
    )
    human_rows = read_jsonl(data_dir / "qa" / "human_reviews.jsonl")
    human = _human_metrics(human_rows)
    extraction_reviews = read_jsonl(data_dir / "reports" / "extraction_reviews.jsonl")
    manual_extraction_rate = (
        sum(bool(row.get("usable")) for row in extraction_reviews) / len(extraction_reviews)
        if extraction_reviews
        else None
    )
    airline_cohort = (
        yaml.safe_load(airline_cohort_path.read_text(encoding="utf-8"))
        if airline_cohort_path.exists()
        else {"status": "missing"}
    )
    cohort_frozen = (
        airline_cohort.get("status") == "frozen"
        and len(
            airline_cohort.get("ranking_inputs", {}).get("passenger_volume", {}).get("top_10", [])
        )
        >= AIRLINE_RANKING_SIZE
        and len(airline_cohort.get("ranking_inputs", {}).get("fleet_size", {}).get("top_10", []))
        >= AIRLINE_RANKING_SIZE
    )

    raw_checksum_ok = all(
        (data_dir / record.storage_path).exists()
        and _file_sha256(data_dir / record.storage_path) == record.sha256
        for record in source_records
    )
    document_checksum_ok = all(
        (data_dir / document.canonical_path).exists()
        and _file_sha256(data_dir / document.canonical_path) == document.canonical_sha256
        for document in extracted
    )
    passage_checksum_ok = all(
        hashlib.sha256(passage.text.encode("utf-8")).hexdigest() == passage.checksum
        for passage in passages
    )
    checksum_coverage = (
        sum((raw_checksum_ok, document_checksum_ok, passage_checksum_ok)) / 3
        if source_records and extracted and passages
        else 0.0
    )
    extraction_rate = len(accepted_documents) / len(extracted) if extracted else 0.0
    terminal_raw = read_jsonl(data_dir / "qa" / "raw_responses.jsonl")
    terminal = [row for row in terminal_raw if row.get("status") in {"accepted", "rejected"}]
    structured_rate = (
        sum(row.get("status") == "accepted" for row in terminal) / len(terminal)
        if terminal
        else 0.0
    )
    evidence_error_reasons = {
        "evidence_passage_missing",
        "evidence_document_missing",
        "evidence_document_mismatch",
        "passage_evidence_offset_mismatch",
        "canonical_evidence_offset_mismatch",
        "evidence_checksum_mismatch",
    }
    evidence_invalid = sum(
        any(reason in evidence_error_reasons for reason in qa.rejection_reasons)
        for qa in rejected_qa
    )
    answerable = sum(qa.answerability.value == "answerable" for qa in [*accepted_qa, *rejected_qa])
    evidence_valid_rate = 1 - evidence_invalid / answerable if answerable else 0.0
    quota_issues = curation.get("quota_issues", [])
    gates = [
        _gate(
            "schema_and_checksum_coverage",
            round(checksum_coverage, 6),
            1.0,
            checksum_coverage == 1.0,
        ),
        _gate(
            "accepted_document_count",
            len(accepted_documents),
            500,
            len(accepted_documents) >= 500,
        ),
        _gate(
            "usable_extraction_manual_sample",
            round(manual_extraction_rate, 6) if manual_extraction_rate is not None else None,
            0.95,
            None if manual_extraction_rate is None else manual_extraction_rate >= 0.95,
        ),
        _gate(
            "structured_generation_success",
            round(structured_rate, 6),
            0.99,
            structured_rate >= 0.99,
        ),
        _gate(
            "accepted_qa_count",
            len(accepted_qa),
            1500,
            len(accepted_qa) >= 1500,
        ),
        _gate(
            "exact_evidence_offset_validity",
            round(evidence_valid_rate, 6),
            1.0,
            evidence_valid_rate == 1.0,
        ),
        _gate(
            "production_tokenizer_pinned",
            passage_report.get("tokenizer", {}).get("revision"),
            "immutable model tokenizer revision",
            bool(passage_report.get("tokenizer", {}).get("production_ready")),
        ),
        _gate(
            "language_and_topic_quotas",
            len(quota_issues),
            0,
            not quota_issues,
        ),
        _gate(
            "qa_balance_quotas",
            len(qa_validation.get("quota_diagnostics", {}).get("issues", [])),
            0,
            not qa_validation.get("quota_diagnostics", {}).get("issues", ["missing"]),
        ),
        _gate(
            "airline_cohort_frozen",
            airline_cohort.get("status"),
            "frozen with two top-10 inputs",
            cohort_frozen,
        ),
        _gate(
            "human_correctness_and_grounding",
            human["correct_and_grounded_rate"],
            0.95,
            (
                None
                if human["correct_and_grounded_rate"] is None
                else human["correct_and_grounded_rate"] >= 0.95
            ),
        ),
        _gate(
            "reviewer_agreement_kappa",
            human["cohens_kappa"],
            0.70,
            (None if human["cohens_kappa"] is None else human["cohens_kappa"] >= 0.70),
        ),
    ]
    statuses = {gate["status"] for gate in gates}
    overall = (
        "fail"
        if "fail" in statuses
        else ("not_evaluated" if "not_evaluated" in statuses else "pass")
    )
    report = {
        "scope": "pilot",
        "overall_status": overall,
        "counts": {
            "source_records": len(source_records),
            "extracted_documents": len(extracted),
            "accepted_documents": len(accepted_documents),
            "passages": len(passages),
            "generated_qa": len(generated_qa),
            "accepted_qa": len(accepted_qa),
            "rejected_qa": len(rejected_qa),
        },
        "language_qa_counts": dict(
            sorted(Counter(qa.question_language.value for qa in accepted_qa).items())
        ),
        "human_review": human,
        "extraction_validation": {
            "automated_acceptance_proxy": round(extraction_rate, 6),
            "manual_review_rows": len(extraction_reviews),
            "manual_usable_rate": manual_extraction_rate,
        },
        "curation": curation,
        "airline_cohort": airline_cohort,
        "gates": gates,
        "fixture_notice": (
            "Bundled fixture runs are expected to fail scale and sampling gates; "
            "full collection must not begin until a real pilot passes."
        ),
    }
    output = data_dir / "reports"
    write_json(output / "pilot_report.json", report)
    output.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Pilot gate report",
        "",
        f"Overall status: **{overall}**",
        "",
        "| Gate | Actual | Threshold | Status |",
        "|---|---:|---:|---|",
    ]
    lines.extend(
        f"| {gate['name']} | {gate['actual']} | {gate['threshold']} | {gate['status']} |"
        for gate in gates
    )
    lines.extend(
        [
            "",
            "Bundled fixture runs are pipeline smoke tests and are expected to fail "
            "scale and sampling gates.",
            "",
        ]
    )
    (output / "pilot_report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
