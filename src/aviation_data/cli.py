from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import click
import typer

from aviation_data.acquisition import fetch_sources
from aviation_data.curation import curate_documents
from aviation_data.evaluation import (
    evaluate_answers,
    evaluate_explanatory,
    evaluate_retrieval,
)
from aviation_data.extraction import extract_sources
from aviation_data.io import read_jsonl, write_jsonl
from aviation_data.passages import build_passages
from aviation_data.qa_generation import build_qa, generate_qa
from aviation_data.qa_lifecycle import promote_qa_run
from aviation_data.qa_planning import CapacityError, qa_run_dir
from aviation_data.qa_validation import validate_qa
from aviation_data.registry import audit_registry, load_registry
from aviation_data.release import export_schemas, package_public
from aviation_data.reporting import build_report
from aviation_data.review import create_extraction_review_sample, create_review_sample

app = typer.Typer(
    name="aviation-data",
    help="Build a rights-aware bilingual aviation corpus and grounded QA benchmark.",
    no_args_is_help=True,
)
rights_app = typer.Typer(help="Audit acquisition and redistribution rights.")
passages_app = typer.Typer(help="Build retrieval-ready passages.")
qa_app = typer.Typer(help="Generate, validate, and review grounded QA.")
schemas_app = typer.Typer(help="Export public record schemas.")
evaluate_app = typer.Typer(help="Run retrieval and answer baselines.")
review_app = typer.Typer(help="Create and complete manual extraction reviews.")
app.add_typer(rights_app, name="rights")
app.add_typer(passages_app, name="passages")
app.add_typer(qa_app, name="qa")
app.add_typer(schemas_app, name="schemas")
app.add_typer(evaluate_app, name="evaluate")
app.add_typer(review_app, name="review")

DEFAULT_REGISTRY = Path("configs/sources.yaml")
DEFAULT_DATA = Path("data")


class Backend(StrEnum):
    FIXTURE = "fixture"
    VLLM = "vllm"


class RetrievalBackend(StrEnum):
    BM25 = "bm25"
    DENSE = "dense"


