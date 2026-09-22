#!/usr/bin/env python
"""Generate the test corpus programmatically.

Checked-in binary fixtures rot and hide their provenance; generating them means
every test file is readable, reviewable and regenerable:

    python scripts/make_fixtures.py            # writes tests/fixtures/
    python scripts/make_fixtures.py --force    # overwrite existing files

The corpus deliberately includes the *unhappy* cases -- a text-less (scanned)
PDF, a binary blob, a ragged CSV, a CSV with markdown-hostile characters --
because those are the cases where a converter silently lies.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --------------------------------------------------------------------- text


def make_plain_text() -> None:
    write_text(
        FIXTURES / "notes.txt",
        """Meeting notes
=============

Attendees: Ana, Bo, Chen

Decisions
---------

1. Ship the converter in two phases.
2. Do not attempt OCR in phase one.

Outstanding
-----------

- #412 needs a fixture
- - this line already looks like a list item
- 3. and this one looks numbered

The report says revenue grew 12% to 45,300 units.
""",
    )
    # GBK-encoded Chinese text: exercises encoding detection.
    write_text(
        FIXTURES / "gbk_notes.txt",
        "会议记录\n\n一、项目进度正常。\n二、下周三前完成验收。\n",
        encoding="gb18030",
    )


def make_markdown() -> None:
    write_text(
        FIXTURES / "guide.md",
        """---
title: Field guide
author: Ana
tags:
  - reference
---

# Field guide

## Setup

Install the package.

### Notes

Nested heading levels must survive the round trip.
""",
    )


# --------------------------------------------------------------------- data


def make_csv() -> None:
    write_text(
        FIXTURES / "people.csv",
        "name,role,city,notes\n"
        'Ana,engineer,"Lisbon, PT","says | pipes matter"\n'
        "Bo,designer,Berlin,\n"
        "Chen,analyst,Singapore,line one\n",
    )
    # Ragged rows + a delimiter that is not a comma.
    write_text(
        FIXTURES / "ragged.tsv",
        "id\titem\tqty\n1\tbolt\t10\n2\tnut\n3\twasher\t5\textra\n",
    )


def make_json() -> None:
    write_text(
        FIXTURES / "config.json",
        json.dumps(
            {
                "service": "converter",
                "version": 2,
                "features": {"pdf": True, "ocr": False},
                "limits": {"max_mb": 512, "workers": 4},
            },
            indent=2,
        ),
    )
    write_text(
        FIXTURES / "records.jsonl",
        "\n".join(
            json.dumps(obj)
            for obj in [
                {"id": 1, "name": "Ana", "score": 91},
                {"id": 2, "name": "Bo", "score": 84},
            ]
        )
        + "\n{not json at all\n",
    )


def make_misc_text() -> None:
    write_text(FIXTURES / "sample.py", 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\n\nclass Calc:\n    def total(self):\n        return 0\n')
    write_text(FIXTURES / "settings.ini", "[server]\nhost = 127.0.0.1\nport = 8765\n\n[paths]\nout = .md\n")
    write_text(FIXTURES / "sample.srt", "1\n00:00:01,000 --> 00:00:03,500\nHello and welcome.\n\n2\n00:00:03,600 --> 00:00:06,000\nToday we convert files.\n")
    write_text(FIXTURES / "patch.diff", "--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,4 @@\n import os\n-print('hi')\n+print('hello')\n+print('world')\n")
    write_text(FIXTURES / "code.md", "def add(a, b):\n    return a + b\n\n# Title written in a .md file that is actually code\n")


def make_notebook() -> None:
    write_text(
        FIXTURES / "analysis.ipynb",
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# Analysis\n", "\n", "Load the data and look at it."]},
                    {"cell_type": "code", "metadata": {}, "execution_count": 1,
                     "source": ["import pandas as pd\n", "df = pd.DataFrame({'a': [1, 2]})\n"],
                     "outputs": [{"output_type": "stream", "name": "stdout", "text": ["rows=2\n"]}]},
                    {"cell_type": "code", "metadata": {}, "execution_count": 2,
                     "source": ["1 / 0"], "outputs": [
                         {"output_type": "error", "ename": "ZeroDivisionError",
                          "evalue": "division by zero",
                          "traceback": ["Traceback (most recent call last)", "ZeroDivisionError: division by zero"]}]},
                ],
                "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"},
                             "language_info": {"name": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            indent=1,
        ),
    )


# --------------------------------------------------------------------- html


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Quarterly review &amp; outlook</title>
  <style>body { color: red }</style>
  <script>console.log('this must not appear in the output')</script>
</head>
<body>
  <nav><a href="/">Home</a> <a href="/about">About</a></nav>
  <header><h1>Quarterly review</h1></header>
  <main>
    <p>The quarter closed with <strong>revenue up 12%</strong> and <em>churn flat</em>.</p>
    <h2>Highlights</h2>
    <ul>
      <li>Shipped the converter
        <ul><li>PDF path</li><li>Office path</li></ul>
      </li>
      <li>Reduced p95 latency by 30ms</li>
    </ul>
    <h2>Numbers</h2>
    <table>
      <thead><tr><th>Metric</th><th>Q1</th><th>Q2</th></tr></thead>
      <tbody>
        <tr><td>Revenue</td><td>1,200</td><td>1,344</td></tr>
        <tr><td>Churn</td><td>2.1%</td><td>2.1%</td></tr>
      </tbody>
    </table>
    <h2>Next steps</h2>
    <ol><li>Close the OCR gap</li><li>Add batch mode</li></ol>
    <blockquote>Deadline is the end of the month.</blockquote>
    <p>See <a href="https://example.com/report">the full report</a>.</p>
    <pre><code class="language-python">def convert(path):
    return path</code></pre>
    <p>Contact <a href="mailto:ana@example.com">Ana</a> for details.</p>
  </main>
  <footer><p>&copy; 2025 Example Corp. Not part of the content.</p></footer>
</body>
</html>
"""


