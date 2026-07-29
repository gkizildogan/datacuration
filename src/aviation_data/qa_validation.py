from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from aviation_data.ids import normalized_for_hash, normalized_tokens, sha256_text, stable_id
from aviation_data.io import read_json, read_jsonl, write_json, write_jsonl
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
from aviation_data.qa_generation import deterministic_variants
from aviation_data.qa_planning import EvidenceCandidate, qa_run_dir

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
    "metin",
    "ne",
    "of",
    "the",
    "to",
    "what",
    "which",
    "ve",
}
NAME_RE = re.compile(
    r"\b(?:[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü'’.-]+"
    r"(?:\s+[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü'’.-]+)+|[A-ZÇĞİÖŞÜ]{2,}\d*)\b"
)
NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")


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
        root: stable_id("splitgroup", *sorted(group), length=24)
        for root, group in members.items()
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
        for word in (
            "hangi",
            "nedir",
            "nasıl",
            "metin",
            "hakkında",
            "belirtir",
            "açıklar",
            "ögeleri",
        )
    )
    english_score = sum(
        bool(re.search(rf"\b{word}\b", lowered))
        for word in ("what", "which", "how", "does", "according", "passage", "described")
    )
    if expected == Language.TURKISH:
        return turkish_score >= english_score
    if expected == Language.ENGLISH:
        return english_score >= turkish_score
    return True


def _token_sequence_in(needle: str, haystack: str) -> bool:
    left = normalized_tokens(needle)
    right = normalized_tokens(haystack)
    return bool(left) and any(
        right[index : index + len(left)] == left
        for index in range(len(right) - len(left) + 1)
    )


def _lexical_support_score(question: str, passage: str) -> float:
    query = {
        token
        for token in normalized_tokens(question)
        if token not in STOPWORDS and len(token) > 2
    }
    if not query:
        return 0.0
    candidate = set(normalized_tokens(passage))
    return len(query & candidate) / len(query)


