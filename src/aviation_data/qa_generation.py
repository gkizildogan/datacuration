from __future__ import annotations

import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from aviation_data.ids import normalized_for_hash, sha256_text, stable_id, tokens
from aviation_data.io import append_jsonl, read_json, read_jsonl, write_json, write_jsonl
from aviation_data.models import (
    Answerability,
    EvidenceSpan,
    GeneratorConfiguration,
    Language,
    QARecord,
    QAType,
)
from aviation_data.qa_planning import (
    CapacityError,
    EvidenceCandidate,
    PlannedTask,
    extend_task_manifest,
    prepare_run,
    qa_run_dir,
)


class RejectedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["reject"]
    reject_reason: str = Field(min_length=1)


class ClosedAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer"]
    question: str = Field(min_length=1)
    answer_items: list[str] = Field(min_length=1)


class ExplanatoryAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer"]
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    rubric: list[str] = Field(min_length=1)


ClosedModelResponse = Annotated[
    ClosedAnswerResponse | RejectedResponse,
    Field(discriminator="kind"),
]
ExplanatoryModelResponse = Annotated[
    ExplanatoryAnswerResponse | RejectedResponse,
    Field(discriminator="kind"),
]
ModelResponse = ClosedAnswerResponse | ExplanatoryAnswerResponse | RejectedResponse


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
            model_revision="local-v2",
            tokenizer_revision="not-applicable",
            container_digest="not-applicable",
            prompt_version="qa_generation_v2.md",
            prompt_sha256=sha256_text(prompt),
            temperature=0.0,
            seed=int(generation["seed"]),
            settings={
                "planner_version": "qa-quota-planner-v2",
                "warning": "Not suitable for benchmark release",
            },
        )
    if backend != "vllm":
        raise ValueError("backend must be 'fixture' or 'vllm'")
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
        prompt_version="qa_generation_v2.md",
        prompt_sha256=sha256_text(prompt),
        temperature=float(generation["temperature"]),
        seed=int(generation["seed"]),
        settings={
            **config["runtime"],
            "model_choice": model_choice,
            "max_output_tokens": generation["max_output_tokens"],
            "planner_version": "qa-quota-planner-v2",
        },
    )


def _is_closed(qa_type: QAType) -> bool:
    return qa_type in {QAType.FACTUAL, QAType.LIST_TABLE, QAType.TEMPORAL}


def _response_adapter(qa_type: QAType) -> TypeAdapter[Any]:
    return TypeAdapter(ClosedModelResponse if _is_closed(qa_type) else ExplanatoryModelResponse)


def _response_schema(qa_type: QAType) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"grounded_{qa_type.value}_qa",
            "strict": True,
            "schema": _response_adapter(qa_type).json_schema(),
        },
    }


def _token_sequence_in(needle: str, haystack: str) -> bool:
    left = [item.casefold() for item in tokens(needle)]
    right = [item.casefold() for item in tokens(haystack)]
    return bool(left) and any(
        right[index : index + len(left)] == left for index in range(len(right) - len(left) + 1)
    )


def deterministic_variants(answer_items: list[str]) -> list[str]:
    if not answer_items:
        return []
    answer = "; ".join(answer_items)
    variants = [answer]
    whitespace_normalized = re.sub(r"\s+", " ", answer).strip()
    if whitespace_normalized != answer:
        variants.append(whitespace_normalized)
    return variants


def _fixture_question(
    task: PlannedTask,
    candidate: EvidenceCandidate,
    subject: str,
) -> str:
    anchor_hint = " ".join(tokens(candidate.anchor_text)[:5])
    if task.question_language == Language.ENGLISH:
        templates = {
            QAType.FACTUAL: f"What exact fact does the passage state about {subject}?",
            QAType.DEFINITION: f"How does the passage describe {subject}?",
            QAType.LIST_TABLE: f"Which entries does the passage give for {subject}?",
            QAType.COMPARISON: f"What explicit relationship involving {subject} is described?",
            QAType.TEMPORAL: f"What explicit date or time is stated for {subject}?",
        }
    else:
        templates = {
            QAType.FACTUAL: f"Metin {subject} hakkında hangi kesin bilgiyi belirtir?",
            QAType.DEFINITION: f"Metin {subject} konusunu nasıl açıklar?",
            QAType.LIST_TABLE: f"Metin {subject} için hangi ögeleri verir?",
            QAType.COMPARISON: f"Metin {subject} ile ilgili hangi açık ilişkiyi anlatır?",
            QAType.TEMPORAL: f"Metin {subject} için hangi açık tarih veya zamanı belirtir?",
        }
    question = templates[task.qa_type]
    if normalized_for_hash(subject) not in normalized_for_hash(question):
        question = f"{question.rstrip('?')} ({anchor_hint})?"
    return question


