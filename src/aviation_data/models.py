from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0.0"
QA_SCHEMA_VERSION = "1.1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RightsState(StrEnum):
    OPEN = "open"
    MANIFEST_ONLY = "manifest_only"
    BLOCKED = "blocked"


class Language(StrEnum):
    ENGLISH = "en"
    TURKISH = "tr"
    MULTILINGUAL = "mul"
    UNDETERMINED = "und"


class Topic(StrEnum):
    REGULATION = "regulation"
    AIRLINES = "airlines"
    AIRPORTS = "airports"
    AIRCRAFT = "aircraft"
    ENGINES = "engines"
    OPERATIONS = "operations"
    SAFETY = "safety"
    GENERAL_AVIATION = "general_aviation"


class Answerability(StrEnum):
    ANSWERABLE = "answerable"
    CORPUS_UNANSWERABLE = "corpus_unanswerable"


class QAType(StrEnum):
    FACTUAL = "factual"
    DEFINITION = "definition"
    LIST_TABLE = "list_table"
    COMPARISON = "comparison"
    TEMPORAL = "temporal"


class Split(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    UNASSIGNED = "unassigned"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    AUTO_ACCEPTED = "auto_accepted"
    AUTO_REJECTED = "auto_rejected"
    HUMAN_ACCEPTED = "human_accepted"
    HUMAN_REJECTED = "human_rejected"
    ADJUDICATED = "adjudicated"


class RightsEvidence(StrictModel):
    state: RightsState
    license_id: str = Field(min_length=1)
    license_url: str = Field(min_length=1)
    terms_url: str = Field(min_length=1)
    reviewed_on: date
    attribution: str = Field(min_length=1)
    release_source: bool
    release_derived_text: bool
    release_qa: bool
    notes: str | None = None


class RegistryProject(StrictModel):
    name: str
    contact: str
    user_agent: str
    default_requests_per_second: float = Field(default=1.0, gt=0)
    default_concurrency: int = Field(default=1, ge=1)
    default_max_bytes: int = Field(default=52_428_800, ge=1)


class MediaWikiApiConfig(StrictModel):
    page_titles: list[str] = Field(default_factory=list)
    category_titles: list[str] = Field(default_factory=list)
    excluded_category_titles: list[str] = Field(default_factory=list)
    excluded_category_patterns: list[str] = Field(default_factory=list)
    excluded_page_category_titles: list[str] = Field(default_factory=list)
    excluded_page_category_patterns: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=200, ge=1, le=5_000)
    max_candidate_pages: int | None = Field(default=None, ge=1, le=5_000)
    max_pages_per_category: int = Field(default=50, ge=1, le=500)
    max_total_bytes: int = Field(default=536_870_912, ge=1)
    batch_size: int = Field(default=20, ge=1, le=50)
    maxlag: int = Field(default=5, ge=1, le=60)
    subcategory_depth: Literal[0, 1] = 0
    max_subcategories: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def has_discovery_seeds(self) -> MediaWikiApiConfig:
        if not self.page_titles and not self.category_titles:
            raise ValueError("MediaWiki API sources need page_titles or category_titles")
        if self.max_candidate_pages is not None and self.max_candidate_pages < self.max_pages:
            raise ValueError("max_candidate_pages must be greater than or equal to max_pages")
        for pattern in [
            *self.excluded_category_patterns,
            *self.excluded_page_category_patterns,
        ]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid excluded category pattern {pattern!r}: {exc}") from exc
        return self


class ExtractionConfig(StrictModel):
    profile: Literal[
        "generic_html_v2",
        "mediawiki_article_v1",
        "dhmi_workbook_v1",
        "shgm_abbreviations_v1",
        "easa_toc_section_v1",
        "faa_purpose_applicability_v1",
    ]
    selection_seed: int | None = None

    @model_validator(mode="after")
    def valid_profile_settings(self) -> ExtractionConfig:
        if self.profile == "easa_toc_section_v1" and self.selection_seed is None:
            raise ValueError("easa_toc_section_v1 requires selection_seed")
        if self.profile != "easa_toc_section_v1" and self.selection_seed is not None:
            raise ValueError("selection_seed is only valid for easa_toc_section_v1")
        return self


