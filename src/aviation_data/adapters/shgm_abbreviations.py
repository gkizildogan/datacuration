from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROFILE = "shgm_abbreviations_v1"
SECTION_HEADING = re.compile(
    r"^\s*tan[ıi]m(?:lar)?(?:\s+ve)?\s+k[ıi]saltmalar\s*$",
    re.IGNORECASE,
)
ARTICLE = re.compile(r"^\s*MADDE\s+(\d+)\b", re.IGNORECASE)
SECTION_BOUNDARY = re.compile(
    r"^\s*[A-ZÇĞİÖŞÜ]+\s+B[ÖO]L[ÜU]M\s*$",
    re.IGNORECASE,
)
ENTRY = re.compile(r"^\s*[a-zçğıöşü]\)\s*([^:]+):\s*(.*)$", re.IGNORECASE)
ABBREVIATION = re.compile(r"^[A-ZÇĞİÖŞÜ0-9][A-ZÇĞİÖŞÜ0-9./+-]*$")


def _strip_english_expansion(value: str) -> str:
    value = value.strip().rstrip(",;")
    match = re.search(r"\s*\(([^()]*)\)\s*$", value)
    if match:
        expansion = match.group(1)
        if re.search(r"[a-z]", expansion) and not re.search(r"[çğıöşüÇĞİÖŞÜ]", expansion):
            value = value[: match.start()].rstrip()
    return value


def _aliases(label: str) -> list[str]:
    primary = label.strip()
    parenthesized = re.findall(r"\(([A-ZÇĞİÖŞÜ0-9./+-]+)\)", primary)
    primary = re.sub(r"\s*\([^)]*\)\s*", " ", primary).strip()
    values = [primary, *parenthesized]
    return list(dict.fromkeys(value for value in values if ABBREVIATION.fullmatch(value)))


def extract_shgm_abbreviations(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("shgm_abbreviations_v1 extraction requires the 'formats' extra") from exc

    document = pymupdf.open(path)
    try:
        lines: list[tuple[int, str]] = []
        for page_number, page in enumerate(document, start=1):
            lines.extend(
                (page_number, line.strip())
                for line in page.get_text("text").splitlines()
                if line.strip()
            )
    finally:
        document.close()
    heading_index = next(
        (index for index, (_, line) in enumerate(lines) if SECTION_HEADING.search(line)),
        None,
    )
    if heading_index is None:
        raise ValueError("Tanım/Tanımlar ve Kısaltmalar heading not found")
    current_article = next(
        (
            (index, int(match.group(1)))
            for index in range(heading_index, len(lines))
            if (match := ARTICLE.match(lines[index][1]))
        ),
        None,
    )
    if current_article is None:
        raise ValueError("abbreviation section has no numbered article")
    article_index, article_number = current_article
    end_index = next(
        (
            index
            for index in range(article_index + 1, len(lines))
            if SECTION_BOUNDARY.match(lines[index][1])
            or ((match := ARTICLE.match(lines[index][1])) and int(match.group(1)) != article_number)
        ),
        len(lines),
    )
    section = lines[article_index:end_index]
    groups: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for row in section:
        if ENTRY.match(row[1]):
            if current:
                groups.append(current)
            current = [row]
        elif current:
            current.append(row)
    if current:
        groups.append(current)

    entries = []
    mapping: dict[str, str] = {}
    for group in groups:
        first = ENTRY.match(group[0][1])
        if first is None:
            continue
        aliases = _aliases(first.group(1))
        if not aliases:
            continue
        continuation = " ".join(line for _, line in group[1:])
        original = re.sub(r"\s+", " ", f"{group[0][1]} {continuation}").strip()
        meaning_source = re.sub(
            r"\s+",
            " ",
            f"{first.group(2)} {continuation}",
        ).strip()
        meaning = _strip_english_expansion(meaning_source)
        for alias in aliases:
            mapping[alias] = meaning
        entries.append(
            {
                "abbreviation": aliases[0],
                "aliases": aliases,
                "turkish_meaning": meaning,
                "source_text": original,
                "page_start": min(page for page, _ in group),
                "page_end": max(page for page, _ in group),
            }
        )
    if not entries:
        raise ValueError("abbreviation section contains no abbreviation-style entries")
    markdown = [
        "# Tanımlar ve Kısaltmalar",
        "",
        "| Abbreviation | Turkish meaning | Source pages |",
        "|---|---|---:|",
        *[
            "| "
            + " / ".join(entry["aliases"])
            + " | "
            + entry["turkish_meaning"].replace("|", "\\|")
            + f" | {entry['page_start']}"
            + (f"–{entry['page_end']}" if entry["page_end"] != entry["page_start"] else "")
            + " |"
            for entry in entries
        ],
    ]
    structured = {
        "schema_version": "1.0.0",
        "extraction_profile": PROFILE,
        "mapping": dict(sorted(mapping.items())),
        "entries": entries,
        "section": {
            "heading": lines[heading_index][1],
            "article_number": article_number,
            "page_start": section[0][0],
            "page_end": section[-1][0],
        },
    }
    return "\n".join(markdown), {
        "extractor": "pymupdf",
        "extraction_profile": PROFILE,
        "entry_count": len(entries),
        "_structured_artifact": {
            "filename": "shgm_abbreviations.json",
            "key": "shgm_abbreviations_json",
            "value": structured,
        },
    }
