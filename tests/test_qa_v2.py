from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aviation_data.ids import sha256_text
from aviation_data.io import write_jsonl
from aviation_data.models import (
    Answerability,
    EvidenceSpan,
    GeneratorConfiguration,
    Language,
    PassageRecord,
    QARecord,
    QAType,
)
from aviation_data.qa_generation import _response_schema, _vllm_preflight, _vllm_response
from aviation_data.qa_llm_review import _vllm_review
from aviation_data.qa_planning import PlannedTask, build_evidence_candidates, quota_plan
from aviation_data.qa_validation import _joint_support, _token_sequence_in
from aviation_data.review import create_review_sample


def _passage(text: str, *, language: Language = Language.ENGLISH) -> PassageRecord:
    return PassageRecord(
        passage_id=f"passage-{language.value}",
        document_id=f"document-{language.value}",
        document_version="revision-1",
        variant_group_id=f"variant-{language.value}",
        language=language,
        topics=["airports"],
        canonical_char_start=100,
        canonical_char_end=100 + len(text),
        text=text,
        token_count=len(text.split()),
        checksum=sha256_text(text),
    )


def _generator() -> GeneratorConfiguration:
    return GeneratorConfiguration(
        backend="fixture",
        model_id="fixture",
        model_revision="local-v2",
        tokenizer_revision="not-applicable",
        container_digest="not-applicable",
        prompt_version="v2",
        prompt_sha256="0" * 64,
        temperature=0,
        seed=1,
    )


def _evidence() -> EvidenceSpan:
    return EvidenceSpan(
        passage_id="passage-en",
        document_id="document-en",
        passage_char_start=0,
        passage_char_end=4,
        canonical_char_start=100,
        canonical_char_end=104,
        quote="fact",
        quote_sha256=sha256_text("fact"),
    )


def test_candidate_index_filters_fragments_and_preserves_exact_offsets() -> None:
    text = """# Detached heading

Too short.

Unsupported label:

Airport Alpha opened in 2018, whereas Airport Beta opened in 2024.

| Code | Airport |
|---|---|
| AAA | Airport Alpha |
| BBB | Airport Beta |
"""
    passage = _passage(text)
    candidates = build_evidence_candidates([passage])

    assert candidates
    assert all(not candidate.anchor_text.lstrip().startswith("#") for candidate in candidates)
    assert all(not candidate.anchor_text.rstrip().endswith(":") for candidate in candidates)
    assert all(candidate.token_count >= 8 for candidate in candidates)
    assert all(
        passage.text[candidate.passage_char_start : candidate.passage_char_end]
        == candidate.anchor_text
        for candidate in candidates
    )
    assert all(
        candidate.canonical_char_start
        == passage.canonical_char_start + candidate.passage_char_start
        for candidate in candidates
    )
    prose = next(
        candidate for candidate in candidates if "Airport Alpha opened" in candidate.anchor_text
    )
    assert QAType.TEMPORAL in prose.compatible_types
    assert QAType.COMPARISON in prose.compatible_types
    table = next(
        candidate for candidate in candidates if QAType.LIST_TABLE in candidate.compatible_types
    )
    assert table.list_items == ["AAA | Airport Alpha", "BBB | Airport Beta"]


def test_1500_quota_plan_is_exact() -> None:
    plan = quota_plan(1500)

    assert plan["question_language"] == {"en": 750, "tr": 750}
    assert plan["answerability"] == {
        "answerable": 1350,
        "corpus_unanswerable": 150,
    }
    assert plan["answerable_types"] == {
        "factual": 540,
        "definition": 405,
        "list_table": 203,
        "comparison": 135,
        "temporal": 67,
    }
    assert plan["answerable_cross_lingual"] == 135
    assert sum(row["count"] for row in plan["answerable_strata"]) == 1350
    assert sum(row["count"] for row in plan["answerable_strata"] if row["cross_lingual"]) == 135


