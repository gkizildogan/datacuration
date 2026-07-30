from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aviation_data.ids import sha256_text, stable_id, tokens
from aviation_data.io import read_json, read_jsonl, write_json, write_jsonl
from aviation_data.models import (
    Answerability,
    DocumentRecord,
    Language,
    PassageRecord,
    QAType,
    RightsState,
)

PLANNER_VERSION = "qa-quota-planner-v2"
QA_TARGET = 1_500
PASSAGE_REUSE_LIMIT = 4
ANCHOR_TOKEN_LIMIT = 160
MIN_ANCHOR_TOKENS = 8
TYPE_WEIGHTS: tuple[tuple[QAType, float], ...] = (
    (QAType.FACTUAL, 0.40),
    (QAType.DEFINITION, 0.30),
    (QAType.LIST_TABLE, 0.15),
    (QAType.COMPARISON, 0.10),
    (QAType.TEMPORAL, 0.05),
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
YEAR_RE = re.compile(r"(?<!\w)(?:1[89]\d{2}|20\d{2}|2100)(?!\w)")
NUMBER_RE = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?(?:\s*%|\s*[A-Za-z²³]+)?(?!\w)")
ENTITY_RE = re.compile(
    r"(?<!\w)(?:[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü'’.-]*"
    r"(?:\s+[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü'’.-]*)+|[A-ZÇĞİÖŞÜ]{2,}\d*)"
)


class CapacityError(ValueError):
    def __init__(self, report: dict[str, Any]):
        super().__init__("QA candidate capacity is insufficient; see capacity_report.json")
        self.report = report


class MutationTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["entity", "date", "number"]
    value: str = Field(min_length=1)


class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str
    passage_id: str
    document_id: str
    document_version: str
    variant_group_id: str
    language: Language
    compatible_types: list[QAType]
    passage_char_start: int = Field(ge=0)
    passage_char_end: int = Field(gt=0)
    canonical_char_start: int = Field(ge=0)
    canonical_char_end: int = Field(gt=0)
    anchor_text: str = Field(min_length=1)
    token_count: int = Field(ge=MIN_ANCHOR_TOKENS, le=ANCHOR_TOKEN_LIMIT)
    list_items: list[str] = Field(default_factory=list)
    mutation_terms: list[MutationTerm] = Field(default_factory=list)


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    task_id: str
    kind: Literal["model", "mutation"] = "model"
    question_language: Language
    qa_type: QAType
    answerability: Answerability
    cross_lingual: bool = False
    anchor_id: str
    parent_task_id: str | None = None
    mutation_kind: Literal["entity", "date", "number"] | None = None
    mutation_source: str | None = None
    mutation_replacement: str | None = None
    donor_document_id: str | None = None


def qa_run_dir(data_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id contains unsupported characters")
    if run_id == "benchmark":
        return data_dir / "qa"
    return data_dir / "qa" / "experiments" / run_id


def require_experiment_run(run_id: str) -> None:
    if run_id == "benchmark":
        raise ValueError(
            "benchmark is a read-only legacy path; use a distinct --run-id and qa promote"
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    rows = []
    for match in re.finditer(r".*(?:\n|$)", text):
        raw = match.group(0)
        if not raw:
            continue
        value = raw[:-1] if raw.endswith("\n") else raw
        rows.append((match.start(), match.start() + len(value), value))
    return rows


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _limit_span(text: str, start: int, end: int) -> tuple[int, int]:
    anchor = text[start:end]
    matches = list(re.finditer(r"\w+(?:['’.-]\w+)*", anchor, re.UNICODE))
    if len(matches) <= ANCHOR_TOKEN_LIMIT:
        return start, end
    limited_end = start + matches[ANCHOR_TOKEN_LIMIT - 1].end()
    return start, limited_end


def _is_separator(value: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s|:-]+\|?", value.strip()))


def _table_items(lines: list[str]) -> list[str]:
    usable = []
    for index, line in enumerate(lines):
        if _is_separator(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        cells = [cell for cell in cells if cell and not _is_separator(cell)]
        if index == 0 and len(lines) > 1 and _is_separator(lines[1]):
            continue
        if cells:
            usable.append(" | ".join(cells))
    return usable


def _list_items(lines: list[str]) -> list[str]:
    return [
        match.group(1).strip()
        for line in lines
        if (match := re.match(r"\s*(?:[-*+]|\d+[.)])\s+(.+)", line))
        and len(tokens(match.group(1))) >= 1
    ]


def _mutation_terms(value: str) -> list[MutationTerm]:
    found: list[MutationTerm] = []
    occupied: set[tuple[int, int]] = set()
    for kind, pattern in (("date", YEAR_RE), ("entity", ENTITY_RE), ("number", NUMBER_RE)):
        for match in pattern.finditer(value):
            span = match.span()
            if any(not (span[1] <= left or span[0] >= right) for left, right in occupied):
                continue
            candidate = match.group(0).strip()
            if kind == "entity" and len(tokens(candidate)) > 8:
                continue
            found.append(MutationTerm(kind=kind, value=candidate))
            occupied.add(span)
    return found


def _prose_types(value: str) -> list[QAType]:
    if value.rstrip().endswith(":") or value.rstrip().endswith("?"):
        return []
    result = [QAType.FACTUAL]
    lowered = value.casefold()
    if re.search(
        r"\b(?:is|are|was|were|means|refers to|defined as|consists of|"
        r"olarak|tanımlanır|tanımlanmış|ifade eder|oluşur)\b",
        lowered,
    ) or re.search(r"\b[\wçğıöşü]+(?:dır|dir|dur|dür|tır|tir|tur|tür)\b", lowered):
        result.append(QAType.DEFINITION)
    if YEAR_RE.search(value) or re.search(
        r"(?<!\w)\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?(?!\w)|"
        r"\b\d{1,2}:\d{2}\b",
        value,
    ):
        result.append(QAType.TEMPORAL)
    if re.search(
        r"\b(?:compared|whereas|while|than|both|respectively|versus|"
        r"karşılaştır|kıyas|oysa|iken|sahipken|göre|daha|hem|sırasıyla|oranla)\b",
        lowered,
    ):
        result.append(QAType.COMPARISON)
    return result


def _candidate(
    passage: PassageRecord,
    start: int,
    end: int,
    compatible_types: list[QAType],
    *,
    list_items: list[str] | None = None,
) -> EvidenceCandidate | None:
    start, end = _trim_span(passage.text, start, end)
    start, end = _limit_span(passage.text, start, end)
    anchor = passage.text[start:end]
    token_count = len(tokens(anchor))
    extractive_list_items = [item for item in (list_items or []) if item in anchor]
    if (
        token_count < MIN_ANCHOR_TOKENS
        or not compatible_types
        or anchor.lstrip().startswith("#")
        or _is_separator(anchor)
        or anchor.rstrip().endswith(":")
        or (QAType.LIST_TABLE in compatible_types and len(extractive_list_items) < 2)
    ):
        return None
    anchor_id = stable_id(
        "anchor",
        passage.passage_id,
        start,
        end,
        sha256_text(anchor),
        length=32,
    )
    return EvidenceCandidate(
        anchor_id=anchor_id,
        passage_id=passage.passage_id,
        document_id=passage.document_id,
        document_version=passage.document_version,
        variant_group_id=passage.variant_group_id,
        language=passage.language,
        compatible_types=list(dict.fromkeys(compatible_types)),
        passage_char_start=start,
        passage_char_end=end,
        canonical_char_start=passage.canonical_char_start + start,
        canonical_char_end=passage.canonical_char_start + end,
        anchor_text=anchor,
        token_count=token_count,
        list_items=extractive_list_items,
        mutation_terms=_mutation_terms(anchor),
    )


def build_evidence_candidates(passages: list[PassageRecord]) -> list[EvidenceCandidate]:
    output: list[EvidenceCandidate] = []
    seen: set[str] = set()
    for passage in sorted(passages, key=lambda item: item.passage_id):
        lines = _line_spans(passage.text)
        consumed: list[tuple[int, int]] = []
        index = 0
        while index < len(lines):
            _, _, line = lines[index]
            is_table = "|" in line and not line.lstrip().startswith("#")
            is_list = bool(re.match(r"\s*(?:[-*+]|\d+[.)])\s+", line))
            if not (is_table or is_list):
                index += 1
                continue
            group = [lines[index]]
            cursor = index + 1
            while cursor < len(lines):
                next_line = lines[cursor][2]
                if (is_table and "|" in next_line and not next_line.lstrip().startswith("#")) or (
                    is_list and re.match(r"\s*(?:[-*+]|\d+[.)])\s+", next_line)
                ):
                    group.append(lines[cursor])
                else:
                    break
                cursor += 1
            raw_lines = [row[2] for row in group]
            items = _table_items(raw_lines) if is_table else _list_items(raw_lines)
            if len(items) >= 2:
                candidate = _candidate(
                    passage,
                    group[0][0],
                    group[-1][1],
                    [QAType.LIST_TABLE],
                    list_items=items,
                )
                if candidate and candidate.anchor_id not in seen:
                    output.append(candidate)
                    seen.add(candidate.anchor_id)
                    consumed.append((group[0][0], group[-1][1]))
                for row_start, row_end, row_text in group:
                    if _is_separator(row_text):
                        continue
                    row_types = _prose_types(row_text)
                    row_candidate = _candidate(
                        passage,
                        row_start,
                        row_end,
                        row_types,
                    )
                    if row_candidate and row_candidate.anchor_id not in seen:
                        output.append(row_candidate)
                        seen.add(row_candidate.anchor_id)
            index = cursor

        paragraph_pattern = re.compile(r"(?:^|\n\s*\n)([^\n].*?)(?=\n\s*\n|\Z)", re.DOTALL)
        for paragraph_match in paragraph_pattern.finditer(passage.text):
            start, end = paragraph_match.span(1)
            if any(start >= left and end <= right for left, right in consumed):
                continue
            value = passage.text[start:end].strip()
            if not value or value.lstrip().startswith("#"):
                continue
            parts = [(start, end)]
            if len(tokens(value)) > ANCHOR_TOKEN_LIMIT:
                parts = [
                    (start + match.start(), start + match.end())
                    for match in re.finditer(
                        r"[^.!?\n]{20,}[.!?](?=\s|$)|[^\n]{20,}",
                        passage.text[start:end],
                    )
                ]
            for part_start, part_end in parts:
                candidate = _candidate(
                    passage,
                    part_start,
                    part_end,
                    _prose_types(passage.text[part_start:part_end]),
                )
                if candidate and candidate.anchor_id not in seen:
                    output.append(candidate)
                    seen.add(candidate.anchor_id)
    return sorted(output, key=lambda item: item.anchor_id)


def eligible_passages(data_dir: Path) -> tuple[list[PassageRecord], dict[str, DocumentRecord]]:
    documents = {
        document.document_id: document
        for document in read_jsonl(
            data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
        )
    }
    passages = [
        passage
        for passage in read_jsonl(data_dir / "passages" / "passages.jsonl", PassageRecord)
        if (document := documents.get(passage.document_id)) is not None
        and document.rights_state == RightsState.OPEN
        and document.release_derived_text
        and document.release_qa
        and passage.language in {Language.ENGLISH, Language.TURKISH}
    ]
    if not passages:
        raise ValueError("no releasable passages available; run 'passages build' first")
    return passages, documents


def _largest_remainder(total: int, weights: list[tuple[str, float]]) -> dict[str, int]:
    raw = [(key, total * weight) for key, weight in weights]
    result = {key: math.floor(value) for key, value in raw}
    remaining = total - sum(result.values())
    order = sorted(
        enumerate(raw),
        key=lambda item: (-(item[1][1] - math.floor(item[1][1])), item[0]),
    )
    for _, (key, _) in order[:remaining]:
        result[key] += 1
    return result


def quota_plan(target: int) -> dict[str, Any]:
    if target < 1:
        raise ValueError("target must be positive")
    language = _largest_remainder(
        target,
        [(Language.ENGLISH.value, 0.5), (Language.TURKISH.value, 0.5)],
    )
    answerability = _largest_remainder(
        target,
        [
            (Answerability.ANSWERABLE.value, 0.9),
            (Answerability.CORPUS_UNANSWERABLE.value, 0.1),
        ],
    )
    unanswerable_by_language = _largest_remainder(
        answerability[Answerability.CORPUS_UNANSWERABLE.value],
        [
            (Language.ENGLISH.value, 0.5),
            (Language.TURKISH.value, 0.5),
        ],
    )
    answerable_by_language = {
        language_code: language[language_code] - unanswerable_by_language[language_code]
        for language_code in language
    }
    type_totals = _largest_remainder(
        answerability[Answerability.ANSWERABLE.value],
        [(qa_type.value, weight) for qa_type, weight in TYPE_WEIGHTS],
    )
    english_raw = {
        qa_type.value: type_totals[qa_type.value]
        * answerable_by_language[Language.ENGLISH.value]
        / max(1, answerability[Answerability.ANSWERABLE.value])
        for qa_type, _ in TYPE_WEIGHTS
    }
    english_by_type = {key: math.floor(value) for key, value in english_raw.items()}
    remaining_english = answerable_by_language[Language.ENGLISH.value] - sum(
        english_by_type.values()
    )
    type_order = {qa_type.value: index for index, (qa_type, _) in enumerate(TYPE_WEIGHTS)}
    for key in sorted(
        english_raw,
        key=lambda item: (-(english_raw[item] - math.floor(english_raw[item])), type_order[item]),
    )[:remaining_english]:
        english_by_type[key] += 1
    by_language_type: dict[str, dict[str, int]] = {
        Language.ENGLISH.value: english_by_type,
        Language.TURKISH.value: {
            key: type_totals[key] - english_by_type[key] for key in type_totals
        },
    }
    cross_total = math.ceil(answerability[Answerability.ANSWERABLE.value] * 0.10)
    cross_by_language = _largest_remainder(
        cross_total,
        [
            (
                language_code,
                count / max(1, answerability[Answerability.ANSWERABLE.value]),
            )
            for language_code, count in answerable_by_language.items()
        ],
    )
    answerable_strata = []
    for language_code in (Language.ENGLISH.value, Language.TURKISH.value):
        cells = by_language_type[language_code]
        cross_cells = _largest_remainder(
            cross_by_language[language_code],
            [
                (qa_type.value, cells[qa_type.value] / max(1, sum(cells.values())))
                for qa_type, _ in TYPE_WEIGHTS
            ],
        )
        for qa_type, _ in TYPE_WEIGHTS:
            total_cell = cells[qa_type.value]
            cross_cell = min(total_cell, cross_cells[qa_type.value])
            if total_cell - cross_cell:
                answerable_strata.append(
                    {
                        "question_language": language_code,
                        "primary_type": qa_type.value,
                        "cross_lingual": False,
                        "count": total_cell - cross_cell,
                    }
                )
            if cross_cell:
                answerable_strata.append(
                    {
                        "question_language": language_code,
                        "primary_type": qa_type.value,
                        "cross_lingual": True,
                        "count": cross_cell,
                    }
                )
    return {
        "target": target,
        "question_language": language,
        "answerability": answerability,
        "unanswerable_by_language": unanswerable_by_language,
        "answerable_by_language": answerable_by_language,
        "answerable_types": type_totals,
        "answerable_cross_lingual": cross_total,
        "answerable_strata": answerable_strata,
    }


def _capacity_report(
    candidates: list[EvidenceCandidate],
    quotas: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: Counter[tuple[str, str]] = Counter()
    passages: dict[tuple[str, str], set[str]] = defaultdict(set)
    for candidate in candidates:
        for qa_type in candidate.compatible_types:
            key = (candidate.language.value, qa_type.value)
            counts[key] += 1
            passages[key].add(candidate.passage_id)
    requested: Counter[tuple[str, str]] = Counter()
    for stratum in quotas["answerable_strata"]:
        question_language = Language(stratum["question_language"])
        evidence_language = (
            Language.TURKISH
            if stratum["cross_lingual"] and question_language == Language.ENGLISH
            else (
                Language.ENGLISH
                if stratum["cross_lingual"] and question_language == Language.TURKISH
                else question_language
            )
        )
        requested[(evidence_language.value, stratum["primary_type"])] += stratum["count"]
    return {
        "planner_version": PLANNER_VERSION,
        "candidate_count": len(candidates),
        "unique_passages": len({candidate.passage_id for candidate in candidates}),
        "requested_by_evidence_language_type": {
            f"{language}:{qa_type}": requested[(language, qa_type)]
            for language, qa_type in sorted(requested)
        },
        "available_candidates_by_language_type": {
            f"{language}:{qa_type}": counts[(language, qa_type)]
            for language, qa_type in sorted(counts)
        },
        "available_unique_passages_by_language_type": {
            f"{language}:{qa_type}": len(passages[(language, qa_type)])
            for language, qa_type in sorted(passages)
        },
        "passage_reuse_limit": PASSAGE_REUSE_LIMIT,
        "issues": issues,
    }


def _candidate_order(
    candidate: EvidenceCandidate,
    *,
    seed: int,
    question_language: Language,
    qa_type: QAType,
    cross_lingual: bool,
) -> str:
    return sha256_text(
        f"{PLANNER_VERSION}:{seed}:{question_language.value}:{qa_type.value}:"
        f"{int(cross_lingual)}:{candidate.anchor_id}"
    )


def plan_tasks(
    candidates: list[EvidenceCandidate],
    target: int,
    seed: int,
) -> tuple[list[PlannedTask], dict[str, Any], dict[str, Any]]:
    quotas = quota_plan(target)
    passage_uses: Counter[str] = Counter()
    document_uses: Counter[str] = Counter()
    passage_types: set[tuple[str, QAType]] = set()
    selected_anchors: set[str] = set()
    tasks: list[PlannedTask] = []
    issues: list[dict[str, Any]] = []

    for stratum in quotas["answerable_strata"]:
        question_language = Language(stratum["question_language"])
        qa_type = QAType(stratum["primary_type"])
        cross_lingual = bool(stratum["cross_lingual"])
        evidence_language = (
            Language.TURKISH
            if cross_lingual and question_language == Language.ENGLISH
            else (
                Language.ENGLISH
                if cross_lingual and question_language == Language.TURKISH
                else question_language
            )
        )
        for _ in range(int(stratum["count"])):
            eligible = [
                candidate
                for candidate in candidates
                if candidate.language == evidence_language
                and qa_type in candidate.compatible_types
                and candidate.anchor_id not in selected_anchors
                and passage_uses[candidate.passage_id] < PASSAGE_REUSE_LIMIT
                and (candidate.passage_id, qa_type) not in passage_types
            ]
            if not eligible:
                issues.append(
                    {
                        "code": "insufficient_stratum_capacity",
                        "question_language": question_language.value,
                        "evidence_language": evidence_language.value,
                        "primary_type": qa_type.value,
                        "cross_lingual": cross_lingual,
                        "scheduled": sum(
                            task.question_language == question_language
                            and task.qa_type == qa_type
                            and task.cross_lingual == cross_lingual
                            for task in tasks
                        ),
                        "requested": stratum["count"],
                    }
                )
                break
            candidate = min(
                eligible,
                key=lambda item: (
                    document_uses[item.document_id],
                    passage_uses[item.passage_id],
                    _candidate_order(
                        item,
                        seed=seed,
                        question_language=question_language,
                        qa_type=qa_type,
                        cross_lingual=cross_lingual,
                    ),
                ),
            )
            index = len(tasks)
            task_id = stable_id(
                "task",
                PLANNER_VERSION,
                seed,
                index,
                question_language.value,
                qa_type.value,
                cross_lingual,
                candidate.anchor_id,
                length=32,
            )
            tasks.append(
                PlannedTask(
                    index=index,
                    task_id=task_id,
                    question_language=question_language,
                    qa_type=qa_type,
                    answerability=Answerability.ANSWERABLE,
                    cross_lingual=cross_lingual,
                    anchor_id=candidate.anchor_id,
                )
            )
            selected_anchors.add(candidate.anchor_id)
            passage_uses[candidate.passage_id] += 1
            document_uses[candidate.document_id] += 1
            passage_types.add((candidate.passage_id, qa_type))

    candidate_by_id = {candidate.anchor_id: candidate for candidate in candidates}
    model_tasks_by_language = {
        language: [task for task in tasks if task.question_language == language]
        for language in (Language.ENGLISH, Language.TURKISH)
    }
    for language in (Language.ENGLISH, Language.TURKISH):
        needed = quotas["unanswerable_by_language"][language.value]
        parent_order = sorted(
            model_tasks_by_language[language],
            key=lambda task: sha256_text(f"mutation-parent:{seed}:{task.task_id}"),
        )
        made = 0
        for parent in parent_order:
            if made >= needed:
                break
            parent_candidate = candidate_by_id[parent.anchor_id]
            donor_choice: tuple[MutationTerm, MutationTerm, EvidenceCandidate] | None = None
            for source_term in parent_candidate.mutation_terms:
                donors = [
                    (donor_term, donor)
                    for donor in candidates
                    if donor.document_id != parent_candidate.document_id
                    for donor_term in donor.mutation_terms
                    if donor_term.kind == source_term.kind
                    and donor_term.value.casefold() != source_term.value.casefold()
                    and donor_term.value.casefold() not in parent_candidate.anchor_text.casefold()
                ]
                if donors:
                    donor_term, donor = min(
                        donors,
                        key=lambda item: sha256_text(
                            f"mutation-donor:{seed}:{parent.task_id}:"
                            f"{source_term.value}:{item[0].value}:{item[1].anchor_id}"
                        ),
                    )
                    donor_choice = (source_term, donor_term, donor)
                    break
            if donor_choice is None:
                continue
            source_term, donor_term, donor = donor_choice
            index = len(tasks)
            task_id = stable_id(
                "task",
                PLANNER_VERSION,
                seed,
                "mutation",
                index,
                parent.task_id,
                source_term.value,
                donor_term.value,
                length=32,
            )
            tasks.append(
                PlannedTask(
                    index=index,
                    task_id=task_id,
                    kind="mutation",
                    question_language=language,
                    qa_type=parent.qa_type,
                    answerability=Answerability.CORPUS_UNANSWERABLE,
                    anchor_id=parent.anchor_id,
                    parent_task_id=parent.task_id,
                    mutation_kind=source_term.kind,
                    mutation_source=source_term.value,
                    mutation_replacement=donor_term.value,
                    donor_document_id=donor.document_id,
                )
            )
            made += 1
        if made < needed:
            issues.append(
                {
                    "code": "insufficient_mutation_capacity",
                    "question_language": language.value,
                    "requested": needed,
                    "scheduled": made,
                }
            )

    # Designated parent questions must contain the exact mutation source.
    mutation_by_parent = {task.parent_task_id: task for task in tasks if task.kind == "mutation"}
    tasks = [
        task.model_copy(
            update={
                "mutation_kind": mutation_by_parent[task.task_id].mutation_kind,
                "mutation_source": mutation_by_parent[task.task_id].mutation_source,
            }
        )
        if task.task_id in mutation_by_parent
        else task
        for task in tasks
    ]
    if len(tasks) != target:
        issues.append(
            {
                "code": "task_total_mismatch",
                "requested": target,
                "scheduled": len(tasks),
            }
        )
    report = _capacity_report(candidates, quotas, issues)
    if issues:
        raise CapacityError(report)
    return tasks, quotas, report


def prepare_run(
    data_dir: Path,
    run_id: str,
    *,
    target: int,
    seed: int,
    generation_config_path: Path,
    prompt_path: Path,
    backend: str,
    model_choice: str,
    generator_manifest: dict[str, Any],
) -> tuple[Path, list[PassageRecord], list[EvidenceCandidate], list[PlannedTask]]:
    require_experiment_run(run_id)
    run_dir = qa_run_dir(data_dir, run_id)
    if (run_dir / "READ_ONLY.json").exists():
        raise ValueError(f"run {run_id} is archived read-only and cannot be resumed")
    run_dir.mkdir(parents=True, exist_ok=True)
    source_passages_path = data_dir / "passages" / "passages.jsonl"
    source_documents_path = data_dir / "curated" / "accepted_documents.jsonl"
    current_inputs = {
        "generation_config_sha256": file_sha256(generation_config_path),
        "prompt_sha256": file_sha256(prompt_path),
        "source_passages_sha256": file_sha256(source_passages_path),
        "source_documents_sha256": file_sha256(source_documents_path),
    }
    manifest_path = run_dir / "run_manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        mismatches = {
            key: {"recorded": manifest["input_hashes"].get(key), "current": value}
            for key, value in current_inputs.items()
            if manifest["input_hashes"].get(key) != value
        }
        for name in ("passage_snapshot.jsonl", "evidence_candidates.jsonl", "task_manifest.jsonl"):
            path = run_dir / name
            recorded = manifest["artifact_hashes"].get(name)
            current = file_sha256(path) if path.exists() else None
            if recorded != current:
                mismatches[name] = {"recorded": recorded, "current": current}
        expected = {
            "target": target,
            "seed": seed,
            "backend": backend,
            "model_choice": model_choice,
            "planner_version": PLANNER_VERSION,
            "generator": generator_manifest,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                mismatches[key] = {"recorded": manifest.get(key), "current": value}
        if mismatches:
            raise ValueError(f"refusing to resume run {run_id}; hash/config mismatch: {mismatches}")
        passages = read_jsonl(run_dir / "passage_snapshot.jsonl", PassageRecord)
        candidates = read_jsonl(run_dir / "evidence_candidates.jsonl", EvidenceCandidate)
        tasks = read_jsonl(run_dir / "task_manifest.jsonl", PlannedTask)
        return run_dir, passages, candidates, tasks

    passages, _ = eligible_passages(data_dir)
    candidates = build_evidence_candidates(passages)
    write_jsonl(run_dir / "passage_snapshot.jsonl", passages)
    write_jsonl(run_dir / "evidence_candidates.jsonl", candidates)
    try:
        tasks, quotas, capacity = plan_tasks(candidates, target, seed)
    except CapacityError as exc:
        write_json(run_dir / "capacity_report.json", exc.report)
        raise
    write_jsonl(run_dir / "task_manifest.jsonl", tasks)
    write_json(run_dir / "capacity_report.json", capacity)
    artifact_hashes = {
        name: file_sha256(run_dir / name)
        for name in (
            "passage_snapshot.jsonl",
            "evidence_candidates.jsonl",
            "task_manifest.jsonl",
        )
    }
    write_json(
        manifest_path,
        {
            "schema_version": "1.1.0",
            "run_id": run_id,
            "target": target,
            "seed": seed,
            "backend": backend,
            "model_choice": model_choice,
            "planner_version": PLANNER_VERSION,
            "generator": generator_manifest,
            "input_hashes": current_inputs,
            "artifact_hashes": artifact_hashes,
            "quota_plan": quotas,
        },
    )
    return run_dir, passages, candidates, tasks


def extend_task_manifest(
    run_dir: Path,
    candidates: list[EvidenceCandidate],
    deficits: list[dict[str, Any]],
    *,
    seed: int,
    valid_parent_questions: dict[str, str],
) -> list[PlannedTask]:
    tasks = read_jsonl(run_dir / "task_manifest.jsonl", PlannedTask)
    selected_anchors = {task.anchor_id for task in tasks if task.kind == "model"}
    passage_uses: Counter[str] = Counter()
    document_uses: Counter[str] = Counter()
    passage_types: set[tuple[str, QAType]] = set()
    candidate_by_id = {candidate.anchor_id: candidate for candidate in candidates}
    for task in tasks:
        if task.kind != "model":
            continue
        candidate = candidate_by_id[task.anchor_id]
        passage_uses[candidate.passage_id] += 1
        document_uses[candidate.document_id] += 1
        passage_types.add((candidate.passage_id, task.qa_type))
    additions: list[PlannedTask] = []
    capacity_issues: list[dict[str, Any]] = []

    for deficit in deficits:
        needed = int(deficit["deficit"])
        if deficit["answerability"] != Answerability.ANSWERABLE.value:
            continue
        question_language = Language(deficit["question_language"])
        qa_type = QAType(deficit["primary_type"])
        cross_lingual = bool(deficit["cross_lingual"])
        evidence_language = (
            Language.TURKISH
            if cross_lingual and question_language == Language.ENGLISH
            else (
                Language.ENGLISH
                if cross_lingual and question_language == Language.TURKISH
                else question_language
            )
        )
        for _ in range(needed):
            eligible = [
                candidate
                for candidate in candidates
                if candidate.language == evidence_language
                and qa_type in candidate.compatible_types
                and candidate.anchor_id not in selected_anchors
                and passage_uses[candidate.passage_id] < PASSAGE_REUSE_LIMIT
                and (candidate.passage_id, qa_type) not in passage_types
            ]
            if not eligible:
                capacity_issues.append(
                    {
                        "code": "fill_stratum_exhausted",
                        "question_language": question_language.value,
                        "evidence_language": evidence_language.value,
                        "primary_type": qa_type.value,
                        "cross_lingual": cross_lingual,
                    }
                )
                break
            candidate = min(
                eligible,
                key=lambda item: (
                    document_uses[item.document_id],
                    passage_uses[item.passage_id],
                    _candidate_order(
                        item,
                        seed=seed + len(tasks) + len(additions),
                        question_language=question_language,
                        qa_type=qa_type,
                        cross_lingual=cross_lingual,
                    ),
                ),
            )
            index = len(tasks) + len(additions)
            task_id = stable_id(
                "task",
                PLANNER_VERSION,
                seed,
                "fill",
                index,
                question_language.value,
                qa_type.value,
                cross_lingual,
                candidate.anchor_id,
                length=32,
            )
            additions.append(
                PlannedTask(
                    index=index,
                    task_id=task_id,
                    question_language=question_language,
                    qa_type=qa_type,
                    answerability=Answerability.ANSWERABLE,
                    cross_lingual=cross_lingual,
                    anchor_id=candidate.anchor_id,
                )
            )
            selected_anchors.add(candidate.anchor_id)
            passage_uses[candidate.passage_id] += 1
            document_uses[candidate.document_id] += 1
            passage_types.add((candidate.passage_id, qa_type))

    used_parent_ids = {task.parent_task_id for task in tasks if task.kind == "mutation"}
    all_model_tasks = [task for task in [*tasks, *additions] if task.kind == "model"]
    for deficit in deficits:
        needed = int(deficit["deficit"])
        if deficit["answerability"] != Answerability.CORPUS_UNANSWERABLE.value:
            continue
        question_language = Language(deficit["question_language"])
        made = 0
        parents = sorted(
            [
                task
                for task in all_model_tasks
                if task.question_language == question_language
                and task.task_id in valid_parent_questions
                and task.task_id not in used_parent_ids
            ],
            key=lambda task: sha256_text(f"fill-mutation-parent:{seed}:{task.task_id}"),
        )
        for parent in parents:
            if made >= needed:
                break
            parent_candidate = candidate_by_id[parent.anchor_id]
            parent_question = valid_parent_questions[parent.task_id]
            donor_choice: tuple[MutationTerm, MutationTerm, EvidenceCandidate] | None = None
            for source_term in parent_candidate.mutation_terms:
                if source_term.value.casefold() not in parent_question.casefold():
                    continue
                donors = [
                    (donor_term, donor)
                    for donor in candidates
                    if donor.document_id != parent_candidate.document_id
                    for donor_term in donor.mutation_terms
                    if donor_term.kind == source_term.kind
                    and donor_term.value.casefold() != source_term.value.casefold()
                    and donor_term.value.casefold() not in parent_candidate.anchor_text.casefold()
                ]
                if donors:
                    donor_term, donor = min(
                        donors,
                        key=lambda item: sha256_text(
                            f"fill-mutation-donor:{seed}:{parent.task_id}:"
                            f"{source_term.value}:{item[0].value}:{item[1].anchor_id}"
                        ),
                    )
                    donor_choice = (source_term, donor_term, donor)
                    break
            if donor_choice is None:
                continue
            source_term, donor_term, donor = donor_choice
            index = len(tasks) + len(additions)
            task_id = stable_id(
                "task",
                PLANNER_VERSION,
                seed,
                "fill-mutation",
                index,
                parent.task_id,
                source_term.value,
                donor_term.value,
                length=32,
            )
            additions.append(
                PlannedTask(
                    index=index,
                    task_id=task_id,
                    kind="mutation",
                    question_language=question_language,
                    qa_type=parent.qa_type,
                    answerability=Answerability.CORPUS_UNANSWERABLE,
                    anchor_id=parent.anchor_id,
                    parent_task_id=parent.task_id,
                    mutation_kind=source_term.kind,
                    mutation_source=source_term.value,
                    mutation_replacement=donor_term.value,
                    donor_document_id=donor.document_id,
                )
            )
            used_parent_ids.add(parent.task_id)
            made += 1
        if made < needed:
            capacity_issues.append(
                {
                    "code": "fill_mutation_capacity_exhausted",
                    "question_language": question_language.value,
                    "requested": needed,
                    "scheduled": made,
                }
            )

    if capacity_issues:
        report = read_json(run_dir / "capacity_report.json")
        report["issues"] = [*report.get("issues", []), *capacity_issues]
        report["fill_cycle_capacity_issues"] = capacity_issues
        write_json(run_dir / "capacity_report.json", report)
        raise CapacityError(report)
    if not additions:
        raise CapacityError(
            {
                "planner_version": PLANNER_VERSION,
                "issues": [{"code": "no_fill_tasks_scheduled"}],
            }
        )
    write_jsonl(run_dir / "task_manifest.jsonl", [*tasks, *additions])
    manifest_path = run_dir / "run_manifest.json"
    manifest = read_json(manifest_path)
    manifest["artifact_hashes"]["task_manifest.jsonl"] = file_sha256(
        run_dir / "task_manifest.jsonl"
    )
    manifest["fill_cycles"] = int(manifest.get("fill_cycles", 0)) + 1
    write_json(manifest_path, manifest)
    return additions
