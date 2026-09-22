"""Office-document converters.

These formats are the *easy* half of the problem and the reason the tool is
worth building at all: unlike PDF, the structure is stored explicitly, so a
correct converter is lossless rather than heuristic.  ``.docx`` knows which
paragraph is a Heading 2; ``.xlsx`` knows which cell is a date; ``.pptx`` knows
what the slide title is.  We read that, we do not guess.

Engines
-------
* ``python-docx`` / ``python-pptx`` / ``openpyxl`` when installed,
* pure-``zipfile``+``ElementTree`` readers for OOXML and ODF otherwise.

Both engines produce the same markdown; the fallback exists so the tool works on
a bare Python install.  When the fallback is used, a warning says so.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import mdutil as md
from ..model import Asset, Document, Status
from ..registry import registry
from ..security import check_archive, sanitize_filename

MAX_TABLE_COLS = 12


# ============================================================== docx / docm

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_HEADING_RE = re.compile(r"(?:heading|标题|標題| Überschrift|titre|encabezado|заголовок)\s*([1-9])",
                         re.I)


def _docx_style_level(style_name: str) -> int | None:
    """Map a Word style name to a markdown heading level, if it is a heading."""
    if not style_name:
        return None
    match = _HEADING_RE.search(style_name)
    if match:
        return int(match.group(1))
    if style_name.strip().lower() in ("title", "标题"):
        return 1
    if style_name.strip().lower() in ("subtitle", "副标题"):
        return 2
    return None


def _docx_is_list(style_name: str, has_numpr: bool) -> bool:
    if has_numpr:
        return True
    lowered = (style_name or "").lower()
    return lowered.startswith(("list", "列表")) and "paragraph" not in lowered


@registry.converter("docx", "python-docx", priority=10, requires="docx",
                    description="Word (OOXML) via python-docx")
def convert_docx(path: Path, options: dict) -> Document:
    doc = Document()
    import docx  # type: ignore
    from docx.document import Document as _Doc  # type: ignore
    from docx.oxml.text.paragraph import CT_P  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    document = docx.Document(str(path))
    body = document.element.body

    stats = {"headings": 0, "tables": 0, "paragraphs": 0, "lists": 0, "images": 0}
    pending_list: list[tuple[int, str]] = []

    def flush_list() -> None:
        if not pending_list:
            return
        items = [text for _lvl, text in pending_list]
        doc.add(md.bullet_list(items))
        pending_list.clear()

    def iter_blocks(parent: Any):
        for child in parent.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, document)
            elif child.tag.endswith("}tbl"):
                yield Table(child, document)

    for block in iter_blocks(body):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            style = ""
            try:
                style = block.style.name or ""
            except Exception:
                style = ""

            if not text:
                continue

            level = _docx_style_level(style)
            numpr = block._p.find(f"{_W}pPr/{_W}numPr") is not None

            if level is not None:
                flush_list()
                doc.add(md.heading(level, text))
                stats["headings"] += 1
                continue

            if _docx_is_list(style, numpr):
                indent_level = 0
                if numpr:
                    ilvl = block._p.find(f"{_W}pPr/{_W}numPr/{_W}ilvl")
                    if ilvl is not None:
                        try:
                            indent_level = int(ilvl.get(f"{_W}val", "0"))
                        except (TypeError, ValueError):
                            indent_level = 0
                pending_list.append((indent_level, ("  " * indent_level) + text))
                stats["lists"] += 1
                continue

            flush_list()
            # Inline formatting is intentionally flattened: markdown emphasis
            # round-trips badly through most downstream tools, and the text is
            # what matters.
            doc.add(text)
            stats["paragraphs"] += 1
        else:  # Table
            flush_list()
            rows = []
            for row in block.rows:
                cells = []
                for cell in row.cells:
                    cells.append(md.squash(cell.text))
                if any(cells):
                    rows.append(cells)
            if rows:
                if max(len(r) for r in rows) > MAX_TABLE_COLS:
                    doc.warn(
                        "wide-table",
                        f"a table has {max(len(r) for r in rows)} columns; rendered as a "
                        "row-per-line block instead of a pipe table",
                        "info",
                    )
                    doc.add("\n".join(" | ".join(r) for r in rows))
                else:
                    doc.add(md.table(rows))
                stats["tables"] += 1

    flush_list()

    # Embedded images -> assets/
    for rel in getattr(document.part, "rels", {}).values():
        if "image" in (rel.reltype or ""):
            try:
                blob = rel.target_part.blob
                name = sanitize_filename(Path(rel.target_ref).name or "image.png")
                doc.assets.append(Asset(filename=name, data=blob,
                                        media_type=f"image/{Path(name).suffix.lstrip('.') or 'png'}",
                                        origin="docx embedded image"))
                stats["images"] += 1
            except Exception:
                continue
    if stats["images"]:
        doc.add(md.details(
            f"Embedded images ({stats['images']})",
            "\n\n".join(md.image(a.filename, f"assets/{a.filename}") for a in doc.assets),
        ))

    core = getattr(document, "core_properties", None)
    if core is not None:
        for label, value in (
            ("Title", core.title), ("Author", core.author),
            ("Subject", core.subject), ("Created", core.created),
        ):
            if value:
                doc.meta[label.lower()] = str(value)
        if core.title:
            doc.title = str(core.title)

    doc.meta.update(stats)
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "the document contains no text", "error")
    return doc


# ------------------------------------------------------------------ fallback


def _ooxml_text(path: Path, part: str, options: dict) -> Document:
    """Read OOXML with nothing but the standard library."""
    doc = Document()

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        doc.set_status(Status.FAILED)
        doc.warn("package-invalid", f"not a readable OOXML package ({exc})", "error")
        return doc

    with zf:
        infos = zf.infolist()
        check_archive(path.stat().st_size, sum(i.file_size for i in infos))
        if part not in zf.namelist():
            doc.set_status(Status.FAILED)
            doc.warn("missing-part", f"'{part}' is missing from the package", "error")
            return doc
        xml = zf.read(part)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        doc.set_status(Status.FAILED)
        doc.warn("xml-parse-failed", f"could not parse {part} ({exc})", "error")
        return doc

    stats = {"paragraphs": 0, "headings": 0, "tables": 0}
    for para in root.iter(f"{_W}p"):
        text = "".join(node.text or "" for node in para.iter(f"{_W}t")).strip()
        if not text:
            continue
        style = para.find(f"{_W}pPr/{_W}pStyle")
        style_name = style.get(f"{_W}val", "") if style is not None else ""
        level = _docx_style_level(style_name)
        if level is None and style_name.lower().startswith("heading"):
            digits = re.findall(r"\d+", style_name)
            level = int(digits[0]) if digits else 2
        if level:
            doc.add(md.heading(level, text))
            stats["headings"] += 1
        else:
            doc.add(text)
            stats["paragraphs"] += 1

    for table in root.iter(f"{_W}tbl"):
        rows = []
        for row in table.iter(f"{_W}tr"):
            cells = [
                md.squash("".join(node.text or "" for node in cell.iter(f"{_W}t")))
                for cell in row.iter(f"{_W}tc")
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            doc.add(md.table(rows) if max(len(r) for r in rows) <= MAX_TABLE_COLS
                    else "\n".join(" | ".join(r) for r in rows))
            stats["tables"] += 1

    doc.meta.update(stats)
    doc.warn("fallback-converter", "converted with the built-in OOXML reader "
                                   "(install python-docx for metadata, images and fidelity)", "info")
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "the document contains no text", "error")
    return doc


@registry.converter("docx", "ooxml-stdlib", priority=90, description="Word via built-in reader")
def convert_docx_fallback(path: Path, options: dict) -> Document:
    return _ooxml_text(path, "word/document.xml", options)


# ====================================================================== pptx


@registry.converter("pptx", "python-pptx", priority=10, requires="pptx",
                    description="PowerPoint via python-pptx")
def convert_pptx(path: Path, options: dict) -> Document:
    doc = Document()
    from pptx import Presentation  # type: ignore
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # type: ignore

    prs = Presentation(str(path))
    include_notes = bool(options.get("pptx_notes", True))
    include_tables = bool(options.get("pptx_tables", True))
    slide_count = 0
    image_count = 0

    for index, slide in enumerate(prs.slides, start=1):
        slide_count += 1
        title = ""
        try:
            if slide.shapes.title is not None:
                title = md.squash(slide.shapes.title.text)
        except Exception:
            title = ""

        doc.add(md.heading(2, title or f"Slide {index}"))

        for shape in slide.shapes:
            if shape == getattr(slide.shapes, "title", None):
                continue
            if getattr(shape, "has_table", False) and include_tables:
                rows = []
                for row in shape.table.rows:
                    rows.append([md.squash(cell.text) for cell in row.cells])
                rows = [r for r in rows if any(r)]
                if rows:
                    doc.add(md.table(rows) if max(len(r) for r in rows) <= MAX_TABLE_COLS
                            else "\n".join(" | ".join(r) for r in rows))
                continue
            if getattr(shape, "has_text_frame", False):
                lines = []
                for para in shape.text_frame.paragraphs:
                    text = md.squash("".join(run.text for run in para.runs) or para.text)
                    if not text:
                        continue
                    depth = para.level or 0
                    lines.append(("  " * depth) + text)
                if lines:
                    levels = {len(ln) - len(ln.lstrip()) for ln in lines}
                    doc.add(md.bullet_list(lines) if len(lines) > 1 or 0 in levels
                            else lines[0].strip())
                continue
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    ext = image.ext or "png"
                    name = sanitize_filename(f"slide{index:03d}_image{image_count + 1}.{ext}")
                    doc.assets.append(Asset(filename=name, data=image.blob,
                                            media_type=image.content_type or f"image/{ext}",
                                            origin=f"slide {index}"))
                    image_count += 1
                    doc.add(md.image(f"slide {index} image", f"assets/{name}"))
                except Exception:
                    continue
            elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for sub in shape.shapes:
                    if getattr(sub, "has_text_frame", False) and sub.text_frame.text.strip():
                        doc.add(md.squash(sub.text_frame.text))

        if include_notes and slide.has_notes_slide:
            notes = md.squash(slide.notes_slide.notes_text_frame.text)
            if notes:
                doc.add(md.blockquote(f"**Speaker notes:** {notes}"))

    if slide_count and image_count:
        doc.meta["images"] = image_count
    doc.meta.update({"slides": slide_count, "images": image_count})
    if not slide_count:
        doc.set_status(Status.EMPTY)
        doc.warn("no-slides", "the presentation has no slides", "error")
    return doc


@registry.converter("pptx", "ooxml-stdlib", priority=90, description="PowerPoint via built-in reader")
def convert_pptx_fallback(path: Path, options: dict) -> Document:
    """Slides via raw XML: group runs by ``a:p`` inside ``p:sp``."""
    doc = Document()
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

    with zipfile.ZipFile(path) as zf:
        check_archive(path.stat().st_size, sum(i.file_size for i in zf.infolist()))
        names = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        if not names:
            doc.set_status(Status.EMPTY)
            doc.warn("no-slides", "no slide parts found in the package", "error")
            return doc
        for index, name in enumerate(names, start=1):
            root = ET.fromstring(zf.read(name))
            lines: list[str] = []
            first = True
            for sp in root.iter(f"{P}sp"):
                texts = [
                    "".join(node.text or "" for node in para.iter(f"{A}t")).strip()
                    for para in sp.iter(f"{A}p")
                ]
                texts = [t for t in texts if t]
                if not texts:
                    continue
                if first:
                    doc.add(md.heading(2, " ".join(texts)))
                    first = False
                else:
                    doc.add(md.bullet_list(texts))

    doc.warn("fallback-converter", "converted with the built-in PPTX reader "
                                   "(install python-pptx for notes, tables and images)", "info")
    return doc


# ====================================================================== xlsx

_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out = []
    for si in root.iter(f"{_X}si"):
        out.append("".join(node.text or "" for node in si.iter(f"{_X}t")))
    return out


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref.upper())
    if not letters:
        return 0
    index = 0
    for ch in letters.group(1):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _xlsx_sheets_stdlib(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Return ``[(sheet_name, rows)]`` using only the standard library."""
    sheets: list[tuple[str, list[list[str]]]] = []
    with zipfile.ZipFile(path) as zf:
        check_archive(path.stat().st_size, sum(i.file_size for i in zf.infolist()))
        shared = _shared_strings(zf)
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels: dict[str, str] = {}
        try:
            rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rel_root:
                rels[rel.get("Id", "")] = rel.get("Target", "")
        except KeyError:
            pass

        R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        for sheet in workbook.iter(f"{_X}sheet"):
            name = sheet.get("name", "Sheet")
            target = rels.get(sheet.get(f"{R}id", ""), "")
            if not target:
                continue
            part = target.lstrip("/")
            if not part.startswith("xl/"):
                part = "xl/" + part
            try:
                root = ET.fromstring(zf.read(part))
            except (KeyError, ET.ParseError):
                continue
            rows: list[list[str]] = []
            for row in root.iter(f"{_X}row"):
                cells: dict[int, str] = {}
                for cell in row.iter(f"{_X}c"):
                    ref = cell.get("r", "")
                    ctype = cell.get("t", "")
                    value_node = cell.find(f"{_X}v")
                    inline = cell.find(f"{_X}is")
                    if ctype == "s" and value_node is not None:
                        try:
                            text = shared[int(value_node.text or "0")]
                        except (ValueError, IndexError):
                            text = ""
                    elif ctype == "inlineStr" and inline is not None:
                        text = "".join(n.text or "" for n in inline.iter(f"{_X}t"))
                    elif value_node is not None:
                        text = value_node.text or ""
                    else:
                        text = ""
                    cells[_col_index(ref)] = text
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
            sheets.append((name, rows))
    return sheets


