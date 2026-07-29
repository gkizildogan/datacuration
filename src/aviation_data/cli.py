from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from aviation_data.acquisition import fetch_sources
from aviation_data.curation import curate_documents
from aviation_data.evaluation import (
    evaluate_answers,
    evaluate_explanatory,
    evaluate_retrieval,
)
from aviation_data.extraction import extract_sources
from aviation_data.passages import build_passages
from aviation_data.qa_generation import generate_qa
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
review_app = typer.Typer(help="Create stratified manual-review assignments.")
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
            help="Use benchmark for pipeline output or a distinct ID for experiments.",
        ),
    ] = "benchmark",
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:8000/v1",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/generation.yaml"),
    prompt_path: Annotated[Path, typer.Option("--prompt")] = Path("prompts/qa_generation.md"),
) -> None:
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
    _echo({"accepted_generation": len(records), "generation_rejections": len(rejections)})
    if len(records) < target:
        raise typer.Exit(code=2)


@qa_app.command("validate")
def qa_validate(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    dense_endpoint: Annotated[str | None, typer.Option("--dense-endpoint")] = None,
    dense_model: Annotated[str | None, typer.Option("--dense-model")] = None,
    dense_revision: Annotated[str | None, typer.Option("--dense-revision")] = None,
) -> None:
    accepted, rejected, stats = validate_qa(
        data_dir,
        dense_endpoint=dense_endpoint,
        dense_model=dense_model,
        dense_revision=dense_revision,
        dense_api_key=os.environ.get("DENSE_API_KEY"),
    )
    _echo({"accepted": len(accepted), "rejected": len(rejected), "stats": stats})


@qa_app.command("review-sample")
def qa_review_sample(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    rate: Annotated[float, typer.Option("--rate", min=0.001, max=1.0)] = 0.15,
) -> None:
    rows = create_review_sample(data_dir, rate)
    _echo({"sample": len(rows), "output": str(data_dir / "qa" / "review_sample.jsonl")})


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


@app.command("report")
def report(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    airline_cohort: Annotated[Path, typer.Option("--airline-cohort")] = Path(
        "configs/airline_cohort.yaml"
    ),
) -> None:
    _echo(build_report(data_dir, airline_cohort))


@app.command("package")
def package(
    public: Annotated[
        bool, typer.Option("--public", help="Build only the rights-filtered public package.")
    ] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA,
    registry_path: Annotated[Path, typer.Option("--registry")] = DEFAULT_REGISTRY,
    output: Annotated[Path, typer.Option("--output")] = Path("release/public"),
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    if not public:
        typer.echo("Refusing ambiguous packaging. Pass --public.")
        raise typer.Exit(code=1)
    _echo(package_public(data_dir, registry_path, output, force=force))


@schemas_app.command("export")
def schemas_export(
    output: Annotated[Path, typer.Option("--output")] = Path("schemas/v1.0.0"),
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
