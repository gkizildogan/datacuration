from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from aviation_data.acquisition import fetch_sources
from aviation_data.curation import curate_documents
from aviation_data.extraction import extract_sources
from aviation_data.passages import build_passages
from aviation_data.qa_generation import _generator_config, build_qa
from aviation_data.qa_validation import validate_qa
from aviation_data.registry import load_registry
from aviation_data.release import package_public
from aviation_data.reporting import build_report
from aviation_data.review import create_extraction_review_sample, create_review_sample

ROOT = Path(__file__).resolve().parents[1]


def test_offline_pipeline_and_public_rights_boundary(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    registry_path = ROOT / "configs" / "sources.yaml"
    registry = load_registry(registry_path)
    registry = registry.model_copy(
        update={
            "sources": [
                source.model_copy(update={"enabled": False})
                if source.adapter == "local_glob"
                else source
                for source in registry.sources
            ]
        }
    )
    source_records, fetch_errors = asyncio.run(
        fetch_sources(
            registry,
            registry_path,
            data_dir,
            date(2026, 7, 29),
            allow_network=False,
        )
    )
    fixture_source_ids = {
        "fixture_airport_markdown_en",
        "fixture_safety_html_tr",
        "fixture_aircraft_json_en",
    }
    assert len(source_records) == 3
    assert fixture_source_ids == {record.registry_source_id for record in source_records}
    assert not fetch_errors
    assert all(record.rights.state.value == "open" for record in source_records)

    documents, extraction_errors = extract_sources(registry, data_dir)
    assert len(documents) == 3
    assert not extraction_errors
    assert any(
        key.startswith("table_parquet:")
        for document in documents
        for key in document.artifact_paths
    )
    accepted_documents, rejected_documents, curation = curate_documents(
        data_dir, ROOT / "configs" / "sampling.yaml"
    )
    assert len(accepted_documents) == 3
    assert not rejected_documents
    assert curation["language_observation"]["blocking"] is False
    assert set(curation["language_observation"]["views"]) == {
        "all_accepted",
        "qa_eligible",
    }
    assert not any(issue["code"] == "language_quota" for issue in curation["quota_issues"])
    extraction_assignments = create_extraction_review_sample(data_dir)
    assert {row["document_id"] for row in extraction_assignments} == {
        document.document_id for document in accepted_documents
    }
    assert all(row["review_scope"] == "accepted_corpus" for row in extraction_assignments)
    assert all(row["canonical_token_count"] > 0 for row in extraction_assignments)

    passages, _ = build_passages(data_dir, ROOT / "configs" / "passages.fixture.yaml")
    assert len(passages) >= 3
    restricted_document_ids: set[str] = set()
    qa_rows, qa_build_report = build_qa(
        data_dir,
        ROOT / "configs" / "generation.yaml",
        ROOT / "prompts" / "qa_generation.md",
        backend="fixture",
        endpoint="http://127.0.0.1:8000/v1",
        target=8,
        model_choice="primary",
        run_id="fixture-e2e",
        max_fill_cycles=2,
    )
    assert len(qa_rows) == 8
    assert qa_build_report["status"] == "complete"
    accepted_qa, rejected_qa, validation = validate_qa(
        data_dir,
        run_id="fixture-e2e",
    )
    assert len(accepted_qa) == 8
    assert all(
        not restricted_document_ids.intersection(qa.source_document_ids)
        for qa in [*accepted_qa, *rejected_qa]
    )
    assert validation["evidence_offsets_valid"]
    assert validation["accepted_qa_language_balance"] == {
        "blocking": False,
        "reference_shares": {"en": 0.5, "tr": 0.5},
        "tolerance_points": 0.05,
        "counts": {"en": 4, "tr": 4},
        "shares": {"en": 0.5, "tr": 0.5},
        "within_tolerance": {"en": True, "tr": True},
        "status": "within_tolerance",
    }
    review_assignments = create_review_sample(
        data_dir,
        run_id="fixture-e2e",
    )
    assert len(review_assignments) % 2 == 0
    assert {row["reviewer_slot"] for row in review_assignments} == {"A", "B"}

    report = build_report(data_dir, qa_run_id="fixture-e2e")
    assert report["overall_status"] == "fail"
    assert report["accepted_qa_language_balance"]["status"] == "within_tolerance"
    assert (
        next(gate for gate in report["gates"] if gate["name"] == "schema_and_checksum_coverage")[
            "status"
        ]
        == "pass"
    )

    release_dir = tmp_path / "release"
    manifest = package_public(
        data_dir,
        registry_path,
        release_dir,
        qa_run_id="fixture-e2e",
    )
    assert manifest["documents"] == 3
    assert manifest["qa"] == 8
    restricted = [
        json.loads(line)
        for line in (release_dir / "restricted_extension_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row["rights"]["state"] for row in restricted} == {
        "manifest_only",
        "blocked",
    }
    serialized = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in release_dir.rglob("*")
        if path.is_file()
    )
    assert str(data_dir) not in serialized
    assert "raw/sha256/" not in serialized


def test_vllm_backend_accepts_only_immutable_container_digest(tmp_path: Path) -> None:
    del tmp_path
    config = yaml.safe_load((ROOT / "configs" / "generation.yaml").read_text())
    prompt = (ROOT / "prompts" / "qa_generation.md").read_text()

    generator = _generator_config(config, prompt, "vllm")
    assert generator.container_digest == config["model"]["container_digest"]

    config["model"]["container_digest"] = "vllm/vllm-openai:v0.25.1"
    with pytest.raises(ValueError, match="container_digest"):
        _generator_config(config, prompt, "vllm")


def test_airline_cohort_gate_requires_two_top_ten_rankings(tmp_path: Path) -> None:
    entries = [{"rank": rank, "airline": f"Airline {rank}"} for rank in range(1, 11)]
    cohort_path = tmp_path / "airline_cohort.yaml"
    cohort = {
        "status": "frozen",
        "ranking_inputs": {
            "passenger_volume": {"top_10": entries},
            "fleet_size": {"top_10": entries},
        },
    }
    cohort_path.write_text(yaml.safe_dump(cohort), encoding="utf-8")

    report = build_report(tmp_path / "data", cohort_path)
    gate = next(item for item in report["gates"] if item["name"] == "airline_cohort_frozen")
    assert gate["status"] == "pass"
    assert gate["threshold"] == "frozen with two top-10 inputs"

    cohort["ranking_inputs"]["fleet_size"]["top_10"] = entries[:9]
    cohort_path.write_text(yaml.safe_dump(cohort), encoding="utf-8")
    report = build_report(tmp_path / "data", cohort_path)
    gate = next(item for item in report["gates"] if item["name"] == "airline_cohort_frozen")
    assert gate["status"] == "fail"