def _fixture_response(task: PlannedTask, candidate: EvidenceCandidate) -> ModelResponse:
    subject = task.mutation_source or next(
        (term.value for term in candidate.mutation_terms),
        " ".join(tokens(candidate.anchor_text)[:4]),
    )
    question = _fixture_question(task, candidate, subject)
    if _is_closed(task.qa_type):
        if task.qa_type == QAType.LIST_TABLE:
            answer_items = [item for item in candidate.list_items if item in candidate.anchor_text][
                :3
            ]
            if not answer_items:
                return RejectedResponse(
                    kind="reject",
                    reject_reason="list/table anchor has no extractive items",
                )
        elif task.qa_type == QAType.TEMPORAL:
            match = re.search(
                r"(?<!\w)(?:1[89]\d{2}|20\d{2}|2100)(?!\w)|"
                r"(?<!\w)\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?(?!\w)|"
                r"\b\d{1,2}:\d{2}\b",
                candidate.anchor_text,
            )
            if not match:
                return RejectedResponse(
                    kind="reject",
                    reject_reason="temporal anchor has no explicit time value",
                )
            answer_items = [match.group(0)]
        else:
            answer_items = [candidate.anchor_text]
        return ClosedAnswerResponse(
            kind="answer",
            question=question,
            answer_items=answer_items,
        )
    return ExplanatoryAnswerResponse(
        kind="answer",
        question=question,
        reference_answer=candidate.anchor_text,
        rubric=[candidate.anchor_text],
    )


def _vllm_preflight(
    client: httpx.Client,
    endpoint: str,
    model_id: str,
) -> dict[str, Any]:
    response = client.get(f"{endpoint.rstrip('/')}/models")
    response.raise_for_status()
    payload = response.json()
    served = {
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }
    if model_id not in served:
        raise ValueError(
            f"configured model {model_id!r} is not served by the endpoint; served={sorted(served)}"
        )
    return payload


def _vllm_response(
    client: httpx.Client,
    endpoint: str,
    task: PlannedTask,
    candidate: EvidenceCandidate,
    generator: GeneratorConfiguration,
    prompt: str,
    max_output_tokens: int,
    attempt: int,
) -> tuple[ModelResponse, dict[str, Any]]:
    desired = {
        "anchor_id": candidate.anchor_id,
        "question_language": task.question_language.value,
        "qa_type": task.qa_type.value,
        "passage_language": candidate.language.value,
        "anchor": candidate.anchor_text,
        "list_items": candidate.list_items,
        "required_question_term": task.mutation_source,
        "contracts": {
            "closed_answer_items_are_exact_anchor_substrings": _is_closed(task.qa_type),
            "answer_items_source_ordered": task.qa_type == QAType.LIST_TABLE,
            "do_not_translate_answers": True,
            "do_not_calculate_or_infer": True,
        },
    }
    response = client.post(
        f"{endpoint.rstrip('/')}/chat/completions",
        json={
            "model": generator.model_id,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(desired, ensure_ascii=False)},
            ],
            "temperature": generator.temperature,
            "seed": generator.seed + task.index * 10 + attempt,
            "max_tokens": max_output_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": _response_schema(task.qa_type),
        },
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    parsed = _response_adapter(task.qa_type).validate_json(content)
    return parsed, raw


