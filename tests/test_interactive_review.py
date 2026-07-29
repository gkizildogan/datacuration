from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aviation_data.cli import app
from aviation_data.io import write_jsonl


def _assignment(data_dir: Path, number: int) -> dict[str, object]:
    canonical_path = Path("extracted") / f"doc_{number}" / "canonical.md"
    full_path = data_dir / canonical_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(
        f"# Document {number}\n\nA coherent aviation fact for review {number}.\n",
        encoding="utf-8",
    )
    return {
        "document_id": f"doc_{number}",
        "title": f"Document {number}",
        "review_scope": "accepted_corpus",
        "canonical_path": canonical_path.as_posix(),
        "source_url": f"https://example.test/document/{number}",
        "reviewer_id": "",
        "usable": None,
        "format": "html",
        "language": "en",
        "topic": ["aircraft"],
        "canonical_token_count": 8,
        "quality_flags": [],
        "notes": "",
    }


def test_interactive_extraction_review_accepts_short_and_full_booleans(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    assignments = [_assignment(data_dir, number) for number in range(1, 5)]
    sample_path = data_dir / "reports" / "extraction_review_sample.jsonl"
    write_jsonl(sample_path, assignments)

    result = CliRunner().invoke(
        app,
        ["review", "extraction", "--data-dir", str(data_dir), "--no-pager"],
        input="reviewer-01\nt\ntrue\nf\nfalse\n",
    )

    assert result.exit_code == 0, result.output
    reviews = [
        json.loads(line)
        for line in (data_dir / "reports" / "extraction_reviews.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["usable"] for row in reviews] == [True, True, False, False]
    assert {row["reviewer_id"] for row in reviews} == {"reviewer-01"}
    assert not (data_dir / "reports" / "extraction_reviews.progress.jsonl").exists()

    unchanged_assignments = [
        json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(row["usable"] is None for row in unchanged_assignments)


def test_interactive_extraction_review_resumes_saved_progress(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assignments = [_assignment(data_dir, number) for number in range(1, 3)]
    write_jsonl(
        data_dir / "reports" / "extraction_review_sample.jsonl",
        assignments,
    )
    runner = CliRunner()

    interrupted = runner.invoke(
        app,
        [
            "review",
            "extraction",
            "--data-dir",
            str(data_dir),
            "--reviewer-id",
            "reviewer-02",
            "--no-pager",
        ],
        input="t\nq\n",
    )
    assert interrupted.exit_code == 0, interrupted.output
    assert not (data_dir / "reports" / "extraction_reviews.jsonl").exists()
    progress_path = data_dir / "reports" / "extraction_reviews.progress.jsonl"
    assert progress_path.exists()

    resumed = runner.invoke(
        app,
        [
            "review",
            "extraction",
            "--data-dir",
            str(data_dir),
            "--reviewer-id",
            "reviewer-02",
            "--no-pager",
        ],
        input="f\n",
    )
    assert resumed.exit_code == 0, resumed.output
    assert "Resuming reviewer-02: 1/2" in resumed.output
    reviews = [
        json.loads(line)
        for line in (data_dir / "reports" / "extraction_reviews.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["usable"] for row in reviews] == [True, False]
    assert not progress_path.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("maybe\nq\n", "Enter t/true, f/false, or q/quit.")],
)
def test_interactive_extraction_review_reprompts_invalid_input(
    tmp_path: Path,
    value: str,
    expected: str,
) -> None:
    data_dir = tmp_path / "data"
    write_jsonl(
        data_dir / "reports" / "extraction_review_sample.jsonl",
        [_assignment(data_dir, 1)],
    )

    result = CliRunner().invoke(
        app,
        [
            "review",
            "extraction",
            "--data-dir",
            str(data_dir),
            "--reviewer-id",
            "reviewer-03",
            "--no-pager",
        ],
        input=value,
    )

    assert result.exit_code == 0, result.output
    assert expected in result.output
