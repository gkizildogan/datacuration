from __future__ import annotations

from pathlib import Path

import pytest

from aviation_data.models import DocumentRecord, Language, RightsState, Topic
from aviation_data.passages import TokenCounter, passage_document


def _document() -> DocumentRecord:
    return DocumentRecord(
        document_id="doc_test",
        document_version="version",
        variant_group_id="variant",
        title="Test",
        language=Language.ENGLISH,
        topics=[Topic.AIRPORTS],
        publisher="Test",
        source_family="test",
        authority_level="fixture",
        source_record_id="source",
        source_url="file:test",
        native_mime="text/markdown",
        native_format="markdown",
        license_id="CC0-1.0",
        attribution="Test",
        rights_state=RightsState.OPEN,
        release_derived_text=True,
        release_qa=True,
        canonical_path="test.md",
        canonical_sha256="0" * 64,
        canonical_char_count=1,
        canonical_token_count=1,
    )


def test_passage_offsets_and_table_integrity() -> None:
    canonical = (
        "# Runway\n\n"
        "A runway supports aircraft take-off and landing operations.\n\n"
        "## Declared data\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        "| Length | 3000 m |\n"
    )
    passages = passage_document(
        _document(),
        canonical,
        {
            "target_tokens": 10,
            "max_tokens": 30,
            "overlap_tokens": 4,
            "max_table_tokens": 50,
        },
    )
    assert passages
    assert any(passage.table_id for passage in passages)
    for passage in passages:
        assert canonical[passage.canonical_char_start : passage.canonical_char_end] == passage.text


def test_local_tokenizer_rejects_checksum_mismatch(tmp_path: Path) -> None:
    (tmp_path / "tokenizer.json").write_text("altered", encoding="utf-8")
    (tmp_path / "tokenizer_config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="tokenizer checksum mismatch"):
        TokenCounter(
            {
                "mode": "huggingface_local",
                "id": "test/tokenizer",
                "revision": "a" * 40,
                "local_path": str(tmp_path),
                "checksums": {
                    "tokenizer.json": "0" * 64,
                    "tokenizer_config.json": "0" * 64,
                },
            }
        )
