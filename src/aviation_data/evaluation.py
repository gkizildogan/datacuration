from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from aviation_data.ids import normalized_tokens, sha256_text
from aviation_data.io import append_jsonl, read_jsonl, write_json
from aviation_data.models import (
    Answerability,
    DocumentRecord,
    PassageRecord,
    QARecord,
    QAType,
)


class BM25Index:
    def __init__(
        self,
        passages: list[PassageRecord],
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.passages = passages
        self.k1 = k1
        self.b = b
        self.corpus = [normalized_tokens(passage.text) for passage in passages]
        self.frequencies = [Counter(words) for words in self.corpus]
        self.document_frequency: Counter[str] = Counter()
        for words in self.corpus:
            self.document_frequency.update(set(words))
        self.average_length = sum(map(len, self.corpus)) / max(1, len(self.corpus))

    def rank(self, query: str) -> list[tuple[str, float]]:
        query_words = normalized_tokens(query)
        scores = []
        for passage, words, frequencies in zip(
            self.passages, self.corpus, self.frequencies, strict=True
        ):
            score = 0.0
            for word in query_words:
                df = self.document_frequency[word]
                idf = math.log(1 + (len(self.corpus) - df + 0.5) / (df + 0.5))
                frequency = frequencies[word]
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * len(words) / max(1, self.average_length)
                )
                score += idf * (frequency * (self.k1 + 1)) / max(1e-12, denominator)
            scores.append((passage.passage_id, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _embeddings(
    endpoint: str,
    model: str,
    texts: list[str],
    *,
    batch_size: int,
    api_key: str | None,
) -> list[list[float]]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    vectors = []
    with httpx.Client(headers=headers, timeout=180) as client:
        for start in range(0, len(texts), batch_size):
            response = client.post(
                f"{endpoint.rstrip('/')}/embeddings",
                json={
                    "model": model,
                    "input": texts[start : start + batch_size],
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(row["embedding"] for row in rows)
    return vectors


def _retrieval_metrics(
    ranking: list[str], relevant: set[str], cutoffs: tuple[int, ...] = (5, 10, 20)
) -> dict[str, float]:
    output = {}
    for cutoff in cutoffs:
        output[f"recall@{cutoff}"] = (
            len(set(ranking[:cutoff]) & relevant) / len(relevant) if relevant else 0.0
        )
    reciprocal = next(
        (
            1 / rank
            for rank, passage_id in enumerate(ranking[:10], start=1)
            if passage_id in relevant
        ),
        0.0,
    )
    output["mrr@10"] = reciprocal
    dcg = sum(
        (1 / math.log2(rank + 1))
        for rank, passage_id in enumerate(ranking[:10], start=1)
        if passage_id in relevant
    )
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(relevant)) + 1))
    output["ndcg@10"] = dcg / ideal if ideal else 0.0
    return output


def evaluate_retrieval(
    data_dir: Path,
    *,
    backend: str = "bm25",
    config_path: Path = Path("configs/evaluation.yaml"),
    dense_endpoint: str | None = None,
    dense_api_key: str | None = None,
) -> dict[str, Any]:
    passages = read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    documents = {
        document.document_id: document
        for document in read_jsonl(
            data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
        )
    }
    qa_rows = [
        qa
        for qa in read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
        if qa.answerability == Answerability.ANSWERABLE
    ]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    lexical_config = config["retrieval"]["lexical"]
    dense_config = config["retrieval"]["dense"]
    bm25 = BM25Index(
        passages,
        k1=float(lexical_config["k1"]),
        b=float(lexical_config["b"]),
    )
    passage_vectors = None
    query_vectors = None
    endpoint = dense_endpoint or str(dense_config["endpoint"])
    if backend == "dense":
        revision = str(dense_config["revision"])
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("dense retriever revision must be an immutable 40-hex commit")
        passage_vectors = _embeddings(
            endpoint,
            str(dense_config["model"]),
            [f"{dense_config['passage_prefix']}{passage.text}" for passage in passages],
            batch_size=int(dense_config["batch_size"]),
            api_key=dense_api_key,
        )
        query_vectors = _embeddings(
            endpoint,
            str(dense_config["model"]),
            [f"{dense_config['query_prefix']}{qa.question}" for qa in qa_rows],
            batch_size=int(dense_config["batch_size"]),
            api_key=dense_api_key,
        )
    elif backend != "bm25":
        raise ValueError("backend must be 'bm25' or 'dense'")
    per_item = []
    for qa_index, qa in enumerate(qa_rows):
        if backend == "bm25":
            ranking = [passage_id for passage_id, _ in bm25.rank(qa.question)]
        else:
            assert passage_vectors is not None and query_vectors is not None
            query_vector = query_vectors[qa_index]
            ranking = [
                passage_id
                for passage_id, _ in sorted(
                    (
                        (passage.passage_id, _cosine(query_vector, vector))
                        for passage, vector in zip(passages, passage_vectors, strict=True)
                    ),
                    key=lambda item: (-item[1], item[0]),
                )
            ]
        relevant = {evidence.passage_id for evidence in qa.evidence}
        metrics = _retrieval_metrics(ranking, relevant)
        per_item.append({"qa": qa, "metrics": metrics})
    metric_names = ("recall@5", "recall@10", "recall@20", "mrr@10", "ndcg@10")
    overall = {
        name: round(sum(item["metrics"][name] for item in per_item) / max(1, len(per_item)), 6)
        for name in metric_names
    }
    breakdowns: dict[str, dict[str, Any]] = {}
    dimensions = {
        "language": lambda qa: qa.question_language.value,
        "cross_lingual": lambda qa: str(qa.cross_lingual).lower(),
        "question_type": lambda qa: qa.primary_type.value,
        "answerability": lambda qa: qa.answerability.value,
        "topic": lambda qa: ",".join(
            sorted(
                {
                    topic.value
                    for document_id in qa.source_document_ids
                    if (document := documents.get(document_id))
                    for topic in document.topics
                }
            )
        ),
        "source_authority": lambda qa: ",".join(
            sorted(
                {
                    document.authority_level
                    for document_id in qa.source_document_ids
                    if (document := documents.get(document_id))
                }
            )
        ),
        "native_format": lambda qa: ",".join(
            sorted(
                {
                    document.native_format
                    for document_id in qa.source_document_ids
                    if (document := documents.get(document_id))
                }
            )
        ),
    }
    for dimension, getter in dimensions.items():
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in per_item:
            groups[getter(item["qa"])].append(item)
        breakdowns[dimension] = {
            key: {
                "count": len(items),
                **{
                    name: round(sum(item["metrics"][name] for item in items) / len(items), 6)
                    for name in metric_names
                },
            }
            for key, items in sorted(groups.items())
        }
    report = {
        "backend": (
            str(lexical_config["backend"])
            if backend == "bm25"
            else {
                "type": "dense",
                "model": dense_config["model"],
                "revision": dense_config["revision"],
            }
        ),
        "items": len(per_item),
        "overall": overall,
        "breakdowns": breakdowns,
    }
    write_json(data_dir / "reports" / f"retrieval_{backend}.json", report)
    return report


def _normalize_answer(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s.,/-]", " ", value)
    value = re.sub(r"\b(?:a|an|the|bir)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _token_f1(prediction: str, reference: str) -> float:
    predicted = Counter(normalized_tokens(prediction))
    expected = Counter(normalized_tokens(reference))
    overlap = sum((predicted & expected).values())
    if not predicted or not expected:
        return float(predicted == expected)
    precision = overlap / sum(predicted.values())
    recall = overlap / sum(expected.values())
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _numbers(value: str) -> list[str]:
    return [item.replace(",", ".") for item in re.findall(r"\b\d+(?:[.,]\d+)?\b", value)]


def _dates(value: str) -> set[date]:
    results = set()
    for match in re.finditer(
        r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})\b",
        value,
    ):
        raw = match.group(0)
        formats = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y")
        for date_format in formats:
            try:
                results.add(datetime.strptime(raw, date_format).date())
                break
            except ValueError:
                continue
    return results