class SourceDefinition(StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    enabled: bool = False
    adapter: str
    seed_urls: list[str]
    publisher: str
    source_family: str
    authority_level: str
    languages: list[Language]
    topics: list[Topic]
    expected_mime_types: list[str]
    title: str | None = None
    native_format: str
    update_cadence: str
    version_discovery: str
    selectors: dict[str, str] = Field(default_factory=dict)
    bulk_patterns: list[str] = Field(default_factory=list)
    requests_per_second: float | None = Field(default=None, gt=0)
    concurrency: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    mediawiki: MediaWikiApiConfig | None = None
    extraction: ExtractionConfig | None = None
    rights: RightsEvidence


class SourceRegistry(StrictModel):
    project: RegistryProject
    sources: list[SourceDefinition]

    @model_validator(mode="after")
    def unique_source_ids(self) -> SourceRegistry:
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source_id values must be unique")
        return self


class SourceRecord(StrictModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    source_record_id: str
    registry_source_id: str
    publisher: str
    source_family: str
    canonical_url: str
    request_url: str
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime
    snapshot_date: date
    source_version: str
    etag: str | None = None
    last_modified: str | None = None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=0)
    detected_mime: str
    storage_path: str
    native_format: str
    title: str | None = None
    languages: list[Language]
    topics: list[Topic]
    authority_level: str
    rights: RightsEvidence
    fetch_recipe: dict[str, Any] = Field(default_factory=dict)


class DocumentRecord(StrictModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    document_id: str
    document_version: str
    variant_group_id: str
    entity_group_id: str | None = None
    title: str
    language: Language
    topics: list[Topic]
    publication_date: date | None = None
    as_of: date | None = None
    publisher: str
    source_family: str
    authority_level: str
    source_record_id: str
    source_url: str
    native_mime: str
    native_format: str
    license_id: str
    attribution: str
    rights_state: RightsState
    release_derived_text: bool
    release_qa: bool
    canonical_path: str
    canonical_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_char_count: int = Field(ge=0)
    canonical_token_count: int = Field(ge=0)
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    derived_from: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    duplicate_group_id: str | None = None
    duplicate_of: str | None = None
    accepted: bool = True


class PassageRecord(StrictModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    passage_id: str
    document_id: str
    document_version: str
    variant_group_id: str
    language: Language
    topics: list[Topic]
    section_path: list[str] = Field(default_factory=list)
    page_number: int | None = Field(default=None, ge=1)
    table_id: str | None = None
    canonical_char_start: int = Field(ge=0)
    canonical_char_end: int = Field(ge=0)
    text: str
    token_count: int = Field(ge=0)
    tokenizer_id: str = "regex-word-v1"
    tokenizer_revision: str = "local-v1"
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid_offsets(self) -> PassageRecord:
        if self.canonical_char_end <= self.canonical_char_start:
            raise ValueError("passage end must be greater than start")
        if self.canonical_char_end - self.canonical_char_start != len(self.text):
            raise ValueError("passage offsets must span the exact text length")
        return self


class EvidenceSpan(StrictModel):
    passage_id: str
    document_id: str
    passage_char_start: int = Field(ge=0)
    passage_char_end: int = Field(ge=0)
    canonical_char_start: int = Field(ge=0)
    canonical_char_end: int = Field(ge=0)
    quote: str = Field(min_length=1)
    quote_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid_offsets(self) -> EvidenceSpan:
        if self.passage_char_end - self.passage_char_start != len(self.quote):
            raise ValueError("passage evidence offsets do not span quote")
        if self.canonical_char_end - self.canonical_char_start != len(self.quote):
            raise ValueError("canonical evidence offsets do not span quote")
        return self


class GeneratorConfiguration(StrictModel):
    backend: str
    model_id: str
    model_revision: str
    tokenizer_revision: str
    container_digest: str
    prompt_version: str
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    temperature: float
    seed: int
    settings: dict[str, Any] = Field(default_factory=dict)


class QARecord(StrictModel):
    schema_version: Literal["1.1.0"] = QA_SCHEMA_VERSION
    qa_id: str
    question: str = Field(min_length=1)
    answer: str | None = None
    answer_items: list[str] = Field(default_factory=list)
    reference_answer: str | None = None
    rubric: list[str] = Field(default_factory=list)
    question_language: Language
    evidence_languages: list[Language] = Field(default_factory=list)
    cross_lingual: bool = False
    primary_type: QAType
    flags: list[str] = Field(default_factory=list)
    answerability: Answerability
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    acceptable_variants: list[str] = Field(default_factory=list)
    provenance_passage_ids: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)
    split_group_id: str
    split: Split = Split.UNASSIGNED
    generator: GeneratorConfiguration
    generation_attempts: int = Field(default=1, ge=1)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    rejection_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def answerability_consistency(self) -> QARecord:
        if self.answerability == Answerability.ANSWERABLE:
            if not self.answer or not self.evidence:
                raise ValueError("answerable QA requires an answer and evidence")
            if self.primary_type in {QAType.FACTUAL, QAType.TEMPORAL}:
                if len(self.answer_items) != 1 or self.answer != self.answer_items[0]:
                    raise ValueError(
                        "factual and temporal QA require one answer_item equal to answer"
                    )
                if self.reference_answer is not None or self.rubric:
                    raise ValueError(
                        "factual and temporal QA do not allow reference_answer or rubric"
                    )
            elif self.primary_type == QAType.LIST_TABLE:
                if not self.answer_items or self.answer != "; ".join(self.answer_items):
                    raise ValueError(
                        "list/table QA requires source-ordered answer_items joined by '; '"
                    )
                if self.reference_answer is not None or self.rubric:
                    raise ValueError("list/table QA does not allow reference_answer or rubric")
            elif self.primary_type in {QAType.DEFINITION, QAType.COMPARISON}:
                if self.answer_items:
                    raise ValueError("explanatory QA must leave answer_items empty")
                if self.answer != self.reference_answer or not self.rubric:
                    raise ValueError(
                        "explanatory QA requires answer == reference_answer and a rubric"
                    )
        elif self.answer is not None or self.evidence:
            raise ValueError("corpus-unanswerable QA must not contain answer or evidence")
        elif self.answer_items or self.reference_answer is not None or self.rubric:
            raise ValueError(
                "corpus-unanswerable QA must not contain answer items, reference answer, or rubric"
            )
        elif (
            self.acceptable_variants
            or self.evidence_languages
            or self.provenance_passage_ids
            or self.source_document_ids
        ):
            raise ValueError("corpus-unanswerable QA must not expose answer/evidence provenance")
        expected_cross_lingual = any(
            language != self.question_language for language in self.evidence_languages
        )
        if self.cross_lingual != expected_cross_lingual:
            raise ValueError("cross_lingual must match question/evidence languages")
        return self


PUBLIC_MODELS = (SourceRecord, DocumentRecord, PassageRecord, QARecord)
