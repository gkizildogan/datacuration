from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

from aviation_data.ids import normalized_for_hash, normalized_tokens, sha256_text, stable_id
from aviation_data.io import read_jsonl, write_json, write_jsonl, write_parquet_if_available
from aviation_data.models import (
    Answerability,
    DocumentRecord,
    Language,
    PassageRecord,
    QARecord,
    QAType,
    ReviewStatus,
    RightsState,
    Split,
)

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "bu",
    "bir",
    "da",
    "de",
    "does",
    "for",
    "hangi",
    "how",
    "ile",
    "in",
    "is",
    "için",
    "ne",
    "of",
    "the",
    "to",
    "what",
    "which",
    "ve",
}


def _question_shingles(value: str) -> set[str]:
    words = normalized_tokens(value)
    if len(words) < 3:
        return set(words)
    return {" ".join(words[index : index + 3]) for index in range(len(words) - 2)}


def _question_signature(shingles: set[str], size: int = 16) -> tuple[int, ...]:
    if not shingles:
        return tuple([0] * size)
    return tuple(
        min(int(sha256_text(f"{seed}:{shingle}")[:16], 16) for shingle in shingles)
        for seed in range(size)
    )


def assign_split(group_id: str) -> Split:
    value = int(sha256_text(group_id)[:8], 16) % 100
    if value < 70:
        return Split.TRAIN
    if value < 85:
        return Split.VALIDATION
    return Split.TEST


def _split_components(
    qa_rows: list[QARecord], documents: dict[str, DocumentRecord]
) -> dict[str, str]:
    nodes = {f"variant:{document.variant_group_id}" for document in documents.values()}
    nodes.update(
        f"entity:{document.entity_group_id}"
        for document in documents.values()
        if document.entity_group_id
    )
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for document in documents.values():
        if document.entity_group_id:
            union(
                f"variant:{document.variant_group_id}",
                f"entity:{document.entity_group_id}",
            )
    for qa in qa_rows:
        qa_nodes = [
            f"variant:{documents[document_id].variant_group_id}"
            for document_id in qa.source_document_ids
            if document_id in documents
        ]
        for node in qa_nodes[1:]:
            union(qa_nodes[0], node)
    members: dict[str, list[str]] = {}
    for node in parent:
        members.setdefault(find(node), []).append(node)
    component_ids = {
        root: stable_id("splitgroup", *sorted(group), length=24) for root, group in members.items()
    }
    output = {}
    for qa in qa_rows:
        first_document = next(
            (
                documents[document_id]
                for document_id in qa.source_document_ids
                if document_id in documents
            ),
            None,
        )
        output[qa.qa_id] = (
            component_ids[find(f"variant:{first_document.variant_group_id}")]
            if first_document
            else qa.split_group_id
        )
    return output


def _language_matches(text: str, expected: Language) -> bool:
    lowered = text.casefold()
    turkish_score = sum(lowered.count(character) for character in "çğıöşü")
    turkish_score += sum(
        bool(re.search(rf"\b{word}\b", lowered))
        for word in ("hangi", "nedir", "nasıl", "bölüm", "hakkında", "belirtmektedir")
    )
    english_score = sum(
        bool(re.search(rf"\b{word}\b", lowered))
        for word in ("what", "which", "how", "does", "according", "passage")
    )
    if expected == Language.TURKISH:
        return turkish_score >= english_score
    if expected == Language.ENGLISH:
        return english_score >= turkish_score
    return True


def _lexical_support_score(question: str, passage: str) -> float:
    query = {
        token for token in normalized_tokens(question) if token not in STOPWORDS and len(token) > 2
    }
    if not query:
        return 0.0
    candidate = set(normalized_tokens(passage))
    return len(query & candidate) / len(query)


