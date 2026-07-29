from __future__ import annotations

from pathlib import Path

import yaml

from aviation_data.models import RightsState, SourceDefinition, SourceRegistry


def load_registry(path: Path) -> SourceRegistry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SourceRegistry.model_validate(raw)


def source_index(registry: SourceRegistry) -> dict[str, SourceDefinition]:
    return {source.source_id: source for source in registry.sources}


def audit_registry(registry: SourceRegistry) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    implemented_adapters = {"file", "direct", "mediawiki_api"}
    contact = registry.project.contact.casefold()
    if "replace-with" in contact or contact.endswith("@example.org"):
        issues.append(
            {
                "severity": "warning",
                "source_id": "_project",
                "code": "placeholder_contact",
                "message": "Replace the project contact before network acquisition.",
            }
        )
    for source in registry.sources:
        rights = source.rights
        if source.enabled and source.adapter not in implemented_adapters:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "adapter_not_implemented",
                    "message": (
                        f"Adapter {source.adapter!r} must be implemented and tested "
                        "before this source is enabled."
                    ),
                }
            )
        if source.enabled and rights.state == RightsState.BLOCKED:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "blocked_enabled",
                    "message": "A blocked source cannot be enabled.",
                }
            )
        if rights.state == RightsState.OPEN and not (
            rights.release_source and rights.release_derived_text
        ):
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "inconsistent_open_rights",
                    "message": "Open sources must permit source and derived-text release.",
                }
            )
        if rights.state != RightsState.OPEN and any(
            (rights.release_source, rights.release_derived_text, rights.release_qa)
        ):
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "restricted_release_flag",
                    "message": "Non-open sources cannot have public release flags.",
                }
            )
        if source.enabled and not source.seed_urls:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "missing_seed",
                    "message": "Enabled sources need at least one seed URL.",
                }
            )
        if source.adapter == "mediawiki_api" and source.mediawiki is None:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "missing_mediawiki_config",
                    "message": "MediaWiki API sources need a bounded mediawiki configuration.",
                }
            )
        if source.adapter == "mediawiki_api" and len(source.seed_urls) != 1:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "invalid_mediawiki_endpoint_count",
                    "message": "A bounded MediaWiki source must use exactly one API endpoint.",
                }
            )
        if source.adapter == "mediawiki_api" and (
            source.extraction is None
            or source.extraction.profile != "mediawiki_article_v1"
        ):
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "missing_mediawiki_extraction_profile",
                    "message": (
                        "MediaWiki API sources must use the versioned "
                        "mediawiki_article_v1 extraction profile."
                    ),
                }
            )
        if source.adapter != "mediawiki_api" and source.mediawiki is not None:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "unexpected_mediawiki_config",
                    "message": "Only the mediawiki_api adapter accepts mediawiki configuration.",
                }
            )
        if not rights.license_url or not rights.terms_url or not rights.attribution:
            issues.append(
                {
                    "severity": "error",
                    "source_id": source.source_id,
                    "code": "incomplete_rights_evidence",
                    "message": "License URL, terms URL, and attribution are required.",
                }
            )
    return issues