def _to_record(
    task: PlannedTask,
    candidate: EvidenceCandidate,
    response: ModelResponse,
    generator: GeneratorConfiguration,
    attempts: int,
) -> QARecord:
    if isinstance(response, RejectedResponse):
        raise ValueError(response.reject_reason)
    if task.answerability != Answerability.ANSWERABLE or task.kind != "model":
        raise ValueError("model responses are only valid for answerable model tasks")
    if task.mutation_source and not _token_sequence_in(task.mutation_source, response.question):
        raise ValueError("question omitted the required mutation source")
    evidence = EvidenceSpan(
        passage_id=candidate.passage_id,
        document_id=candidate.document_id,
        passage_char_start=candidate.passage_char_start,
        passage_char_end=candidate.passage_char_end,
        canonical_char_start=candidate.canonical_char_start,
        canonical_char_end=candidate.canonical_char_end,
        quote=candidate.anchor_text,
        quote_sha256=sha256_text(candidate.anchor_text),
    )
    if isinstance(response, ClosedAnswerResponse):
        answer_items = response.answer_items
        if task.qa_type in {QAType.FACTUAL, QAType.TEMPORAL} and len(answer_items) != 1:
            raise ValueError("factual/temporal tasks require exactly one answer_item")
        if task.qa_type == QAType.LIST_TABLE and not answer_items:
            raise ValueError("list/table tasks require answer_items")
        positions = []
        for answer_item in answer_items:
            position = candidate.anchor_text.find(answer_item)
            if position < 0:
                raise ValueError("closed answer_item is not an exact anchor substring")
            positions.append(position)
        if positions != sorted(positions):
            raise ValueError("list/table answer_items are not in source order")
        answer = "; ".join(answer_items)
        reference_answer = None
        rubric: list[str] = []
    else:
        if task.qa_type not in {QAType.DEFINITION, QAType.COMPARISON}:
            raise ValueError("explanatory response returned for a closed task")
        answer_items = []
        answer = response.reference_answer
        reference_answer = response.reference_answer
        rubric = response.rubric
    qa_id = stable_id(
        "qa",
        task.task_id,
        response.question,
        answer,
        evidence.quote_sha256,
        length=32,
    )
    return QARecord(
        qa_id=qa_id,
        question=response.question,
        answer=answer,
        answer_items=answer_items,
        reference_answer=reference_answer,
        rubric=rubric,
        question_language=task.question_language,
        evidence_languages=[candidate.language],
        cross_lingual=candidate.language != task.question_language,
        primary_type=task.qa_type,
        flags=["fixture_only"] if generator.backend == "fixture" else [],
        answerability=Answerability.ANSWERABLE,
        evidence=[evidence],
        acceptable_variants=deterministic_variants(answer_items),
        provenance_passage_ids=[candidate.passage_id],
        source_document_ids=[candidate.document_id],
        split_group_id=candidate.variant_group_id,
        generator=generator,
        generation_attempts=attempts,
        created_at=datetime.now(UTC),
    )


def _generate_task(
    task: PlannedTask,
    candidate: EvidenceCandidate,
    *,
    backend: Literal["fixture", "vllm"],
    client: httpx.Client | None,
    endpoint: str,
    generator: GeneratorConfiguration,
    prompt: str,
    max_output_tokens: int,
    max_retries: int,
) -> tuple[QARecord | None, list[dict[str, Any]], dict[str, Any] | None]:
    events: list[dict[str, Any]] = []
    last_error = ""
    for attempt in range(1, max_retries + 1):
        raw: dict[str, Any] | None = None
        response: ModelResponse | None = None
        try:
            if backend == "fixture":
                response = _fixture_response(task, candidate)
                raw = {"fixture_response": response.model_dump(mode="json")}
            else:
                assert client is not None
                response, raw = _vllm_response(
                    client,
                    endpoint,
                    task,
                    candidate,
                    generator,
                    prompt,
                    max_output_tokens,
                    attempt,
                )
            if isinstance(response, RejectedResponse):
                raise ValueError(response.reject_reason)
            record = _to_record(task, candidate, response, generator, attempt)
            events.append(
                {
                    "task_id": task.task_id,
                    "task_index": task.index,
                    "construction_stage": "model",
                    "status": "accepted",
                    "schema_valid": True,
                    "record_constructed": True,
                    "attempt": attempt,
                    "anchor_id": candidate.anchor_id,
                    "input_passage_ids": [candidate.passage_id],
                    "raw_response": raw,
                    "record": record.model_dump(mode="json"),
                    "qa_id": record.qa_id,
                }
            )
            return record, events, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            retry = attempt < max_retries and not isinstance(response, RejectedResponse)
            events.append(
                {
                    "task_id": task.task_id,
                    "task_index": task.index,
                    "construction_stage": "model",
                    "status": "retry" if retry else "rejected",
                    "schema_valid": response is not None,
                    "record_constructed": False,
                    "attempt": attempt,
                    "anchor_id": candidate.anchor_id,
                    "input_passage_ids": [candidate.passage_id],
                    "raw_response": raw,
                    "error": last_error,
                }
            )
            if not retry:
                break
    rejection = {
        "task_id": task.task_id,
        "task_index": task.index,
        "construction_stage": "model",
        "input_passage_ids": [candidate.passage_id],
        "retry_count": attempt,
        "reason": last_error,
    }
    return None, events, rejection


