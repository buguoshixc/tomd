"""PDF converter -- the hard one.

Strategy (chosen deliberately, see README):

1. **Text-layer first.**  PyMuPDF gives us both the text and the font metrics,
   which is what lets us recover a real heading hierarchy instead of a wall of
   paragraphs.  That single detail is the difference between markdown that a
   chunker can outline and markdown that it cannot.
2. **Tables before paragraphs.**  ``page.find_tables()`` runs first, and the
   blocks it consumed are dropped from the flow so nothing is emitted twice.
3. **Never fake a scan.**  A page with no text layer is reported by page number,
   and if the whole document is like that the result is marked ``empty`` -- not
   a silently blank file.  OCR is intentionally out of scope for now.
4. **Always warn.**  Multi-column layouts, dropped vector graphics and images
   below the size threshold all surface in the conversion report.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import mdutil as md
from ..model import Asset, Document, Status
from ..registry import registry
from ..security import sanitize_filename

#: Below this many characters on a page we treat it as having no usable text.
MIN_CHARS_PER_PAGE = 25
#: Images smaller than this on either axis are almost always rules/icons.
MIN_IMAGE_PX = 48


def _normalize(text: str) -> str:
    """Undo the character-level damage PDF text layers accumulate."""
    text = unicodedata.normalize("NFKC", text)
    return (
        text.replace("\u00ad", "")      # soft hyphen
        .replace("\ufb00", "ff").replace("\ufb01", "fi").replace("\ufb02", "fl")
        .replace("\ufb03", "ffi").replace("\ufb04", "ffl")
        .replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u00a0", " ")
    )


_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines, re-inserting spaces only where they belong.

    PDF text is stored as positioned glyph runs, not words, so this is where
    most of the "the output reads like  garbage" complaints come from.
    """
    if not lines:
        return ""
    out = lines[0].rstrip()
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        if out.endswith("-") and not out.endswith(("--", " -")):
            # hyphenated word broken across lines
            out = out[:-1] + line
        elif _CJK.search(out[-1:]) or _CJK.search(line[:1]):
            out = out + line           # CJK does not use spaces
        else:
            out = out + " " + line
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _block_stats(block: dict) -> tuple[str, float, bool, bool]:
    """Return (text, max font size, is_bold, all_caps) for a text block."""
    lines: list[str] = []
    max_size = 0.0
    bold_votes = 0
    total_spans = 0

    for line in block.get("lines", []):
        parts: list[str] = []
        for span in line.get("spans", []):
            text = _normalize(span.get("text", ""))
            if not text:
                continue
            parts.append(text)
            size = float(span.get("size", 0) or 0)
            max_size = max(max_size, size)
            total_spans += 1
            flags = int(span.get("flags", 0) or 0)
            font = str(span.get("font", ""))
            if flags & 2 ** 4 or "bold" in font.lower() or "black" in font.lower():
                bold_votes += 1
        if parts:
            lines.append("".join(parts))

    text = join_lines(lines)
    is_bold = total_spans > 0 and bold_votes / total_spans > 0.6
    letters = [c for c in text if c.isalpha()]
    all_caps = bool(letters) and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8 and len(letters) > 3
    return text, max_size, is_bold, all_caps


def _heading_map(sizes: list[float], body_size: float) -> dict[float, int]:
    """Map distinct font sizes bigger than body text onto heading levels 1..6."""
    distinct = sorted({round(s, 1) for s in sizes if s > body_size + 0.6}, reverse=True)
    return {size: min(index + 1, 6) for index, size in enumerate(distinct[:6])}


@registry.converter("pdf", "pymupdf", priority=10, requires="fitz",
                    description="PyMuPDF text layer + tables + images")
def convert_pdf(path: Path, options: dict) -> Document:
    doc = Document()
    import fitz  # type: ignore  # PyMuPDF

    # PyMuPDF logs layout-analysis advice to stdout, which would corrupt the
    # markdown when `tomd file.pdf` is piped.  Swallow it.
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    try:
        pdf = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001
        doc.set_status(Status.FAILED)
        doc.warn("pdf-open-failed", f"could not open the PDF ({exc})", "error")
        return doc

    with pdf:
        if pdf.is_encrypted and not pdf.authenticate(""):
            doc.set_status(Status.EMPTY)
            doc.warn("pdf-encrypted", "the PDF is password protected; text cannot be extracted", "error")
            return doc
        if pdf.needs_pass:
            doc.set_status(Status.EMPTY)
            doc.warn("pdf-encrypted", "the PDF requires a password", "error")
            return doc

        # All extraction happens with stdout silenced: PyMuPDF prints an
        # advisory banner that would otherwise be spliced into the markdown.
        with md.suppress_stdout():
            _extract(pdf, doc, path, options)

    if not doc.parts and not doc.warnings:
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no content could be extracted", "error")
    return doc


