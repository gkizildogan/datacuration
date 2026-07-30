# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives in `src/aviation_data/`; the Typer CLI is defined in
`cli.py`, domain models in `models.py`, and source integrations under `adapters/`.
Tests are in `tests/` and use local inputs from `fixtures/`. Pipeline settings
belong in `configs/`, prompts in `prompts/`, and versioned public contracts in
`schemas/`. Documentation is split between `docs/`, `manuscript/`, and
`publication/`. The generated `data/` tree contains raw, extracted, curated,
passage, QA, manifest, and report artifacts; treat raw snapshots as immutable.

## Build, Test, and Development Commands

- `uv sync --extra dev --extra formats` installs the package, test tools, and
  optional document-format readers.
- `uv run aviation-data --help` lists pipeline commands.
- `uv run pytest` runs the complete test suite; use
  `uv run pytest tests/test_passages.py` for a focused run.
- `uv run ruff check .` checks imports, style, upgrades, and common bugs.
- `uv run ruff format --check .` verifies formatting.
- `uv run dvc repro` executes the reproducible fixture pipeline after installing
  the `release` extra.
- `uv build` creates source and wheel distributions.

Use the fixture QA backend for local smoke tests; live source acquisition and
model-backed generation are opt-in.

## Coding Style & Naming Conventions

Target Python 3.11+ with four-space indentation, type hints, and a 100-character
line limit. Ruff enforces `E`, `F`, `I`, `UP`, `B`, and `SIM` rules. Use
`snake_case` for modules, functions, variables, and CLI commands; use
`PascalCase` for Pydantic models. Keep pipeline behavior deterministic and put
user-adjustable values in YAML rather than hard-coding them.

## Testing Guidelines

Pytest discovers `tests/test_*.py`; name individual tests `test_<behavior>`.
Add regression coverage beside the affected feature and prefer offline fixtures
over network calls or machine-local model assets. Run the full suite before
submitting changes. No numeric coverage threshold is configured, but new paths
and failure gates should be exercised.

## Commit & Pull Request Guidelines

History is currently sparse (`Init push`, `v1.1 Updates`), so no strict commit
prefix is established. Use a short, imperative subject that identifies the
change, and keep unrelated work separate. Pull requests should explain the
motivation, list validation commands, link relevant issues, and call out schema,
configuration, or generated-data changes. Include sample output when CLI or
report behavior changes.

## Security, Rights, and Configuration

Do not enable network fetching without reviewing `configs/sources.yaml`.
Preserve provenance, checksums, immutable revision pins, and the `open`,
`manifest_only`, and `blocked` rights states. Never place unclear-rights source
content in a public release.