def _normalized_replace(question: str, source: str, replacement: str) -> str | None:
    direct = question.find(source)
    if direct >= 0:
        return question[:direct] + replacement + question[direct + len(source) :]
    match = re.search(re.escape(source), question, flags=re.IGNORECASE)
    if match:
        return question[: match.start()] + replacement + question[match.end() :]
    return None


def _mutation_record(
    task: PlannedTask,
    parent: QARecord,
    candidate: EvidenceCandidate,
) -> tuple[QARecord, dict[str, Any]]:
    assert task.mutation_source and task.mutation_replacement and task.mutation_kind
    if task.mutation_replacement.casefold() in candidate.anchor_text.casefold():
        raise ValueError("mutation replacement occurs in the parent evidence")
    question = _normalized_replace(
        parent.question,
        task.mutation_source,
        task.mutation_replacement,
    )
    if not question or normalized_for_hash(question) == normalized_for_hash(parent.question):
        raise ValueError("parent question does not contain a replaceable mutation source")
    qa_id = stable_id(
        "qa",
        task.task_id,
        question,
        task.mutation_kind,
        task.mutation_replacement,
        length=32,
    )
    record = QARecord(
        qa_id=qa_id,
        question=question,
        answer=None,
        answer_items=[],
        reference_answer=None,
        rubric=[],
        question_language=task.question_language,
        evidence_languages=[],
        cross_lingual=False,
        primary_type=task.qa_type,
        flags=sorted(
            {
                "deterministic_mutation",
                *(["fixture_only"] if parent.generator.backend == "fixture" else []),
            }
        ),
        answerability=Answerability.CORPUS_UNANSWERABLE,
        evidence=[],
        acceptable_variants=[],
        provenance_passage_ids=[],
        source_document_ids=[],
        split_group_id=stable_id("mutation-group", task.task_id, length=24),
        generator=parent.generator,
        generation_attempts=1,
        created_at=datetime.now(UTC),
    )
    provenance = {
        "qa_id": qa_id,
        "task_id": task.task_id,
        "parent_task_id": task.parent_task_id,
        "parent_qa_id": parent.qa_id,
        "mutation_kind": task.mutation_kind,
        "source_value": task.mutation_source,
        "replacement_value": task.mutation_replacement,
        "donor_document_id": task.donor_document_id,
        "parent_document_id": candidate.document_id,
        "parent_passage_id": candidate.passage_id,
        "parent_evidence_sha256": sha256_text(candidate.anchor_text),
        "parent_answer_sha256": sha256_text(parent.answer or ""),
    }
    return record, provenance


def _recover_records(run_dir: Path) -> tuple[list[QARecord], dict[str, QARecord]]:
    generated_path = run_dir / "generated.jsonl"
    records = read_jsonl(generated_path, QARecord)
    records_by_id = {record.qa_id: record for record in records}
    by_task: dict[str, QARecord] = {}
    recovered = False
    for row in read_jsonl(run_dir / "raw_responses.jsonl"):
        if row.get("status") != "accepted" or not isinstance(row.get("record"), dict):
            continue
        record = QARecord.model_validate(row["record"])
        by_task[str(row["task_id"])] = record
        if record.qa_id not in records_by_id:
            records.append(record)
            records_by_id[record.qa_id] = record
            recovered = True
    if recovered:
        records.sort(key=lambda item: item.qa_id)
        write_jsonl(generated_path, records)
    return records, by_task