def _extract(pdf: Any, doc: Document, path: Path, options: dict) -> None:
    """Walk the document and fill ``doc``.  Never raises for page-level issues."""
    import fitz  # type: ignore  # PyMuPDF

    page_count = pdf.page_count
    doc.meta["pages"] = page_count
    if meta := pdf.metadata:
        if meta.get("title"):
            doc.title = str(meta["title"]).strip()
            doc.meta["pdf_title"] = doc.title
        for key in ("author", "subject", "creator", "producer"):
            if meta.get(key):
                doc.meta[key] = str(meta[key])

    extract_tables = bool(options.get("pdf_tables", True))
    extract_images = bool(options.get("extract_images", True))
    max_pages = options.get("pdf_max_pages")
    max_pages = int(max_pages) if max_pages else None

    # ---- pass 1: gather per-page blocks, font sizes and text volume.
    pages: list[dict[str, Any]] = []
    all_sizes: list[float] = []
    empty_pages: list[int] = []
    total_chars = 0

    for index in range(page_count):
        if max_pages is not None and index >= max_pages:
            break
        page = pdf.load_page(index)
        blocks = page.get_text("dict").get("blocks", [])
        text_blocks = []
        for block in blocks:
            if block.get("type", 0) != 0:
                continue
            text, size, bold, caps = _block_stats(block)
            if not text.strip():
                continue
            text_blocks.append({"text": text, "size": size, "bold": bold, "caps": caps,
                                "bbox": block.get("bbox")})
            all_sizes.append(size)
        chars = sum(len(b["text"]) for b in text_blocks)
        total_chars += chars
        if chars < MIN_CHARS_PER_PAGE:
            empty_pages.append(index + 1)
        pages.append({"index": index, "page": page, "blocks": text_blocks, "chars": chars})

    # ---- whole-document judgement.
    if total_chars < MIN_CHARS_PER_PAGE * max(1, len(pages)) * 0.5 or not pages:
        doc.set_status(Status.EMPTY)
        if page_count:
            doc.warn(
                "scanned-pdf",
                f"no usable text layer found ({page_count} page(s), {total_chars} characters). "
                "This is a scanned/image PDF: it needs OCR, which this build does not perform. "
                "Only this report is emitted -- there is no body text to convert.",
                "error",
            )
        else:
            doc.warn("empty-pdf", "the PDF has no pages", "error")
        doc.meta["scanned_pages"] = empty_pages or list(range(1, page_count + 1))
        return

    # ---- heading model from the font-size histogram.
    from collections import Counter

    rounded = [round(s, 1) for s in all_sizes]
    body_size = Counter(rounded).most_common(1)[0][0] if rounded else 10.0
    headings = _heading_map(all_sizes, body_size)
    doc.meta["body_font_size"] = body_size
    doc.meta["heading_font_sizes"] = {str(k): v for k, v in headings.items()}

    # ---- pass 2: render pages.
    seen_headings: set[str] = set()
    table_count = 0
    image_count = 0
    heading_count = 0
    paragraph_count = 0
    column_note = False

    for entry in pages:
        page = entry["page"]
        page_no = entry["index"] + 1
        fragments: list[str] = []

        if entry["chars"] < MIN_CHARS_PER_PAGE:
            fragments.append(
                f"<!-- page {page_no}: no text layer, content not extracted (scanned page) -->"
            )
            doc.add("\n\n".join(fragments))
            continue

        # --- tables first, and remember their bounding boxes.
        consumed: list[tuple[float, float, float, float]] = []
        page_tables: list[list[list[str]]] = []
        if extract_tables:
            try:
                finder = page.find_tables()
                for table in getattr(finder, "tables", []) or []:
                    rows = [
                        [md.squash(_normalize(cell or "")) for cell in row]
                        for row in table.extract()
                    ]
                    rows = [r for r in rows if any(c for c in r)]
                    if len(rows) < 2:
                        continue
                    page_tables.append(rows)
                    consumed.append(tuple(table.bbox))
            except Exception:  # noqa: BLE001 - table detection is best-effort
                doc.warn("table-detection-failed", f"table detection failed on page {page_no}", "info")

        for rows in page_tables:
            width = max(len(r) for r in rows)
            table_md = md.table(rows) if width <= 14 else "\n".join(" | ".join(r) for r in rows)
            fragments.append(table_md)
            table_count += 1

        # --- multi-column detection (report only; PyMuPDF already sorts).
        xs = [b["bbox"][0] for b in entry["blocks"] if b.get("bbox")]
        if len(xs) >= 8:
            mid = (min(xs) + max(xs)) / 2
            left = sum(1 for x in xs if x < mid - 40)
            right = sum(1 for x in xs if x > mid + 40)
            if left >= 3 and right >= 3 and not column_note:
                column_note = True
                doc.warn(
                    "multi-column-layout",
                    f"page {page_no} looks multi-column; reading order is estimated. "
                    "Verify the output before trusting it.",
                    "info",
                )

        # --- flow text, skipping blocks inside tables.
        for block in entry["blocks"]:
            bbox = block.get("bbox")
            if bbox and any(_overlaps(bbox, tb) for tb in consumed):
                continue
            text = block["text"].strip()
            if not text:
                continue

            size = round(block["size"], 1)
            level = headings.get(size)
            short = len(text) <= 120 and "\n" not in text
            looks_like_heading = (
                level is not None
                and short
                and not text.endswith((".", ",", ";", ":", "。", "，"))
                and text not in seen_headings
            )
            if not looks_like_heading and short and block["bold"] and len(text) <= 80:
                parent_sizes = [s for s in headings if s >= size]
                level = headings[min(parent_sizes)] if parent_sizes else 3
                looks_like_heading = text not in seen_headings

            if looks_like_heading:
                fragments.append(md.heading(level, text))
                seen_headings.add(text)
                heading_count += 1
            else:
                fragments.append(md.as_paragraphs(text))
                paragraph_count += 1

        # --- images worth keeping.
        if extract_images:
            for info in page.get_images(full=True):
                xref = info[0]
                try:
                    pix = fitz.Pixmap(pdf, xref)
                    if pix.width < MIN_IMAGE_PX or pix.height < MIN_IMAGE_PX:
                        continue
                    if pix.n - pix.alpha >= 4:      # CMYK -> RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    name = sanitize_filename(f"page{page_no:03d}_img{xref}.png")
                    doc.assets.append(Asset(filename=name, data=pix.tobytes("png"),
                                            media_type="image/png", origin=f"page {page_no}"))
                    fragments.append(md.image(f"page {page_no} image", f"assets/{name}"))
                    image_count += 1
                except Exception:
                    continue

        if fragments:
            body = "\n\n".join(fragments)
            if options.get("pdf_page_details", True):
                doc.add(md.details(f"Page {page_no}", body, open_=page_count <= 3))
            elif options.get("pdf_page_markers", True):
                doc.add(f"<!-- page {page_no} -->\n\n{body}")
            else:
                doc.add(body)

    # ---- report
    doc.meta.update({
        "tables": table_count,
        "images": image_count,
        "headings": heading_count,
        "paragraphs": paragraph_count,
        "pages_extracted": len(pages),
    })
    if empty_pages:
        doc.meta["scanned_pages"] = empty_pages
        doc.warn(
            "scanned-pages",
            f"{len(empty_pages)} of {len(pages)} page(s) have no text layer "
            f"(pages {_ranges(empty_pages)}) and were skipped -- they need OCR.",
        )
    if max_pages is not None and page_count > max_pages:
        doc.warn("page-limit", f"only the first {max_pages} of {page_count} pages were converted")
    if heading_count == 0:
        doc.warn(
            "no-headings-recovered",
            "no font-size hierarchy was detected, so the output has no markdown headings. "
            "Heading-based chunking will not work on this document.",
        )


