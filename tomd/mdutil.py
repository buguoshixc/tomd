"""Markdown building blocks.

Converters should never hand-write a pipe table or a fence.  Doing it here
keeps the output byte-identical across formats, which matters a lot when the
result is fed to a downstream chunker or diffed between versions.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------- hygiene


@contextlib.contextmanager
def suppress_stdout() -> Iterator[None]:
    """Silence anything a third-party library decides to print.

    Several document libraries emit advisory banners to stdout (PyMuPDF's
    layout hint is the one that bites in practice).  When ``tomd file.pdf`` is
    piped into another program, that banner lands *inside* the markdown and
    corrupts the artifact.  Wrapping the library call is the only reliable fix,
    because the message comes from C code that never touches ``sys.stdout``
    directly.
    """
    sys.stdout.flush() if hasattr(sys.stdout, "flush") else None
    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = original


# --------------------------------------------------------------------- text


@dataclass
class TextResult:
    """Decoded file content plus how we decoded it."""

    text: str
    encoding: str
    had_errors: bool = False
    newline: str = "\n"


def read_text(path: Path, *, limit: int | None = None) -> TextResult:
    """Read a text file, detecting BOM/UTF-8/locale encodings.

    Never raises on encoding problems: undecodable bytes are replaced and the
    fact is reported so the caller can warn about it.
    """
    from .detect import _detect_encoding  # local import avoids a cycle

    raw = path.read_bytes()
    truncated = limit is not None and len(raw) > limit
    if truncated:
        raw = raw[:limit]

    encoding, _ = _detect_encoding(raw)
    had_errors = False
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode(encoding, "replace")
        had_errors = True
    if "\ufffd" in text and not had_errors:
        had_errors = True

    newline = "\r\n" if "\r\n" in text else "\n"
    return TextResult(
        text=text.replace("\r\n", "\n").replace("\r", "\n"),
        encoding=encoding,
        had_errors=had_errors,
        newline=newline,
    )

_WS = re.compile(r"[ \t\u00a0\u3000]+")


def squash(text: str) -> str:
    """Collapse *all* whitespace runs (including newlines) into single spaces."""
    return _WS.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()


def inline_value(value: Any) -> str:
    """Render a value for a table cell or inline text.

    Booleans become ``true``/``false`` and integral floats lose their ``.0`` so
    a Python-sourced value is not mistaken for prose at a glance.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, (list, tuple, dict)):
        import json

        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)
    return str(value)


def escape_inline(text: str) -> str:
    """Escape characters that would start markdown syntax mid-line."""
    out = []
    for ch in text:
        if ch in "\\`*_{}[]<>|":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


_BLOCK_START = re.compile(
    r"^(\s{0,3})(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|```|~~~|\|)"
)


def escape_block_line(line: str) -> str:
    """Escape a line that is *incidentally* structured markdown.

    Plain-text files are full of lines like ``- item`` or ``1. step`` that are
    not actually markdown.  Left alone they silently become lists/headings, so
    we neutralise the opening token.
    """
    m = _BLOCK_START.match(line)
    if not m:
        return line
    indent, token = m.group(1), m.group(2)
    return f"{indent}\\{token}{line[m.end():]}"


_LIST_ITEM = re.compile(r"^(\s{0,3})(?:\d+[.)]|[-*+])(\s+\S)")


