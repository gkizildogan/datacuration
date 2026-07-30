from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

PROFILE = "easa_toc_section_v1"
ELIGIBLE = re.compile(r"^(?:CS-E\b|AMC\b)", re.IGNORECASE)


def _coordinate(entry: list[Any]) -> tuple[int, float]:
    details = entry[3]
    page = int(details.get("page", int(entry[2]) - 1))
    point = details.get("to")
    y = float(getattr(point, "y", 0.0))
    return page, y


def _extract_between(
    document: Any,
    start: tuple[int, float],
    end: tuple[int, float],
) -> str:
    chunks = []
    start_page, start_y = start
    end_page, end_y = end
    for page_index in range(start_page, end_page + 1):
        page = document[page_index]
        top = start_y if page_index == start_page else 0.0
        bottom = end_y if page_index == end_page else page.rect.height
        if bottom <= top:
            continue
        text = page.get_text(
            "text",
            clip=(0, top, page.rect.width, bottom),
            sort=True,
        ).strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def extract_easa_section(
    path: Path,
    *,
    seed: int,
    checksum: str,
) -> tuple[str, dict[str, Any]]:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("easa_toc_section_v1 extraction requires the 'formats' extra") from exc

    document = pymupdf.open(path)
    try:
        toc = document.get_toc(simple=False)
        if not toc:
            raise ValueError("PDF has no usable bookmark TOC")
        eligible = [
            (index, entry)
            for index, entry in enumerate(toc)
            if int(entry[0]) in {2, 3}
            and ELIGIBLE.match(str(entry[1]).strip())
            and "appendix" not in str(entry[1]).casefold()
        ]
        if not eligible:
            raise ValueError("PDF TOC has no eligible CS-E or AMC sections")
        digest = hashlib.sha256(f"{seed}:{checksum}".encode()).hexdigest()
        selected_position = int(digest, 16) % len(eligible)
        toc_index, selected = eligible[selected_position]
        level = int(selected[0])
        boundary = next(
            (
                entry
                for entry in toc[toc_index + 1 :]
                if int(entry[0]) <= level and int(entry[2]) > 0
            ),
            None,
        )
        start = _coordinate(selected)
        end = (
            _coordinate(boundary)
            if boundary is not None
            else (len(document) - 1, document[-1].rect.height)
        )
        text = _extract_between(document, start, end)
        if len(re.findall(r"\w+", text)) < 5:
            raise ValueError("selected EASA section is structurally empty")
    finally:
        document.close()
    title = re.sub(r"\s+", " ", str(selected[1])).strip()
    structured = {
        "schema_version": "1.0.0",
        "extraction_profile": PROFILE,
        "eligible_count": len(eligible),
        "chosen_index": selected_position,
        "chosen_title": title,
        "hierarchy_level": level,
        "page_range": [start[0] + 1, end[0] + 1],
        "start_coordinate": {"page": start[0] + 1, "y": start[1]},
        "end_coordinate": {"page": end[0] + 1, "y": end[1]},
        "selection_seed": seed,
        "source_checksum": checksum,
    }
    return f"# {title}\n\n{text}", {
        "extractor": "pymupdf-bookmarks",
        "extraction_profile": PROFILE,
        "eligible_sections": len(eligible),
        "_structured_artifact": {
            "filename": "easa_section.json",
            "key": "easa_section_json",
            "value": structured,
        },
    }
