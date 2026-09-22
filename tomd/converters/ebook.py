"""Ebook and email converters.

EPUB is a zip of XHTML with a declared reading order, which makes it one of the
*easiest* formats to convert well -- as long as the spine order is respected
instead of just globbing the files alphabetically.  FB2 is XML.  Both reuse the
HTML renderer from :mod:`tomd.converters.html`, so headings, lists and tables
behave identically to the web path.

MOBI/AZW is explicitly *not* faked: it is a proprietary container and the
honest answer is "convert it to EPUB first", which is what the warning says.
"""

from __future__ import annotations

import email
import html as html_mod
import re
import zipfile
import xml.etree.ElementTree as ET
from email import policy
from pathlib import Path

from .. import mdutil as md
from ..converters.html import html_to_markdown
from ..model import Document, Status
from ..registry import registry
from ..security import check_archive, sanitize_filename

_CONTAINER = "META-INF/container.xml"
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _epub_opf_path(zf: zipfile.ZipFile) -> str | None:
    try:
        root = ET.fromstring(zf.read(_CONTAINER))
    except (KeyError, ET.ParseError):
        return None
    for rootfile in root.iter("{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"):
        path = rootfile.get("full-path")
        if path:
            return path
    # some producers omit the namespace
    for rootfile in root.iter():
        if rootfile.tag.endswith("rootfile") and rootfile.get("full-path"):
            return rootfile.get("full-path")
    return None


def _epub_spine(zf: zipfile.ZipFile, opf_path: str) -> tuple[list[str], dict[str, str]]:
    base = str(Path(opf_path).parent).replace("\\", "/")
    base = "" if base == "." else base + "/"
    manifest: dict[str, str] = {}
    spine: list[str] = []
    meta: dict[str, str] = {}

    root = ET.fromstring(zf.read(opf_path))
    for item in root.iter(f"{_OPF_NS}item"):
        item_id, href = item.get("id"), item.get("href")
        if item_id and href:
            manifest[item_id] = base + href.split("#")[0]
    for itemref in root.iter(f"{_OPF_NS}itemref"):
        idref = itemref.get("idref")
        if idref and idref in manifest:
            spine.append(manifest[idref])
    for node in root.iter():
        tag = node.tag
        if tag.startswith(_DC_NS):
            meta[tag[len(_DC_NS):]] = (node.text or "").strip()

    # Reading order can be empty in malformed files: fall back to manifest order.
    if not spine:
        spine = [href for href in manifest.values() if href.lower().endswith((".xhtml", ".html", ".htm"))]
    return spine, meta