@registry.converter("xlsx", "openpyxl", priority=10, requires="openpyxl",
                    description="Excel (OOXML) via openpyxl")
def convert_xlsx(path: Path, options: dict) -> Document:
    doc = Document()
    import openpyxl  # type: ignore

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    max_rows = int(options.get("xlsx_max_rows", 200))
    stats = {"sheets": 0, "rows": 0}

    try:
        for ws in wb.worksheets:
            stats["sheets"] += 1
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                if row is None:
                    continue
                cells = ["" if c is None else (c.isoformat() if hasattr(c, "isoformat") else str(c))
                         for c in row]
                if any(c.strip() for c in cells):
                    rows.append(cells)
                if len(rows) > max_rows:
                    break

            doc.add(md.heading(2, ws.title))
            if not rows:
                doc.add("*(empty sheet)*")
                continue
            if len(rows) > max_rows:
                doc.warn(
                    "sheet-truncated",
                    f"sheet '{ws.title}' was truncated at {max_rows} rows "
                    "(raise --xlsx-max-rows to include more)",
                )
            width = max(len(r) for r in rows)
            doc.add(md.kv_table([("Rows", len(rows)), ("Columns", width)], key_header="Sheet"))
            doc.add(md.table(rows) if width <= MAX_TABLE_COLS
                    else "\n".join(" | ".join(r) for r in rows))
            stats["rows"] += len(rows)
    finally:
        wb.close()

    doc.meta.update(stats)
    if not stats["sheets"]:
        doc.set_status(Status.EMPTY)
        doc.warn("no-sheets", "the workbook has no sheets", "error")
    return doc


