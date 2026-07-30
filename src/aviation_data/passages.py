from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aviation_data.ids import TOKEN_RE, sha256_text, stable_id
from aviation_data.io import read_jsonl, write_json, write_jsonl, write_parquet_if_available
from aviation_data.models import DocumentRecord, PassageRecord

PAGE_RE = re.compile(r"^\s*<!--\s*page:(\d+)\s*-->\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    section: tuple[str, ...]
    page: int | None
    is_table: bool = False
    table_id: str | None = None


class TokenCounter:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {
            "mode": "regex_fixture",
            "id": "regex-word-v1",
            "revision": "local-v1",
        }
        self.mode = str(config.get("mode", "regex_fixture"))
        self.tokenizer_id = str(config.get("id", "regex-word-v1"))
        self.revision = str(config.get("revision", "local-v1"))
        self.checksums: dict[str, str] = {}
        self.tokenizer = None
        if self.mode == "huggingface_local":
            if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
                raise ValueError("passage tokenizer revision must be an immutable 40-hex commit")
            local_path = config.get("local_path")
            if not local_path:
                raise ValueError("huggingface_local tokenization requires local_path")
            tokenizer_path = Path(str(local_path))
            raw_checksums = config.get("checksums")
            if not isinstance(raw_checksums, dict):
                raise ValueError("huggingface_local tokenization requires file checksums")
            required_files = {"tokenizer.json", "tokenizer_config.json"}
            if not required_files.issubset(raw_checksums):
                raise ValueError(
                    "tokenizer checksums must include tokenizer.json and tokenizer_config.json"
                )
            for filename, raw_expected in raw_checksums.items():
                if Path(str(filename)).name != filename:
                    raise ValueError(f"tokenizer checksum path must be a filename: {filename}")
                expected = str(raw_expected)
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError(f"invalid tokenizer sha256 for {filename}")
                asset_path = tokenizer_path / filename
                if not asset_path.is_file():
                    raise ValueError(f"missing pinned tokenizer asset: {asset_path}")
                digest = hashlib.sha256()
                with asset_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"tokenizer checksum mismatch: {filename}")
                self.checksums[filename] = expected
            try:
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise ValueError(
                    "huggingface_local tokenization requires transformers in the model runtime"
                ) from exc
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(tokenizer_path),
                revision=self.revision,
                local_files_only=True,
                use_fast=True,
            )
            if not self.tokenizer.is_fast:
                raise ValueError("offset-preserving passage construction needs a fast tokenizer")
        elif self.mode != "regex_fixture":
            raise ValueError(f"unsupported tokenizer mode: {self.mode}")

    def spans(self, text: str) -> list[tuple[int, int]]:
        if self.tokenizer is None:
            return [(match.start(), match.end()) for match in TOKEN_RE.finditer(text)]
        encoded = self.tokenizer(
            text,
            add_special_tokens=False,
            return_offsets_mapping=True,
        )
        return [
            (int(start), int(end))
            for start, end in encoded["offset_mapping"]
            if int(end) > int(start)
        ]

    def count(self, text: str) -> int:
        return len(self.spans(text))


def _raw_blocks(text: str) -> list[tuple[int, int]]:
    spans = []
    cursor = 0
    for match in re.finditer(r"\n[ \t]*\n+", text):
        raw_start, raw_end = cursor, match.start()
        while raw_start < raw_end and text[raw_start].isspace():
            raw_start += 1
        while raw_end > raw_start and text[raw_end - 1].isspace():
            raw_end -= 1
        if raw_end > raw_start:
            spans.append((raw_start, raw_end))
        cursor = match.end()
    raw_start, raw_end = cursor, len(text)
    while raw_start < raw_end and text[raw_start].isspace():
        raw_start += 1
    while raw_end > raw_start and text[raw_end - 1].isspace():
        raw_end -= 1
    if raw_end > raw_start:
        spans.append((raw_start, raw_end))
    return spans


def _blocks(text: str) -> list[Block]:
    section: list[str] = []
    page: int | None = None
    table_counter = 0
    blocks: list[Block] = []
    for start, end in _raw_blocks(text):
        value = text[start:end]
        page_match = PAGE_RE.match(value)
        if page_match:
            page = int(page_match.group(1))
            continue
        heading_match = HEADING_RE.match(value.splitlines()[0])
        if heading_match:
            level = len(heading_match.group(1))
            section = section[: level - 1]
            section.append(heading_match.group(2).strip())
        meaningful_lines = [line.strip() for line in value.splitlines() if line.strip()]
        is_table = len(meaningful_lines) >= 2 and all(
            line.startswith("|") and line.endswith("|") for line in meaningful_lines
        )
        table_id = None
        if is_table:
            table_counter += 1
            table_id = f"table-{table_counter}"
        blocks.append(
            Block(
                start=start,
                end=end,
                section=tuple(section),
                page=page,
                is_table=is_table,
                table_id=table_id,
            )
        )
    return blocks


def _split_large_span(
    text: str,
    start: int,
    end: int,
    max_tokens: int,
    overlap_tokens: int,
    token_counter: TokenCounter,
) -> list[tuple[int, int]]:
    offsets = [
        (start + token_start, start + token_end)
        for token_start, token_end in token_counter.spans(text[start:end])
    ]
    if len(offsets) <= max_tokens:
        return [(start, end)]
    spans = []
    token_start = 0
    while token_start < len(offsets):
        token_end = min(len(offsets), token_start + max_tokens)
        char_start = start if token_start == 0 else offsets[token_start][0]
        char_end = end if token_end == len(offsets) else offsets[token_end - 1][1]
        spans.append((char_start, char_end))
        if token_end == len(offsets):
            break
        token_start = max(token_start + 1, token_end - overlap_tokens)
    return spans


