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

Schema v1.1 closed answers are extractive. Factual and temporal items contain
one exact `answer_items` span. List/table items contain source-ordered spans;
the displayed answer joins them with `"; "`, so the joined string need not be
contiguous in the evidence. Definition and comparison items leave
`answer_items` empty and require `answer == reference_answer` plus a rubric.
Translated, calculated, inferred, or reordered closed answers are invalid.

`corpus_unanswerable` means unanswerable from the frozen corpus, not false in
the world. Such items are created by plausible entity/date/attribute
substitution and must survive lexical and dense-retrieval checks. Reviewers
reject questions that are ambiguous, malformed, reveal the answer without
meaningful reformulation, or accidentally mix languages.

The pilot double-review sample is exactly 225 of 1,500 items, producing 450
independent A/B assignment rows. The same deterministic stratification scales
to 3,000 of 20,000 items for the full benchmark. Disagreements are adjudicated.
Cohen's kappa and a binomial 95% confidence interval for estimated error are
reported.

Extraction reviewers record one JSONL row per sampled document at
`data/reports/extraction_reviews.jsonl` with `document_id`, `reviewer_id`,
`usable` (boolean), `format`, `language`, `topic`, and `notes`. The 95%
extraction gate uses this manual sample; the automated acceptance ratio is only
a diagnostic proxy.

Generate the extraction sample only after `aviation-data curate`. It is drawn
from the accepted candidate corpus, not from documents already quarantined by
automatic policy. Each assignment includes the title, canonical path, pinned
source URL, length, and quality flags.

Review the sample interactively:

```bash
uv run aviation-data review extraction
```

The command asks for the reviewer ID once, opens each canonical document in the
terminal pager, and then accepts `t`/`true` or `f`/`false`. Quit the pager to
return to the decision prompt. Enter `q`/`quit` at that prompt to stop while
preserving completed decisions. Running the command again with the same
reviewer ID resumes from
`data/reports/extraction_reviews.progress.jsonl`. Use `--no-pager` to print
documents directly or `--restart` to intentionally discard saved progress.
Only after every assignment is complete does the command write the finalized
`data/reports/extraction_reviews.jsonl`.

Set `usable: true` only when the canonical document can be used for grounded QA
without manual cleanup:

- main content is coherent and remains in source order;
- the beginning is not a detached group of headings or template labels;
- navigation, references, authority-control data, and other boilerplate do not
  dominate the end or any other part;
- meaningful prose, lists, equations, and tables are not materially missing or
  duplicated; and
- language, characters, and table/list structure are readable.

Set `usable: false` when any material failure above applies. One useful
paragraph surrounded by extraction noise is not usable. A clean short document
may be usable even if it supports only a small number of questions. This label
assesses extraction fitness, not whether every paragraph should produce a QA
pair and not whether the source's claims are independently true. Compare the
canonical artifact with the pinned `source_url` or raw artifact when omission
or ordering is uncertain, and describe the first concrete failure in `notes`.

Every assigned document must have exactly one finalized extraction-review row,
a non-empty `reviewer_id`, and a JSON boolean `usable`. Incomplete, duplicate,
stale, rejected, or unassigned rows do not contribute to the manual gate.