@registry.converter("xlsx", "xlsx-stdlib", priority=90, description="Excel via built-in reader")
def convert_xlsx_fallback(path: Path, options: dict) -> Document:
    doc = Document()
    max_rows = int(options.get("xlsx_max_rows", 200))
    stats = {"sheets": 0, "rows": 0}
    try:
        sheets = _xlsx_sheets_stdlib(path)
    except zipfile.BadZipFile as exc:
        doc.set_status(Status.FAILED)
        doc.warn("package-invalid", f"not a readable XLSX package ({exc})", "error")
        return doc
    except Exception as exc:  # noqa: BLE001
        doc.set_status(Status.FAILED)
        doc.warn("xlsx-read-failed", f"could not read the workbook ({exc})", "error")
        return doc

    if not sheets:
        doc.set_status(Status.FAILED)
        doc.warn(
            "no-sheets-found",
            "the package contains no readable worksheet (or it is not an XLSX file)",
            "error",
        )
        return doc

    for name, rows in sheets:
        stats["sheets"] += 1
        doc.add(md.heading(2, name))
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            doc.add("*(empty sheet)*")
            continue
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        width = max(len(r) for r in rows)
        doc.add(md.table(rows) if width <= MAX_TABLE_COLS
                else "\n".join(" | ".join(r) for r in rows))
        stats["rows"] += len(rows)
        if truncated:
            doc.warn("sheet-truncated", f"sheet '{name}' truncated at {max_rows} rows")

    doc.meta.update(stats)
    doc.warn("fallback-converter", "converted with the built-in XLSX reader "
                                   "(install openpyxl to keep formulas/types)", "info")
    return doc