def _make_passage(
    document: DocumentRecord,
    canonical: str,
    start: int,
    end: int,
    section: tuple[str, ...],
    page: int | None,
    table_id: str | None,
    token_counter: TokenCounter,
) -> PassageRecord:
    text = canonical[start:end]
    checksum = sha256_text(text)
    passage_id = stable_id(
        "passage",
        document.document_id,
        document.document_version,
        start,
        end,
        checksum,
        length=32,
    )
    return PassageRecord(
        passage_id=passage_id,
        document_id=document.document_id,
        document_version=document.document_version,
        variant_group_id=document.variant_group_id,
        language=document.language,
        topics=document.topics,
        section_path=list(section),
        page_number=page,
        table_id=table_id,
        canonical_char_start=start,
        canonical_char_end=end,
        text=text,
        token_count=token_counter.count(text),
        tokenizer_id=token_counter.tokenizer_id,
        tokenizer_revision=token_counter.revision,
        checksum=checksum,
    )


def passage_document(
    document: DocumentRecord,
    canonical: str,
    config: dict[str, Any],
    token_counter: TokenCounter | None = None,
) -> list[PassageRecord]:
    token_counter = token_counter or TokenCounter(config.get("tokenizer"))
    target = int(config["target_tokens"])
    maximum = int(config["max_tokens"])
    overlap = int(config["overlap_tokens"])
    max_table = int(config["max_table_tokens"])
    blocks = _blocks(canonical)
    passages: list[PassageRecord] = []
    pending: list[Block] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        passages.append(
            _make_passage(
                document,
                canonical,
                pending[0].start,
                pending[-1].end,
                pending[0].section,
                pending[0].page,
                None,
                token_counter,
            )
        )
        pending = []

    for block in blocks:
        block_tokens = token_counter.count(canonical[block.start : block.end])
        if block.is_table:
            heading_start = None
            heading_section = block.section
            heading_page = block.page
            if (
                pending
                and sum(token_counter.count(canonical[item.start : item.end]) for item in pending)
                <= 8
            ):
                heading_start = pending[0].start
                heading_section = pending[0].section
                heading_page = pending[0].page
                pending = []
            else:
                flush()
            pending = []
            for start, end in _split_large_span(
                canonical,
                heading_start if heading_start is not None else block.start,
                block.end,
                max_table,
                overlap,
                token_counter,
            ):
                passages.append(
                    _make_passage(
                        document,
                        canonical,
                        start,
                        end,
                        heading_section,
                        heading_page,
                        block.table_id,
                        token_counter,
                    )
                )
            continue
        if block_tokens > maximum:
            flush()
            pending = []
            for start, end in _split_large_span(
                canonical,
                block.start,
                block.end,
                maximum,
                overlap,
                token_counter,
            ):
                passages.append(
                    _make_passage(
                        document,
                        canonical,
                        start,
                        end,
                        block.section,
                        block.page,
                        None,
                        token_counter,
                    )
                )
            continue
        pending_tokens = (
            token_counter.count(canonical[pending[0].start : pending[-1].end]) if pending else 0
        )
        crosses_page = pending and block.page != pending[-1].page
        crosses_section = (
            pending and block.section != pending[-1].section and pending_tokens >= target
        )
        if pending and (pending_tokens + block_tokens > maximum or crosses_page or crosses_section):
            flush()
        pending.append(block)
        current_tokens = token_counter.count(canonical[pending[0].start : pending[-1].end])
        if current_tokens >= target:
            flush()
    flush()
    unique = {passage.passage_id: passage for passage in passages}
    ordered = sorted(unique.values(), key=lambda item: item.canonical_char_start)
    for passage in ordered:
        if canonical[passage.canonical_char_start : passage.canonical_char_end] != passage.text:
            raise AssertionError(f"passage offset mismatch: {passage.passage_id}")
    return ordered


def build_passages(data_dir: Path, config_path: Path) -> tuple[list[PassageRecord], dict[str, Any]]:
    accepted_documents = read_jsonl(
        data_dir / "curated" / "accepted_documents.jsonl", DocumentRecord
    )
    documents = [
        document
        for document in accepted_documents
        if document.release_derived_text and document.release_qa
    ]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    token_counter = TokenCounter(config.get("tokenizer"))
    passages: list[PassageRecord] = []
    for document in documents:
        canonical = (data_dir / document.canonical_path).read_text(encoding="utf-8")
        passages.extend(passage_document(document, canonical, config, token_counter))
    passages.sort(key=lambda item: (item.document_id, item.canonical_char_start))
    stats = {
        "accepted_documents": len(accepted_documents),
        "documents": len(documents),
        "rights_excluded_documents": len(accepted_documents) - len(documents),
        "passages": len(passages),
        "average_tokens": round(
            sum(passage.token_count for passage in passages) / max(1, len(passages)), 2
        ),
        "maximum_tokens": max((passage.token_count for passage in passages), default=0),
        "table_passages": sum(passage.table_id is not None for passage in passages),
        "tokenizer": {
            "id": token_counter.tokenizer_id,
            "revision": token_counter.revision,
            "mode": token_counter.mode,
            "checksums": token_counter.checksums,
            "production_ready": token_counter.mode == "huggingface_local",
        },
    }
    output = data_dir / "passages"
    write_jsonl(output / "passages.jsonl", passages)
    write_parquet_if_available(output / "passages.parquet", passages)
    write_json(output / "report.json", stats)
    return passages, stats
