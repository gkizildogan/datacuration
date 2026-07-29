from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from aviation_data.ids import sha256_text
from aviation_data.io import read_jsonl, write_jsonl
from aviation_data.models import DocumentRecord, QARecord


def create_extraction_review_sample(data_dir: Path, rate: float = 0.10) -> list[dict[str, object]]:
    if not 0 < rate <= 1:
        raise ValueError("rate must be in (0, 1]")
    documents = read_jsonl(data_dir / "extracted" / "documents.jsonl", DocumentRecord)
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
                    "stratum": list(key),
                    "canonical_path": document.canonical_path,
                    "reviewer_id": "",
                    "usable": None,
                    "format": document.native_format,
                    "language": document.language.value,
                    "topic": [topic.value for topic in document.topics],
                    "notes": "",
                }
            )
    selected.sort(key=lambda item: str(item["document_id"]))
    write_jsonl(data_dir / "reports" / "extraction_review_sample.jsonl", selected)
    return selected


def create_review_sample(data_dir: Path, rate: float = 0.15) -> list[dict[str, object]]:
    if not 0 < rate <= 1:
        raise ValueError("rate must be in (0, 1]")
    qa_rows = read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
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
    selected = []
    for key, rows in sorted(strata.items()):
        ordered = sorted(rows, key=lambda qa: sha256_text(f"review:{qa.qa_id}"))
        count = min(len(rows), max(1, math.ceil(len(rows) * rate)))
        for qa in ordered[:count]:
            for reviewer_slot in ("A", "B"):
                selected.append(
                    {
                        "qa_id": qa.qa_id,
                        "reviewer_slot": reviewer_slot,
                        "stratum": list(key),
                        "question": qa.question,
                        "answer": qa.answer,
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
    write_jsonl(data_dir / "qa" / "review_sample.jsonl", selected)
    return selected
