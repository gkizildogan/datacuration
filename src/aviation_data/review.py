from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from aviation_data.ids import sha256_text
from aviation_data.io import read_jsonl, write_jsonl
from aviation_data.models import DocumentRecord, QARecord
from aviation_data.qa_planning import qa_run_dir


def create_extraction_review_sample(data_dir: Path, rate: float = 0.10) -> list[dict[str, object]]:
    if not 0 < rate <= 1:
        raise ValueError("rate must be in (0, 1]")
    accepted_path = data_dir / "curated" / "accepted_documents.jsonl"
    if not accepted_path.exists():
        raise FileNotFoundError(
            f"{accepted_path} does not exist; run 'aviation-data curate' before sampling"
        )
    documents = read_jsonl(accepted_path, DocumentRecord)
    strata: dict[tuple[str, ...], list[DocumentRecord]] = defaultdict(list)
    for document in documents:
        key = (
            document.language.value,
            document.native_format,
            document.authority_level,
            ",".join(sorted(topic.value for topic in document.topics)),
        )
        strata[key].append(document)
    selected = []
    for key, rows in sorted(strata.items()):
        ordered = sorted(rows, key=lambda row: sha256_text(f"extraction-review:{row.document_id}"))
        count = min(len(rows), max(1, math.ceil(len(rows) * rate)))
        for document in ordered[:count]:
            selected.append(
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "review_scope": "accepted_corpus",
                    "stratum": list(key),
                    "canonical_path": document.canonical_path,
                    "source_url": document.source_url,
                    "reviewer_id": "",
                    "usable": None,
                    "format": document.native_format,
                    "language": document.language.value,
                    "topic": [topic.value for topic in document.topics],
                    "canonical_char_count": document.canonical_char_count,
                    "canonical_token_count": document.canonical_token_count,
                    "quality_flags": document.quality_flags,
                    "notes": "",
                }
            )
    selected.sort(key=lambda item: str(item["document_id"]))
    write_jsonl(data_dir / "reports" / "extraction_review_sample.jsonl", selected)
    return selected


def create_review_sample(
    data_dir: Path,
    rate: float = 0.15,
    *,
    run_id: str,
) -> list[dict[str, object]]:
    if not 0 < rate <= 1:
        raise ValueError("rate must be in (0, 1]")
    run_dir = qa_run_dir(data_dir, run_id)
    accepted_path = run_dir / "accepted.jsonl"
    if not accepted_path.is_file():
        raise FileNotFoundError(
            f"{accepted_path} does not exist; run 'aviation-data qa validate --run-id "
            f"{run_id}' before sampling"
        )
    qa_rows = read_jsonl(accepted_path, QARecord)
    if not qa_rows:
        raise ValueError(f"{accepted_path} contains no accepted QA records")
    documents = {
        document.document_id: document
        for document in read_jsonl(
            data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
        )
    }
    strata: dict[tuple[str, ...], list[QARecord]] = defaultdict(list)
    for qa in qa_rows:
        key = (
            qa.question_language.value,
            qa.primary_type.value,
            str(qa.cross_lingual),
            qa.answerability.value,
            ",".join(
                sorted(
                    {
                        topic.value
                        for document_id in qa.source_document_ids
                        if (document := documents.get(document_id))
                        for topic in document.topics
                    }
                )
            ),
            ",".join(
                sorted(
                    {
                        document.source_family
                        for document_id in qa.source_document_ids
                        if (document := documents.get(document_id))
                    }
                )
            ),
            ",".join(
                sorted(
                    {
                        document.native_format
                        for document_id in qa.source_document_ids
                        if (document := documents.get(document_id))
                    }
                )
            ),
            next(
                (flag for flag in qa.flags if flag.startswith("difficulty:")), "difficulty:standard"
            ),
        )
        strata[key].append(qa)
    desired_unique = math.ceil(len(qa_rows) * rate)
    allocations = {key: math.floor(len(rows) * rate) for key, rows in strata.items()}
    remaining = desired_unique - sum(allocations.values())
    allocation_order = sorted(
        strata,
        key=lambda key: (
            -(len(strata[key]) * rate - math.floor(len(strata[key]) * rate)),
            key,
        ),
    )
    for key in allocation_order[:remaining]:
        allocations[key] += 1

    selected = []
    for key, rows in sorted(strata.items()):
        ordered = sorted(rows, key=lambda qa: sha256_text(f"review:{qa.qa_id}"))
        count = allocations[key]
        for qa in ordered[:count]:
            for reviewer_slot in ("A", "B"):
                selected.append(
                    {
                        "qa_id": qa.qa_id,
                        "reviewer_slot": reviewer_slot,
                        "stratum": list(key),
                        "question": qa.question,
                        "answer": qa.answer,
                        "answer_items": qa.answer_items,
                        "evidence": [item.quote for item in qa.evidence],
                        "reviewer_id": "",
                        "clarity": None,
                        "correctness": None,
                        "evidence_sufficiency": None,
                        "language_quality": None,
                        "notes": "",
                    }
                )
    selected.sort(key=lambda item: (str(item["qa_id"]), str(item["reviewer_slot"])))
    if len({str(item["qa_id"]) for item in selected}) != desired_unique:
        raise AssertionError("review sampler did not produce the exact unique-item target")
    if len(selected) != desired_unique * 2:
        raise AssertionError("review sampler did not produce exactly two assignments per item")
    write_jsonl(run_dir / "review_sample.jsonl", selected)
    return selected
