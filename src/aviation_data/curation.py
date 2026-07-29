from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from aviation_data.ids import normalized_for_hash, normalized_tokens, stable_id
from aviation_data.io import read_jsonl, write_json, write_jsonl, write_parquet_if_available
from aviation_data.models import DocumentRecord, Topic


def _shingles(text: str, width: int = 5) -> set[str]:
    words = normalized_tokens(text)
    if len(words) < width:
        return {" ".join(words)} if words else set()
    return {" ".join(words[index : index + width]) for index in range(len(words) - width + 1)}


def _minhash_signature(shingles: set[str], permutations: int = 32) -> tuple[int, ...]:
    if not shingles:
        return tuple([0] * permutations)
    signature = []
    for seed in range(permutations):
        signature.append(
            min(
                int.from_bytes(
                    hashlib.blake2b(f"{seed}:{shingle}".encode(), digest_size=8).digest(),
                    "big",
                )
                for shingle in shingles
            )
        )
    return tuple(signature)


class _DisjointSet:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _near_duplicate_groups(texts: dict[str, str], threshold: float = 0.85) -> dict[str, str]:
    ids = sorted(texts)
    sets = {document_id: _shingles(texts[document_id]) for document_id in ids}
    signatures = {
        document_id: _minhash_signature(shingles) for document_id, shingles in sets.items()
    }
    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
    rows_per_band = 4
    for document_id, signature in signatures.items():
        for start in range(0, len(signature), rows_per_band):
            buckets[(start // rows_per_band, signature[start : start + rows_per_band])].append(
                document_id
            )
    candidates: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        ordered = sorted(bucket)
        candidates.update(
            (ordered[left], ordered[right])
            for left in range(len(ordered))
            for right in range(left + 1, len(ordered))
        )
    disjoint = _DisjointSet(ids)
    for left, right in candidates:
        union = len(sets[left] | sets[right])
        similarity = len(sets[left] & sets[right]) / union if union else 1.0
        if similarity >= threshold:
            disjoint.union(left, right)
    members: dict[str, list[str]] = defaultdict(list)
    for document_id in ids:
        members[disjoint.find(document_id)].append(document_id)
    groups = {}
    for member_ids in members.values():
        group_id = stable_id("dupe", *member_ids, length=24)
        for document_id in member_ids:
            groups[document_id] = group_id
    return groups


def _possible_personal_data(text: str) -> bool:
    patterns = (
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"\b(?:T\.?C\.?\s*)?(?:kimlik|identity)\s*(?:no|number|numarası)?\s*[:#]?\s*\d{11}\b",
        r"\b(?:home|residential|ev)\s+address\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _quota_report(
    accepted: list[DocumentRecord], config: dict[str, Any], data_dir: Path
) -> dict[str, Any]:
    del data_dir
    representatives: dict[str, DocumentRecord] = {}
    for document in accepted:
        previous = representatives.get(document.variant_group_id)
        if previous is None or (
            document.canonical_token_count,
            document.document_id,
        ) > (
            previous.canonical_token_count,
            previous.document_id,
        ):
            representatives[document.variant_group_id] = document
    counting_documents = list(representatives.values())
    language_tokens: Counter[str] = Counter()
    topic_tokens: Counter[str] = Counter()
    family_tokens: Counter[str] = Counter()
    publisher_tokens: Counter[str] = Counter()
    authority_tokens: Counter[str] = Counter()
    format_tokens: Counter[str] = Counter()
    period_tokens: Counter[str] = Counter()
    total = 0
    for document in counting_documents:
        count = document.canonical_token_count
        total += count
        language_tokens[document.language.value] += count
        family_tokens[document.source_family] += count
        publisher_tokens[document.publisher] += count
        authority_tokens[document.authority_level] += count
        format_tokens[document.native_format] += count
        period = (
            f"{(document.publication_date.year // 10) * 10}s"
            if document.publication_date
            else "unknown"
        )
        period_tokens[period] += count
        for topic in document.topics:
            topic_tokens[topic.value] += count / max(1, len(document.topics))

    def shares(counter: Counter[str]) -> dict[str, float]:
        return {
            key: round(value / total, 6) if total else 0.0 for key, value in sorted(counter.items())
        }

    language_shares = shares(language_tokens)
    topic_shares = shares(topic_tokens)
    family_shares = shares(family_tokens)
    tolerance = float(config.get("quota_tolerance", 0.05))
    language_targets = config.get("language_token_targets", {"en": 0.7, "tr": 0.3})
    issues = []
    for language, target in language_targets.items():
        actual = language_shares.get(language, 0.0)
        if abs(actual - float(target)) > tolerance:
            issues.append(
                {
                    "code": "language_quota",
                    "key": language,
                    "target": target,
                    "actual": actual,
                }
            )
    minimum_topic = float(config.get("minimum_topic_share", 0.05))
    for topic in Topic:
        actual = topic_shares.get(topic.value, 0.0)
        if actual < minimum_topic:
            issues.append(
                {
                    "code": "topic_minimum",
                    "key": topic.value,
                    "target": minimum_topic,
                    "actual": actual,
                }
            )
    maximum_family = float(config.get("maximum_source_family_share", 0.4))
    for family, actual in family_shares.items():
        if actual > maximum_family:
            issues.append(
                {
                    "code": "source_family_cap",
                    "key": family,
                    "target": maximum_family,
                    "actual": actual,
                }
            )
    return {
        "accepted_documents": len(accepted),
        "variant_counting_units": len(counting_documents),
        "canonical_tokens": total,
        "language_token_shares": language_shares,
        "topic_token_shares": topic_shares,
        "source_family_token_shares": family_shares,
        "sampling_matrix": {
            "publisher_token_shares": shares(publisher_tokens),
            "authority_token_shares": shares(authority_tokens),
            "native_format_token_shares": shares(format_tokens),
            "publication_period_token_shares": shares(period_tokens),
        },
        "quota_issues": issues,
        "note": "The bundled fixtures are a smoke test and are not expected to meet pilot quotas.",
    }


def curate_documents(
    data_dir: Path,
    sampling_config_path: Path,
) -> tuple[list[DocumentRecord], list[DocumentRecord], dict[str, Any]]:
    documents = read_jsonl(data_dir / "extracted" / "documents.jsonl", DocumentRecord)
    texts = {
        document.document_id: (data_dir / document.canonical_path).read_text(encoding="utf-8")
        for document in documents
    }
    groups = _near_duplicate_groups(texts)
    exact_seen: dict[str, str] = {}
    accepted: list[DocumentRecord] = []
    rejected: list[DocumentRecord] = []
    for document in sorted(documents, key=lambda item: item.document_id):
        flags = list(document.quality_flags)
        normalized = normalized_for_hash(texts[document.document_id])
        exact_hash = hashlib.sha256(normalized.encode()).hexdigest()
        duplicate_of = exact_seen.get(exact_hash)
        if duplicate_of:
            flags.append("exact_duplicate")
        else:
            exact_seen[exact_hash] = document.document_id
        if _possible_personal_data(texts[document.document_id]):
            flags.append("possible_personal_data")
        if (
            Topic.AIRLINES in document.topics
            and document.as_of is None
            and re.search(
                r"\b(?:current|fleet|filo|mevcut|güncel)\b",
                texts[document.document_id],
                re.IGNORECASE,
            )
        ):
            flags.append("current_airline_claim_missing_as_of")
        accepted_value = not any(
            flag
            in {
                "very_short",
                "encoding_replacement_noise",
                "exact_duplicate",
                "possible_personal_data",
                "current_airline_claim_missing_as_of",
            }
            for flag in flags
        )
        updated = document.model_copy(
            update={
                "quality_flags": sorted(set(flags)),
                "duplicate_group_id": groups[document.document_id],
                "duplicate_of": duplicate_of,
                "accepted": accepted_value,
            }
        )
        (accepted if accepted_value else rejected).append(updated)
    config = yaml.safe_load(sampling_config_path.read_text(encoding="utf-8"))
    stats = _quota_report(accepted, config, data_dir)
    output = data_dir / "curated"
    write_jsonl(output / "documents.jsonl", [*accepted, *rejected])
    write_jsonl(output / "accepted_documents.jsonl", accepted)
    write_jsonl(output / "rejected_documents.jsonl", rejected)
    write_parquet_if_available(output / "accepted_documents.parquet", accepted)
    write_json(output / "curation_report.json", stats)
    return accepted, rejected, stats