@registry.converter("epub", "epub", priority=10, description="EPUB in spine order")
def convert_epub(path: Path, options: dict) -> Document:
    doc = Document()
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        doc.set_status(Status.FAILED)
        doc.warn("epub-invalid", f"not a valid EPUB container ({exc})", "error")
        return doc

    with zf:
        check_archive(path.stat().st_size, sum(i.file_size for i in zf.infolist()))
        opf = _epub_opf_path(zf)
        if not opf:
            doc.set_status(Status.FAILED)
            doc.warn("epub-invalid", "META-INF/container.xml is missing or malformed", "error")
            return doc
        try:
            spine, meta = _epub_spine(zf, opf)
        except Exception as exc:  # noqa: BLE001
            doc.set_status(Status.FAILED)
            doc.warn("epub-invalid", f"could not read the OPF package ({exc})", "error")
            return doc

        for key in ("title", "creator", "language", "publisher", "date", "identifier"):
            if meta.get(key):
                doc.meta[key] = meta[key]
        if meta.get("title"):
            doc.title = meta["title"]

        chapters = 0
        images = 0
        missing: list[str] = []
        heading_seen = 0

        for href in spine:
            try:
                raw = zf.read(href)
            except KeyError:
                missing.append(href)
                continue
            try:
                chapter_html = raw.decode("utf-8")
            except UnicodeDecodeError:
                chapter_html = raw.decode("utf-8", "replace")

            body, stats, _engine = html_to_markdown(
                chapter_html,
                engine=options.get("html_engine", "auto"),
                keep_images=bool(options.get("keep_images", True)),
            )
            heading_seen += stats.get("headings", 0)
            if not body.strip():
                continue
            chapters += 1
            doc.add(f"<!-- chapter: {Path(href).name} -->\n\n{body}")

        # Collect images referenced by the book.
        if options.get("extract_images", True):
            for name in zf.namelist():
                if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                    continue
                try:
                    data = zf.read(name)
                except KeyError:
                    continue
                if len(data) < 2048:
                    continue
                safe = sanitize_filename(Path(name).name)
                from ..model import Asset

                doc.assets.append(Asset(filename=safe, data=data,
                                        media_type=f"image/{Path(safe).suffix.lstrip('.')}",
                                        origin=f"epub: {name}"))
                images += 1
            if images:
                doc.add(md.details(
                    f"Images in the book ({images})",
                    "\n\n".join(md.image(a.filename, f"assets/{a.filename}") for a in doc.assets),
                ))

        doc.meta.update({"chapters": chapters, "spine_items": len(spine), "images": images})
        if missing:
            doc.warn("epub-missing-parts", f"{len(missing)} spine item(s) were listed but absent: "
                                           + ", ".join(missing[:5]))
        if heading_seen == 0 and chapters:
            doc.warn("no-headings", "the book produced no markdown headings; "
                                    "chapter structure may be lost", "info")
        if chapters == 0:
            doc.set_status(Status.EMPTY)
            doc.warn("empty-document", "no chapter content could be extracted", "error")
    return doc


_FB2_NS = "{http://www.gribuser.ru/xml/fictionbook/2.0}"


@registry.converter("fb2", "fb2", priority=10, description="FictionBook 2 XML")
def convert_fb2(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    try:
        root = ET.fromstring(read.text)
    except ET.ParseError as exc:
        doc.set_status(Status.FAILED)
        doc.warn("xml-parse-failed", f"could not parse the FB2 file ({exc})", "error")
        return doc

    def text_of(node: ET.Element) -> str:
        return md.squash("".join(node.itertext()))

    for tag in ("book-title", "author", "annotation"):
        nodes = list(root.iter(f"{_FB2_NS}{tag}")) or list(root.iter(tag))
        for node in nodes[:3]:
            value = text_of(node)
            if value:
                doc.meta[tag.replace("-", "_")] = value

    body_nodes = list(root.iter(f"{_FB2_NS}body")) or list(root.iter("body"))
    sections = 0
    for body in body_nodes:
        for section in body.iter(f"{_FB2_NS}section"):
            title_node = section.find(f"{_FB2_NS}title")
            if title_node is not None:
                title = text_of(title_node)
                if title:
                    doc.add(md.heading(2, title))
                    sections += 1
            for para in section.findall(f"{_FB2_NS}p"):
                text = text_of(para)
                if text:
                    doc.add(text)

    doc.meta["sections"] = sections
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "no text found in the FB2 file", "error")
    return doc


@registry.converter("mobi", "mobi-unsupported", priority=10,
                    description="MOBI/AZW is not supported (explicit failure)")
def convert_mobi(path: Path, options: dict) -> Document:
    doc = Document()
    doc.set_status(Status.EMPTY)
    doc.warn(
        "unsupported-mobi",
        "MOBI/AZW is a proprietary container and is not supported. Convert it to EPUB first "
        "(Calibre: `ebook-convert book.mobi book.epub`), then convert the EPUB.",
        "error",
    )
    doc.add("> **Unsupported format.** Convert this MOBI/AZW file to EPUB first, then re-run.")
    return doc


# ====================================================================== email