# ======================================================================= odf

_O = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


@registry.converter("odf", "odf", priority=10, description="OpenDocument (odt/ods/odp)")
def convert_odf(path: Path, options: dict) -> Document:
    doc = Document()
    with zipfile.ZipFile(path) as zf:
        check_archive(path.stat().st_size, sum(i.file_size for i in zf.infolist()))
        if "content.xml" not in zf.namelist():
            doc.set_status(Status.FAILED)
            doc.warn("missing-part", "content.xml is missing; not an OpenDocument file", "error")
            return doc
        root = ET.fromstring(zf.read("content.xml"))
        mimetype = ""
        if "mimetype" in zf.namelist():
            mimetype = zf.read("mimetype").decode("ascii", "replace").strip()

    doc.meta["mimetype"] = mimetype
    stats = {"paragraphs": 0, "headings": 0, "tables": 0}

    for element in root.iter():
        tag = element.tag
        if tag == f"{_O}h":
            level = int(element.get(f"{_O}outline-level", "1") or 1)
            text = md.squash("".join(element.itertext()))
            if text:
                doc.add(md.heading(level, text))
                stats["headings"] += 1
        elif tag == f"{_O}p":
            # skip paragraphs that are inside a table cell (handled below)
            text = md.squash("".join(element.itertext()))
            if text and not text.startswith(("•", "-")):
                doc.add(text)
                stats["paragraphs"] += 1
        elif tag == f"{_T}table":
            rows = []
            for row in element.iter(f"{_T}table-row"):
                cells = [md.squash("".join(cell.itertext()))
                         for cell in row.findall(f"{_T}table-cell")]
                if any(cells):
                    rows.append(cells)
            if rows:
                doc.add(md.table(rows) if max(len(r) for r in rows) <= MAX_TABLE_COLS
                        else "\n".join(" | ".join(r) for r in rows))
                stats["tables"] += 1

    doc.meta.update(stats)
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text content found", "error")
    return doc


