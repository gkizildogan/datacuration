from __future__ import annotations

from pathlib import Path

import pytest

from aviation_data.models import (
    Answerability,
    GeneratorConfiguration,
    Language,
    QARecord,
    QAType,
)
from aviation_data.registry import audit_registry, load_registry


def test_source_registry_is_valid_and_fail_closed() -> None:
    registry = load_registry(Path("configs/sources.yaml"))
    issues = audit_registry(registry)
    assert not [issue for issue in issues if issue["severity"] == "error"]
    assert {source.rights.state.value for source in registry.sources} == {
        "open",
        "manifest_only",
        "blocked",
    }
    blocked = next(source for source in registry.sources if source.rights.state.value == "blocked")
    assert not blocked.enabled
    assert not blocked.rights.release_source
    assert not blocked.rights.release_derived_text
    assert not blocked.rights.release_qa


def test_answerable_qa_requires_evidence() -> None:
    generator = GeneratorConfiguration(
        backend="test",
        model_id="test",
        model_revision="test",
        tokenizer_revision="test",
        container_digest="test",
        prompt_version="test",
        prompt_sha256="0" * 64,
        temperature=0,
        seed=1,
    )
    with pytest.raises(ValueError, match="answer and evidence"):
        QARecord(
            qa_id="qa_test",
            question="What is stated?",
            answer="A claim.",
            question_language=Language.ENGLISH,
            primary_type=QAType.FACTUAL,
            answerability=Answerability.ANSWERABLE,
            split_group_id="group",
            generator=generator,
        )