def make_html() -> None:
    write_text(FIXTURES / "review.html", HTML)
    # An HTML fragment with no <html> wrapper at all.
    write_text(FIXTURES / "fragment.html", "<h2>Fragment</h2><p>No doctype, no html tag.</p><table><tr><td>a</td><td>b</td></tr></table>")
    write_text(
        FIXTURES / "chart.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        "<title>Sales by region</title>"
        '<text x="5" y="20">EMEA</text><text x="5" y="40">APAC</text></svg>',
    )


# ------------------------------------------------------------------- office


def make_docx() -> None:
    try:
        import docx
    except ImportError:
        print("  ! python-docx missing -- skipping fixture.docx")
        return

    document = docx.Document()
    document.add_heading("Conversion requirements", level=1)
    document.add_paragraph(
        "This document exercises headings, lists, tables and metadata."
    )
    document.add_heading("Scope", level=2)
    document.add_paragraph("Formats in phase one:", style="List Bullet")
    document.add_paragraph("PDF with a text layer", style="List Bullet")
    document.add_paragraph("Word, PowerPoint, Excel", style="List Bullet")
    document.add_paragraph("Nested detail", style="List Bullet 2")
    document.add_heading("Metrics", level=3)
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    data = [["Metric", "Target", "Actual"], ["Coverage", "80%", "83%"], ["Silent errors", "0", "0"]]
    for r, row in enumerate(data):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    document.add_paragraph("Numbers must survive exactly: 45,300 and 12%.")
    document.core_properties.title = "Conversion requirements"
    document.core_properties.author = "Ana"
    document.save(str(FIXTURES / "spec.docx"))


