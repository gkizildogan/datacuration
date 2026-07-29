from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pymupdf
from docx import Document

from aviation_data.extraction import (
    _csv_to_markdown,
    _docx_to_markdown,
    _json_to_markdown,
    _pdf_to_markdown,
    _xlsx_to_markdown,
    _xml_to_markdown,
)


def test_structured_text_extractors() -> None:
    json_markdown, json_layout = _json_to_markdown(
        json.dumps({"aircraft": "Example", "engines": 2})
    )
    assert "| aircraft | Example |" in json_markdown
    assert json_layout["extractor"] == "json"

    csv_markdown, csv_layout = _csv_to_markdown("code,name\nLTFM,Istanbul\n")
    assert "| LTFM | Istanbul |" in csv_markdown
    assert csv_layout["extractor"] == "csv"

    xml_markdown, xml_layout = _xml_to_markdown(
        "<airport><code>LTFM</code><name>Istanbul</name></airport>"
    )
    assert "| airport/code | LTFM |" in xml_markdown
    assert xml_layout["extractor"] == "xml.etree"


def test_binary_extractors(tmp_path: Path) -> None:
    docx_path = tmp_path / "fixture.docx"
    document = Document()
    document.add_heading("Maintenance", level=1)
    document.add_paragraph("The inspection interval is recorded in the approved programme.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Item"
    table.cell(0, 1).text = "Interval"
    table.cell(1, 0).text = "Inspection"
    table.cell(1, 1).text = "100 hours"
    document.save(docx_path)
    docx_markdown, docx_layout = _docx_to_markdown(docx_path)
    assert "# Maintenance" in docx_markdown
    assert "100 hours" in docx_markdown
    assert docx_layout["extractor"] == "python-docx"

    xlsx_path = tmp_path / "fixture.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Runways"
    sheet.append(["Designator", "Length"])
    sheet.append(["18", 3000])
    workbook.save(xlsx_path)
    xlsx_markdown, xlsx_layout = _xlsx_to_markdown(xlsx_path)
    assert "# Runways" in xlsx_markdown
    assert "| 18 | 3000 |" in xlsx_markdown
    assert xlsx_layout["extractor"] == "openpyxl"

    pdf_path = tmp_path / "fixture.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (72, 72),
        "Airport operations use declared distances for runway planning and safe performance.",
    )
    pdf.save(pdf_path)
    pdf.close()
    pdf_markdown, pdf_layout = _pdf_to_markdown(pdf_path)
    assert "Airport operations" in pdf_markdown
    assert pdf_layout["extractor"] == "pymupdf4llm"
