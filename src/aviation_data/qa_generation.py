from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from aviation_data.ids import sha256_text, stable_id, tokens
from aviation_data.io import append_jsonl, read_jsonl, write_json, write_jsonl
from aviation_data.models import (
    Answerability,
    DocumentRecord,
    EvidenceSpan,
    GeneratorConfiguration,
    Language,
    PassageRecord,
    QARecord,
    QAType,
    RightsState,
)


class ModelQAResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    answer: str | None = None
    reference_answer: str | None = None
    rubric: list[str] = Field(default_factory=list)
    evidence_quote: str | None = None
    acceptable_variants: list[str] = Field(default_factory=list)
    reject_reason: str | None = None


@dataclass(frozen=True)
class GenerationTask:
    index: int
    task_id: str
    question_language: Language
    qa_type: QAType
    answerability: Answerability
    passage: PassageRecord


def _type_for_index(index: int) -> QAType:
    cycle = (
        QAType.FACTUAL,
        QAType.DEFINITION,
        QAType.FACTUAL,
        QAType.LIST_TABLE,
        QAType.FACTUAL,
        QAType.DEFINITION,
        QAType.COMPARISON,
        QAType.FACTUAL,
        QAType.TEMPORAL,
        QAType.DEFINITION,
        QAType.FACTUAL,
        QAType.LIST_TABLE,
        QAType.DEFINITION,
        QAType.FACTUAL,
        QAType.COMPARISON,
        QAType.DEFINITION,
        QAType.FACTUAL,
        QAType.LIST_TABLE,
        QAType.DEFINITION,
        QAType.FACTUAL,
    )
    return cycle[index % len(cycle)]