def make_pptx() -> None:
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError:
        print("  ! python-pptx missing -- skipping fixture.pptx")
        return

    prs = Presentation()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "Phase one"
    body = slide.placeholders[1].text_frame
    body.text = "PDF text layer"
    for line in ("Office formats", "Batch mode"):
        para = body.add_paragraph()
        para.text = line
    slide.notes_slide.notes_text_frame.text = "Remember to mention the OCR gap."

    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "Not covered"
    box = slide2.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "Scanned PDFs need OCR"

    prs.save(str(FIXTURES / "deck.pptx"))


def make_xlsx() -> None:
    try:
        import openpyxl
    except ImportError:
        print("  ! openpyxl missing -- skipping fixture.xlsx (the built-in reader is tested anyway)")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Metrics"
    ws.append(["Metric", "Q1", "Q2"])
    ws.append(["Revenue", 1200, 1344])
    ws.append(["Churn", 0.021, 0.021])
    ws2 = wb.create_sheet("Empty")
    wb.save(str(FIXTURES / "book.xlsx"))


def make_legacy_and_binary() -> None:
    # A fake legacy .doc: OLE magic + a UTF-16LE text run.
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    ole += "Legacy Word document text that should be recovered heuristically.".encode("utf-16-le")
    write_bytes(FIXTURES / "legacy.doc", ole)
    # Unsupported binary.
    write_bytes(FIXTURES / "blob.bin", b"\x7fELF" + bytes(range(256)) * 4)
    # A text file that is really JSON.
    write_text(FIXTURES / "actually.json.txt", '{"looks": "like json", "but": "named .txt"}')
    # A CSV named .txt.
    write_text(FIXTURES / "actually.csv.txt", "a,b,c\n1,2,3\n4,5,6\n")


def make_epub() -> None:
    import zipfile

    path = FIXTURES / "book.epub"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            "<metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
            "<dc:title>Small Book</dc:title><dc:creator>Ana</dc:creator>"
            "<dc:language>en</dc:language></metadata>"
            "<manifest>"
            '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>',
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            "<html><body><h1>Chapter one</h1><p>The first chapter.</p>"
            "<ul><li>point one</li><li>point two</li></ul></body></html>",
        )
        zf.writestr(
            "OEBPS/ch2.xhtml",
            "<html><body><h1>Chapter two</h1><p>The second chapter.</p>"
            "<table><tr><th>k</th><th>v</th></tr><tr><td>a</td><td>1</td></tr></table>"
            "</body></html>",
        )


def make_email() -> None:
    # Real .eml files use CRLF line endings; the parser needs that to find the
    # MIME boundaries, so the fixture must too.
    message = (
        "From: Ana <ana@example.com>\r\n"
        "To: Bo <bo@example.com>\r\n"
        "Subject: =?utf-8?q?Quarterly_review_=E2=80=94_please_read?=\r\n"
        "Date: Mon, 14 Jul 2025 09:12:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=\"BOUND\"\r\n"
        "\r\n"
        "--BOUND\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Hi Bo,\r\n"
        "\r\n"
        "The numbers look good. Attached is the sheet.\r\n"
        "\r\n"
        "Thanks,\r\n"
        "Ana\r\n"
        "--BOUND\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
        "<html><body><p>Hi Bo,</p><p>The numbers look <b>good</b>.</p></body></html>\r\n"
        "--BOUND\r\n"
        "Content-Type: text/csv; name=\"numbers.csv\"\r\n"
        "Content-Disposition: attachment; filename=\"numbers.csv\"\r\n"
        "\r\n"
        "metric,value\r\n"
        "revenue,1344\r\n"
        "--BOUND--\r\n"
    )
    write_bytes(FIXTURES / "message.eml", message.encode("utf-8"))


# ---------------------------------------------------------------------- pdf