# ======================================================================= rtf


_RTF_DEST_SKIP = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata",
    "datastore", "latentstyles", "listtable", "listoverridetable", "rsidtbl",
    "generator", "filetbl", "xmlnstbl",
}


def strip_rtf(text: str) -> str:
    """Best-effort RTF -> plain text, ignoring embedded objects and pictures."""
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), text)

    out: list[str] = []
    stack: list[bool] = []
    skipping = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\":
            match = re.match(r"\\([a-zA-Z]+)(-?\d+)?[ ]?", text[i:])
            if match:
                word, arg = match.group(1), match.group(2)
                i += match.end()
                if word in _RTF_DEST_SKIP:
                    stack.append(skipping)
                    skipping = True
                elif word in ("par", "line", "sect", "page"):
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word in ("u", "uc") and word == "u" and arg is not None:
                    try:
                        out.append(chr(int(arg) % 65536))
                    except ValueError:
                        pass
                elif word == "bullet":
                    out.append("- ")
                continue
            if i + 1 < length:
                nxt = text[i + 1]
                out.append({"\\": "\\", "{": "{", "}": "}", "~": "\u00a0", "_": "-", "*": ""}.get(nxt, nxt))
                i += 2
                continue
            i += 1
            continue
        if ch == "{":
            stack.append(skipping)
            i += 1
            continue
        if ch == "}":
            if stack:
                skipping = stack.pop()
            i += 1
            continue
        if not skipping:
            out.append(ch)
        i += 1

    result = "".join(out)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


