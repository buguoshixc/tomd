"""HTML-family converters: ``html`` and ``svg``.

Two engines, chosen automatically:

* ``markdownify`` / ``html2text`` when installed (better edge-case coverage),
* a built-in :class:`html.parser.HTMLParser` renderer otherwise.

The built-in one exists so that the tool is useful on a machine with nothing
installed -- and because a bounded, auditable renderer is easier to reason about
than a general-purpose HTML-to-markdown library when the input is untrusted.

Both engines strip navigation/chrome, keep heading structure (this is the part
naive converters get wrong: no ``#`` means no outline for downstream chunking),
and render tables as pipe tables.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from .. import mdutil as md
from ..model import Document, Status
from ..registry import registry

#: Tags whose entire subtree is chrome/noise rather than content.  ``title`` is
#: in here because it lives in ``<head>``: rendering it inline would duplicate
#: the document title (the converter surfaces it as metadata instead).
DROP_SUBTREE = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "form",
    "title", "meta", "link", "base",
}
#: Tags that are usually page chrome: buffered, then kept or dropped depending
#: on whether they actually carry content (a heading, or any emitted block).
DROP_TAG_KEEP_TEXT = {"nav", "footer", "header", "aside"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "blockquote", "pre", "figure",
    "figcaption", "dl", "dt", "dd", "hr", "form", "fieldset",
}
HEADINGS = {f"h{i}": i for i in range(1, 7)}
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "track", "wbr", "col", "area", "base"}
#: Unlinked chrome text shorter than this is treated as decoration (a logo
#: caption, a copyright strip) rather than content worth keeping.
CHROME_SHORT_TEXT = 24


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class _ChromeScanner(HTMLParser):
    """Pre-pass: which ``<header>``/``<nav>``/``<footer>`` carry real content?

    Site chrome and a document's own header look identical in markup.  The
    discriminator is content-driven:

    * it holds a heading -> it is the document's header, keep it;
    * everything inside it is links (a menu, a breadcrumb, a footer nav) -> drop;
    * otherwise -> keep, because dropping real prose is the worse failure.

    That cannot be answered while streaming an element, so this cheap pre-pass
    walks the same document, gives each chrome element a preorder index, and
    records the ones worth keeping.  The render pass then drops the rest
    immediately -- no buffering, no interference with inline state.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.keep: set[int] = set()
        self._stack: list[list] = []  # [index, tag, has_heading, prose_chars, links]
        self._anchor_depth = 0
        self._counter = 0
        self._current: list | None = None

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        if tag in DROP_TAG_KEEP_TEXT:
            # Only the *outermost* chrome is a candidate: a <nav> inside a
            # <header> is part of the header's decision, not a second one, and
            # counting it twice would let the inner element's score leak out.
            if self._stack:
                return
            self._counter += 1
            self._stack.append([self._counter, tag, False, 0, 0])
            return
        if not self._stack:
            return
        frame = self._stack[-1]
        if tag in HEADINGS:
            frame[2] = True
            if tag != "h1":
                # Only a first-level heading marks a real document header; an
                # <h3> inside a nav is a menu group label.
                frame[3] += 1
        elif tag == "a":
            self._anchor_depth += 1
            frame[4] += 1

    def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
        if tag == "a":
            self._anchor_depth = max(0, self._anchor_depth - 1)
            return
        if not self._stack:
            return
        if tag == self._stack[-1][1]:
            frame = self._stack.pop()
            if self._worth_keeping(frame):
                self.keep.add(frame[0])

    def handle_data(self, data: str) -> None:
        text = _clean_ws(data).strip()
        if not text or not self._stack:
            return
        frame = self._stack[-1]
        # Text inside an <a> is a link label, not prose.
        if self._anchor_depth:
            frame[4] += max(1, len(text) // 8)
        else:
            frame[3] += len(text)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        """``<img/>``: HTMLParser routes self-closing tags here, not to starttag."""
        self.handle_starttag(tag, attrs)

    @staticmethod
    def _worth_keeping(frame: list) -> bool:
        """Keep chrome that carries content; drop bare link bars and legal lines.

        Erring towards *keeping* is deliberate: a page banner leaking into the
        output is a cosmetic annoyance, whereas dropping the ``<h1>`` a page
        keeps in its ``<header>`` destroys the document's title.

        The one content that is still dropped is the unlinked one-liner -- a
        logo, a copyright strip -- whose whole text is shorter than
        :data:`CHROME_SHORT_TEXT`.  Longer unlinked prose is kept because it may
        be the only place a document says something.
        """
        _index, tag, has_heading, prose_chars, links = frame
        if has_heading or prose_chars >= CHROME_SHORT_TEXT:
            return True
        if prose_chars > 0:
            return tag == "header" and links > 0
        # No prose at all: a menu bar, a logo, a breadcrumb trail.
        return False

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        """``<img/>`` and friends: HTMLParser does not route these to starttag."""
        self.handle_starttag(tag, attrs)


class _Renderer(HTMLParser):
    """Tolerant HTML -> markdown renderer.

    Emits block-level markdown fragments into ``self.out``; unclosed or
    mismatched tags are handled by tracking depth per tag name.
    """

    def __init__(self, *, keep_links: bool = True, keep_images: bool = True,
                 base_url: str = "", drop_chrome: bool = True,
                 keep_chrome: set[int] | None = None, chrome_counter: int = 0) -> None:
        super().__init__(convert_charrefs=True)
        self.keep_links = keep_links
        self.keep_images = keep_images
        self.base_url = base_url
        self.drop_chrome = drop_chrome
        self.keep_chrome = keep_chrome if keep_chrome is not None else set()
        self._chrome_counter = chrome_counter
        self._chrome_open = False

        self.out: list[str] = []
        self.buf: list[str] = []
        self._drop_depth = 0
        self._stack: list[str] = []

        self._lists: list[dict] = []
        self._in_pre = 0
        self._pre_lang = ""
        self._pre_buf: list[str] = []
        self._last_pre_class = ""
        self._code_depth = 0
        self._code_in_pre: list[bool] = []
        self._code_buf: list[str] = []
        self._link_stack: list[bool] = []
        self._href_stack: list[str] = []
        self._table_stack: list[dict] = []
        self._quote_depth = 0
        self._headings = 0
        self._chrome: _Chrome | None = None
        self._images = 0
        self._tables = 0
        self._links = 0

    # ------------------------------------------------------------- plumbing

    def _flush(self) -> None:
        text = _clean_ws("".join(self.buf)).strip()
        self.buf.clear()
        if text:
            self.out.append(text)

    def _flush_list(self) -> None:
        """Close the innermost open list, if any.

        Called when a block element interrupts a list -- an inline ``<li>`` that
        opens a nested ``<ul>`` must not be emitted after its own children.
        """
        if not self._lists:
            return
        # The depth of this list is measured *before* popping it: after the pop
        # an outer list has an empty stack, and using that as the indent would
        # silently flatten every nested list.
        depth = len(self._lists) - 1
        bundle = self._lists.pop()
        if bundle["current"] is not None:
            bundle["items"].append(bundle["current"])

        def normalize(item: list[str]) -> str:
            # Nested-list structure is carried by newlines *and* by the leading
            # indentation, so protect the indentation from the whitespace
            # collapser -- otherwise "    - child" collapses to " - child" and
            # the nesting is silently lost.
            lines = "".join(item).split("\n")
            protected = []
            for line in lines:
                stripped = line.lstrip(" ")
                depth = len(line) - len(stripped)
                protected.append("\x01" * depth + stripped)
            joined = "\x00".join(protected)
            collapsed = _clean_ws(joined)
            collapsed = collapsed.replace("\x00", "\n").replace("\x01", " ")
            return collapsed.rstrip()

        items = [normalize(i) for i in bundle["items"] if "".join(i).strip()]
        if not items:
            return

        # Render this list, then indent it by its nesting depth.  Two spaces per
        # level is the CommonMark continuation indent for a list marker.
        body = md.numbered_list(items) if bundle["ordered"] else md.bullet_list(items)
        indent = "  " * len(self._lists)

        if not indent:
            self._emit_block(body)
            return

        # A nested list belongs to the parent's last item: attach it there so the
        # parent's own text comes first and the children follow it.
        indented = "\n".join(indent + line for line in body.split("\n"))
        if self._lists and self._lists[-1]["current"] is not None:
            parent = self._lists[-1]["current"]
            parent[-1] = parent[-1].rstrip()
            parent.append("\n" + indented)
            return
        self._emit_block(indented)

    def _emit_block(self, fragment: str) -> None:
        """Append a block-level fragment verbatim.

        Used for anything containing newlines (fences, nested lists, tables):
        it must not pass through the inline whitespace collapser, which would
        flatten a code block into a single line.
        """
        self._flush()
        if fragment.strip():
            self._target().append(fragment.strip("\n"))

    def _target(self) -> list[str]:
        """Where block fragments go (kept for symmetry with the inline buffer)."""
        return self.out

    def _emit(self, fragment: str) -> None:
        self._flush()
        if fragment.strip():
            self._target().append(fragment.strip())

    def _inline(self) -> str:
        """Current inline buffer as markdown text (for table cells)."""
        return _clean_ws("".join(self.buf)).strip()

    # -------------------------------------------------------------- handlers

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrib = {k.lower(): (v or "") for k, v in attrs}
        if self._drop_depth:
            if tag not in VOID_TAGS:
                self._drop_depth += 1
            return

        # Chrome candidates come first: whether <nav>/<header>/<footer> is page
        # chrome or the document's own content was decided by the pre-pass.
        # Only outermost chrome carries an index, exactly as in the scanner.
        if self.drop_chrome and tag in DROP_TAG_KEEP_TEXT and not self._chrome_open:
            self._chrome_counter += 1
            if self._chrome_counter not in self.keep_chrome:
                if tag not in VOID_TAGS:
                    self._drop_depth = 1
                return
            if tag not in VOID_TAGS:
                self._chrome_open = True

        # Scripts/styles are dropped even inside kept chrome: they are never
        # content, and a banner's inline script must not reach the markdown.
        if self.drop_chrome and tag in DROP_SUBTREE:
            if tag not in VOID_TAGS:
                self._drop_depth = 1
            return

        if tag not in VOID_TAGS:
            self._stack.append(tag)

        if tag in HEADINGS:
            self._flush_list()
            self._flush()
            self._stack.append(f"__h{tag}")
        elif tag == "pre":
            # Must precede the generic block-tag branch: "pre" is a block tag,
            # and flushing here would run before the fence state is set up.
            self._flush_list()
            self._flush()
            self._in_pre += 1
            self._pre_buf = []
            self._last_pre_class = attrib.get("class", "") or attrib.get("data-lang", "")
            self._pre_lang = attrib.get("data-lang", "") or ""
        elif tag == "code" and self._in_pre:
            # <pre><code class="language-x"> is the common way to name a fence
            # language; remember it for the </pre> handler.
            if not self._pre_lang:
                self._last_pre_class = attrib.get("class", "") or self._last_pre_class
        elif tag == "code" and not self._in_pre:
            self._code_depth += 1
            self._code_in_pre.append(False)
            self._code_buf = []
            self.buf.append("`")
        elif tag == "hr":
            # Checked before the generic block-tag branch: "hr" is a block tag
            # too, and would otherwise be flushed away with no marker emitted.
            self._flush_list()
            self._emit_block("---")
        elif tag == "p" or tag in BLOCK_TAGS:
            self._flush_list()
            self._flush()
        elif tag == "br":
            self.buf.append("  \n")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag in ("del", "s", "strike"):
            self.buf.append("~~")
        elif tag == "a":
            href = attrib.get("href", "")
            usable = self.keep_links and bool(href) and not href.lower().startswith(("javascript:", "data:"))
            self._link_stack.append(usable)
            self._href_stack.append(href if usable else "")
            if usable:
                self.buf.append("[")
        elif tag == "img":
            if self.keep_images:
                src = urljoin(self.base_url, attrib.get("src", "")) if self.base_url else attrib.get("src", "")
                if src:
                    self.buf.append(md.image(attrib.get("alt", ""), src, attrib.get("title", "")))
                    self._images += 1
        elif tag in ("ul", "ol"):
            self._flush()
            self._lists.append({"ordered": tag == "ol", "items": [], "current": None})
        elif tag == "li":
            self._flush()
            if self._lists:
                if self._lists[-1]["current"] is not None:
                    self._lists[-1]["items"].append(self._lists[-1]["current"])
                self._lists[-1]["current"] = []
        elif tag == "blockquote":
            self._flush_list()
            self._flush()
            self._quote_depth += 1
        elif tag == "table":
            self._flush_list()
            self._flush()
            self._table_stack.append({"rows": [], "row": None, "cell": None, "header": False})
            self._tables += 1
        elif tag == "tr":
            if self._table_stack:
                self._table_stack[-1]["row"] = []
        elif tag in ("td", "th"):
            if self._table_stack:
                self._table_stack[-1]["cell"] = []
                if tag == "th":
                    self._table_stack[-1]["header"] = True

    def handle_endtag(self, tag: str) -> None:
        if self._drop_depth:
            self._drop_depth -= 1
            return
        if tag in VOID_TAGS:
            return
        if self._chrome_open and tag in DROP_TAG_KEEP_TEXT:
            self._chrome_open = False
        # pop the matching open tag (tolerate malformed nesting)
        if tag in self._stack:
            while self._stack:
                top = self._stack.pop()
                if top == tag or top == f"__h{tag}":
                    break

        if tag in HEADINGS:
            level = HEADINGS[tag]
            text = self._inline()
            self.buf.clear()
            if text:
                self._emit(md.heading(level, text))
                self._headings += 1
        elif tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            code = unescape("".join(self._pre_buf)).strip("\n")
            self._pre_buf = []
            if code.strip():
                lang = self._pre_lang
                if not lang:
                    match = re.search(r'(?:language|lang|brush)[-:]([A-Za-z0-9+#]+)', self._last_pre_class)
                    lang = match.group(1).lower() if match else ""
                self._emit_block(md.fence(code, lang))
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag in ("del", "s", "strike"):
            self.buf.append("~~")
        elif tag == "code" and self._code_in_pre and not self._code_in_pre.pop():
            raw = "".join(self._code_buf)
            self._code_depth = max(0, self._code_depth - 1)
            self._code_buf = []
            if "\n" in raw.strip():
                # A multi-line <code> outside <pre>: a fence is the honest form.
                self._emit_block(md.fence(unescape(raw), ""))
            else:
                inner = _clean_ws(raw).strip()
                ticks = "``" if "`" in inner else "`"
                self.buf.append(f"{ticks}{inner}{ticks}")
        elif tag == "a":
            # The stack holds a boolean: did this <a> open a "[" label?
            if self._link_stack and self._link_stack.pop():
                self.buf.append("](")
                # the href itself was captured at open time; re-read from stack
                self.buf.append(self._href_stack.pop() if self._href_stack else "")
                self.buf.append(")")
                self._links += 1
        elif tag in ("ul", "ol"):
            self._flush_list()
        elif tag == "li":
            if self._lists and self._lists[-1]["current"] is not None:
                self._lists[-1]["items"].append(self._lists[-1]["current"])
                self._lists[-1]["current"] = None
        elif tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            text = self._inline()
            self.buf.clear()
            if text:
                self._emit(md.blockquote(text))
        elif tag in ("td", "th"):
            if self._table_stack and self._table_stack[-1]["cell"] is not None:
                cell = _clean_ws("".join(self._table_stack[-1]["cell"])).strip()
                self._table_stack[-1]["cell"] = None
                if self._table_stack[-1]["row"] is not None:
                    self._table_stack[-1]["row"].append(cell)
        elif tag == "tr":
            if self._table_stack and self._table_stack[-1]["row"] is not None:
                row = self._table_stack[-1]["row"]
                if any(c for c in row):
                    self._table_stack[-1]["rows"].append(row)
                self._table_stack[-1]["row"] = None
        elif tag == "table":
            if self._table_stack:
                bundle = self._table_stack.pop()
                rows = [r for r in bundle["rows"] if any(c for c in r)]
                if rows:
                    self._emit(md.table(rows, header=bundle["header"] or True))
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_pre:
            self._pre_buf.append(data)
            return
        if self._code_depth:
            self._code_buf.append(data)
            return
        if self._table_stack and self._table_stack[-1]["cell"] is not None:
            self._table_stack[-1]["cell"].append(data)
            return
        if self._lists and self._lists[-1]["current"] is not None:
            self._lists[-1]["current"].append(data)
            return
        self.buf.append(data)

    @property
    def _link_text_only(self) -> bool:
        text = _clean_ws("".join(self.buf)).strip()
        stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
        stripped = re.sub(r"\[[^\]]*\]", "", stripped)
        return not stripped.strip()

    def _inside_content(self) -> bool:
        """Are we inside something that is unambiguously article content?"""
        return any(t in ("main", "article", "section", "div", "table") for t in self._stack)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        """Self-closing tags (``<img/>``) must reach the start-tag handler."""
        self.handle_starttag(tag, attrs)

    @property
    def _link_text_only(self) -> bool:
        """Have we emitted nothing but link text since the current inner text?"""
        text = _clean_ws("".join(self.buf)).strip()
        # "[Home](/) [About](/)" / "[Home]" collapse to nothing but link syntax
        stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
        stripped = re.sub(r"\[[^\]]*\]", "", stripped)
        return not stripped.strip()

    def result(self) -> tuple[str, dict]:
        self._flush()
        # anything left open at EOF
        while self._lists:
            self._flush_list()
        for bundle in self._table_stack:
            rows = [r for r in bundle["rows"] if any(c for c in r)]
            if rows:
                self.out.append(md.table(rows))

        body = "\n\n".join(f for f in self.out if f.strip())
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        stats = {
            "headings": self._headings,
            "tables": self._tables,
            "links": self._links,
            "images": self._images,
        }
        return body, stats


def html_to_markdown(html: str, *, engine: str = "auto", base_url: str = "",
                     keep_links: bool = True, keep_images: bool = True,
                     drop_chrome: bool = True) -> tuple[str, dict, str]:
    """Convert an HTML string. Returns ``(markdown, stats, engine_used)``."""
    if engine in ("auto", "markdownify"):
        try:
            from markdownify import markdownify as _md  # type: ignore

            stripped = html
            if drop_chrome:
                stripped = re.sub(
                    r"<(script|style|noscript|template|svg|nav|footer|header|iframe)\b.*?</\1>",
                    "",
                    html,
                    flags=re.S | re.I,
                )
            body = _md(
                stripped,
                heading_style="ATX",
                bullets="-",
                strip=["img"] if not keep_images else None,
            )
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            stats = {
                "headings": len(re.findall(r"^#{1,6}\s+\S", body, re.M)),
                "tables": body.count("| ---"),
                "links": len(re.findall(r"\]\(", body)),
                "images": len(re.findall(r"!\[", body)),
            }
            return body, stats, "markdownify"
        except ImportError:
            if engine == "markdownify":
                raise
        except Exception:
            if engine == "markdownify":
                raise

    # Pre-pass: decide which <header>/<nav>/<footer> elements are content.
    keep_chrome: set[int] = set()
    if drop_chrome:
        scanner = _ChromeScanner()
        scanner.feed(html)
        scanner.close()
        keep_chrome = scanner.keep

    renderer = _Renderer(keep_links=keep_links, keep_images=keep_images,
                         base_url=base_url, drop_chrome=drop_chrome,
                         keep_chrome=keep_chrome)
    renderer.feed(html)
    renderer.close()
    body, stats = renderer.result()
    stats["chrome_dropped"] = _count_chrome(html) - len(keep_chrome)
    return body, stats, "builtin"


def _count_chrome(html: str) -> int:
    """How many chrome-candidate elements the document has (for reporting)."""
    return sum(len(re.findall(rf"<{tag}[\s>]", html, re.I)) for tag in DROP_TAG_KEEP_TEXT)


def _extract_title(html: str) -> str:
    """Prefer the document's own first heading over <title>.

    They usually agree; when they disagree the ``<h1>`` is the one that is
    actually in the body, so using it avoids printing two different titles.
    """
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if match:
        title = md.squash(unescape(re.sub(r"<[^>]+>", "", match.group(1))))
        if title:
            return title
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if match:
        return md.squash(unescape(re.sub(r"<[^>]+>", "", match.group(1))))
    return ""


@registry.converter("html", "html", priority=10, description="HTML -> structured markdown")
def convert_html(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    html = read.text

    body, stats, engine = html_to_markdown(
        html,
        engine=options.get("html_engine", "auto"),
        keep_links=bool(options.get("keep_links", True)),
        keep_images=bool(options.get("keep_images", True)),
        drop_chrome=bool(options.get("drop_chrome", True)),
    )

    title = _extract_title(html)
    if title:
        doc.title = title

    if not body.strip():
        doc.set_status(Status.EMPTY)
        doc.warn("empty-html", "no convertible content found in the HTML", "error")
        return doc

    doc.add(body)
    doc.meta.update(stats)
    doc.meta["engine"] = engine

    if stats["headings"] == 0:
        doc.warn(
            "no-headings",
            "the page produced no markdown headings; if it is a single-article page "
            "the source may use <div> instead of <h1>-<h6>",
            "info",
        )
    if read.had_errors:
        doc.warn("encoding-uncertain", f"decoded from {read.encoding} with replacement characters")
    if options.get("keep_links", True):
        external = len(re.findall(r"\]\(https?://", body))
        if external > 50:
            doc.warn("link-heavy", f"{external} external links retained; "
                                   "use --no-links to drop them", "info")
    return doc


# ----------------------------------------------------------------------- svg


@registry.converter("image", "image-metadata", priority=50, description="image size + EXIF (no OCR)")
def convert_image(path: Path, options: dict) -> Document:
    """Images cannot be converted to markdown without OCR.

    Rather than pretending, emit an honest placeholder that keeps the asset and
    its real dimensions, so a document that references the image still works.
    """
    doc = Document()
    size = path.stat().st_size
    info: list[tuple[str, object]] = [("File", path.name), ("Bytes", f"{size:,}")]

    width = height = None
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            width, height = img.size
            info += [
                ("Dimensions", f"{width} x {height}"),
                ("Mode", img.mode),
                ("Format", img.format or ""),
            ]
            exif = getattr(img, "getexif", lambda: None)()
            if exif:
                from PIL.ExifTags import TAGS  # type: ignore

                interesting = {271: "Make", 272: "Model", 306: "DateTime", 274: "Orientation"}
                for tag_id, label in interesting.items():
                    value = exif.get(tag_id)
                    if value:
                        info.append((label, str(value)))
    except ImportError:
        raw = path.read_bytes()[:64]
        width, height = _sniff_dimensions(raw)
        if width:
            info.append(("Dimensions", f"{width} x {height}"))
        doc.warn("pillow-missing", "Pillow is not installed; only basic image info available", "info")
    except Exception as exc:  # noqa: BLE001
        doc.warn("image-read-failed", f"could not read image metadata ({exc})")

    doc.add("> **Image — not transcribed.** This converter does not perform OCR. "
            "Text inside this image is not present in the output.")
    doc.add(md.kv_table(info, key_header="Property"))

    doc.assets.append(_asset_from(path))
    doc.add(md.image(path.name, f"assets/{path.name}"))

    doc.meta.update({"width": width, "height": height, "bytes": size})
    doc.warn(
        "image-not-transcribed",
        "image content was not transcribed (no OCR configured). "
        "The file is recorded in assets/ and referenced by a markdown image link.",
    )
    return doc


def _asset_from(path: Path):
    from ..model import Asset
    from ..security import sanitize_filename

    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/x-icon",
    }.get(path.suffix.lower(), "application/octet-stream")
    return Asset(filename=sanitize_filename(path.name), data=path.read_bytes(), media_type=media)


def _sniff_dimensions(head: bytes) -> tuple[int | None, int | None]:
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head.startswith(b"GIF") and len(head) >= 10:
        return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
    if head.startswith(b"BM") and len(head) >= 26:
        return int.from_bytes(head[18:22], "little"), int.from_bytes(head[22:26], "little")
    return None, None
