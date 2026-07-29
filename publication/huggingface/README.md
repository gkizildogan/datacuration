---
language:
- en
- tr
task_categories:
- question-answering
- text-retrieval
pretty_name: Bilingual Aviation Corpus and RAG QA Benchmark
license: other
---

# Bilingual Aviation Corpus and RAG QA Benchmark

This card is finalized from `release/public/package_manifest.json` only after
all pilot gates pass. Do not upload the bundled deterministic fixtures as the
research dataset.

The release contains browsable source/document metadata, canonical documents,
retrieval passages, and evidence-linked QA in JSONL and Parquet, plus a simple
QA CSV. Each row preserves its own license and attribution. There is no blanket
dataset license; `license_shards/` separates license families.

## Intended use

Research on bilingual and cross-lingual retrieval, grounded generation,
closed-answer evaluation, explanatory-answer judging, and aviation-domain data
curation. This is a frozen research snapshot, not an operational aeronautical
information, safety, navigation, or regulatory service.

## Limitations and sensitive data

English/Turkish and topic quotas are reported by canonical tokens. Current
facts have `as_of` provenance. No owner, airman, passenger, victim, employee,
address, or other unnecessary personal data is intentionally curated. OCR,
tables, community sources, and authority levels are diagnostic dimensions.

## Rights

The restricted extension contains public URLs, checksums, and fetch recipes
only. Restricted binaries, derived text, passages, and QA are not uploaded.
Consult `THIRD_PARTY_NOTICES.md` and every record's rights fields.