@registry.converter("rtf", "rtf", priority=20, description="RTF basic text extraction")
def convert_rtf(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    text = strip_rtf(read.text)
    if not text.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text recovered from the RTF file", "error")
        return doc
    doc.add(md.as_paragraphs(text))
    doc.warn(
        "rtf-lossy",
        "RTF was reduced to text: headings, tables and inline formatting are not recovered. "
        "Install LibreOffice and use --prefer-soffice for a faithful conversion.",
        "info",
    )
    return doc


# ============================================================ legacy binary


def _extract_ole_text(path: Path, options: dict) -> tuple[str, int]:
    """Pull language-plausible text runs out of an OLE compound file."""
    from ..security import check_size

    check_size(path, options.get("max_bytes"))
    raw = path.read_bytes()

    runs: list[str] = []
    # Word 97+ stores text as UTF-16LE runs; PowerPoint/Excel mix in cp1252.
    for encoding, min_run in (("utf-16-le", 24), ("cp1252", 40)):
        try:
            decoded = raw.decode(encoding, "ignore")
        except Exception:
            continue
        for match in re.finditer(r"[^\x00-\x08\x0b\x0c\x0e-\x1f]{%d,}" % min_run, decoded):
            chunk = match.group(0)
            letters = sum(ch.isprintable() and not ch.isspace() for ch in chunk)
            if letters / max(1, len(chunk)) < 0.85:
                continue
            cleaned = md.squash(chunk)
            if len(cleaned) >= min_run:
                runs.append(cleaned)
    # de-duplicate while keeping order
    seen: set[str] = set()
    unique = [r for r in runs if not (r in seen or seen.add(r))]
    return "\n\n".join(unique), len(unique)


@registry.converter("doc_legacy", "ole-doc", priority=20, description="legacy .doc text recovery")
def convert_doc_legacy(path: Path, options: dict) -> Document:
    doc = Document()
    text, runs = _extract_ole_text(path, options)
    if not text.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text recovered from the legacy .doc file", "error")
        return doc
    doc.add(text)
    doc.meta["runs"] = runs
    doc.warn(
        "legacy-format-lossy",
        "legacy .doc has no reliable structure: text was recovered heuristically and "
        "reading order is not guaranteed. Convert to .docx (Word/LibreOffice) for fidelity.",
    )
    return doc


@registry.converter("ppt_legacy", "ole-ppt", priority=20, description="legacy .ppt text recovery")
def convert_ppt_legacy(path: Path, options: dict) -> Document:
    doc = Document()
    text, runs = _extract_ole_text(path, options)
    if not text.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text recovered from the legacy .ppt file", "error")
        return doc
    doc.add(text)
    doc.meta["runs"] = runs
    doc.warn("legacy-format-lossy",
             "legacy .ppt text was recovered heuristically; slide boundaries are not preserved.")
    return doc


@registry.converter("xls_legacy", "ole-xls", priority=20, description="legacy .xls text recovery")
def convert_xls_legacy(path: Path, options: dict) -> Document:
    doc = Document()
    text, runs = _extract_ole_text(path, options)
    if not text.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text recovered from the legacy .xls file", "error")
        return doc
    doc.add(text)
    doc.warn(
        "legacy-format-lossy",
        "legacy .xls cell structure is not recovered (only text runs). "
        "Install xlrd or re-save as .xlsx for a real table conversion.",
    )
    return doc


@registry.converter("msg_legacy", "ole-msg", priority=20, description="Outlook .msg text recovery")
def convert_msg_legacy(path: Path, options: dict) -> Document:
    doc = Document()
    text, _runs = _extract_ole_text(path, options)
    if not text.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text recovered from the .msg file", "error")
        return doc
    doc.add(text)
    doc.warn("legacy-format-lossy",
             "Outlook .msg was reduced to text runs; attachments and recipients are not parsed.")
    return doc