def make_pdf() -> None:
    try:
        import fitz
    except ImportError:
        print("  ! PyMuPDF missing -- skipping PDF fixtures")
        return

    # --- a text PDF with a real heading hierarchy (font sizes differ)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 90), "Annual Report", fontsize=24, fontname="helv")
    page.insert_text((72, 120), "Prepared by the analytics team", fontsize=10, fontname="helv")
    page.insert_text((72, 160), "Summary", fontsize=16, fontname="hebo")
    page.insert_text(
        (72, 182),
        "Revenue for the year reached 45,300 units, an increase of 12 percent "
        "over the previous period and ahead of the internal target.",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text((72, 250), "Details", fontsize=16, fontname="hebo")
    page.insert_text((72, 272), "Churn remained flat at 2.1 percent.", fontsize=10, fontname="helv")

    # --- a real table, drawn with lines so find_tables() can see it
    page2 = pdf.new_page()
    page2.insert_text((72, 80), "Metrics", fontsize=16, fontname="hebo")
    x0, y0, col_w, row_h = 72, 100, 140, 24
    rows = [["Metric", "Q1", "Q2"], ["Revenue", "1200", "1344"], ["Churn", "2.1%", "2.1%"]]
    for r in range(len(rows) + 1):
        page2.draw_line(fitz.Point(x0, y0 + r * row_h), fitz.Point(x0 + col_w * 3, y0 + r * row_h))
    for c in range(4):
        page2.draw_line(fitz.Point(x0 + c * col_w, y0), fitz.Point(x0 + c * col_w, y0 + len(rows) * row_h))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page2.insert_text((x0 + c * col_w + 6, y0 + r * row_h + 16), value, fontsize=10, fontname="helv")

    pdf.set_metadata({"title": "Annual Report 2025", "author": "Analytics"})
    pdf.save(str(FIXTURES / "report.pdf"))
    pdf.close()

    # --- an image-only PDF: no text layer at all (the "scanned" case)
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (600, 200), "white")
        draw = ImageDraw.Draw(image)
        draw.text((20, 80), "SCANNED PAGE - no text layer", fill="black")
        png = FIXTURES / "_scan_source.png"
        image.save(png)

        scanned = fitz.open()
        page3 = scanned.new_page(width=600, height=200)
        page3.insert_image(fitz.Rect(0, 0, 600, 200), filename=str(png))
        scanned.save(str(FIXTURES / "scanned.pdf"))
        scanned.close()
        png.unlink()
    except ImportError:
        print("  ! Pillow missing -- skipping scanned.pdf")


def make_image() -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  ! Pillow missing -- skipping diagram.png")
        return
    image = Image.new("RGB", (240, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, 230, 110], outline="black")
    draw.text((30, 55), "diagram with text", fill="black")
    image.save(FIXTURES / "diagram.png")


# ---------------------------------------------------------------------- main


def main() -> int:
    global FIXTURES

    parser = argparse.ArgumentParser(description="Generate the tomd test corpus")
    parser.add_argument("--force", action="store_true", help="regenerate even if files exist")
    parser.add_argument("--dir", default=str(FIXTURES), help="target directory")
    args = parser.parse_args()

    FIXTURES = Path(args.dir)
    if FIXTURES.exists() and not args.force:
        existing = list(FIXTURES.rglob("*"))
        if any(p.is_file() for p in existing):
            print(f"{FIXTURES} already has fixtures; pass --force to regenerate")
            return 0
    FIXTURES.mkdir(parents=True, exist_ok=True)

    builders = [
        make_plain_text, make_markdown, make_csv, make_json, make_misc_text,
        make_notebook, make_html, make_docx, make_pptx, make_xlsx,
        make_legacy_and_binary, make_epub, make_email, make_pdf, make_image,
    ]
    for builder in builders:
        print(f"  {builder.__name__} ...")
        builder()

    files = sorted(p for p in FIXTURES.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"\n{len(files)} fixtures, {total / 1024:.1f} KB in {FIXTURES}")
    for path in files:
        print(f"  {path.relative_to(FIXTURES)!s:34} {path.stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