def _tasks(passages: list[PassageRecord], target: int, start: int = 0) -> list[GenerationTask]:
    if not passages:
        return []
    by_language = {
        language: [passage for passage in passages if passage.language == language]
        for language in (Language.ENGLISH, Language.TURKISH)
    }
    tasks = []
    for index in range(start, target):
        question_language = Language.ENGLISH if index % 2 == 0 else Language.TURKISH
        answerability = (
            Answerability.CORPUS_UNANSWERABLE if index % 10 == 9 else Answerability.ANSWERABLE
        )
        force_cross_lingual = answerability == Answerability.ANSWERABLE and index % 10 == 0
        evidence_language = (
            (Language.TURKISH if question_language == Language.ENGLISH else Language.ENGLISH)
            if force_cross_lingual
            else question_language
        )
        candidates = by_language.get(evidence_language) or passages
        passage = candidates[(index // 2) % len(candidates)]
        qa_type = _type_for_index(index)
        task_id = stable_id(
            "task",
            index,
            question_language.value,
            qa_type.value,
            answerability.value,
            passage.passage_id,
            length=28,
        )
        tasks.append(
            GenerationTask(
                index=index,
                task_id=task_id,
                question_language=question_language,
                qa_type=qa_type,
                answerability=answerability,
                passage=passage,
            )
        )
    return tasks


def _sentence(passsage: PassageRecord, index: int) -> str | None:
    candidates = []
    for block in re.split(r"\n\s*\n", passsage.text):
        if block.lstrip().startswith("#"):
            continue
        for match in re.finditer(r"[^.!?]{24,}?[.!?](?=\s|$)", block, re.DOTALL):
            value = match.group(0).strip()
            if len(tokens(value)) >= 5:
                candidates.append(value)
    if not candidates:
        lines = [
            line.strip()
            for line in passsage.text.splitlines()
            if len(tokens(line)) >= 5 and not re.fullmatch(r"\|?[\s|:-]+\|?", line)
        ]
        candidates = lines
    return candidates[index % len(candidates)] if candidates else None


def _fixture_response(task: GenerationTask) -> ModelQAResponse:
    if task.answerability == Answerability.CORPUS_UNANSWERABLE:
        fictional_code = f"ZX-{900 + task.index}"
        question = (
            f"Which certified runway length is recorded for the fictional {fictional_code} "
            "aerodrome?"
            if task.question_language == Language.ENGLISH
            else f"Kurgusal {fictional_code} meydanı için hangi onaylı pist uzunluğu "
            "kaydedilmiştir?"
        )
        return ModelQAResponse(question=question)
    # Interleaved question-language scheduling means consecutive uses of one
    # evidence language are two task indexes apart.
    quote = _sentence(task.passage, task.index // 2)
    if not quote:
        return ModelQAResponse(
            question="Unavailable",
            reject_reason="passage contains no usable sentence",
        )
    section = (
        task.passage.section_path[-1]
        if task.passage.section_path
        else (
            "the aviation subject"
            if task.question_language == Language.ENGLISH
            else "havacılık konusu"
        )
    )
    keywords = [
        token
        for token in tokens(quote)
        if token.casefold()
        not in {
            "a",
            "an",
            "and",
            "bir",
            "bu",
            "the",
            "ve",
        }
    ][:3]
    subject = " ".join(keywords) if keywords else section
    if task.question_language == Language.ENGLISH:
        templates = {
            QAType.FACTUAL: f"What does the frozen passage state about {subject}?",
            QAType.DEFINITION: f"How is {subject} described in the passage?",
            QAType.LIST_TABLE: f"What information does the passage list for {subject}?",
            QAType.COMPARISON: f"What relationship involving {subject} is stated in the passage?",
            QAType.TEMPORAL: f"What version-specific statement about {subject} appears here?",
        }
    else:
        templates = {
            QAType.FACTUAL: f"Dondurulmuş bölüm {subject} hakkında ne belirtmektedir?",
            QAType.DEFINITION: f"Bölümde {subject} nasıl açıklanmaktadır?",
            QAType.LIST_TABLE: f"Bölüm {subject} için hangi bilgiyi listelemektedir?",
            QAType.COMPARISON: f"Bölümde {subject} ile ilgili hangi ilişki belirtilmektedir?",
            QAType.TEMPORAL: f"Burada {subject} hakkında sürüme özgü hangi ifade yer almaktadır?",
        }
    rubric = [quote] if task.qa_type in {QAType.DEFINITION, QAType.COMPARISON} else []
    return ModelQAResponse(
        question=templates[task.qa_type],
        answer=quote,
        reference_answer=quote if rubric else None,
        rubric=rubric,
        evidence_quote=quote,
        acceptable_variants=[quote],
    )


def _generator_config(
    config: dict[str, Any],
    prompt: str,
    backend: str,
    model_choice: str = "primary",
) -> GeneratorConfiguration:
    model = config["model"]
    generation = config["generation"]
    if backend == "fixture":
        return GeneratorConfiguration(
            backend="fixture",
            model_id="deterministic-smoke-generator",
            model_revision="local-v1",
            tokenizer_revision="not-applicable",
            container_digest="not-applicable",
            prompt_version="qa_generation.md",
            prompt_sha256=sha256_text(prompt),
            temperature=0.0,
            seed=int(generation["seed"]),
            settings={"warning": "Not suitable for benchmark release"},
        )
    if model_choice not in {"primary", "fallback"}:
        raise ValueError("model_choice must be 'primary' or 'fallback'")
    model_id = str(model[model_choice])
    revision = str(model["revision"] if model_choice == "primary" else model["fallback_revision"])
    tokenizer_revision = str(
        model["tokenizer_revision"] if model_choice == "primary" else model["fallback_revision"]
    )
    digest = str(model["container_digest"])
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("model.revision must be an immutable 40-hex commit")
    if not re.fullmatch(r"[0-9a-f]{40}", tokenizer_revision):
        raise ValueError("model.tokenizer_revision must be an immutable 40-hex commit")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("model.container_digest must be an immutable sha256 digest")
    return GeneratorConfiguration(
        backend="vllm",
        model_id=model_id,
        model_revision=revision,
        tokenizer_revision=tokenizer_revision,
        container_digest=digest,
        prompt_version="qa_generation.md",
        prompt_sha256=sha256_text(prompt),
        temperature=float(generation["temperature"]),
        seed=int(generation["seed"]),
        settings={
            **config["runtime"],
            "model_choice": model_choice,
            "max_output_tokens": generation["max_output_tokens"],
        },
    )


def _response_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "grounded_qa",
            "strict": True,
            "schema": ModelQAResponse.model_json_schema(),
        },
    }


def _vllm_response(
    client: httpx.Client,
    endpoint: str,
    task: GenerationTask,
    generator: GeneratorConfiguration,
    prompt: str,
    max_output_tokens: int,
) -> tuple[ModelQAResponse, dict[str, Any]]:
    desired = {
        "question_language": task.question_language.value,
        "qa_type": task.qa_type.value,
        "answerability": task.answerability.value,
        "passage_id": task.passage.passage_id,
        "passage_language": task.passage.language.value,
        "passage": task.passage.text,
    }
    response = client.post(
        f"{endpoint.rstrip('/')}/chat/completions",
        json={
            "model": generator.model_id,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(desired, ensure_ascii=False),
                },
            ],
            "temperature": generator.temperature,
            "seed": generator.seed + task.index,
            "max_tokens": max_output_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": _response_schema(),
        },
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    return ModelQAResponse.model_validate_json(content), raw