def escape_structure(text: str) -> str:
    """Escape lines that are *incidentally* structured markdown.

    Plain-text files are full of lines like ``- item`` that are not markdown.
    Left alone they silently become lists, so the opening token is neutralised
    -- but a genuine run of list items (a real list someone typed into a .txt)
    is preserved, because turning that into paragraphs is the worse error.

    Escaping is applied per block: a run of 3+ consecutive list items is kept,
    anything shorter is escaped.  The asymmetry is intentional -- escaping a
    real two-item list costs a little fidelity, while failing to escape a plain
    sentence that happens to start with ``-`` rewrites the document's meaning.
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[str] = []
    for block in blocks:
        raw_lines = block.split("\n")
        is_item = [bool(_LIST_ITEM.match(ln)) for ln in raw_lines]

        # Longest run of consecutive items in this block.
        best_run = current = 0
        for flag in is_item:
            current = current + 1 if flag else 0
            best_run = max(best_run, current)
        keep_lists = best_run >= 3

        lines: list[str] = []
        for line, item in zip(raw_lines, is_item):
            stripped = line.rstrip()
            if item and not keep_lists:
                stripped = escape_block_line(stripped)
            elif not item:
                stripped = escape_block_line(stripped)
            lines.append(stripped)
        out.append("\n".join(lines).strip())
    return "\n\n".join(b for b in out if b)


def as_paragraphs(text: str) -> str:
    """Split on blank lines and keep paragraph breaks as they are."""
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n")]
        out.append("\n".join(lines).strip())
    return "\n\n".join(b for b in out if b)


# ------------------------------------------------------------------- fences


def fence(content: str, lang: str = "", *, min_ticks: int = 3) -> str:
    """Wrap content in a backtick fence long enough to survive its own backticks."""
    longest = 0
    for run in re.findall(r"`+", content):
        longest = max(longest, len(run))
    ticks = "`" * max(min_ticks, longest + 1)
    body = content.strip("\n")
    return f"{ticks}{lang}\n{body}\n{ticks}"


def heading(level: int, text: str) -> str:
    level = max(1, min(6, level))
    return f"{'#' * level} {squash(text)}".rstrip()


# ------------------------------------------------------------------- tables


def _cell(value: Any) -> str:
    text = squash(inline_value(value))
    # Pipe tables cannot carry literal pipes or newlines in a cell.
    return text.replace("|", "\\|")


def table(rows: Sequence[Sequence[Any]], header: bool = True) -> str:
    """Render rows as a GitHub-flavoured pipe table.

    The table is rectangularised to the widest row; missing cells become empty.
    """
    grid = [[_cell(c) for c in row] for row in rows]
    grid = [row for row in grid if any(c for c in row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]

    if header:
        head = grid[0]
        body = grid[1:]
    else:
        head = [""] * width
        body = grid

    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def kv_table(pairs: Iterable[tuple[str, Any]], key_header: str = "Field") -> str:
    rows = [(k, v) for k, v in pairs if v not in (None, "")]
    if not rows:
        return ""
    return table([[key_header, "Value"], *rows])


# -------------------------------------------------------------------- lists


def bullet_list(items: Iterable[str], marker: str = "-") -> str:
    out = []
    for item in items:
        text = item.strip()
        if not text:
            continue
        lines = text.split("\n")
        out.append(f"{marker} {lines[0]}")
        pad = " " * (len(marker) + 1)
        out += [f"{pad}{ln}" for ln in lines[1:]]
    return "\n".join(out)


def numbered_list(items: Iterable[str], start: int = 1) -> str:
    out = []
    for i, item in enumerate(items, start=start):
        text = item.strip()
        if not text:
            continue
        lines = text.split("\n")
        out.append(f"{i}. {lines[0]}")
        out += [" " * 3 + ln for ln in lines[1:]]
    return "\n".join(out)


# -------------------------------------------------------------------- misc


def image(alt: str, path: str, title: str = "") -> str:
    alt = squash(alt).replace("[", "(").replace("]", ")")
    title_part = f' "{squash(title)}"' if title else ""
    return f"![{alt}]({path}{title_part})"


def link(text: str, url: str) -> str:
    return f"[{squash(text) or url}]({url})"


def blockquote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.split("\n"))


def details(summary: str, body: str, *, open_: bool = False) -> str:
    tag = "<details open>" if open_ else "<details>"
    return f"{tag}\n<summary>{escape_inline(summary)}</summary>\n\n{body.strip()}\n\n</details>"


def front_matter(data: dict[str, Any]) -> str:
    """Minimal, dependency-free YAML front matter writer."""
    lines = ["---"]
    for key, value in data.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines += [f"  - {_scalar(v)}" for v in value]
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = squash(str(value))
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text.lower() in {
        "true",
        "false",
        "null",
        "yes",
        "no",
    }:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text