def generate_qa(
    data_dir: Path,
    generation_config_path: Path,
    prompt_path: Path,
    *,
    backend: Literal["fixture", "vllm"],
    endpoint: str,
    target: int,
    model_choice: Literal["primary", "fallback"] = "primary",
    run_id: str,
) -> tuple[list[QARecord], list[dict[str, Any]]]:
    from aviation_data.qa_lifecycle import preserve_legacy_baseline

    preserve_legacy_baseline(data_dir)
    config = yaml.safe_load(generation_config_path.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8")
    generator = _generator_config(config, prompt, backend, model_choice)
    run_dir, _, candidates, tasks = prepare_run(
        data_dir,
        run_id,
        target=target,
        seed=generator.seed,
        generation_config_path=generation_config_path,
        prompt_path=prompt_path,
        backend=backend,
        model_choice=model_choice,
        generator_manifest=generator.model_dump(mode="json"),
    )
    candidate_by_id = {candidate.anchor_id: candidate for candidate in candidates}
    records, records_by_task = _recover_records(run_dir)
    raw_path = run_dir / "raw_responses.jsonl"
    terminal_rows = [
        row for row in read_jsonl(raw_path) if row.get("status") in {"accepted", "rejected"}
    ]
    completed = {str(row["task_id"]) for row in terminal_rows}
    rejections: list[dict[str, Any]] = read_jsonl(run_dir / "generation_rejections.jsonl")
    model_tasks = [task for task in tasks if task.kind == "model" and task.task_id not in completed]
    max_retries = int(config["generation"]["max_retries"])
    timeout = float(config["generation"]["timeout_seconds"])
    max_output_tokens = int(config["generation"]["max_output_tokens"])
    headers = {}
    api_key = os.environ.get("VLLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.Client(headers=headers, timeout=timeout) if backend == "vllm" else None
    concurrency = int(config["runtime"].get("max_num_seqs", 1)) if backend == "vllm" else 1
    if concurrency < 1:
        raise ValueError("runtime.max_num_seqs must be at least 1")
    try:
        model_preflight = (
            _vllm_preflight(client, endpoint, generator.model_id)
            if backend == "vllm" and client is not None
            else None
        )
        if model_tasks:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                iterator = iter(model_tasks)
                in_flight: dict[Any, PlannedTask] = {}
                buffered: dict[int, tuple[QARecord | None, list[dict[str, Any]], Any]] = {}
                next_positions = iter(sorted(task.index for task in model_tasks))
                next_index = next(next_positions, None)

                def submit() -> None:
                    while len(in_flight) < concurrency:
                        task = next(iterator, None)
                        if task is None:
                            break
                        candidate = candidate_by_id[task.anchor_id]
                        future = executor.submit(
                            _generate_task,
                            task,
                            candidate,
                            backend=backend,
                            client=client,
                            endpoint=endpoint,
                            generator=generator,
                            prompt=prompt,
                            max_output_tokens=max_output_tokens,
                            max_retries=max_retries,
                        )
                        in_flight[future] = task

                submit()
                while in_flight:
                    finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in finished:
                        task = in_flight.pop(future)
                        buffered[task.index] = future.result()
                    while next_index is not None and next_index in buffered:
                        record, events, rejection = buffered.pop(next_index)
                        for event in events:
                            append_jsonl(raw_path, event)
                        if record is not None:
                            records.append(record)
                            records_by_task[events[-1]["task_id"]] = record
                            append_jsonl(run_dir / "generated.jsonl", record)
                        else:
                            rejections.append(rejection)
                            append_jsonl(run_dir / "generation_rejections.jsonl", rejection)
                        next_index = next(next_positions, None)
                    submit()
    finally:
        if client:
            client.close()

    # Mutations are constructed only from successfully generated parent questions.
    mutation_provenance = {
        str(row["task_id"]): row for row in read_jsonl(run_dir / "mutation_provenance.jsonl")
    }
    for task in [item for item in tasks if item.kind == "mutation"]:
        if task.task_id in completed or task.task_id in mutation_provenance:
            continue
        parent = records_by_task.get(str(task.parent_task_id))
        candidate = candidate_by_id[task.anchor_id]
        try:
            if parent is None:
                raise ValueError("mutation parent did not produce a valid record")
            record, provenance = _mutation_record(task, parent, candidate)
            records.append(record)
            records_by_task[task.task_id] = record
            append_jsonl(run_dir / "generated.jsonl", record)
            append_jsonl(run_dir / "mutation_provenance.jsonl", provenance)
            append_jsonl(
                raw_path,
                {
                    "task_id": task.task_id,
                    "task_index": task.index,
                    "construction_stage": "mutation",
                    "status": "accepted",
                    "attempt": 1,
                    "record": record.model_dump(mode="json"),
                    "qa_id": record.qa_id,
                },
            )
        except Exception as exc:
            rejection = {
                "task_id": task.task_id,
                "task_index": task.index,
                "construction_stage": "mutation",
                "retry_count": 1,
                "reason": f"{type(exc).__name__}: {exc}",
            }
            rejections.append(rejection)
            append_jsonl(run_dir / "generation_rejections.jsonl", rejection)
            append_jsonl(
                raw_path,
                {
                    "task_id": task.task_id,
                    "task_index": task.index,
                    "construction_stage": "mutation",
                    "status": "rejected",
                    "attempt": 1,
                    "error": rejection["reason"],
                },
            )

    # Normalize checkpoint order after possibly concurrent appends/recovery.
    records = list({record.qa_id: record for record in records}.values())
    task_order = {
        str(row.get("qa_id")): int(row["task_index"])
        for row in read_jsonl(raw_path)
        if row.get("status") == "accepted" and row.get("qa_id")
    }
    records.sort(key=lambda item: (task_order.get(item.qa_id, 10**12), item.qa_id))
    write_jsonl(run_dir / "generated.jsonl", records)
    terminal = [
        row for row in read_jsonl(raw_path) if row.get("status") in {"accepted", "rejected"}
    ]
    terminal_model = [row for row in terminal if row.get("construction_stage") == "model"]
    write_json(
        run_dir / "generation_report.json",
        {
            "schema_version": "1.1.0",
            "backend": backend,
            "model_choice": model_choice,
            "run_id": run_id,
            "target": target,
            "constructed": len(records),
            "rejected": len(rejections),
            "json_schema_success_rate": (
                sum(bool(row.get("schema_valid")) for row in terminal_model) / len(terminal_model)
                if terminal_model
                else 0.0
            ),
            "record_construction_success_rate": (
                sum(bool(row.get("record_constructed")) for row in terminal_model)
                / len(terminal_model)
                if terminal_model
                else 0.0
            ),
            "generator": generator.model_dump(mode="json"),
            "endpoint": endpoint if backend == "vllm" else None,
            "model_preflight": model_preflight,
            "task_manifest": str(run_dir / "task_manifest.jsonl"),
        },
    )
    return records, rejections


def build_qa(
    data_dir: Path,
    generation_config_path: Path,
    prompt_path: Path,
    *,
    backend: Literal["fixture", "vllm"],
    endpoint: str,
    target: int,
    model_choice: Literal["primary", "fallback"],
    run_id: str,
    dense_endpoint: str | None = None,
    dense_model: str | None = None,
    dense_revision: str | None = None,
    dense_api_key: str | None = None,
    max_fill_cycles: int = 8,
) -> tuple[list[QARecord], dict[str, Any]]:
    from aviation_data.qa_validation import validate_qa

    history = []
    for cycle in range(max_fill_cycles + 1):
        generated, generation_rejections = generate_qa(
            data_dir,
            generation_config_path,
            prompt_path,
            backend=backend,
            endpoint=endpoint,
            target=target,
            model_choice=model_choice,
            run_id=run_id,
        )
        accepted, _, validation = validate_qa(
            data_dir,
            run_id=run_id,
            dense_endpoint=dense_endpoint,
            dense_model=dense_model,
            dense_revision=dense_revision,
            dense_api_key=dense_api_key,
        )
        history.append(
            {
                "cycle": cycle,
                "generated": len(generated),
                "generation_rejections": len(generation_rejections),
                "valid_pool": validation["valid_pool"],
                "accepted": len(accepted),
                "deficits": validation["quota_diagnostics"]["deficits"],
            }
        )
        run_dir = qa_run_dir(data_dir, run_id)
        if validation["quota_diagnostics"]["clean"] and len(accepted) == target:
            report = {
                "run_id": run_id,
                "target": target,
                "status": "complete",
                "fill_cycles": cycle,
                "history": history,
            }
            write_json(run_dir / "build_report.json", report)
            return accepted, report
        if cycle >= max_fill_cycles:
            break

        valid_ids = {qa.qa_id for qa in read_jsonl(run_dir / "valid_pool.jsonl", QARecord)}
        valid_parent_questions = {
            str(row["task_id"]): QARecord.model_validate(row["record"]).question
            for row in read_jsonl(run_dir / "raw_responses.jsonl")
            if row.get("status") == "accepted"
            and isinstance(row.get("record"), dict)
            and str(row.get("qa_id")) in valid_ids
            and QARecord.model_validate(row["record"]).answerability == Answerability.ANSWERABLE
        }
        run_manifest = read_json(run_dir / "run_manifest.json")
        candidates = read_jsonl(run_dir / "evidence_candidates.jsonl", EvidenceCandidate)
        additions = extend_task_manifest(
            run_dir,
            candidates,
            validation["quota_diagnostics"]["deficits"],
            seed=int(run_manifest["seed"]),
            valid_parent_questions=valid_parent_questions,
        )
        history[-1]["scheduled_fill_tasks"] = len(additions)

    report = {
        "run_id": run_id,
        "target": target,
        "status": "capacity_exhausted",
        "fill_cycles": max_fill_cycles,
        "history": history,
    }
    write_json(qa_run_dir(data_dir, run_id) / "build_report.json", report)
    raise CapacityError(report)