def _to_record(
    task: GenerationTask,
    response: ModelQAResponse,
    generator: GeneratorConfiguration,
    attempts: int,
) -> QARecord:
    if response.reject_reason:
        raise ValueError(response.reject_reason)
    evidence = []
    evidence_languages = []
    answer = response.answer
    if task.answerability == Answerability.ANSWERABLE:
        if not response.evidence_quote:
            raise ValueError("answerable response has no evidence_quote")
        start = task.passage.text.find(response.evidence_quote)
        if start < 0:
            raise ValueError("evidence quote is not an exact passage substring")
        end = start + len(response.evidence_quote)
        evidence = [
            EvidenceSpan(
                passage_id=task.passage.passage_id,
                document_id=task.passage.document_id,
                passage_char_start=start,
                passage_char_end=end,
                canonical_char_start=task.passage.canonical_char_start + start,
                canonical_char_end=task.passage.canonical_char_start + end,
                quote=response.evidence_quote,
                quote_sha256=sha256_text(response.evidence_quote),
            )
        ]
        evidence_languages = [task.passage.language]
        if not answer:
            raise ValueError("answerable response has no answer")
    else:
        answer = None
        if response.answer or response.evidence_quote:
            raise ValueError("unanswerable response supplied answer/evidence")
    qa_id = stable_id(
        "qa",
        task.task_id,
        response.question,
        answer,
        *(item.quote_sha256 for item in evidence),
        length=32,
    )
    return QARecord(
        qa_id=qa_id,
        question=response.question,
        answer=answer,
        reference_answer=response.reference_answer,
        rubric=response.rubric,
        question_language=task.question_language,
        evidence_languages=evidence_languages,
        cross_lingual=any(language != task.question_language for language in evidence_languages),
        primary_type=task.qa_type,
        flags=["fixture_only"] if generator.backend == "fixture" else [],
        answerability=task.answerability,
        evidence=evidence,
        acceptable_variants=response.acceptable_variants,
        provenance_passage_ids=[task.passage.passage_id],
        source_document_ids=[task.passage.document_id],
        split_group_id=task.passage.variant_group_id,
        generator=generator,
        generation_attempts=attempts,
        created_at=datetime.now(UTC),
    )