def test_task_specific_schemas_do_not_expose_unrelated_answer_fields() -> None:
    closed = json.dumps(_response_schema(QAType.FACTUAL))
    explanatory = json.dumps(_response_schema(QAType.DEFINITION))

    assert "answer_items" in closed
    assert "reference_answer" not in closed
    assert "reference_answer" in explanatory
    assert "answer_items" not in explanatory


def test_qa_v11_type_contracts_are_fail_closed() -> None:
    common = {
        "qa_id": "qa-test",
        "question": "What fact is stated?",
        "answer": "fact",
        "answer_items": ["fact"],
        "question_language": "en",
        "evidence_languages": ["en"],
        "primary_type": "factual",
        "answerability": "answerable",
        "evidence": [_evidence().model_dump(mode="json")],
        "provenance_passage_ids": ["passage-en"],
        "source_document_ids": ["document-en"],
        "split_group_id": "variant-en",
        "generator": _generator().model_dump(mode="json"),
    }
    assert QARecord.model_validate(common).schema_version == "1.1.0"

    invalid_list = {
        **common,
        "primary_type": "list_table",
        "answer_items": ["first", "second"],
        "answer": "second; first",
    }
    with pytest.raises(ValidationError, match="source-ordered"):
        QARecord.model_validate(invalid_list)

    invalid_explanation = {
        **common,
        "primary_type": "definition",
        "answer_items": [],
        "reference_answer": "fact",
        "rubric": [],
    }
    with pytest.raises(ValidationError, match="rubric"):
        QARecord.model_validate(invalid_explanation)


def test_answer_exposure_uses_token_sequences() -> None:
    assert not _token_sequence_in("3", "Which limitation applies to A321?")
    assert _token_sequence_in("3", "Which aircraft has 3 engines?")


def test_dense_similarity_alone_does_not_make_unanswerable_supported() -> None:
    parent = _passage("Airport Alpha has a paved runway.")
    qa = QARecord(
        qa_id="qa-mutation",
        question="What runway is stated for Airport Gamma?",
        question_language=Language.ENGLISH,
        primary_type=QAType.FACTUAL,
        answerability=Answerability.CORPUS_UNANSWERABLE,
        split_group_id="mutation-group",
        generator=_generator(),
    )
    reasons = _joint_support(
        qa,
        {
            "source_value": "Airport Alpha",
            "replacement_value": "Airport Gamma",
            "parent_passage_id": parent.passage_id,
        },
        [parent],
        lexical_scores=[0.99],
        dense_scores=[1.0],
    )

    assert "unanswerable_jointly_supported" not in reasons


def test_vllm_preflight_requires_the_configured_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "served-model"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert _vllm_preflight(client, "https://example.test/v1", "served-model")
        with pytest.raises(ValueError, match="not served"):
            _vllm_preflight(client, "https://example.test/v1", "other-model")


def test_review_sample_has_exact_unique_and_assignment_counts(tmp_path: Path) -> None:
    run_dir = tmp_path / "data" / "qa" / "experiments" / "review-counts"
    rows = []
    for index in range(1500):
        rows.append(
            QARecord(
                qa_id=f"qa-{index:04d}",
                question=f"What fact is stated for item {index}?",
                answer="fact",
                answer_items=["fact"],
                question_language=(Language.ENGLISH if index % 2 == 0 else Language.TURKISH),
                evidence_languages=([Language.ENGLISH] if index % 2 == 0 else [Language.TURKISH]),
                primary_type=QAType.FACTUAL,
                answerability=Answerability.ANSWERABLE,
                evidence=[_evidence()],
                acceptable_variants=["fact"],
                provenance_passage_ids=["passage-en"],
                source_document_ids=["document-en"],
                split_group_id="variant-en",
                generator=_generator(),
                created_at=datetime.now(UTC),
            )
        )
    write_jsonl(run_dir / "accepted.jsonl", rows)

    assignments = create_review_sample(
        tmp_path / "data",
        run_id="review-counts",
        rate=0.15,
    )

    assert len({row["qa_id"] for row in assignments}) == 225
    assert len(assignments) == 450
    assert all(
        sum(row["qa_id"] == qa_id for row in assignments) == 2
        for qa_id in {row["qa_id"] for row in assignments}
    )


