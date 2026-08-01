from __future__ import annotations

from typer.testing import CliRunner

import aviation_data.cli as cli_module


def test_qa_build_prints_cycle_progress(monkeypatch) -> None:
    def fake_build_qa(*args, **kwargs):
        kwargs["progress_callback"](3, 7)
        return [object()] * 7, {"status": "complete"}

    monkeypatch.setattr(cli_module, "build_qa", fake_build_qa)

    result = CliRunner().invoke(
        cli_module.app,
        ["qa", "build", "--target", "10", "--max-fill-cycles", "8"],
    )

    assert result.exit_code == 0, result.output
    assert "QA build progress: cycle=3 qa_count=7/10" in result.output
