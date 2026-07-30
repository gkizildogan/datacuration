# Data dictionary

Source, document, and passage records carry `schema_version: "1.0.0"`.
QA records use `schema_version: "1.1.0"`. JSONL is UTF-8 with one complete
record per line. Parquet columns mirror the JSON representation.

## SourceRecord

One immutable acquisition event. It records the registry source, publisher and
source family; request and final URLs; redirect chain; filtered response
headers; retrieval/snapshot/version metadata; SHA-256 and byte size; detected
MIME and content-addressed path; authority, language, and topics; exact rights
evidence; and the fetch recipe.

For `local_glob` acquisition, the fetch recipe also records the configured
glob, concrete project-relative path, checksum, and extraction profile.

## DocumentRecord

One independently identifiable source work/version. Stable document,
version/entity/variant/duplicate groups support deduplication and leakage-safe
splits. Canonical text has a checksum, character/token counts, language/topics,
native MIME/format, dates, rights and attribution, derivation links, artifact
paths, quality flags, and acceptance status.

Source-specific structured JSON artifacts are registered under descriptive
`artifact_paths` keys. DHMI, SHGM, EASA, and FAA artifacts use
`schema_version: "1.0.0"` and name their versioned extraction profile.

## PassageRecord

One retrieval unit linked to a document version. It stores heading path,
page/table location, exact canonical character offsets, text/checksum,
tokenizer ID/revision, token count, language, and topics. Character slices must
reproduce `text` exactly.

## QARecord

One grounded question. It stores `answer_items`, answer/reference/rubric, question and evidence
languages, cross-lingual label, primary type, answerability, exact
EvidenceSpans, variants, provenance, group split, immutable generator
configuration, attempts, flags, review status, and rejection reasons.

An EvidenceSpan has both passage-relative and canonical-document offsets plus a
quote checksum. Factual and temporal records have exactly one extractive
`answer_items` value. List/table records have source-ordered items and join them
with `"; "` for the display answer. Definition/comparison records have no
items and require `answer == reference_answer` plus a non-empty rubric.
Corpus-unanswerable records contain no answer, evidence, or parent provenance.
