# QA annotation guide

An answerable QA item must be understandable without hidden context and must
have exact evidence in one or more accepted passages. Evidence annotations use
passage-relative and canonical-document character offsets. The quote at both
locations must match byte-for-byte after JSON decoding.

Reviewers independently score:

- question clarity;
- answer correctness;
- evidence sufficiency;
- language quality.

Each dimension is binary for gate computation and may include a note. A
definition or explanatory answer also has a concise rubric listing required
points. A cross-lingual item is one whose question language differs from at
least one evidence passage language.

`corpus_unanswerable` means unanswerable from the frozen corpus, not false in
the world. Such items are created by plausible entity/date/attribute
substitution and must survive lexical and dense-retrieval checks. Reviewers
reject questions that are ambiguous, malformed, reveal the answer without
meaningful reformulation, or accidentally mix languages.

The double-review sample is a deterministic, stratified 15% of the 20,000-item
benchmark (3,000 items). Disagreements are adjudicated. Cohen's kappa and a
binomial 95% confidence interval for estimated error are reported.

Extraction reviewers record one JSONL row per sampled document at
`data/reports/extraction_reviews.jsonl` with `document_id`, `reviewer_id`,
`usable` (boolean), `format`, `language`, `topic`, and `notes`. The 95%
extraction gate uses this manual sample; the automated acceptance ratio is only
a diagnostic proxy.
