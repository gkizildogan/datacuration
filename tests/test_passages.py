from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aviation_data.io import write_jsonl
from aviation_data.models import DocumentRecord, Language, RightsState, Topic
from aviation_data.passages import TokenCounter, build_passages, passage_document


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


def test_build_passages_excludes_manifest_only_documents(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    open_document = _document().model_copy(
        update={
            "document_id": "doc_open",
            "variant_group_id": "variant-open",
            "canonical_path": "canonical/open.md",
        }
    )
    restricted_document = _document().model_copy(
        update={
            "document_id": "doc_restricted",
            "variant_group_id": "variant-restricted",
            "canonical_path": "canonical/restricted.md",
            "rights_state": RightsState.MANIFEST_ONLY,
            "release_derived_text": False,
            "release_qa": False,
        }
    )
    for document in (open_document, restricted_document):
        path = data_dir / document.canonical_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Aviation\n\nA sufficiently detailed aviation passage for rights testing.",
            encoding="utf-8",
        )
    write_jsonl(
        data_dir / "curated" / "accepted_documents.jsonl",
        [open_document, restricted_document],
    )
    config_path = tmp_path / "passages.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "target_tokens": 10,
                "max_tokens": 30,
                "overlap_tokens": 4,
                "max_table_tokens": 50,
                "tokenizer": {
                    "mode": "regex_fixture",
                    "id": "regex-word-v1",
                    "revision": "local-v1",
                },
            }
        ),
        encoding="utf-8",
    )

    passages, report = build_passages(data_dir, config_path)

    assert {passage.document_id for passage in passages} == {"doc_open"}
    assert report["accepted_documents"] == 2
    assert report["documents"] == 1
    assert report["rights_excluded_documents"] == 1