def _dense_embeddings(
    endpoint: str, model: str, texts: list[str], api_key: str | None
) -> list[list[float]]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(headers=headers, timeout=180) as client:
        response = client.post(
            f"{endpoint.rstrip('/')}/embeddings",
            json={"model": model, "input": texts, "encoding_format": "float"},
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda item: item["index"])
        return [row["embedding"] for row in rows]


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _validate_evidence(
    qa: QARecord,
    passages: dict[str, PassageRecord],
    documents: dict[str, DocumentRecord],
    data_dir: Path,
) -> list[str]:
    reasons = []
    quotes = []
    for evidence in qa.evidence:
        passage = passages.get(evidence.passage_id)
        if passage is None:
            reasons.append("evidence_passage_missing")
            continue
        document = documents.get(evidence.document_id)
        if document is None:
            reasons.append("evidence_document_missing")
            continue
        if passage.document_id != evidence.document_id:
            reasons.append("evidence_document_mismatch")
        passage_quote = passage.text[evidence.passage_char_start : evidence.passage_char_end]
        if passage_quote != evidence.quote:
            reasons.append("passage_evidence_offset_mismatch")
        canonical = (data_dir / document.canonical_path).read_text(encoding="utf-8")
        canonical_quote = canonical[evidence.canonical_char_start : evidence.canonical_char_end]
        if canonical_quote != evidence.quote:
            reasons.append("canonical_evidence_offset_mismatch")
        if sha256_text(evidence.quote) != evidence.quote_sha256:
            reasons.append("evidence_checksum_mismatch")
        if (
            document.rights_state != RightsState.OPEN
            or not document.release_qa
            or not document.release_derived_text
        ):
            reasons.append("evidence_not_publicly_releasable")
        quotes.append(evidence.quote)
    evidence_text = "\n".join(quotes)
    if qa.answer and qa.primary_type in {
        QAType.FACTUAL,
        QAType.LIST_TABLE,
        QAType.TEMPORAL,
    }:
        answer_normalized = normalized_for_hash(qa.answer)
        evidence_normalized = normalized_for_hash(evidence_text)
        if answer_normalized not in evidence_normalized:
            reasons.append("closed_answer_not_contained_in_evidence")
    if qa.answer:
        answer_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", qa.answer))
        evidence_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", evidence_text))
        question_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", qa.question))
        if not answer_numbers.issubset(evidence_numbers | question_numbers):
            reasons.append("unsupported_number")
        named_pattern = re.compile(
            r"\b(?:[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü.-]+"
            r"(?:\s+[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü.-]+)+|[A-Z]{2,6}\d*)\b"
        )
        answer_names = {match.group(0).casefold() for match in named_pattern.finditer(qa.answer)}
        support_text = f"{evidence_text}\n{qa.question}".casefold()
        if any(name not in support_text for name in answer_names):
            reasons.append("unsupported_name")
        if len(normalized_tokens(qa.answer)) <= 12 and normalized_for_hash(
            qa.answer
        ) in normalized_for_hash(qa.question):
            reasons.append("question_exposes_answer")
    return reasons