def generate_qa(
    data_dir: Path,
    generation_config_path: Path,
    prompt_path: Path,
    *,
    backend: Literal["fixture", "vllm"],
    endpoint: str,
    target: int,
    model_choice: Literal["primary", "fallback"] = "primary",
    run_id: str = "benchmark",
) -> tuple[list[QARecord], list[dict[str, Any]]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("run_id contains unsupported characters")
    passages = read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    documents = {
        document.document_id: document
        for document in read_jsonl(
            data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
        )
    }
    passages = [
        passage
        for passage in passages
        if (document := documents.get(passage.document_id)) is not None
        and document.rights_state == RightsState.OPEN
        and document.release_derived_text
        and document.release_qa
    ]
    if not passages:
        raise ValueError("no passages available; run 'passages build' first")
    config = yaml.safe_load(generation_config_path.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8")
    generator = _generator_config(config, prompt, backend, model_choice)
    qa_dir = data_dir / "qa" if run_id == "benchmark" else data_dir / "qa" / "experiments" / run_id
    generated_path = qa_dir / "generated.jsonl"
    existing = read_jsonl(generated_path, QARecord)
    existing = existing[:target]
    if len(existing) >= target:
        return existing, []
    raw_path = qa_dir / "raw_responses.jsonl"
    raw_rows = read_jsonl(raw_path)
    completed_task_indexes = {
        int(row["task_index"]) for row in raw_rows if row.get("status") in {"accepted", "rejected"}
    }
    start = max(completed_task_indexes, default=-1) + 1
    rejections: list[dict[str, Any]] = read_jsonl(qa_dir / "generation_rejections.jsonl")
    max_retries = int(config["generation"]["max_retries"])
    timeout = float(config["generation"]["timeout_seconds"])
    max_output_tokens = int(config["generation"]["max_output_tokens"])
    headers = {}
    api_key = os.environ.get("VLLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.Client(headers=headers, timeout=timeout) if backend == "vllm" else None
    try:
        # Schedule beyond the requested accepted target to compensate for
        # generation rejections without changing earlier task identities.
        for task in _tasks(passages, target + len(rejections) + 100, start=start):
            if len(existing) >= target:
                break
            last_error = ""
            for attempt in range(1, max_retries + 1):
                try:
                    if backend == "fixture":
                        response = _fixture_response(task)
                        raw: dict[str, Any] = response.model_dump(mode="json")
                    else:
                        assert client is not None
                        response, raw = _vllm_response(
                            client,
                            endpoint,
                            task,
                            generator,
                            prompt,
                            max_output_tokens,
                        )
                    record = _to_record(task, response, generator, attempt)
                    existing.append(record)
                    append_jsonl(
                        raw_path,
                        {
                            "task_id": task.task_id,
                            "task_index": task.index,
                            "status": "accepted",
                            "attempt": attempt,
                            "input_passage_ids": [task.passage.passage_id],
                            "raw_response": raw,
                            "qa_id": record.qa_id,
                        },
                    )
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    append_jsonl(
                        raw_path,
                        {
                            "task_id": task.task_id,
                            "task_index": task.index,
                            "status": "retry" if attempt < max_retries else "rejected",
                            "attempt": attempt,
                            "input_passage_ids": [task.passage.passage_id],
                            "error": last_error,
                        },
                    )
            else:
                rejection = {
                    "task_id": task.task_id,
                    "task_index": task.index,
                    "input_passage_ids": [task.passage.passage_id],
                    "retry_count": max_retries,
                    "reason": last_error,
                }
                rejections.append(rejection)
                append_jsonl(qa_dir / "generation_rejections.jsonl", rejection)
    finally:
        if client:
            client.close()
    write_jsonl(generated_path, existing)
    write_json(
        qa_dir / "generation_manifest.json",
        {
            "backend": backend,
            "model_choice": model_choice,
            "run_id": run_id,
            "target": target,
            "accepted": len(existing),
            "rejected": len(rejections),
            "generator": generator.model_dump(mode="json"),
            "endpoint": endpoint if backend == "vllm" else None,
            "prompt_path": str(prompt_path),
        },
    )
    return existing, rejections
