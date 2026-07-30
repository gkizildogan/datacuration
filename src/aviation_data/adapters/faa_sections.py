from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROFILE = "faa_purpose_applicability_v1"
PURPOSE = re.compile(r"^\s*(\d+)\s+PURPOSE\b", re.IGNORECASE)
APPLICABILITY = re.compile(r"^\s*(\d+)\s+APPLICABILITY\b", re.IGNORECASE)
TOP_LEVEL = re.compile(
    r"^\s*(\d+)\s+[A-Z][A-Z0-9 /&(),’'–—-]+[.:]?\s*$",
    re.IGNORECASE,
)


def _bookmark_coordinate(entry: list[Any]) -> tuple[int, float]:
    details = entry[3]
    page = int(details.get("page", int(entry[2]) - 1))
    point = details.get("to")
    return page, float(getattr(point, "y", 0.0))


def _page_heading_coordinates(document: Any, minimum_page: int) -> list[dict[str, Any]]:
    headings = []
    for page_index in range(minimum_page, len(document)):
        page = document[page_index]
        words = page.get_text("words", sort=True)
        lines: dict[tuple[int, int], list[tuple[float, str]]] = {}
        boxes: dict[tuple[int, int], tuple[float, float]] = {}
        for x0, y0, _x1, y1, word, block, line, _word in words:
            key = (int(block), int(line))
            lines.setdefault(key, []).append((float(x0), str(word)))
            boxes[key] = (
                min(float(y0), boxes.get(key, (float(y0), float(y1)))[0]),
                max(float(y1), boxes.get(key, (float(y0), float(y1)))[1]),
            )
        ordered = [
            (" ".join(word for _, word in sorted(lines[key])), boxes[key][0])
            for key in sorted(lines, key=lambda item: boxes[item][0])
        ]
        for index, (line, y) in enumerate(ordered):
            combined = line
            if re.fullmatch(r"\d+", line.strip()) and index + 1 < len(ordered):
                combined = f"{line} {ordered[index + 1][0]}"
            if (
                PURPOSE.match(combined)
                or APPLICABILITY.match(combined)
                or TOP_LEVEL.match(combined)
            ):
                headings.append(
                    {
                        "title": combined,
                        "page": page_index,
                        "y": y,
                    }
                )
    return headings


def _extract_between(
    document: Any,
    start: tuple[int, float],
    end: tuple[int, float],
) -> str:
    chunks = []
    for page_index in range(start[0], end[0] + 1):
        page = document[page_index]
        top = start[1] if page_index == start[0] else 55.0
        bottom = end[1] - 12.0 if page_index == end[0] else page.rect.height - 40.0
        if bottom <= top:
            continue
        blocks = page.get_text(
            "blocks",
            clip=(0, top, page.rect.width, bottom),
            sort=True,
        )
        retained = []
        for block in blocks:
            value = str(block[4]).strip()
            if not value:
                continue
            if float(block[1]) >= page.rect.height - 100.0 and re.fullmatch(
                r"(?:[A-Z]-)?\d+", value
            ):
                continue
            retained.append(value)
        text = "\n".join(retained)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def extract_faa_sections(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError(
            "faa_purpose_applicability_v1 extraction requires the 'formats' extra"
        ) from exc

    document = pymupdf.open(path)
    try:
        contents_pages = [
            index
            for index, page in enumerate(document)
            if re.search(r"(?im)^\s*Contents(?:\s*\(continued\))?\s*$", page.get_text())
        ]
        final_contents_page = max(contents_pages, default=-1)
        toc = document.get_toc(simple=False)
        headings = [
            {
                "title": re.sub(r"\s+", " ", str(entry[1])).strip(),
                "page": _bookmark_coordinate(entry)[0],
                "y": _bookmark_coordinate(entry)[1],
            }
            for entry in toc
            if int(entry[2]) > 0 and _bookmark_coordinate(entry)[0] > final_contents_page
        ]
        purpose_index = next(
            (index for index, heading in enumerate(headings) if PURPOSE.match(heading["title"])),
            None,
        )
        applicability_index = next(
            (
                index
                for index, heading in enumerate(headings)
                if purpose_index is not None
                and index > purpose_index
                and APPLICABILITY.match(heading["title"])
            ),
            None,
        )
        method = "bookmarks"
        if purpose_index is None or applicability_index is None:
            headings = _page_heading_coordinates(document, final_contents_page + 1)
            purpose_index = next(
                (
                    index
                    for index, heading in enumerate(headings)
                    if PURPOSE.match(heading["title"])
                ),
                None,
            )
            applicability_index = next(
                (
                    index
                    for index, heading in enumerate(headings)
                    if purpose_index is not None
                    and index > purpose_index
                    and APPLICABILITY.match(heading["title"])
                ),
                None,
            )
            method = "normalized_heading_fallback"
        if purpose_index is None:
            raise ValueError("real PURPOSE heading not found after final Contents page")
        if applicability_index is None:
            raise ValueError("real APPLICABILITY heading not found after PURPOSE")
        purpose_heading = headings[purpose_index]
        applicability_heading = headings[applicability_index]
        applicability_number = int(APPLICABILITY.match(applicability_heading["title"]).group(1))
        following = next(
            (
                heading
                for heading in headings[applicability_index + 1 :]
                if (match := TOP_LEVEL.match(heading["title"]))
                and int(match.group(1)) != applicability_number
            ),
            None,
        )
        if following is None:
            raise ValueError("numbered section following APPLICABILITY not found")
        purpose_start = (purpose_heading["page"], purpose_heading["y"])
        applicability_start = (
            applicability_heading["page"],
            applicability_heading["y"],
        )
        end = (following["page"], following["y"])
        purpose_text = _extract_between(document, purpose_start, applicability_start)
        applicability_text = _extract_between(document, applicability_start, end)
        if len(re.findall(r"\w+", purpose_text)) < 5:
            raise ValueError("PURPOSE section is structurally empty")
        if len(re.findall(r"\w+", applicability_text)) < 5:
            raise ValueError("APPLICABILITY section is structurally empty")
    finally:
        document.close()
    structured = {
        "schema_version": "1.0.0",
        "extraction_profile": PROFILE,
        "heading_detection": method,
        "final_contents_page": final_contents_page + 1,
        "purpose": {
            "text": purpose_text,
            "page_range": [purpose_start[0] + 1, applicability_start[0] + 1],
        },
        "applicability": {
            "text": applicability_text,
            "page_range": [applicability_start[0] + 1, end[0] + 1],
        },
    }
    markdown = f"# PURPOSE\n\n{purpose_text}\n\n# APPLICABILITY\n\n{applicability_text}"
    return markdown, {
        "extractor": "pymupdf-sections",
        "extraction_profile": PROFILE,
        "_structured_artifact": {
            "filename": "faa_sections.json",
            "key": "faa_sections_json",
            "value": structured,
        },
    }
