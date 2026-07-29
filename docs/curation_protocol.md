# Frozen curation protocol

## Scope and unit of counting

Version 1 is a frozen civil-aviation research snapshot. Military and space
material are excluded except when needed as context in a civil regulator
document. A document is one independently identifiable source work or generated
entity profile. Alternate formats and versions share a `variant_group_id` and
do not inflate topic counts.

No personal aircraft-owner, airman, passenger, victim, employee, address, or
registry-owner data is intentionally curated. Current fleet, airport, company,
and regulatory claims require an `as_of` date and field-level provenance.

## Extraction policy

HTML extraction uses versioned, source-specific profiles. MediaWiki sources
must declare `mediawiki_article_v1`; the registry audit rejects a MediaWiki
source without it. This profile:

- walks the DOM in document order instead of grouping elements by tag;
- retains the article title, prose, headings, captions, nested lists,
  definition lists, equations, and readable tables;
- removes edit controls, citations, reference lists, navigation templates,
  authority-control boxes, and other non-content templates; and
- excludes non-content sections such as References, See also, External links,
  Kaynakça, Ayrıca bakınız, and Dış bağlantılar.

Each layout artifact records the profile, removed selectors, excluded sections,
block counts, normalized equation count, and heading-order diagnostics.
Extraction adds automatic flags for front-loaded detached headings, excessive
duplicate lines, remaining HTML boilerplate, oversized documents, and
list-heavy documents. Curation quarantines structural failures and oversized
documents. A list-heavy document is diagnostic only unless another rejection
condition is present.

MediaWiki `revision_timestamp` is copied to `DocumentRecord.as_of`, so current
company and fleet claims retain the date of their immutable source revision.
After changing an extraction profile, rebuild extraction and curation before
creating a new manual sample. Old review rows are preserved but are not counted
against changed, rejected, or unassigned document IDs.

## Taxonomy

1. Regulation, standards, licensing, and passenger rights
2. Airlines, operators, fleets, and aviation economics
3. Airports, heliports, runways, navigation aids, and codes
4. Aircraft, airworthiness, certification, and manufacturers
5. Engines, propulsion, components, and maintenance
6. Operations, air traffic management, meteorology, and training
7. Safety, accidents, incidents, and human factors
8. General aviation, rotorcraft, balloons, and unmanned aircraft

Every major topic must contribute at least 5% of accepted canonical tokens.
English/Turkish token share targets 70/30, with a tolerance of five percentage
points at the pilot gate. No source family may exceed 40% of canonical tokens.
Sampling is tracked across topic, language, publisher, authority, native format,
and publication period.

## Frozen airline cohort

Each release records the union of the top 10 airlines by passenger volume and
the top 10 by fleet size, plus Turkish Airlines, SunExpress, Pegasus, AJet, and
Corendon. The ranking source, year, metric, ties, and snapshot date are release
metadata. Proprietary IATA/ICAO products are not scraped.

## Rights states

- `open`: source and permitted derivatives may be packaged.
- `manifest_only`: public metadata, checksum, URL, and fetch recipe only.
- `blocked`: do not acquire.

Public availability is not evidence of permission. QA derived from share-alike
content remains in a compatible license shard. The registry records separate
permissions for source bytes, derived text, and derived QA.

## Pilot gates

The 500-document/1,500-QA pilot must reach 100% schema/checksum validity, zero
unclear-rights binaries in public output, 95% usable extraction in a stratified
manual sample, 99% structured generation success after retry, 100% answerable
evidence-offset validity, 95% human correctness/grounding, reviewer kappa of
0.70, and quotas within five percentage points. Missing human review data is
reported as `not_evaluated`, never silently passed.
