"""HTML-family converters: ``html`` and ``svg``.

Two engines:

* the built-in :class:`html.parser.HTMLParser` renderer -- the default, and the
  only one ``auto`` ever picks;
* ``markdownify``, opted into explicitly with ``--html-engine markdownify``.

The built-in one is the default because it is always available, because a
bounded auditable renderer is easier to reason about than a general-purpose
library when the input is untrusted, and because output should not silently
change depending on what happens to be installed in the environment.  The
third-party engine is still offered for its broader tag coverage; when it is
used, the chrome decision and fence languages the built-in renderer handles for
free are restored around it.

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

#: Placeholder for ``<br>``.  Markdown's hard break is "two spaces then a
#: newline", but the surrounding line is whitespace-normalised before output,
#: which would strip those spaces.  The sentinel contains characters that cannot
#: arise from HTML entity decoding, so it cannot collide with real content.
HARD_BREAK = "\x00\x01br\x01\x00"


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


class _ChromeFilter(HTMLParser):
    """Re-emit HTML with content-free page chrome removed, the rest untouched.

    The built-in renderer decides chrome-vs-content while it renders; the
    opt-in markdownify engine cannot, so the same policy
    (:meth:`_ChromeScanner._worth_keeping`) is applied here, on the raw markup,
    before markdownify sees it.  Everything that is *not* chrome is re-emitted
    byte-for-byte -- start tags keep their original spelling via
    :meth:`~html.parser.HTMLParser.get_starttag_text`, and ``convert_charrefs``
    stays off so entities survive -- because the point is to hand markdownify a
    document it can parse, not a re-serialised approximation of one.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        # One buffer per open chrome element; edits land in the innermost one so
        # a dropped element takes its whole subtree with it.
        self._bufs: list[list[str]] = [self._out]
        self._frames: list[list] = []
        self._anchor_depth = 0

    def _emit(self, text: str) -> None:
        self._bufs[-1].append(text)

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        raw = self.get_starttag_text() or f"<{tag}>"
        if tag in DROP_TAG_KEEP_TEXT and not self._frames:
            # A <nav> inside a <header> is part of the header's decision, not a
            # second one -- same rule as the scanner's outermost-only counting.
            self._bufs.append([])
            self._frames.append([0, tag, False, 0, 0])
            return
        if self._frames:
            frame = self._frames[-1]
            if tag in HEADINGS:
                frame[2] = True
                if tag != "h1":
                    # An <h3> inside a nav is a menu group label: it counts as a
                    # hint of prose, not as the mark of a document header.
                    frame[3] += 1
            elif tag == "a":
                self._anchor_depth += 1
                frame[4] += 1
        self._emit(raw)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        """Self-closing tags are emitted verbatim; none of them is chrome."""
        self._emit(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._anchor_depth = max(0, self._anchor_depth - 1)
        elif self._frames and tag == self._frames[-1][1]:
            frame = self._frames.pop()
            content = self._bufs.pop()
            if _ChromeScanner._worth_keeping(frame):
                self._bufs[-1].extend(content)
                self._emit(f"</{tag}>")
            return
        self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._frames:
            self._emit(data)
            return
        frame = self._frames[-1]
        text = _clean_ws(data).strip()
        if text:
            # Same scoring as the scanner, including the link-label weighting.
            if self._anchor_depth:
                frame[4] += max(1, len(text) // 8)
            else:
                frame[3] += len(text)
        self._emit(data)

    def handle_entityref(self, name: str) -> None:
        """``HTMLParser`` drops references when ``convert_charrefs`` is off.

        The default handler is a no-op, so entities have to be written back out
        by hand -- otherwise ``&amp;`` silently disappears from the document.
        """
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._emit(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._emit(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self._emit(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self._emit(f"<![{data}]>")

    def close(self) -> None:  # noqa: D102
        super().close()
        # Chrome left open at EOF cannot be judged; keep it, because dropping
        # the remainder of a malformed document is far worse than a stray banner.
        while self._frames:
            self._frames.pop()
            self._bufs[-2].extend(self._bufs.pop())

    def result(self) -> str:
        return "".join(self._out)


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
        if any(bundle["cell"] is not None for bundle in self._table_stack):
            # An open cell owns the inline buffer: flushing here would spill the
            # cell's text into the document body as its own paragraph.
            return
        text = _clean_ws("".join(self.buf)).strip()
        self.buf.clear()
        if text:
            self.out.append(text)

    def _current_item_text(self) -> str:
        """The innermost item's inline text, without consuming the buffer.

        Taking a *slice* rather than moving list objects is what keeps inline
        markup paired with its text: an inline element appends its opening
        delimiter to ``self.buf``, and if the text before it has been moved into
        a different list, the delimiter is left dangling and the item loses its
        own words.
        """
        if not self._lists:
            return ""
        frame = self._lists[-1]
        if frame["current"] is None:
            return ""
        return "".join(self.buf[frame["current"]:])

    def _take_current_item_text(self) -> str:
        """As :meth:`_current_item_text`, but drops the raw text from the buffer.

        The buffer keeps its inline delimiters, so a span still open across this
        boundary can still be closed correctly later.
        """
        text = self._current_item_text()
        if self._lists and self._lists[-1]["current"] is not None:
            start = self._lists[-1]["current"]
            del self.buf[start:]
            self._lists[-1]["current"] = len(self.buf)
        return text

    def _enter_list_item(self, frame: dict) -> None:
        """Open a ``<li>``: close the previous item, then start recording."""
        if frame["current"] is not None:
            text = _clean_ws("".join(self.buf[frame["current"]:])).strip()
            del self.buf[frame["current"]:]
            if text:
                frame["items"].append([text])
        frame["current"] = len(self.buf)

    def _exit_list_item(self, frame: dict) -> None:
        """Close a ``<li>``: record its inline text."""
        if frame["current"] is not None:
            text = _clean_ws("".join(self.buf[frame["current"]:])).strip()
            del self.buf[frame["current"]:]
            if text:
                frame["items"].append([text])
            frame["current"] = None

    def _flush_list(self, flush_inline: bool = True) -> None:
        """Close the innermost open list, if any.

        ``flush_inline=False`` is used while unwinding a nested list: the buffer
        from the parent item's start position belongs to the parent item, whose
        own ``</li>`` records it.  Consuming it here would emit the parent's text
        after its children.
        """
        if not self._lists:
            return
        # The depth of this list is measured *before* popping it: after the pop
        # an outer list has an empty stack, and using that as the indent would
        # silently flatten every nested list.
        depth = len(self._lists) - 1
        bundle = self._lists.pop()

        if bundle["current"] is not None:
            if flush_inline:
                text = _clean_ws("".join(self.buf[bundle["current"]:])).strip()
                del self.buf[bundle["current"]:]
                if text:
                    bundle["items"].append([text])
            bundle["current"] = None

        def flatten(item: list) -> str:
            # Items are nested lists: an item holds its own text plus, appended
            # after it, the rendered blocks of any nested list.
            return "".join(flatten(part) if isinstance(part, list) else part for part in item)

        def normalize(item: list) -> str:
            # Nested-list structure is carried by newlines *and* by the leading
            # indentation, so protect the indentation from the whitespace
            # collapser -- otherwise "    - child" collapses to " - child" and
            # the nesting is silently lost.
            lines = flatten(item).split("\n")
            protected = []
            for line in lines:
                stripped = line.lstrip(" ")
                indent_width = len(line) - len(stripped)
                protected.append("\x01" * indent_width + stripped)
            joined = "\x00".join(protected)
            collapsed = _clean_ws(joined)
            collapsed = collapsed.replace("\x00", "\n").replace("\x01", " ")
            return collapsed.rstrip()

        items = [normalize(i) for i in bundle["items"] if flatten(i).strip()]
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
        # parent's own text comes first and the children follow it.  The parent's
        # text was already appended to ``items`` when the nested ``<ul>`` opened,
        # so appending the indented block preserves that order.
        indented = "\n".join(indent + line for line in body.split("\n"))
        if self._lists and self._lists[-1]["items"]:
            self._lists[-1]["items"][-1].append(["\n" + indented])
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
        elif tag == "br":
            # A markdown hard break is "two spaces then a newline", but the line
            # is about to be whitespace-normalised, which would eat those
            # spaces.  A sentinel survives normalisation and is converted back
            # in result(); a bare newline would be a *soft* break, which
            # renderers collapse into one line, silently losing the structure.
            self.buf.append(HARD_BREAK)
        elif tag == "p" or tag in BLOCK_TAGS:
            self._flush_list()
            self._flush()
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
            if self._lists and self._lists[-1]["current"] is not None:
                # Text written before a nested list belongs to the parent item.
                # Close that item off so the nested list's own content cannot be
                # mistaken for the parent's, then let the nested list record.
                frame = self._lists[-1]
                text = _clean_ws("".join(self.buf[frame["current"]:])).strip()
                del self.buf[frame["current"]:]
                if text:
                    frame["items"].append([text])
                frame["current"] = None
            else:
                self._flush()
            self._lists.append({"ordered": tag == "ol", "items": [], "current": None})
        elif tag == "li":
            if self._lists:
                self._enter_list_item(self._lists[-1])
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
                # A cell records a position into the inline buffer, for the same
                # reason a list item does: `<code>`/`<b>`/`<a>` append a
                # delimiter *and* their own text to that single buffer, and
                # slicing from a position is what keeps the two together.
                self._table_stack[-1]["cell"] = len(self.buf)
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
                span = f"{ticks}{inner}{ticks}"
                # The open tag appended a placeholder backtick; replace that
                # placeholder with the finished span instead of appending a
                # second backtick beside it.
                if self.buf and self.buf[-1] == "`":
                    self.buf[-1] = span
                else:
                    self.buf.append(span)
        elif tag == "a":
            # The stack holds a boolean: did this <a> open a "[" label?
            if self._link_stack and self._link_stack.pop():
                self.buf.append("](")
                # the href itself was captured at open time; re-read from stack
                self.buf.append(self._href_stack.pop() if self._href_stack else "")
                self.buf.append(")")
                self._links += 1
        elif tag in ("ul", "ol"):
            # Unwinding: the buffer holds the parent item's text, which the
            # parent's own </li> will fold in.
            self._flush_list(flush_inline=False)
        elif tag == "li":
            if self._lists:
                self._exit_list_item(self._lists[-1])
        elif tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            text = self._inline()
            self.buf.clear()
            if text:
                self._emit(md.blockquote(text))
        elif tag in ("td", "th"):
            if self._table_stack and self._table_stack[-1]["cell"] is not None:
                bundle = self._table_stack[-1]
                start = bundle["cell"]
                cell = _clean_ws("".join(self.buf[start:])).strip()
                del self.buf[start:]
                bundle["cell"] = None
                # A pipe-table row cannot contain a line break, so a <br> in a
                # cell becomes a space rather than a hard-break sentinel that
                # would be expanded after the table was already rendered.
                if bundle["row"] is not None:
                    bundle["row"].append(cell.replace(HARD_BREAK, " "))
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
                if not rows:
                    return
                if bundle["header"]:
                    self._emit(md.table(rows, header=True))
                else:
                    # No <th> anywhere, so the first row is data, not a header.
                    # Promoting it would mislabel real values as column names;
                    # an explicit placeholder keeps the alignment honest.
                    table = md.table(rows, header=False)
                    self._emit(table.replace("|  |", "| (no header) |", 1))
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
        # List items and table cells record a position in this buffer rather
        # than a list of their own, so inline delimiters stay paired with their
        # text -- and so text inside a cell cannot escape the row.
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
        # ``<br>`` sentinels survived whitespace normalisation; turn them into
        # real markdown hard breaks (two trailing spaces) now that no further
        # whitespace collapsing will happen.
        body = body.replace(HARD_BREAK, "  \n")
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        stats = {
            "headings": self._headings,
            "tables": self._tables,
            "links": self._links,
            "images": self._images,
        }
        return body, stats


#: Tags that are never content, whatever engine is used.  ``head`` goes too: it
#: holds ``<title>``/``<meta>``, and markdownify would otherwise print the
#: document title as body text.
_NOISE_SUBTREE_RE = re.compile(
    r"<(head|title|script|style|noscript|template|svg|canvas|iframe)\b[^>]*>.*?</\1\s*>",
    re.S | re.I,
)
#: ``<pre>`` blocks, pulled out before the third-party engine runs so that their
#: fence language (which markdownify discards) and their literal whitespace
#: (which it reflows) survive.
_PRE_RE = re.compile(r"<pre\b[^>]*>(.*?)</pre\s*>", re.S | re.I)
_FENCE_LANG_RE = re.compile(
    r"""class\s*=\s*(?:"[^"]*?|'[^']*?|)(?:language|lang|highlight|brush|source)[-_:]([A-Za-z0-9+#._-]+)""",
    re.I,
)
_CODE_SLOT = "TOMDCODESLOT"


def _strip_noise(html: str) -> str:
    """Remove subtrees that can never carry markdown content."""
    previous = None
    while previous != html:
        previous = html
        html = _NOISE_SUBTREE_RE.sub("", html)
    # Unclosed <head>/<title> (the regex needs a closing tag) still must go.
    return re.sub(r"</?(?:head|title)\b[^>]*>", "", html, flags=re.I)


def _protect_pre_blocks(html: str) -> tuple[str, list[str]]:
    """Swap every ``<pre>`` for a slot; return the fences to put back later."""
    fences: list[str] = []

    def swap(match: re.Match) -> str:
        inner = match.group(1)
        code_tag = re.search(r"<code\b[^>]*>", inner, re.I)
        lang = ""
        if code_tag:
            lang_match = _FENCE_LANG_RE.search(code_tag.group(0))
            if lang_match:
                lang = lang_match.group(1)
        text = unescape(re.sub(r"<[^>]+>", "", inner))
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        fences.append(md.fence(text, lang))
        return f"<p>{_CODE_SLOT}{len(fences) - 1}</p>"

    return _PRE_RE.sub(swap, html), fences


def _markdownify_engine(html: str, *, keep_links: bool, keep_images: bool,
                        drop_chrome: bool) -> str:
    """Render with markdownify, plus the fixes it needs to be usable here."""
    try:
        from markdownify import markdownify as _md  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "markdownify is not installed; install it with "
            '`pip install "tomd[recommended]"`, or use --html-engine builtin'
        ) from exc

    stripped = _strip_noise(html)
    if drop_chrome:
        # A regex that deletes every <header>/<nav> also deletes the <h1> a page
        # keeps in its header, so ask the same content-driven question the
        # built-in renderer asks.
        chrome = _ChromeFilter()
        chrome.feed(stripped)
        chrome.close()
        stripped = chrome.result()
    stripped, fences = _protect_pre_blocks(stripped)

    # ``strip`` drops the tag but keeps its text, which is what --no-links and
    # --no-images mean everywhere else in the tool.
    strip = [tag for tag, keep in (("a", keep_links), ("img", keep_images)) if not keep]
    body = _md(stripped, heading_style="ATX", bullets="-", strip=strip or None)
    for index, fenced in enumerate(fences):
        slot = re.compile(rf"(?m)^[ \t]*{_CODE_SLOT}{index}[ \t]*$")
        if slot.search(body):
            body = slot.sub(lambda _m, _f=fenced: _f, body)
        else:  # pragma: no cover - only if markdownify rewrote the slot line
            body = body.replace(f"{_CODE_SLOT}{index}", fenced)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def html_to_markdown(html: str, *, engine: str = "auto", base_url: str = "",
                     keep_links: bool = True, keep_images: bool = True,
                     drop_chrome: bool = True) -> tuple[str, dict, str]:
    """Convert an HTML string. Returns ``(markdown, stats, engine_used)``.

    ``auto`` and ``builtin`` both use the built-in renderer: it is always
    available, and pinning ``auto`` to it keeps output identical whether or not
    markdownify happens to be installed.  ``markdownify`` is an explicit opt-in.
    """
    if engine not in ("auto", "builtin", "markdownify"):
        raise ValueError(
            f"unknown HTML engine {engine!r}; expected 'auto', 'builtin' or 'markdownify'"
        )

    if engine == "markdownify":
        body = _markdownify_engine(
            html, keep_links=keep_links, keep_images=keep_images, drop_chrome=drop_chrome
        )
        stats = {
            "headings": len(re.findall(r"^#{1,6}\s+\S", body, re.M)),
            "tables": body.count("| ---"),
            "links": len(re.findall(r"\]\(", body)),
            "images": len(re.findall(r"!\[", body)),
        }
        return body, stats, "markdownify"

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