class ModelChoice(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


def _echo(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@rights_app.command("audit")
def rights_audit(
    registry_path: Annotated[Path, typer.Option("--registry")] = DEFAULT_REGISTRY,
    strict: Annotated[
        bool, typer.Option(help="Treat warnings as a non-zero audit result.")
    ] = False,
) -> None:
    registry = load_registry(registry_path)
    issues = audit_registry(registry)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    summary = {
        "sources": len(registry.sources),
        "open": sum(source.rights.state.value == "open" for source in registry.sources),
        "manifest_only": sum(
            source.rights.state.value == "manifest_only" for source in registry.sources
        ),
        "blocked": sum(source.rights.state.value == "blocked" for source in registry.sources),
        "issues": issues,
    }
    _echo(summary)
    if errors or (strict and issues):
        raise typer.Exit(code=1)


@app.command("fetch")
def fetch(
    snapshot: Annotated[str, typer.Option("--snapshot", help="Frozen snapshot date (YYYY-MM-DD).")],
    registry_path: Annotated[Path, typer.Option("--registry")] = DEFAULT_REGISTRY,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    network: Annotated[
        bool, typer.Option("--network", help="Explicitly permit HTTP(S) acquisition.")
    ] = False,
) -> None:
    try:
        snapshot_date = date.fromisoformat(snapshot)
    except ValueError as exc:
        raise typer.BadParameter("snapshot must use YYYY-MM-DD") from exc
    registry = load_registry(registry_path)
    issues = audit_registry(registry)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    if errors:
        _echo({"rights_audit_errors": errors})
        raise typer.Exit(code=1)
    if network and any(issue["code"] == "placeholder_contact" for issue in issues):
        typer.echo("Replace the placeholder registry contact before network acquisition.")
        raise typer.Exit(code=1)
    records, fetch_errors = asyncio.run(
        fetch_sources(
            registry,
            registry_path,
            data_dir,
            snapshot_date,
            allow_network=network,
        )
    )
    _echo({"records": len(records), "errors": fetch_errors})
    if fetch_errors:
        raise typer.Exit(code=2)


@app.command("extract")
def extract(
    registry_path: Annotated[Path, typer.Option("--registry")] = DEFAULT_REGISTRY,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
) -> None:
    documents, errors = extract_sources(load_registry(registry_path), data_dir)
    _echo({"documents": len(documents), "errors": errors})
    if errors:
        raise typer.Exit(code=2)


@app.command("curate")
def curate(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    sampling_config: Annotated[Path, typer.Option("--sampling-config")] = Path(
        "configs/sampling.yaml"
    ),
) -> None:
    accepted, rejected, stats = curate_documents(data_dir, sampling_config)
    _echo({"accepted": len(accepted), "rejected": len(rejected), "stats": stats})


@passages_app.command("build")
def passages_build(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/passages.yaml"),
) -> None:
    passages, stats = build_passages(data_dir, config_path)
    _echo({"passages": len(passages), "stats": stats})


@qa_app.command("generate")
def qa_generate(
    backend: Annotated[Backend, typer.Option("--backend")] = Backend.FIXTURE,
    target: Annotated[int, typer.Option("--target", min=1)] = 1500,
    model_choice: Annotated[ModelChoice, typer.Option("--model-choice")] = ModelChoice.PRIMARY,
    run_id: Annotated[
        str,
        typer.Option(
            "--run-id",
            help="Isolated QA experiment ID. Use qa promote to update benchmark paths.",
        ),
    ] = "qa-v2",
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:8000/v1",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/generation.yaml"),
    prompt_path: Annotated[Path, typer.Option("--prompt")] = Path("prompts/qa_generation.md"),
) -> None:
    try:
        records, rejections = generate_qa(
            data_dir,
            config_path,
            prompt_path,
            backend=backend.value,
            endpoint=endpoint,
            target=target,
            model_choice=model_choice.value,
            run_id=run_id,
        )
    except CapacityError as exc:
        _echo({"run_id": run_id, "status": "insufficient_capacity", "report": exc.report})
        raise typer.Exit(code=2) from exc
    _echo({"accepted_generation": len(records), "generation_rejections": len(rejections)})
    if len(records) < target:
        raise typer.Exit(code=2)


@qa_app.command("validate")
def qa_validate(
    run_id: Annotated[str, typer.Option("--run-id")] = "qa-v2",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    dense_endpoint: Annotated[str | None, typer.Option("--dense-endpoint")] = None,
    dense_model: Annotated[str | None, typer.Option("--dense-model")] = None,
    dense_revision: Annotated[str | None, typer.Option("--dense-revision")] = None,
) -> None:
    accepted, rejected, stats = validate_qa(
        data_dir,
        run_id=run_id,
        dense_endpoint=dense_endpoint,
        dense_model=dense_model,
        dense_revision=dense_revision,
        dense_api_key=os.environ.get("DENSE_API_KEY"),
    )
    _echo({"accepted": len(accepted), "rejected": len(rejected), "stats": stats})


@qa_app.command("review-sample")
def qa_review_sample(
    run_id: Annotated[str, typer.Option("--run-id")] = "qa-v2",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    rate: Annotated[float, typer.Option("--rate", min=0.001, max=1.0)] = 0.15,
) -> None:
    rows = create_review_sample(data_dir, rate, run_id=run_id)
    unique = len({str(row["qa_id"]) for row in rows})
    _echo(
        {
            "run_id": run_id,
            "unique_sampled_items": unique,
            "assignment_rows": len(rows),
            "output": str(qa_run_dir(data_dir, run_id) / "review_sample.jsonl"),
        }
    )


@qa_app.command("build")
def qa_build(
    run_id: Annotated[str, typer.Option("--run-id")] = "qa-v2",
    backend: Annotated[Backend, typer.Option("--backend")] = Backend.FIXTURE,
    target: Annotated[int, typer.Option("--target", min=1)] = 1500,
    model_choice: Annotated[ModelChoice, typer.Option("--model-choice")] = ModelChoice.PRIMARY,
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:8000/v1",
    dense_endpoint: Annotated[str | None, typer.Option("--dense-endpoint")] = None,
    dense_model: Annotated[str | None, typer.Option("--dense-model")] = None,
    dense_revision: Annotated[str | None, typer.Option("--dense-revision")] = None,
    max_fill_cycles: Annotated[int, typer.Option("--max-fill-cycles", min=0)] = 8,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/generation.yaml"),
    prompt_path: Annotated[Path, typer.Option("--prompt")] = Path("prompts/qa_generation.md"),
) -> None:
    def report_progress(cycle: int, qa_count: int) -> None:
        typer.echo(f"QA build progress: cycle={cycle} qa_count={qa_count}/{target}")

    try:
        records, build_report = build_qa(
            data_dir,
            config_path,
            prompt_path,
            backend=backend.value,
            endpoint=endpoint,
            target=target,
            model_choice=model_choice.value,
            run_id=run_id,
            dense_endpoint=dense_endpoint,
            dense_model=dense_model,
            dense_revision=dense_revision,
            dense_api_key=os.environ.get("DENSE_API_KEY"),
            max_fill_cycles=max_fill_cycles,
            progress_callback=report_progress,
        )
    except CapacityError as exc:
        _echo({"run_id": run_id, "status": "insufficient_capacity", "report": exc.report})
        raise typer.Exit(code=2) from exc
    _echo({"accepted": len(records), "build": build_report})


@qa_app.command("promote")
def qa_promote(
    run_id: Annotated[str, typer.Option("--run-id")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    airline_cohort: Annotated[Path, typer.Option("--airline-cohort")] = Path(
        "configs/airline_cohort.yaml"
    ),
) -> None:
    _echo(
        promote_qa_run(
            data_dir,
            run_id,
            airline_cohort_path=airline_cohort,
        )
    )


@review_app.command("extraction-sample")
def extraction_review_sample(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    rate: Annotated[float, typer.Option("--rate", min=0.001, max=1.0)] = 0.10,
) -> None:
    rows = create_extraction_review_sample(data_dir, rate)
    _echo(
        {
            "sample": len(rows),
            "output": str(data_dir / "reports" / "extraction_review_sample.jsonl"),
        }
    )


def _reviewer_id(value: str | None) -> str:
    reviewer_id = value.strip() if value is not None else ""
    while not reviewer_id:
        reviewer_id = typer.prompt("Reviewer ID").strip()
        if not reviewer_id:
            typer.echo("Reviewer ID cannot be empty.")
    return reviewer_id


def _review_answer() -> bool | None:
    while True:
        value = (
            typer.prompt(
                "Usable? [t/true, f/false, q/quit]",
                show_default=False,
            )
            .strip()
            .casefold()
        )
        if value in {"t", "true"}:
            return True
        if value in {"f", "false"}:
            return False
        if value in {"q", "quit"}:
            return None
        typer.echo("Enter t/true, f/false, or q/quit.")


def _canonical_review_text(
    assignment: dict[str, object],
    *,
    data_dir: Path,
    index: int,
    total: int,
) -> str:
    relative_path = Path(str(assignment.get("canonical_path", "")))
    canonical_path = (data_dir / relative_path).resolve()
    try:
        canonical_path.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise typer.BadParameter(
            f"canonical_path leaves the data directory: {relative_path}"
        ) from exc
    if not canonical_path.is_file():
        raise typer.BadParameter(f"canonical document does not exist: {canonical_path}")
    metadata = [
        "=" * 80,
        f"Document {index}/{total}: {assignment.get('title', '')}",
        f"ID:       {assignment.get('document_id', '')}",
        (
            "Metadata: "
            f"{assignment.get('language', '')} · {assignment.get('format', '')} · "
            f"{assignment.get('canonical_token_count', '?')} tokens"
        ),
        f"Source:   {assignment.get('source_url', '')}",
        f"File:     {relative_path}",
        "=" * 80,
        "",
        canonical_path.read_text(encoding="utf-8"),
    ]
    return "\n".join(metadata)


@review_app.command("extraction")
def extraction_review(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    reviewer_id: Annotated[str | None, typer.Option("--reviewer-id")] = None,
    pager: Annotated[
        bool,
        typer.Option(
            "--pager/--no-pager",
            help="Show each canonical document in a terminal pager.",
        ),
    ] = True,
    restart: Annotated[
        bool,
        typer.Option(
            "--restart",
            help="Discard saved progress for the current extraction sample.",
        ),
    ] = False,
) -> None:
    """Interactively review the current extraction sample."""
    sample_path = data_dir / "reports" / "extraction_review_sample.jsonl"
    output_path = data_dir / "reports" / "extraction_reviews.jsonl"
    progress_path = data_dir / "reports" / "extraction_reviews.progress.jsonl"
    if not sample_path.is_file():
        raise typer.BadParameter(
            f"{sample_path} does not exist; run 'aviation-data review extraction-sample'"
        )
    assignments = read_jsonl(sample_path)
    if not assignments:
        raise typer.BadParameter(f"{sample_path} contains no assignments")

    current_reviewer = _reviewer_id(reviewer_id)
    assignment_ids = [str(row.get("document_id", "")) for row in assignments]
    if not all(assignment_ids) or len(set(assignment_ids)) != len(assignment_ids):
        raise typer.BadParameter("the extraction sample has missing or duplicate document IDs")

    if restart and progress_path.exists():
        progress_path.unlink()

    completed_by_id: dict[str, dict[str, object]] = {}
    if progress_path.exists():
        progress_rows = read_jsonl(progress_path)
        for row in progress_rows:
            document_id = str(row.get("document_id", ""))
            if (
                document_id not in assignment_ids
                or row.get("reviewer_id") != current_reviewer
                or not isinstance(row.get("usable"), bool)
                or document_id in completed_by_id
            ):
                raise typer.BadParameter(
                    f"{progress_path} does not match this sample and reviewer; "
                    "rerun with --restart to discard it"
                )
            completed_by_id[document_id] = row
        typer.echo(
            f"Resuming {current_reviewer}: {len(completed_by_id)}/{len(assignments)} "
            "documents already reviewed."
        )

    for index, assignment in enumerate(assignments, start=1):
        document_id = str(assignment["document_id"])
        if document_id in completed_by_id:
            continue
        review_text = _canonical_review_text(
            assignment,
            data_dir=data_dir,
            index=index,
            total=len(assignments),
        )
        if pager:
            click.echo_via_pager(review_text)
        else:
            typer.echo(review_text)
        usable = _review_answer()
        if usable is None:
            if completed_by_id:
                typer.echo(
                    f"Progress saved: {len(completed_by_id)}/{len(assignments)} reviews "
                    f"in {progress_path}"
                )
            else:
                typer.echo("No review decisions recorded; the finalized file was not changed.")
            return
        completed = {
            **assignment,
            "reviewer_id": current_reviewer,
            "usable": usable,
        }
        completed_by_id[document_id] = completed
        write_jsonl(
            progress_path,
            [completed_by_id[item_id] for item_id in assignment_ids if item_id in completed_by_id],
        )

    finalized = [completed_by_id[document_id] for document_id in assignment_ids]
    write_jsonl(output_path, finalized)
    progress_path.unlink(missing_ok=True)
    _echo(
        {
            "reviewer_id": current_reviewer,
            "reviewed": len(finalized),
            "usable": sum(bool(row["usable"]) for row in finalized),
            "not_usable": sum(not bool(row["usable"]) for row in finalized),
            "output": str(output_path),
        }
    )


@app.command("report")
def report(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    qa_run_id: Annotated[str, typer.Option("--qa-run-id")] = "benchmark",
    airline_cohort: Annotated[Path, typer.Option("--airline-cohort")] = Path(
        "configs/airline_cohort.yaml"
    ),
) -> None:
    _echo(build_report(data_dir, airline_cohort, qa_run_id=qa_run_id))


@app.command("package")
def package(
    public: Annotated[
        bool, typer.Option("--public", help="Build only the rights-filtered public package.")
    ] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    qa_run_id: Annotated[str, typer.Option("--qa-run-id")] = "benchmark",
    registry_path: Annotated[Path, typer.Option("--registry")] = DEFAULT_REGISTRY,
    output: Annotated[Path, typer.Option("--output")] = Path("release/public"),
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    if not public:
        typer.echo("Refusing ambiguous packaging. Pass --public.")
        raise typer.Exit(code=1)
    _echo(
        package_public(
            data_dir,
            registry_path,
            output,
            force=force,
            qa_run_id=qa_run_id,
        )
    )


@schemas_app.command("export")
def schemas_export(
    output: Annotated[Path, typer.Option("--output")] = Path("schemas/v1.1.0"),
) -> None:
    _echo({"schemas": [str(path) for path in export_schemas(output)]})


@evaluate_app.command("retrieval")
def evaluation_retrieval(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    backend: Annotated[RetrievalBackend, typer.Option("--backend")] = RetrievalBackend.BM25,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/evaluation.yaml"),
    dense_endpoint: Annotated[str | None, typer.Option("--dense-endpoint")] = None,
) -> None:
    _echo(
        evaluate_retrieval(
            data_dir,
            backend=backend.value,
            config_path=config_path,
            dense_endpoint=dense_endpoint,
            dense_api_key=os.environ.get("DENSE_API_KEY"),
        )
    )


@evaluate_app.command("answers")
def evaluation_answers(
    predictions: Annotated[Path, typer.Option("--predictions", exists=True)],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
) -> None:
    _echo(evaluate_answers(data_dir, predictions))


@evaluate_app.command("judge")
def evaluation_judge(
    predictions: Annotated[Path, typer.Option("--predictions", exists=True)],
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:8000/v1",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    generation_config: Annotated[Path, typer.Option("--generation-config")] = Path(
        "configs/generation.yaml"
    ),
    evaluation_config: Annotated[Path, typer.Option("--evaluation-config")] = Path(
        "configs/evaluation.yaml"
    ),
    allow_uncalibrated: Annotated[
        bool,
        typer.Option(
            "--allow-uncalibrated",
            help="Permit a diagnostic report before the human calibration sample is complete.",
        ),
    ] = False,
) -> None:
    _echo(
        evaluate_explanatory(
            data_dir,
            predictions,
            generation_config,
            evaluation_config,
            endpoint=endpoint,
            api_key=os.environ.get("VLLM_API_KEY"),
            allow_uncalibrated=allow_uncalibrated,
        )
    )


if __name__ == "__main__":
    app()
