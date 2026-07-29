# Release and publication

The code, configuration, prompts, schemas, tests, and DVC graph are intended for
GitHub under Apache-2.0. The open corpus, passages, and QA are published on
Hugging Face as JSONL and Parquet shards partitioned by license family. Zenodo
receives the immutable versioned archive, checksums, source/rights manifest,
data dictionary, annotation guide, and model/prompt manifest.

`aviation-data package --public` is the single packaging path. It verifies that
all included documents are `open`, that source/derivative/QA permissions are
affirmative, and that every evidence passage belongs to an included open
document. It publishes only a scrubbed manifest for restricted extensions.

Before uploading:

1. run all pilot gates and complete human review;
2. freeze source revisions and the airline cohort;
3. pin model/tokenizer commits and the container image digest;
4. archive the generated `checksums.sha256`;
5. create a Git tag matching the dataset version;
6. upload the identical archive to Zenodo and record the DOI in the data card.

The manuscript skeleton in `manuscript/data_descriptor.md` follows a Scientific
Data-style Data Descriptor and can be reformatted for Data in Brief.

