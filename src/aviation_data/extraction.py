from __future__ import annotations

import csv
import html
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aviation_data.ids import normalize_text, sha256_text, stable_id, tokens
from aviation_data.io import read_jsonl, write_json, write_jsonl, write_parquet_if_available
from aviation_data.models import (
    DocumentRecord,
    Language,
    SourceDefinition,
    SourceRecord,
    SourceRegistry,
)
from aviation_data.registry import source_index


class ExtractionError(RuntimeError):
    pass


class _ReadableHTML(HTMLParser):
    block_tags = {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }
    ignored = {"script", "style", "nav", "footer", "aside", "noscript", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.heading_prefix: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.ignored:
            self.ignored_depth += 1
        if self.ignored_depth:
            return
        if tag in self.block_tags:
            self.parts.append("\n")
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_prefix = "#" * int(tag[1]) + " "
            self.parts.append(self.heading_prefix)
        elif tag == "li":
            self.parts.append("- ")
        elif tag in {"td", "th"}:
            self.parts.append("| ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.ignored and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(html.unescape(data))

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "windows-1254", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionError("unsupported text encoding")


def _html_to_markdown(text: str, source: SourceDefinition) -> tuple[str, dict[str, Any]]:
    try:
        from selectolax.parser import HTMLParser as SelectolaxParser
    except ImportError:
        parser = _ReadableHTML()
        parser.feed(text)
        return parser.markdown(), {"extractor": "stdlib.html.parser"}

    tree = SelectolaxParser(text)
    for selector in ("script", "style", "nav", "footer", "aside", "form", "noscript"):
        for node in tree.css(selector):
            node.decompose()
    content_selector = source.selectors.get("content")
    root = tree.css_first(content_selector) if content_selector else None
    root = root or tree.css_first("main") or tree.css_first("article") or tree.body
    if root is None:
        raise ExtractionError("HTML has no readable content root")
    chunks: list[str] = []
    tables: list[list[list[str]]] = []
    for node in root.css("h1,h2,h3,h4,h5,h6,p,li,table"):
        if node.tag == "table":
            rows = []
            for row in node.css("tr"):
                cells = [cell.text(separator=" ", strip=True) for cell in row.css("th,td")]
                if cells:
                    rows.append(cells)
            if rows:
                width = max(len(row) for row in rows)
                padded = [row + [""] * (width - len(row)) for row in rows]
                tables.append(padded)
                chunks.append("| " + " | ".join(padded[0]) + " |")
                chunks.append("| " + " | ".join(["---"] * width) + " |")
                chunks.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        else:
            value = node.text(separator=" ", strip=True)
            if not value:
                continue
            if re.fullmatch(r"h[1-6]", node.tag):
                value = f"{'#' * int(node.tag[1])} {value}"
            elif node.tag == "li":
                value = f"- {value}"
            chunks.append(value)
    return "\n\n".join(chunks), {
        "extractor": "selectolax",
        "content_selector": content_selector or "main/article/body",
        "tables": tables,
    }


def _flatten_json(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_json(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(_flatten_json(child, f"{prefix}[{index}]"))
    else:
        rows.append((prefix, "" if value is None else str(value)))
    return rows


def _json_to_markdown(text: str) -> tuple[str, dict[str, Any]]:
    value = json.loads(text)
    rows = _flatten_json(value)
    markdown = ["# Structured record", "", "| Field | Value |", "|---|---|"]
    for key, item in rows:
        safe_key = key.replace("|", "\\|")
        safe_item = item.replace("|", "\\|")
        markdown.append(f"| {safe_key} | {safe_item} |")
    return "\n".join(markdown), {"extractor": "json", "value": value, "rows": rows}


def _csv_to_markdown(text: str) -> tuple[str, dict[str, Any]]:
    dialect = csv.Sniffer().sniff(text[:4096])
    rows = list(csv.reader(text.splitlines(), dialect))
    if not rows:
        raise ExtractionError("empty CSV")
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    markdown = ["# Table", "", "| " + " | ".join(rows[0]) + " |"]
    markdown.append("| " + " | ".join(["---"] * width) + " |")
    markdown.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(markdown), {"extractor": "csv", "rows": rows}


def _xml_to_markdown(text: str) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(text)
    rows: list[tuple[str, str]] = []

    def walk(element: ET.Element, path: str) -> None:
        tag = element.tag.rsplit("}", maxsplit=1)[-1]
        current = f"{path}/{tag}" if path else tag
        value = " ".join(part.strip() for part in element.itertext() if part.strip())
        if value and len(element) == 0:
            rows.append((current, value))
        for child in element:
            walk(child, current)

    walk(root, "")
    markdown = ["# XML record", "", "| Element | Value |", "|---|---|"]
    markdown.extend(f"| {path} | {value} |" for path, value in rows)
    return "\n".join(markdown), {"extractor": "xml.etree", "rows": rows}


def _docx_to_markdown(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        from docx import Document
    except ImportError as exc:
        raise ExtractionError("DOCX extraction requires the 'formats' extra") from exc
    document = Document(path)
    chunks: list[str] = []
    paragraphs = []
    for paragraph in document.paragraphs:
        value = paragraph.text.strip()
        if not value:
            continue
        style = paragraph.style.name if paragraph.style else ""
        heading = re.search(r"Heading\s+([1-6])", style, re.IGNORECASE)
        output = f"{'#' * int(heading.group(1))} {value}" if heading else value
        chunks.append(output)
        paragraphs.append({"style": style, "text": value})
    tables = []
    for table_index, table in enumerate(document.tables, start=1):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        chunks.extend(
            [
                f"## Table {table_index}",
                "| " + " | ".join(rows[0]) + " |",
                "| " + " | ".join(["---"] * width) + " |",
                *["| " + " | ".join(row) + " |" for row in rows[1:]],
            ]
        )
        tables.append(rows)
    return "\n\n".join(chunks), {
        "extractor": "python-docx",
        "paragraphs": paragraphs,
        "tables": tables,
    }


def _xlsx_to_markdown(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise ExtractionError("XLSX extraction requires the 'formats' extra") from exc
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    chunks: list[str] = []
    sheets: dict[str, list[list[str]]] = {}
    for sheet in workbook.worksheets:
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        chunks.extend(
            [
                f"# {sheet.title}",
                "| " + " | ".join(rows[0]) + " |",
                "| " + " | ".join(["---"] * width) + " |",
                *["| " + " | ".join(row) + " |" for row in rows[1:]],
            ]
        )
        sheets[sheet.title] = rows
    workbook.close()
    if not chunks:
        raise ExtractionError("XLSX contains no non-empty cells")
    return "\n\n".join(chunks), {"extractor": "openpyxl", "sheets": sheets}


def _pdf_to_markdown(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError as exc:
        raise ExtractionError("PDF extraction requires the 'formats' extra") from exc
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, write_images=False)
    if isinstance(chunks, str):
        markdown = chunks
        layout = [{"page": None, "text": chunks}]
    else:
        pages = []
        layout = []
        for index, chunk in enumerate(chunks, start=1):
            value = chunk.get("text", "")
            pages.append(f"<!-- page:{index} -->\n\n{value}")
            layout.append(chunk)
        markdown = "\n\n".join(pages)
    if len(tokens(markdown)) < 10:
        document = pymupdf.open(path)
        ocr_pages = []
        for index, page in enumerate(document, start=1):
            text_page = page.get_textpage_ocr(language="eng+tur", dpi=300, full=True)
            ocr_pages.append(f"<!-- page:{index} -->\n\n{page.get_text(textpage=text_page)}")
        document.close()
        ocr_text = "\n\n".join(ocr_pages)
        if len(tokens(ocr_text)) > len(tokens(markdown)):
            markdown = ocr_text
            layout = [{"extractor": "pymupdf-ocr", "languages": "eng+tur"}]
    return markdown, {"extractor": "pymupdf4llm", "pages": layout}


def _extract_payload(
    path: Path, record: SourceRecord, source: SourceDefinition
) -> tuple[str, dict[str, Any]]:
    mime = record.detected_mime.casefold()
    suffix = Path(urlparse(record.canonical_url).path).suffix.casefold()
    native = record.native_format.casefold()
    if mime == "application/pdf" or suffix == ".pdf" or native == "pdf":
        return _pdf_to_markdown(path)
    if suffix == ".docx" or native == "docx":
        return _docx_to_markdown(path)
    if suffix == ".xlsx" or native == "xlsx":
        return _xlsx_to_markdown(path)
    text = _decode(path.read_bytes())
    if mime == "text/html" or suffix in {".html", ".htm"} or native == "html":
        return _html_to_markdown(text, source)
    if mime in {"application/json", "text/json"} or suffix == ".json" or native == "json":
        return _json_to_markdown(text)
    if mime in {"application/xml", "text/xml"} or suffix == ".xml" or native == "xml":
        return _xml_to_markdown(text)
    if mime in {"text/csv", "application/csv"} or suffix == ".csv" or native == "csv":
        return _csv_to_markdown(text)
    if mime.startswith("text/") or suffix in {".md", ".markdown", ".txt"}:
        return text, {"extractor": "text", "encoding": "utf-8-normalized"}
    raise ExtractionError(f"unsupported MIME/native format: {mime}/{native}")


def _table_rows(layout: dict[str, Any]) -> list[tuple[str, list[dict[str, str]]]]:
    collections: list[tuple[str, list[list[str]]]] = []
    extractor = layout.get("extractor")
    if extractor in {"json", "xml.etree"}:
        return [
            (
                "fields",
                [
                    {"field": str(field), "value": str(value)}
                    for field, value in layout.get("rows", [])
                ],
            )
        ]
    if extractor == "csv":
        collections.append(("table", layout.get("rows", [])))
    for index, rows in enumerate(layout.get("tables", []), start=1):
        collections.append((f"table_{index}", rows))
    for name, rows in layout.get("sheets", {}).items():
        collections.append((f"sheet_{name}", rows))
    output = []
    for name, rows in collections:
        if not rows:
            continue
        width = max(len(row) for row in rows)
        header = [
            (str(value).strip() or f"column_{index + 1}")
            for index, value in enumerate(rows[0] + [""] * (width - len(rows[0])))
        ]
        deduplicated = []
        counts: dict[str, int] = {}
        for value in header:
            counts[value] = counts.get(value, 0) + 1
            deduplicated.append(value if counts[value] == 1 else f"{value}_{counts[value]}")
        output.append(
            (
                name,
                [
                    {
                        key: str(value)
                        for key, value in zip(
                            deduplicated,
                            row + [""] * (width - len(row)),
                            strict=True,
                        )
                    }
                    for row in rows[1:]
                ],
            )
        )
    return output


def _language(text: str, declared: list[Language]) -> Language:
    if len(declared) == 1:
        return declared[0]
    lowered = text.casefold()
    turkish_markers = sum(lowered.count(char) for char in "çğıöşü")
    turkish_words = sum(
        len(re.findall(rf"\b{word}\b", lowered))
        for word in ("bir", "ve", "için", "hava", "olan", "ile", "pist")
    )
    english_words = sum(
        len(re.findall(rf"\b{word}\b", lowered))
        for word in ("the", "and", "for", "aircraft", "runway", "with")
    )
    if turkish_markers + turkish_words > english_words:
        return Language.TURKISH
    if english_words:
        return Language.ENGLISH
    return Language.UNDETERMINED


def extract_sources(
    registry: SourceRegistry,
    data_dir: Path,
) -> tuple[list[DocumentRecord], list[dict[str, str]]]:
    records = read_jsonl(data_dir / "manifests" / "source_records.jsonl", SourceRecord)
    definitions = source_index(registry)
    documents: list[DocumentRecord] = []
    errors: list[dict[str, str]] = []
    for record in records:
        source = definitions.get(record.registry_source_id)
        if source is None:
            errors.append(
                {
                    "source_record_id": record.source_record_id,
                    "error": "source missing from current registry",
                }
            )
            continue
        source_path = data_dir / record.storage_path
        try:
            markdown, layout = _extract_payload(source_path, record, source)
            canonical = normalize_text(markdown)
            flags = []
            if len(tokens(canonical)) < 10:
                flags.append("very_short")
            replacement_ratio = canonical.count("\ufffd") / max(1, len(canonical))
            if replacement_ratio > 0.001:
                flags.append("encoding_replacement_noise")
            digest = sha256_text(canonical)
            document_id = stable_id(
                "doc", record.registry_source_id, record.source_version, digest, length=32
            )
            artifact_dir = data_dir / "extracted" / "artifacts" / document_id
            canonical_path = artifact_dir / "canonical.md"
            plain_path = artifact_dir / "canonical.txt"
            layout_path = artifact_dir / "layout.json"
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_path.write_text(canonical, encoding="utf-8", newline="\n")
            plain = re.sub(r"^#{1,6}\s+", "", canonical, flags=re.MULTILINE)
            plain_path.write_text(plain, encoding="utf-8", newline="\n")
            write_json(layout_path, layout)
            canonical_relative = canonical_path.relative_to(data_dir).as_posix()
            artifact_paths = {
                "source": record.storage_path,
                "canonical_markdown": canonical_relative,
                "canonical_text": plain_path.relative_to(data_dir).as_posix(),
                "layout_json": layout_path.relative_to(data_dir).as_posix(),
            }
            for table_name, rows in _table_rows(layout):
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", table_name)
                table_path = artifact_dir / f"{safe_name}.parquet"
                if write_parquet_if_available(table_path, rows):
                    artifact_paths[f"table_parquet:{table_name}"] = table_path.relative_to(
                        data_dir
                    ).as_posix()
            documents.append(
                DocumentRecord(
                    document_id=document_id,
                    document_version=record.source_version,
                    variant_group_id=stable_id(
                        "variant",
                        record.registry_source_id,
                        record.canonical_url,
                        length=24,
                    ),
                    title=record.title or Path(urlparse(record.canonical_url).path).stem,
                    language=_language(canonical, record.languages),
                    topics=record.topics,
                    publisher=record.publisher,
                    source_family=record.source_family,
                    authority_level=record.authority_level,
                    source_record_id=record.source_record_id,
                    source_url=record.canonical_url,
                    native_mime=record.detected_mime,
                    native_format=record.native_format,
                    license_id=record.rights.license_id,
                    attribution=record.rights.attribution,
                    rights_state=record.rights.state,
                    release_derived_text=record.rights.release_derived_text,
                    release_qa=record.rights.release_qa,
                    canonical_path=canonical_relative,
                    canonical_sha256=digest,
                    canonical_char_count=len(canonical),
                    canonical_token_count=len(tokens(canonical)),
                    artifact_paths=artifact_paths,
                    derived_from=[record.source_record_id],
                    quality_flags=flags,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "source_record_id": record.source_record_id,
                    "source_id": record.registry_source_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    documents.sort(key=lambda item: item.document_id)
    write_jsonl(data_dir / "extracted" / "documents.jsonl", documents)
    write_parquet_if_available(data_dir / "extracted" / "documents.parquet", documents)
    write_json(data_dir / "extracted" / "errors.json", errors)
    return documents, errors