@registry.converter("email", "eml", priority=10, description="RFC 822 .eml message")
def convert_email(path: Path, options: dict) -> Document:
    doc = Document()
    raw = path.read_bytes()
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
    except Exception as exc:  # noqa: BLE001
        doc.set_status(Status.FAILED)
        doc.warn("eml-parse-failed", f"could not parse the message ({exc})", "error")
        return doc

    def header(name: str) -> str:
        value = message.get(name)
        return md.squash(str(value)) if value else ""

    subject = header("Subject")
    if subject:
        doc.title = subject
        doc.add(md.heading(1, subject))

    doc.add(md.kv_table([
        ("From", header("From")),
        ("To", header("To")),
        ("Cc", header("Cc")),
        ("Date", header("Date")),
        ("Reply-To", header("Reply-To")),
    ]))

    text_body = ""
    html_body = ""
    attachments: list[tuple[str, bytes, str]] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()

        if disposition == "attachment" or (filename and content_type not in ("text/plain", "text/html")):
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            attachments.append((sanitize_filename(filename or "attachment.bin"), payload, content_type))
            continue

        if content_type == "text/plain" and not text_body:
            try:
                text_body = part.get_content()
            except Exception:
                text_body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        elif content_type == "text/html" and not html_body:
            try:
                html_body = part.get_content()
            except Exception:
                html_body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")

    if text_body.strip():
        doc.add(md.heading(2, "Body"))
        doc.add(md.as_paragraphs(text_body))
        if html_body.strip() and options.get("eml_prefer_html", False):
            body, _stats, _engine = html_to_markdown(html_body, engine=options.get("html_engine", "auto"))
            if body.strip():
                doc.add(md.details("HTML version of the body", body))
    elif html_body.strip():
        body, _stats, _engine = html_to_markdown(html_body, engine=options.get("html_engine", "auto"))
        doc.add(md.heading(2, "Body"))
        doc.add(body)
        doc.warn("html-only-email", "the message had no text/plain part; "
                                    "the body was rendered from HTML", "info")

    if attachments:
        doc.add(md.heading(2, "Attachments"))
        doc.add(md.table([["Filename", "Type", "Bytes"],
                          *[[name, ctype, f"{len(data):,}"] for name, data, ctype in attachments]]))
        doc.warn(
            "attachments-not-converted",
            f"{len(attachments)} attachment(s) were listed but not converted. "
            "Extract them and convert separately for their content.",
            "info",
        )

    doc.meta["attachments"] = len(attachments)
    if doc.is_empty():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-document", "the message has no readable body", "error")
    return doc


@registry.converter("archive", "zip-listing", priority=10, description="zip inventory (not extracted)")
def convert_archive(path: Path, options: dict) -> Document:
    """List a zip's contents instead of silently exploding it into the output.

    Auto-recursing into archives is how converters turn one 10 KB upload into a
    10 GB conversion job, so this converter inventories and stops.
    """
    doc = Document()
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            check_archive(path.stat().st_size, sum(i.file_size for i in infos))
            rows = [[sanitize_filename(i.filename), f"{i.file_size:,}",
                     "dir" if i.is_dir() else sanitize_filename(Path(i.filename).suffix or "-")]
                    for i in infos[:500]]
    except zipfile.BadZipFile as exc:
        doc.set_status(Status.FAILED)
        doc.warn("zip-invalid", f"not a valid zip file ({exc})", "error")
        return doc
    except Exception as exc:  # noqa: BLE001
        doc.set_status(Status.FAILED)
        doc.warn("zip-rejected", f"archive rejected: {exc}", "error")
        return doc

    doc.add(md.heading(1, f"Archive: {path.name}"))
    doc.add(md.kv_table([("Entries", len(infos)),
                         ("Total uncompressed", f"{sum(i.file_size for i in infos):,} bytes"),
                         ("Compressed", f"{path.stat().st_size:,} bytes")]))
    doc.add(md.table([["Entry", "Size", "Type"], *rows]))
    doc.meta["entries"] = len(infos)
    doc.warn(
        "archive-not-extracted",
        "the archive was inventoried, not converted. Point the tool at the extracted "
        "files (or use --recurse-archives) to convert their contents.",
        "info",
    )
    return doc