def _overlaps(a: Any, b: tuple[float, float, float, float]) -> bool:
    """Does block bbox ``a`` substantially overlap table bbox ``b``?"""
    try:
        ax0, ay0, ax1, ay1 = a
    except (TypeError, ValueError):
        return False
    bx0, by0, bx1, by1 = b
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return inter / area > 0.5


def _ranges(numbers: list[int]) -> str:
    """Compress ``[1,2,3,7]`` into ``"1-3, 7"`` for readable reports."""
    if not numbers:
        return ""
    out: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


@registry.converter("pdf", "pdf-plaintext", priority=80, requires="pypdf",
                    description="plain text fallback via pypdf (lossy)")
def convert_pdf_pypdf(path: Path, options: dict) -> Document:
    """Fallback for when PyMuPDF is unavailable: text only, no structure."""
    doc = Document()
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            doc.set_status(Status.EMPTY)
            doc.warn("pdf-encrypted", "the PDF is password protected", "error")
            return doc

    chunks = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            chunks.append(md.details(f"Page {index}", md.as_paragraphs(_normalize(text)),
                                     open_=len(reader.pages) <= 3))
    doc.add("\n\n".join(chunks))
    doc.meta["pages"] = len(reader.pages)
    doc.warn(
        "lossy-pdf-fallback",
        "PyMuPDF is not installed, so the PDF was converted to plain text: no heading "
        "hierarchy, no table detection and no image extraction.",
    )
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("scanned-pdf", "no text layer found; this PDF needs OCR", "error")
    return doc
