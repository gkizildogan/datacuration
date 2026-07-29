from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, value: Any) -> None:
    ensure_parent(path)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, value: BaseModel | dict[str, Any]) -> None:
    ensure_parent(path)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_jsonl(path: Path, values: Iterable[BaseModel | dict[str, Any]]) -> int:
    ensure_parent(path)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def read_jsonl(path: Path, model: type[T] | None = None) -> list[T] | list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[Any] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                rows.append(model.model_validate(value) if model else value)
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return rows


def write_parquet_if_available(path: Path, values: Iterable[BaseModel | dict[str, Any]]) -> bool:
    rows = [
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value for value in values
    ]
    if not rows:
        return False
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    ensure_parent(path)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")
    return True


def write_qa_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    ensure_parent(path)
    fieldnames = [
        "qa_id",
        "question",
        "answer",
        "reference_answer",
        "question_language",
        "answerability",
        "primary_type",
        "split",
        "source_document_ids",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            output = {key: row.get(key) for key in fieldnames}
            output["source_document_ids"] = "|".join(row.get("source_document_ids", []))
            writer.writerow(output)
