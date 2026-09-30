from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from aviation_data.io import read_jsonl, write_json, write_jsonl
from aviation_data.qa_generation import _generator_config, _vllm_preflight
from aviation_data.qa_planning import qa_run_dir

REVIEWER_SLOTS = {
    "A": "primary",
    "B": "fallback",
}


class LLMReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clarity: bool
    correctness: bool
    evidence_sufficiency: bool
    language_quality: bool
    notes: str = ""


def _response_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "qa_review",
            "strict": True,
            "schema": LLMReviewResponse.model_json_schema(),
        },
    }


def _fixture_review(row: dict[str, Any]) -> LLMReviewResponse:
    question = str(row.get("question", ""))
    evidence = row.get("evidence") or []
    return LLMReviewResponse(
        clarity=bool(question.strip()),
        correctness=True,
        evidence_sufficiency=bool(evidence),
        language_quality=bool(question.strip()),
        notes="" if evidence else "no evidence quote attached",
    )


def _vllm_review(
    client: httpx.Client,
    endpoint: str,
    row: dict[str, Any],
    generator_model_id: str,
    prompt: str,
    temperature: float,
    seed: int,
    max_output_tokens: int,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> LLMReviewResponse:
    question_language = str((row.get("stratum") or [""])[0])
    payload = {
        "question_language": question_language,
        "question": row.get("question"),
        "answer": row.get("answer"),
        "answer_items": row.get("answer_items"),
        "evidence": row.get("evidence"),
        "section_paths": row.get("section_paths") or [],
    }
    response = client.post(
        f"{endpoint.rstrip('/')}/chat/completions",
        json={
            "model": generator_model_id,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_output_tokens,
            "chat_template_kwargs": chat_template_kwargs or {"enable_thinking": False},
            "response_format": _response_schema(),
        },
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    return LLMReviewResponse.model_validate_json(content)


def run_llm_review(
    data_dir: Path,
    run_id: str,
    *,
    reviewer_slot: Literal["A", "B"],
    backend: Literal["fixture", "vllm"],
    endpoint: str = "http://127.0.0.1:8000/v1",
    config_path: Path = Path("configs/generation.yaml"),
    prompt_path: Path = Path("prompts/qa_review.md"),
    max_output_tokens: int = 512,
) -> list[dict[str, Any]]:
    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("reviewer_slot must be 'A' or 'B'")
    model_choice = REVIEWER_SLOTS[reviewer_slot]
    run_dir = qa_run_dir(data_dir, run_id)
    sample_path = run_dir / "review_sample.jsonl"
    if not sample_path.is_file():
        raise FileNotFoundError(
            f"{sample_path} does not exist; run 'aviation-data qa review-sample "
            f"--run-id {run_id}' before llm-review"
        )
    sample = read_jsonl(sample_path)
    rows = [row for row in sample if str(row.get("reviewer_slot")) == reviewer_slot]
    if not rows:
        raise ValueError(f"review_sample.jsonl has no rows for reviewer slot {reviewer_slot}")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8")
    generator = _generator_config(config, prompt, backend, model_choice)
    reviewer_id = f"llm:{model_choice}:{generator.model_id}@{generator.model_revision}"

    client: httpx.Client | None = None
    if backend == "vllm":
        headers = {}
        api_key = os.environ.get("VLLM_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = float(config["generation"]["timeout_seconds"])
        client = httpx.Client(headers=headers, timeout=timeout)

    reviewed: list[dict[str, Any]] = []
    try:
        if client is not None:
            _vllm_preflight(client, endpoint, generator.model_id)
        for row in rows:
            if backend == "fixture":
                result = _fixture_review(row)
            else:
                assert client is not None
                result = _vllm_review(
                    client,
                    endpoint,
                    row,
                    generator.model_id,
                    prompt,
                    generator.temperature,
                    generator.seed,
                    max_output_tokens,
                    chat_template_kwargs=generator.settings.get("chat_template_kwargs"),
                )
            reviewed.append(
                {
                    "qa_id": row["qa_id"],
                    "reviewer_slot": reviewer_slot,
                    "reviewer_id": reviewer_id,
                    "clarity": result.clarity,
                    "correctness": result.correctness,
                    "evidence_sufficiency": result.evidence_sufficiency,
                    "language_quality": result.language_quality,
                    "notes": result.notes,
                }
            )
    finally:
        if client is not None:
            client.close()

    reviewed.sort(key=lambda item: str(item["qa_id"]))
    write_jsonl(run_dir / f"llm_review_{reviewer_slot}.jsonl", reviewed)
    merged = _merge_if_ready(run_dir)
    return merged if merged is not None else reviewed


def _merge_if_ready(run_dir: Path) -> list[dict[str, Any]] | None:
    partials = {slot: run_dir / f"llm_review_{slot}.jsonl" for slot in REVIEWER_SLOTS}
    if not all(path.is_file() for path in partials.values()):
        return None
    merged = [
        *read_jsonl(partials["A"]),
        *read_jsonl(partials["B"]),
    ]
    merged.sort(key=lambda item: (str(item["qa_id"]), str(item["reviewer_slot"])))
    write_jsonl(run_dir / "human_reviews.jsonl", merged)
    write_json(
        run_dir / "llm_review_manifest.json",
        {"reviewer_A": "primary", "reviewer_B": "fallback", "rows": len(merged)},
    )
    return merged