def test_generator_and_reviewer_receive_section_path(tmp_path: Path) -> None:
    text = "The aircraft was repainted again in 2000–2001 at Southend Airport."
    passage = _passage(text).model_copy(
        update={"section_path": ["Avro Vulcan XL426", "History", "After service"]}
    )
    candidate = next(
        item
        for item in build_evidence_candidates([passage])
        if QAType.TEMPORAL in item.compatible_types
    )
    assert candidate.section_path == ["Avro Vulcan XL426", "History", "After service"]

    task = PlannedTask(
        index=0,
        task_id="task-0",
        question_language=Language.ENGLISH,
        qa_type=QAType.TEMPORAL,
        answerability=Answerability.ANSWERABLE,
        anchor_id=candidate.anchor_id,
    )
    sent: dict[str, object] = {}

    def generation_handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(json.loads(request.content)["messages"][1]["content"]))
        content = json.dumps(
            {
                "kind": "answer",
                "question": "When was Avro Vulcan XL426 repainted again?",
                "answer_items": ["2000–2001"],
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with httpx.Client(transport=httpx.MockTransport(generation_handler)) as client:
        _vllm_response(
            client, "https://example.test/v1", task, candidate, _generator(), "prompt", 256, 0
        )
    assert sent["section_path"] == ["Avro Vulcan XL426", "History", "After service"]

    run_dir = tmp_path / "data" / "qa" / "experiments" / "section-path"
    evidence = EvidenceSpan(
        passage_id=passage.passage_id,
        document_id=passage.document_id,
        passage_char_start=candidate.passage_char_start,
        passage_char_end=candidate.passage_char_end,
        canonical_char_start=candidate.canonical_char_start,
        canonical_char_end=candidate.canonical_char_end,
        quote=candidate.anchor_text,
        quote_sha256=sha256_text(candidate.anchor_text),
    )
    qa = QARecord(
        qa_id="qa-0000",
        question="When was Avro Vulcan XL426 repainted again?",
        answer="2000–2001",
        answer_items=["2000–2001"],
        question_language=Language.ENGLISH,
        evidence_languages=[Language.ENGLISH],
        primary_type=QAType.TEMPORAL,
        answerability=Answerability.ANSWERABLE,
        evidence=[evidence],
        acceptable_variants=["2000–2001"],
        provenance_passage_ids=[passage.passage_id],
        source_document_ids=[passage.document_id],
        split_group_id=passage.variant_group_id,
        generator=_generator(),
        created_at=datetime.now(UTC),
    )
    write_jsonl(run_dir / "accepted.jsonl", [qa])
    write_jsonl(run_dir / "passage_snapshot.jsonl", [passage])
    assignments = create_review_sample(tmp_path / "data", run_id="section-path", rate=1.0)
    assert all(
        row["section_paths"] == [["Avro Vulcan XL426", "History", "After service"]]
        for row in assignments
    )

    reviewed: dict[str, object] = {}

    def review_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        reviewed.update(json.loads(body["messages"][1]["content"]))
        reviewed["chat_template_kwargs"] = body["chat_template_kwargs"]
        content = json.dumps(
            {
                "clarity": True,
                "correctness": True,
                "evidence_sufficiency": True,
                "language_quality": True,
                "notes": "",
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with httpx.Client(transport=httpx.MockTransport(review_handler)) as client:
        _vllm_review(
            client, "https://example.test/v1", assignments[0], "model", "prompt", 0.0, 1, 256
        )
    assert reviewed["section_paths"] == [["Avro Vulcan XL426", "History", "After service"]]
    # Qwen3 thinking must be disabled, otherwise the reasoning parser leaves content=None.
    assert reviewed["chat_template_kwargs"] == {"enable_thinking": False}