def _dense_embeddings(
    endpoint: str, model: str, texts: list[str], api_key: str | None
) -> list[list[float]]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    embeddings: list[list[float]] = []
    with httpx.Client(headers=headers, timeout=180) as client:
        for start in range(0, len(texts), 32):
            response = client.post(
                f"{endpoint.rstrip('/')}/embeddings",
                json={
                    "model": model,
                    "input": texts[start : start + 32],
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda item: item["index"])
            embeddings.extend(row["embedding"] for row in rows)
    return embeddings


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _support_fields(value: str) -> tuple[set[str], set[str]]:
    numbers = set(NUMBER_RE.findall(value))
    names = {match.group(0).casefold() for match in NAME_RE.finditer(value)}
    return numbers, names


def _validate_type_contract(qa: QARecord) -> list[str]:
    reasons = []
    if qa.answerability == Answerability.CORPUS_UNANSWERABLE:
        return reasons
    if qa.primary_type in {QAType.FACTUAL, QAType.TEMPORAL}:
        if len(qa.answer_items) != 1 or qa.answer != qa.answer_items[0]:
            reasons.append("closed_single_item_contract")
    elif qa.primary_type == QAType.LIST_TABLE:
        if not qa.answer_items or qa.answer != "; ".join(qa.answer_items):
            reasons.append("list_answer_items_contract")
    else:
        if qa.answer_items:
            reasons.append("explanatory_answer_items_not_empty")
        if qa.answer != qa.reference_answer:
            reasons.append("explanatory_answer_reference_mismatch")
        if not qa.rubric or any(not item.strip() for item in qa.rubric):
            reasons.append("explanatory_rubric_missing")
    expected_variants = deterministic_variants(qa.answer_items)
    if qa.acceptable_variants != expected_variants:
        reasons.append("non_deterministic_acceptable_variants")
    return reasons


def _validate_evidence(
    qa: QARecord,
    passages: dict[str, PassageRecord],
    documents: dict[str, DocumentRecord],
    data_dir: Path,
    generation_candidates: dict[tuple[str, int, int], EvidenceCandidate],
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
        canonical_path = data_dir / document.canonical_path
        if not canonical_path.is_file():
            reasons.append("canonical_document_missing")
        else:
            canonical = canonical_path.read_text(encoding="utf-8")
            canonical_quote = canonical[
                evidence.canonical_char_start : evidence.canonical_char_end
            ]
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
        candidate = generation_candidates.get(
            (
                evidence.passage_id,
                evidence.passage_char_start,
                evidence.passage_char_end,
            )
        )
        if candidate is None or candidate.anchor_text != evidence.quote:
            reasons.append("evidence_anchor_not_generation_candidate")
        elif qa.primary_type not in candidate.compatible_types:
            reasons.append("evidence_anchor_type_incompatible")
        quotes.append(evidence.quote)
    evidence_text = "\n".join(quotes)
    if qa.primary_type in {QAType.FACTUAL, QAType.LIST_TABLE, QAType.TEMPORAL}:
        positions = []
        for item in qa.answer_items:
            position = evidence_text.find(item)
            if position < 0:
                reasons.append("closed_answer_item_not_exact_evidence_substring")
            else:
                positions.append(position)
            if _token_sequence_in(item, qa.question):
                reasons.append("question_exposes_answer")
        if qa.primary_type == QAType.LIST_TABLE and positions != sorted(positions):
            reasons.append("list_answer_items_reordered")
    support_text = f"{evidence_text}\n{qa.question}"
    for value in [qa.answer or "", *qa.rubric]:
        value_numbers, value_names = _support_fields(value)
        support_numbers, support_names = _support_fields(support_text)
        if not value_numbers.issubset(support_numbers):
            reasons.append("unsupported_number")
        if not value_names.issubset(support_names):
            reasons.append("unsupported_name")
    if qa.primary_type in {QAType.DEFINITION, QAType.COMPARISON} and qa.answer:
        answer_content = {
            token
            for token in normalized_tokens(qa.answer)
            if token not in STOPWORDS and len(token) > 2
        }
        evidence_content = set(normalized_tokens(evidence_text))
        overlap = (
            len(answer_content & evidence_content) / len(answer_content)
            if answer_content
            else 0.0
        )
        if overlap < 0.5:
            reasons.append("explanatory_reference_weakly_grounded")
    if (
        qa.primary_type == QAType.TEMPORAL
        and qa.answer_items
        and not (
            re.search(r"(?:1[89]\d{2}|20\d{2}|2100)", qa.answer_items[0])
            or re.search(
                r"\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{1,2}:\d{2}",
                qa.answer_items[0],
            )
        )
    ):
        reasons.append("temporal_answer_has_no_explicit_time")
    return reasons


def _joint_support(
    qa: QARecord,
    provenance: dict[str, Any],
    passage_rows: list[PassageRecord],
    *,
    lexical_scores: list[float],
    dense_scores: list[float] | None,
) -> list[str]:
    reasons = []
    replacement = str(provenance.get("replacement_value", "")).strip()
    source = str(provenance.get("source_value", "")).strip()
    if not replacement or not source:
        return ["unanswerable_mutation_provenance_incomplete"]
    if not _token_sequence_in(replacement, qa.question):
        reasons.append("unanswerable_replacement_missing_from_question")
    if _token_sequence_in(source, qa.question):
        reasons.append("unanswerable_source_still_in_question")
    parent_passage_id = str(provenance.get("parent_passage_id", ""))
    parent = next(
        (passage for passage in passage_rows if passage.passage_id == parent_passage_id),
        None,
    )
    if parent is None:
        reasons.append("unanswerable_parent_passage_missing")
        return reasons
    if _token_sequence_in(replacement, parent.text):
        reasons.append("unanswerable_replacement_present_in_parent_evidence")

    predicate_tokens = {
        token
        for token in normalized_tokens(qa.question)
        if token not in STOPWORDS
        and token not in set(normalized_tokens(replacement))
        and len(token) > 2
    }
    lexical_top = sorted(
        range(len(passage_rows)),
        key=lambda index: (-lexical_scores[index], passage_rows[index].passage_id),
    )[:20]
    dense_top = (
        sorted(
            range(len(passage_rows)),
            key=lambda index: (-dense_scores[index], passage_rows[index].passage_id),
        )[:20]
        if dense_scores is not None
        else []
    )
    replacement_candidates = [
        index
        for index, passage in enumerate(passage_rows)
        if _token_sequence_in(replacement, passage.text)
    ]
    candidate_indexes = set(lexical_top) | set(dense_top) | set(replacement_candidates)
    required_predicate = min(2, len(predicate_tokens))
    for index in sorted(candidate_indexes):
        passage = passage_rows[index]
        if not _token_sequence_in(replacement, passage.text):
            continue
        overlap = predicate_tokens & set(normalized_tokens(passage.text))
        if required_predicate and len(overlap) >= required_predicate:
            reasons.append("unanswerable_jointly_supported")
            break
    return reasons


def _task_order(run_dir: Path) -> dict[str, int]:
    return {
        str(row["qa_id"]): int(row["task_index"])
        for row in read_jsonl(run_dir / "raw_responses.jsonl")
        if row.get("status") == "accepted" and row.get("qa_id")
    }


def _quota_select(
    valid: list[QARecord],
    quota_plan: dict[str, Any],
    order: dict[str, int],
) -> tuple[list[QARecord], list[QARecord], list[dict[str, Any]]]:
    remaining = sorted(valid, key=lambda qa: (order.get(qa.qa_id, 10**12), qa.qa_id))
    selected: list[QARecord] = []
    deficits = []
    for stratum in quota_plan["answerable_strata"]:
        matches = [
            qa
            for qa in remaining
            if qa.answerability == Answerability.ANSWERABLE
            and qa.question_language.value == stratum["question_language"]
            and qa.primary_type.value == stratum["primary_type"]
            and qa.cross_lingual == bool(stratum["cross_lingual"])
        ]
        take = min(len(matches), int(stratum["count"]))
        chosen = matches[:take]
        selected.extend(chosen)
        chosen_ids = {qa.qa_id for qa in chosen}
        remaining = [qa for qa in remaining if qa.qa_id not in chosen_ids]
        if take < int(stratum["count"]):
            deficits.append(
                {
                    "answerability": Answerability.ANSWERABLE.value,
                    **stratum,
                    "actual": take,
                    "deficit": int(stratum["count"]) - take,
                }
            )
    for language, count in quota_plan["unanswerable_by_language"].items():
        matches = [
            qa
            for qa in remaining
            if qa.answerability == Answerability.CORPUS_UNANSWERABLE
            and qa.question_language.value == language
        ]
        take = min(len(matches), int(count))
        chosen = matches[:take]
        selected.extend(chosen)
        chosen_ids = {qa.qa_id for qa in chosen}
        remaining = [qa for qa in remaining if qa.qa_id not in chosen_ids]
        if take < int(count):
            deficits.append(
                {
                    "answerability": Answerability.CORPUS_UNANSWERABLE.value,
                    "question_language": language,
                    "count": count,
                    "actual": take,
                    "deficit": int(count) - take,
                }
            )
    selected.sort(key=lambda qa: (order.get(qa.qa_id, 10**12), qa.qa_id))
    return selected, remaining, deficits


def _rejection_breakdown(
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    dimensions = {
        "reason": Counter(),
        "language": Counter(),
        "type": Counter(),
        "passage": Counter(),
        "document": Counter(),
        "construction_stage": Counter(),
    }
    for row in diagnostics:
        for reason in row.get("reasons", []):
            dimensions["reason"][reason] += 1
        dimensions["language"][str(row.get("question_language", "schema_invalid"))] += 1
        dimensions["type"][str(row.get("primary_type", "schema_invalid"))] += 1
        for passage_id in row.get("passage_ids", []):
            dimensions["passage"][str(passage_id)] += 1
        for document_id in row.get("document_ids", []):
            dimensions["document"][str(document_id)] += 1
        dimensions["construction_stage"][str(row.get("construction_stage", "validation"))] += 1
    return {
        key: dict(sorted(counter.items()))
        for key, counter in dimensions.items()
    }


def validate_qa(
    data_dir: Path,
    *,
    run_id: str,
    dense_endpoint: str | None = None,
    dense_model: str | None = None,
    dense_revision: str | None = None,
    dense_api_key: str | None = None,
) -> tuple[list[QARecord], list[QARecord], dict[str, Any]]:
    run_dir = qa_run_dir(data_dir, run_id)
    generated_path = run_dir / "generated.jsonl"
    if not generated_path.is_file():
        raise FileNotFoundError(
            f"{generated_path} does not exist; run 'aviation-data qa generate' first"
        )
    raw_rows = read_jsonl(generated_path)
    qa_rows: list[QARecord] = []
    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, start=1):
        try:
            qa_rows.append(QARecord.model_validate(raw))
        except ValidationError as exc:
            diagnostics.append(
                {
                    "row": index,
                    "qa_id": raw.get("qa_id") if isinstance(raw, dict) else None,
                    "construction_stage": "schema",
                    "reasons": ["schema_invalid"],
                    "detail": exc.errors(include_url=False),
                }
            )
    passage_rows = read_jsonl(
        run_dir / "passage_snapshot.jsonl", PassageRecord
    ) or read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    document_rows = read_jsonl(
        data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
    )
    passages = {passage.passage_id: passage for passage in passage_rows}
    documents = {document.document_id: document for document in document_rows}
    component_groups = _split_components(qa_rows, documents)
    provenance = {
        str(row["qa_id"]): row
        for row in read_jsonl(run_dir / "mutation_provenance.jsonl")
        if row.get("qa_id")
    }
    generation_candidates = {
        (
            candidate.passage_id,
            candidate.passage_char_start,
            candidate.passage_char_end,
        ): candidate
        for candidate in read_jsonl(
            run_dir / "evidence_candidates.jsonl", EvidenceCandidate
        )
    }

    dense_ready = all((dense_endpoint, dense_model, dense_revision))
    if dense_ready and not re.fullmatch(r"[0-9a-f]{40}", str(dense_revision)):
        raise ValueError("dense_revision must be an immutable 40-hex commit")
    production_unanswerables = any(
        qa.answerability == Answerability.CORPUS_UNANSWERABLE
        and qa.generator.backend != "fixture"
        for qa in qa_rows
    )
    if production_unanswerables and not dense_ready:
        raise ValueError(
            "production unanswerable validation requires dense endpoint/model/revision"
        )
    dense_passage_vectors = (
        _dense_embeddings(
            str(dense_endpoint),
            str(dense_model),
            [passage.text for passage in passage_rows],
            dense_api_key,
        )
        if dense_ready
        else None
    )
    unanswerable_rows = [
        qa for qa in qa_rows if qa.answerability == Answerability.CORPUS_UNANSWERABLE
    ]
    dense_query_vectors = (
        _dense_embeddings(
            str(dense_endpoint),
            str(dense_model),
            [qa.question for qa in unanswerable_rows],
            dense_api_key,
        )
        if dense_ready and unanswerable_rows
        else []
    )
    dense_query_by_id = {
        qa.qa_id: vector
        for qa, vector in zip(
            unanswerable_rows,
            dense_query_vectors,
            strict=bool(dense_query_vectors),
        )
    }

    intrinsically_valid: list[QARecord] = []
    intrinsically_rejected: list[QARecord] = []
    for qa in qa_rows:
        reasons = []
        if not _language_matches(qa.question, qa.question_language):
            reasons.append("question_language_mismatch")
        reasons.extend(_validate_type_contract(qa))
        if qa.answerability == Answerability.ANSWERABLE:
            reasons.extend(
                _validate_evidence(
                    qa,
                    passages,
                    documents,
                    data_dir,
                    generation_candidates,
                )
            )
        else:
            mutation = provenance.get(qa.qa_id)
            if mutation is None:
                reasons.append("unanswerable_mutation_provenance_missing")
            else:
                lexical_scores = [
                    _lexical_support_score(qa.question, passage.text)
                    for passage in passage_rows
                ]
                dense_scores = None
                if dense_passage_vectors is not None:
                    query_vector = dense_query_by_id[qa.qa_id]
                    dense_scores = [
                        _cosine(query_vector, passage_vector)
                        for passage_vector in dense_passage_vectors
                    ]
                reasons.extend(
                    _joint_support(
                        qa,
                        mutation,
                        passage_rows,
                        lexical_scores=lexical_scores,
                        dense_scores=dense_scores,
                    )
                )
        split_group_id = component_groups[qa.qa_id]
        updated = qa.model_copy(
            update={
                "split_group_id": split_group_id,
                "split": assign_split(split_group_id),
                "review_status": (
                    ReviewStatus.AUTO_REJECTED if reasons else ReviewStatus.AUTO_ACCEPTED
                ),
                "rejection_reasons": sorted(set(reasons)),
            }
        )
        if reasons:
            intrinsically_rejected.append(updated)
            diagnostics.append(
                {
                    "qa_id": qa.qa_id,
                    "question_language": qa.question_language.value,
                    "primary_type": qa.primary_type.value,
                    "passage_ids": qa.provenance_passage_ids,
                    "document_ids": qa.source_document_ids,
                    "construction_stage": (
                        "unanswerable_check"
                        if qa.answerability == Answerability.CORPUS_UNANSWERABLE
                        else "intrinsic_validation"
                    ),
                    "reasons": sorted(set(reasons)),
                }
            )
        else:
            intrinsically_valid.append(updated)

    # Duplicate indexes only see intrinsically valid candidates.
    question_seen: dict[str, str] = {}
    question_sets: dict[str, set[str]] = {}
    question_buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
    valid_pool: list[QARecord] = []
    duplicate_rejected: list[QARecord] = []
    order = _task_order(run_dir)
    for qa in sorted(
        intrinsically_valid,
        key=lambda item: (order.get(item.qa_id, 10**12), item.qa_id),
    ):
        reasons = []
        normalized_question = normalized_for_hash(qa.question)
        if normalized_question in question_seen:
            reasons.append("duplicate_question")
        shingles = _question_shingles(qa.question)
        signature = _question_signature(shingles)
        candidates = {
            candidate_id
            for start in range(0, len(signature), 4)
            for candidate_id in question_buckets[(start // 4, signature[start : start + 4])]
        }
        for candidate_id in candidates:
            union = len(shingles | question_sets[candidate_id])
            similarity = len(shingles & question_sets[candidate_id]) / union if union else 1.0
            if similarity >= 0.90:
                reasons.append("near_duplicate_question")
                break
        if reasons:
            rejected = qa.model_copy(
                update={
                    "review_status": ReviewStatus.AUTO_REJECTED,
                    "rejection_reasons": sorted(set(reasons)),
                }
            )
            duplicate_rejected.append(rejected)
            diagnostics.append(
                {
                    "qa_id": qa.qa_id,
                    "question_language": qa.question_language.value,
                    "primary_type": qa.primary_type.value,
                    "passage_ids": qa.provenance_passage_ids,
                    "document_ids": qa.source_document_ids,
                    "construction_stage": "deduplication",
                    "reasons": sorted(set(reasons)),
                }
            )
            continue
        valid_pool.append(qa)
        question_seen[normalized_question] = qa.qa_id
        question_sets[qa.qa_id] = shingles
        for start in range(0, len(signature), 4):
            question_buckets[(start // 4, signature[start : start + 4])].append(qa.qa_id)

    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"{manifest_path} is required for quota-aware selection")
    run_manifest = read_json(manifest_path)
    quota_plan = run_manifest["quota_plan"]
    accepted, overflow, deficits = _quota_select(valid_pool, quota_plan, order)
    overflow = [
        qa.model_copy(
            update={
                "review_status": ReviewStatus.AUTO_REJECTED,
                "rejection_reasons": ["quota_overflow"],
            }
        )
        for qa in overflow
    ]
    rejected = [*intrinsically_rejected, *duplicate_rejected]
    write_jsonl(run_dir / "valid_pool.jsonl", valid_pool)
    write_jsonl(run_dir / "accepted.jsonl", accepted)
    write_jsonl(run_dir / "rejected.jsonl", rejected)
    write_jsonl(run_dir / "quota_overflow.jsonl", overflow)
    write_jsonl(run_dir / "validation_rejections.jsonl", diagnostics)

    rejection_counts = Counter(
        reason for qa in rejected for reason in qa.rejection_reasons
    )
    exact_counts = {
        "question_language": dict(
            sorted(Counter(qa.question_language.value for qa in accepted).items())
        ),
        "answerability": dict(
            sorted(Counter(qa.answerability.value for qa in accepted).items())
        ),
        "answerable_type": dict(
            sorted(
                Counter(
                    qa.primary_type.value
                    for qa in accepted
                    if qa.answerability == Answerability.ANSWERABLE
                ).items()
            )
        ),
        "answerable_cross_lingual": sum(
            qa.cross_lingual
            for qa in accepted
            if qa.answerability == Answerability.ANSWERABLE
        ),
    }
    stats = {
        "schema_version": "1.1.0",
        "run_id": run_id,
        "generated": len(raw_rows),
        "schema_valid": len(qa_rows),
        "valid_pool": len(valid_pool),
        "accepted": len(accepted),
        "rejected": len(rejected) + sum(
            "schema_invalid" in row.get("reasons", []) for row in diagnostics
        ),
        "quota_overflow": len(overflow),
        "evidence_offsets_valid": not any(
            reason
            in {
                "passage_evidence_offset_mismatch",
                "canonical_evidence_offset_mismatch",
            }
            for reason in rejection_counts
        ),
        "rejection_reasons": dict(sorted(rejection_counts.items())),
        "duplicate_and_near_duplicate_rate": (
            sum(
                any(
                    reason in {"duplicate_question", "near_duplicate_question"}
                    for reason in qa.rejection_reasons
                )
                for qa in duplicate_rejected
            )
            / len(qa_rows)
            if qa_rows
            else 0.0
        ),
        "rejection_breakdown": _rejection_breakdown(diagnostics),
        "split_counts": dict(
            sorted(Counter(qa.split.value for qa in accepted).items())
        ),
        "quota_diagnostics": {
            "target": quota_plan,
            "actual": exact_counts,
            "deficits": deficits,
            "issues": deficits,
            "clean": not deficits and len(accepted) == int(quota_plan["target"]),
        },
        "dense_unanswerable_check": {
            "enabled": bool(dense_ready),
            "model": dense_model,
            "revision": dense_revision,
            "policy": "retrieval_candidates_plus_deterministic_joint_support",
        },
    }
    write_json(run_dir / "validation_report.json", stats)
    return accepted, rejected, stats