def _list_items(value: str) -> set[str]:
    return {
        _normalize_answer(item)
        for item in re.split(r"[,;]|\band\b|\bve\b", value)
        if _normalize_answer(item)
    }


def evaluate_answers(data_dir: Path, predictions_path: Path) -> dict[str, Any]:
    predictions = {
        str(row["qa_id"]): str(row.get("prediction", "")) for row in read_jsonl(predictions_path)
    }
    qa_rows = [
        qa
        for qa in read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
        if qa.answerability == Answerability.ANSWERABLE and qa.answer is not None
    ]
    rows = []
    for qa in qa_rows:
        prediction = predictions.get(qa.qa_id, "")
        references = [qa.answer, *qa.acceptable_variants]
        exact = max(
            _normalize_answer(prediction) == _normalize_answer(reference)
            for reference in references
        )
        token_f1 = max(_token_f1(prediction, reference) for reference in references)
        reference_numbers = [number for reference in references for number in _numbers(reference)]
        reference_dates = set().union(*(_dates(reference) for reference in references))
        numeric_date = None
        if reference_numbers or reference_dates:
            numeric_date = max(
                (
                    _numbers(prediction) == _numbers(reference)
                    and _dates(prediction) == _dates(reference)
                )
                for reference in references
            )
        predicted_set = _list_items(prediction)
        list_scores = []
        for reference in references:
            reference_set = _list_items(reference)
            overlap = len(predicted_set & reference_set)
            precision = overlap / len(predicted_set) if predicted_set else 0.0
            recall = overlap / len(reference_set) if reference_set else 0.0
            list_scores.append(
                2 * precision * recall / (precision + recall) if precision + recall else 0.0
            )
        rows.append(
            {
                "qa_id": qa.qa_id,
                "exact_match": float(exact),
                "token_f1": token_f1,
                "numeric_date_equivalence": (
                    float(numeric_date) if numeric_date is not None else None
                ),
                "list_set_f1": (max(list_scores) if qa.primary_type == QAType.LIST_TABLE else None),
            }
        )
    metrics = {}
    for key in (
        "exact_match",
        "token_f1",
        "numeric_date_equivalence",
        "list_set_f1",
    ):
        values = [row[key] for row in rows if row[key] is not None]
        metrics[key] = {
            "score": round(sum(values) / max(1, len(values)), 6),
            "items": len(values),
        }
    report = {
        "items": len(rows),
        "prediction_file_sha256": sha256_text(predictions_path.read_text(encoding="utf-8")),
        "metrics": metrics,
    }
    write_json(data_dir / "reports" / "answer_metrics.json", report)
    return report


class JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=4)
    required_points_met: list[str]
    explanation: str


def evaluate_explanatory(
    data_dir: Path,
    predictions_path: Path,
    generation_config_path: Path,
    evaluation_config_path: Path,
    *,
    endpoint: str,
    api_key: str | None,
    allow_uncalibrated: bool = False,
) -> dict[str, Any]:
    qa_rows = [
        qa
        for qa in read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
        if qa.answerability == Answerability.ANSWERABLE
        and qa.primary_type in {QAType.DEFINITION, QAType.COMPARISON}
    ]
    predictions = {
        str(row["qa_id"]): str(row.get("prediction", "")) for row in read_jsonl(predictions_path)
    }
    reviews = read_jsonl(data_dir / "qa" / "human_reviews.jsonl")
    reviewed_ids = {
        str(row["qa_id"])
        for row in reviews
        if all(
            isinstance(row.get(dimension), bool)
            for dimension in (
                "clarity",
                "correctness",
                "evidence_sufficiency",
                "language_quality",
            )
        )
    }
    evaluation_config = yaml.safe_load(evaluation_config_path.read_text(encoding="utf-8"))
    judge_config = evaluation_config["judge"]
    required_human = min(
        int(judge_config["maximum_human_items"]),
        math.ceil(
            len(read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord))
            * float(judge_config["sample_fraction"])
        ),
    )
    if (
        judge_config.get("calibration_required", True)
        and len(reviewed_ids) < required_human
        and not allow_uncalibrated
    ):
        raise ValueError(
            f"judge calibration requires {required_human} reviewed QA items; "
            f"found {len(reviewed_ids)}"
        )
    generation_config = yaml.safe_load(generation_config_path.read_text(encoding="utf-8"))
    model = generation_config["model"]
    revision = str(model["revision"])
    digest = str(model["container_digest"])
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("judge model revision must be an immutable 40-hex commit")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("judge container digest must be immutable")
    raw_path = data_dir / "reports" / "judge_raw.jsonl"
    completed = {
        str(row["qa_id"]): row for row in read_jsonl(raw_path) if row.get("status") == "ok"
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "explanatory_answer_judge",
            "strict": True,
            "schema": JudgeResponse.model_json_schema(),
        },
    }
    with httpx.Client(headers=headers, timeout=180) as client:
        for qa in qa_rows:
            if qa.qa_id in completed:
                continue
            payload = {
                "question": qa.question,
                "candidate_answer": predictions.get(qa.qa_id, ""),
                "reference_answer": qa.reference_answer or qa.answer,
                "required_points": qa.rubric,
                "evidence": [item.quote for item in qa.evidence],
                "instruction": (
                    "Score 0-4 using only the evidence and rubric. A score of 3 or 4 is passing."
                ),
            }
            response = client.post(
                f"{endpoint.rstrip('/')}/chat/completions",
                json={
                    "model": model["primary"],
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a strict grounded-answer evaluator. "
                                "Return only schema-valid JSON."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                    ],
                    "temperature": 0,
                    "seed": generation_config["generation"]["seed"],
                    "max_tokens": 256,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": schema,
                },
            )
            response.raise_for_status()
            raw = response.json()
            parsed = JudgeResponse.model_validate_json(raw["choices"][0]["message"]["content"])
            row = {
                "qa_id": qa.qa_id,
                "status": "ok",
                "judge": parsed.model_dump(mode="json"),
                "model": model["primary"],
                "revision": revision,
                "container_digest": digest,
            }
            append_jsonl(raw_path, row)
            completed[qa.qa_id] = row
    scored = [completed[qa.qa_id] for qa in qa_rows if qa.qa_id in completed]
    pass_rate = sum(row["judge"]["score"] >= 3 for row in scored) / len(scored) if scored else 0.0
    human_votes: dict[str, list[bool]] = defaultdict(list)
    for row in reviews:
        dimensions = (
            row.get("clarity"),
            row.get("correctness"),
            row.get("evidence_sufficiency"),
            row.get("language_quality"),
        )
        if not all(isinstance(value, bool) for value in dimensions):
            continue
        human_votes[str(row["qa_id"])].append(all(dimensions))
    calibration_pairs = []
    for row in scored:
        votes = human_votes.get(str(row["qa_id"]), [])
        if votes:
            human_label = sum(votes) * 2 >= len(votes)
            calibration_pairs.append((human_label, row["judge"]["score"] >= 3))
    agreement = (
        sum(human == judge for human, judge in calibration_pairs) / len(calibration_pairs)
        if calibration_pairs
        else None
    )
    kappa = None
    if calibration_pairs:
        human_positive = sum(human for human, _ in calibration_pairs) / len(calibration_pairs)
        judge_positive = sum(judge for _, judge in calibration_pairs) / len(calibration_pairs)
        expected = human_positive * judge_positive + (1 - human_positive) * (1 - judge_positive)
        kappa = (agreement - expected) / (1 - expected) if expected < 1 else 1.0
    report = {
        "items": len(scored),
        "pass_rate": round(pass_rate, 6),
        "average_score": round(
            sum(row["judge"]["score"] for row in scored) / max(1, len(scored)), 6
        ),
        "calibration": {
            "required_human_items": required_human,
            "available_human_items": len(reviewed_ids),
            "paired_items": len(calibration_pairs),
            "agreement": round(agreement, 6) if agreement is not None else None,
            "cohens_kappa": round(kappa, 6) if kappa is not None else None,
            "status": (
                "calibrated" if len(reviewed_ids) >= required_human else "uncalibrated_diagnostic"
            ),
        },
        "model": {
            "id": model["primary"],
            "revision": revision,
            "container_digest": digest,
        },
    }
    write_json(data_dir / "reports" / "explanatory_judge.json", report)
    return report
