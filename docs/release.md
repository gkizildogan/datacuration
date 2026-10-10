# Release and publication

This GitHub repository is the single published source for the work: the code,
configuration, prompts, schemas, tests, DVC graph, and the 50-pair QA sample
(`50_qa_pairs.csv`). The code is Apache-2.0; third-party data keeps its own
per-record license.

`aviation-data package --public` is the single packaging path. It verifies that
all included documents are `open`, that source/derivative/QA permissions are
affirmative, and that every evidence passage belongs to an included open
document. It publishes only a scrubbed manifest for restricted extensions. The
generated `release/public/` tree is local output and is not committed.

Before tagging a release:

1. run all pilot gates and complete review;
2. freeze source revisions and the airline cohort;
3. pin model/tokenizer commits and the container image digest;
4. keep the generated `checksums.sha256` with the local package;
5. create a Git tag matching the dataset version and cite that tag and commit.

The manuscript skeleton in `manuscript/data_descriptor.md` follows a Scientific
Data-style Data Descriptor and can be reformatted for Data in Brief.