def validate_qa(
    data_dir: Path,
    *,
    dense_endpoint: str | None = None,
    dense_model: str | None = None,
    dense_revision: str | None = None,
    dense_api_key: str | None = None,
) -> tuple[list[QARecord], list[QARecord], dict[str, Any]]:
    qa_rows = read_jsonl(data_dir / "qa" / "generated.jsonl", QARecord)
    passage_rows = read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    document_rows = read_jsonl(data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord)
    passages = {passage.passage_id: passage for passage in passage_rows}
    documents = {document.document_id: document for document in document_rows}
    component_groups = _split_components(qa_rows, documents)
    question_seen: dict[str, str] = {}
    question_sets: dict[str, set[str]] = {}
    question_buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
    accepted: list[QARecord] = []
    rejected: list[QARecord] = []

    dense_ready = all((dense_endpoint, dense_model, dense_revision))
    if dense_ready and not re.fullmatch(r"[0-9a-f]{40}", str(dense_revision)):
        raise ValueError("dense_revision must be an immutable 40-hex commit")
    dense_passage_vectors: list[list[float]] | None = None
    if dense_ready:
        dense_passage_vectors = _dense_embeddings(
            str(dense_endpoint),
            str(dense_model),
            [passage.text for passage in passage_rows],
            dense_api_key,
        )

    for qa in qa_rows:
        reasons: list[str] = []
        normalized_question = normalized_for_hash(qa.question)
        if normalized_question in question_seen:
            reasons.append("duplicate_question")
        else:
            question_seen[normalized_question] = qa.qa_id
        shingles = _question_shingles(qa.question)
        signature = _question_signature(shingles)
        candidates = {
            candidate
            for start in range(0, len(signature), 4)
            for candidate in question_buckets[(start // 4, signature[start : start + 4])]
        }
        for candidate in candidates:
            union = len(shingles | question_sets[candidate])
            similarity = len(shingles & question_sets[candidate]) / union if union else 1.0
            if similarity >= 0.90:
                reasons.append("near_duplicate_question")
                break
        question_sets[qa.qa_id] = shingles
        for start in range(0, len(signature), 4):
            question_buckets[(start // 4, signature[start : start + 4])].append(qa.qa_id)
        if not _language_matches(qa.question, qa.question_language):
            reasons.append("question_language_mismatch")
        if qa.answerability == Answerability.ANSWERABLE:
            reasons.extend(_validate_evidence(qa, passages, documents, data_dir))
            if qa.primary_type in {QAType.DEFINITION, QAType.COMPARISON} and not qa.rubric:
                reasons.append("explanatory_rubric_missing")
        else:
            scores = [_lexical_support_score(qa.question, passage.text) for passage in passage_rows]
            if max(scores, default=0.0) >= 0.8:
                reasons.append("unanswerable_lexically_supported")
            is_fixture = qa.generator.backend == "fixture"
            if dense_ready and dense_passage_vectors is not None:
                query_vector = _dense_embeddings(
                    str(dense_endpoint),
                    str(dense_model),
                    [qa.question],
                    dense_api_key,
                )[0]
                maximum = max(
                    (
                        _cosine(query_vector, passage_vector)
                        for passage_vector in dense_passage_vectors
                    ),
                    default=0.0,
                )
                if maximum >= 0.82:
                    reasons.append("unanswerable_densely_supported")
            elif not is_fixture:
                reasons.append("dense_unanswerable_check_missing")
        split_group_id = component_groups[qa.qa_id]
        split = assign_split(split_group_id)
        updated = qa.model_copy(
            update={
                "split_group_id": split_group_id,
                "split": split,
                "review_status": (
                    ReviewStatus.AUTO_REJECTED if reasons else ReviewStatus.AUTO_ACCEPTED
                ),
                "rejection_reasons": sorted(set(reasons)),
                "flags": sorted(
                    set(
                        [
                            *qa.flags,
                            *(
                                ["lexical_only_unanswerable_check"]
                                if qa.answerability == Answerability.CORPUS_UNANSWERABLE
                                and not dense_ready
                                else []
                            ),
                        ]
                    )
                ),
            }
        )
        (rejected if reasons else accepted).append(updated)

    splits_by_group: dict[str, set[Split]] = {}
    for qa in [*accepted, *rejected]:
        splits_by_group.setdefault(qa.split_group_id, set()).add(qa.split)
    if any(len(splits) > 1 for splits in splits_by_group.values()):
        raise AssertionError("split leakage detected within group")

    output = data_dir / "qa"
    write_jsonl(output / "accepted.jsonl", accepted)
    write_jsonl(output / "rejected.jsonl", rejected)
    write_parquet_if_available(output / "accepted.parquet", accepted)
    rejection_counts = Counter(reason for qa in rejected for reason in qa.rejection_reasons)
    total_accepted = len(accepted)
    answerable_accepted = [qa for qa in accepted if qa.answerability == Answerability.ANSWERABLE]
    language_shares = {
        language.value: (
            sum(qa.question_language == language for qa in accepted) / total_accepted
            if total_accepted
            else 0.0
        )
        for language in (Language.ENGLISH, Language.TURKISH)
    }
    answerability_shares = {
        answerability.value: (
            sum(qa.answerability == answerability for qa in accepted) / total_accepted
            if total_accepted
            else 0.0
        )
        for answerability in Answerability
    }
    type_targets = {
        QAType.FACTUAL: 0.40,
        QAType.DEFINITION: 0.30,
        QAType.LIST_TABLE: 0.15,
        QAType.COMPARISON: 0.10,
        QAType.TEMPORAL: 0.05,
    }
    type_shares = {
        qa_type.value: (
            sum(qa.primary_type == qa_type for qa in answerable_accepted) / len(answerable_accepted)
            if answerable_accepted
            else 0.0
        )
        for qa_type in QAType
    }
    cross_lingual_share = (
        sum(qa.cross_lingual for qa in answerable_accepted) / len(answerable_accepted)
        if answerable_accepted
        else 0.0
    )
    quota_issues = []
    for language, share in language_shares.items():
        if abs(share - 0.5) > 0.05:
            quota_issues.append(
                {"dimension": "question_language", "key": language, "actual": share}
            )
    if abs(answerability_shares[Answerability.CORPUS_UNANSWERABLE.value] - 0.10) > 0.05:
        quota_issues.append(
            {
                "dimension": "answerability",
                "key": Answerability.CORPUS_UNANSWERABLE.value,
                "actual": answerability_shares[Answerability.CORPUS_UNANSWERABLE.value],
            }
        )
    if cross_lingual_share < 0.10:
        quota_issues.append(
            {
                "dimension": "cross_lingual",
                "key": "answerable",
                "actual": cross_lingual_share,
            }
        )
    for qa_type, target_share in type_targets.items():
        if abs(type_shares[qa_type.value] - target_share) > 0.05:
            quota_issues.append(
                {
                    "dimension": "primary_type",
                    "key": qa_type.value,
                    "actual": type_shares[qa_type.value],
                    "target": target_share,
                }
            )
    stats = {
        "generated": len(qa_rows),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "evidence_offsets_valid": not any(
            reason
            in {
                "passage_evidence_offset_mismatch",
                "canonical_evidence_offset_mismatch",
            }
            for reason in rejection_counts
        ),
        "rejection_reasons": dict(sorted(rejection_counts.items())),
        "split_counts": dict(sorted(Counter(qa.split.value for qa in accepted).items())),
        "quota_diagnostics": {
            "question_language_shares": language_shares,
            "answerability_shares": answerability_shares,
            "answerable_type_shares": type_shares,
            "answerable_cross_lingual_share": cross_lingual_share,
            "issues": quota_issues,
        },
        "dense_unanswerable_check": {
            "enabled": bool(dense_ready),
            "model": dense_model,
            "revision": dense_revision,
        },
    }
    write_json(output / "validation_report.json", stats)
    return accepted, rejected, stats
