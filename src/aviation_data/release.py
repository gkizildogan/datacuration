from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from aviation_data.io import (
    read_jsonl,
    write_json,
    write_jsonl,
    write_parquet_if_available,
    write_qa_csv,
)
from aviation_data.models import (
    PUBLIC_MODELS,
    DocumentRecord,
    PassageRecord,
    QARecord,
    RightsState,
    SourceRecord,
)
from aviation_data.registry import load_registry


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for model in PUBLIC_MODELS:
        name = model.__name__
        path = output_dir / f"{name}.schema.json"
        write_json(path, model.model_json_schema())
        paths.append(path)
    return paths


def _safe_shard(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def _copy_artifact(data_dir: Path, release_dir: Path, relative: str, target: str) -> str:
    source = data_dir / relative
    if not source.exists():
        raise ValueError(f"artifact missing: {source}")
    destination = release_dir / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return target


def _checksum_lines(root: Path) -> list[str]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "checksums.sha256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return rows


def package_public(
    data_dir: Path,
    registry_path: Path,
    release_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    if release_dir.exists():
        if not force:
            raise FileExistsError(f"{release_dir} exists; pass --force to replace it")
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)
    registry = load_registry(registry_path)
    source_records = read_jsonl(data_dir / "manifests" / "source_records.jsonl", SourceRecord)
    documents = read_jsonl(data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord)
    passages = read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
    qa_rows = read_jsonl(data_dir / "qa" / "accepted.jsonl", QARecord)
    source_by_id = {record.source_record_id: record for record in source_records}

    open_documents = []
    included_sources: dict[str, SourceRecord] = {}
    for document in documents:
        if (
            document.rights_state != RightsState.OPEN
            or not document.release_derived_text
            or not document.release_qa
        ):
            continue
        source = source_by_id.get(document.source_record_id)
        if source is None:
            raise ValueError(f"document source record missing: {document.document_id}")
        if (
            source.rights.state != RightsState.OPEN
            or not source.rights.release_source
            or not source.rights.release_derived_text
        ):
            raise ValueError(f"inconsistent open rights: {document.document_id}")
        source_target = f"corpus/source/sha256/{source.sha256[:2]}/{source.sha256}"
        canonical_target = f"corpus/canonical/{document.document_id}.md"
        _copy_artifact(data_dir, release_dir, source.storage_path, source_target)
        _copy_artifact(data_dir, release_dir, document.canonical_path, canonical_target)
        public_artifacts = {
            "source": source_target,
            "canonical_markdown": canonical_target,
        }
        for artifact_name, artifact_path in document.artifact_paths.items():
            if artifact_name in {"source", "canonical_markdown"}:
                continue
            artifact_target = f"corpus/derived/{document.document_id}/{Path(artifact_path).name}"
            public_artifacts[artifact_name] = _copy_artifact(
                data_dir,
                release_dir,
                artifact_path,
                artifact_target,
            )
        included_sources[source.source_record_id] = source.model_copy(
            update={"storage_path": source_target}
        )
        open_documents.append(
            document.model_copy(
                update={
                    "canonical_path": canonical_target,
                    "artifact_paths": public_artifacts,
                }
            )
        )

    document_ids = {document.document_id for document in open_documents}
    open_passages = [passage for passage in passages if passage.document_id in document_ids]
    passage_ids = {passage.passage_id for passage in open_passages}
    open_qa = []
    for qa in qa_rows:
        if any(document_id not in document_ids for document_id in qa.source_document_ids):
            continue
        if any(evidence.passage_id not in passage_ids for evidence in qa.evidence):
            raise ValueError(f"QA evidence not in public passage set: {qa.qa_id}")
        if qa.generator.backend != "fixture" and (
            not re.fullmatch(r"[0-9a-f]{40}", qa.generator.model_revision)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", qa.generator.container_digest)
        ):
            raise ValueError(f"QA generator is not immutably pinned: {qa.qa_id}")
        open_qa.append(qa)

    records_dir = release_dir / "records"
    write_jsonl(
        records_dir / "sources.jsonl",
        sorted(included_sources.values(), key=lambda item: item.source_record_id),
    )
    write_jsonl(records_dir / "documents.jsonl", open_documents)
    write_jsonl(records_dir / "passages.jsonl", open_passages)
    write_jsonl(records_dir / "qa.jsonl", open_qa)
    write_parquet_if_available(records_dir / "sources.parquet", included_sources.values())
    write_parquet_if_available(records_dir / "documents.parquet", open_documents)
    write_parquet_if_available(records_dir / "passages.parquet", open_passages)
    write_parquet_if_available(records_dir / "qa.parquet", open_qa)
    write_qa_csv(records_dir / "qa.csv", [qa.model_dump(mode="json") for qa in open_qa])

    documents_by_id = {document.document_id: document for document in open_documents}
    passages_by_license: dict[str, list[PassageRecord]] = {}
    documents_by_license: dict[str, list[DocumentRecord]] = {}
    qa_by_license: dict[str, list[QARecord]] = {}
    for document in open_documents:
        documents_by_license.setdefault(document.license_id, []).append(document)
    for passage in open_passages:
        license_id = documents_by_id[passage.document_id].license_id
        passages_by_license.setdefault(license_id, []).append(passage)
    for qa in open_qa:
        licenses = sorted(
            {documents_by_id[document_id].license_id for document_id in qa.source_document_ids}
        )
        license_key = "+".join(licenses) or "no-evidence"
        qa_by_license.setdefault(license_key, []).append(qa)
    for license_id in sorted(
        set(documents_by_license) | set(passages_by_license) | set(qa_by_license)
    ):
        shard = release_dir / "license_shards" / _safe_shard(license_id)
        write_jsonl(shard / "documents.jsonl", documents_by_license.get(license_id, []))
        write_jsonl(shard / "passages.jsonl", passages_by_license.get(license_id, []))
        write_jsonl(shard / "qa.jsonl", qa_by_license.get(license_id, []))

    restricted_manifest = []
    fetched_by_source: dict[str, list[SourceRecord]] = {}
    for record in source_records:
        fetched_by_source.setdefault(record.registry_source_id, []).append(record)
    for definition in registry.sources:
        if definition.rights.state == RightsState.OPEN:
            continue
        fetched = fetched_by_source.get(definition.source_id, [])
        restricted_manifest.append(
            {
                "source_id": definition.source_id,
                "publisher": definition.publisher,
                "seed_urls": definition.seed_urls,
                "rights": definition.rights.model_dump(mode="json"),
                "fetch_recipe": {
                    "adapter": definition.adapter,
                    "version_discovery": definition.version_discovery,
                    "update_cadence": definition.update_cadence,
                },
                "fetched_objects": [
                    {
                        "canonical_url": record.canonical_url,
                        "snapshot_date": record.snapshot_date.isoformat(),
                        "sha256": record.sha256,
                        "byte_size": record.byte_size,
                        "detected_mime": record.detected_mime,
                    }
                    for record in fetched
                ],
            }
        )
    write_jsonl(release_dir / "restricted_extension_manifest.jsonl", restricted_manifest)
    export_schemas(release_dir / "schemas" / "v1.0.0")
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "docs" / "annotation_guide.md",
        release_dir / "annotation_guide.md",
    )
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "docs" / "data_dictionary.md",
        release_dir / "data_dictionary.md",
    )
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md",
        release_dir / "THIRD_PARTY_NOTICES.md",
    )
    model_manifests = {qa.generator.model_dump_json() for qa in open_qa}
    write_json(
        release_dir / "model_prompt_manifest.json",
        [json.loads(value) for value in sorted(model_manifests)],
    )
    licenses = sorted({document.license_id for document in open_documents})
    fixture_qa = sum("fixture_only" in qa.flags for qa in open_qa)
    data_card = [
        "# Bilingual Aviation Corpus and RAG QA Benchmark",
        "",
        "This directory is a frozen, rights-filtered public package.",
        "",
        f"- Documents: {len(open_documents)}",
        f"- Passages: {len(open_passages)}",
        f"- QA items: {len(open_qa)}",
        f"- Deterministic smoke-test QA items: {fixture_qa}",
        f"- License families: {', '.join(licenses) if licenses else 'none'}",
        "",
        "There is no blanket data license. Consult each record's `license_id`, "
        "`attribution`, and the corresponding license shard. The restricted "
        "extension manifest contains metadata and fetch recipes only.",
        "",
    ]
    if fixture_qa:
        data_card.extend(
            [
                "> [!WARNING]",
                "> QA carrying `fixture_only` is deterministic pipeline-test output, "
                "not benchmark-quality model output.",
                "",
            ]
        )
    (release_dir / "README.md").write_text("\n".join(data_card), encoding="utf-8")
    (release_dir / "checksums.sha256").write_text(
        "\n".join(_checksum_lines(release_dir)) + "\n", encoding="utf-8"
    )
    manifest = {
        "documents": len(open_documents),
        "passages": len(open_passages),
        "qa": len(open_qa),
        "sources": len(included_sources),
        "restricted_sources": len(restricted_manifest),
        "licenses": licenses,
        "fixture_qa": fixture_qa,
        "rights_boundary_verified": True,
        "output": str(release_dir),
    }
    write_json(release_dir / "package_manifest.json", manifest)
    # Recompute after adding package_manifest.
    (release_dir / "checksums.sha256").write_text(
        "\n".join(_checksum_lines(release_dir)) + "\n", encoding="utf-8"
    )
    return manifest
