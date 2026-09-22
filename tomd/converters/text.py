"""Plain-text family converters: ``text``, ``markdown``, ``code``, config files.

These are the formats where "conversion" really means *deciding what is
structure*.  A ``.txt`` file has no markup, so the only tools available are
heuristics -- and heuristics that guess wrong are worse than no guess at all.
Every heuristic here is therefore conservative and reports what it did.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import mdutil as md
from ..model import Document, Status
from ..registry import registry

# Underlined heading pairs: "Title\n=====" and "Section\n-------" (see
# _promote_underline_headings, which does the real work line by line).
_UNDERLINE = re.compile(r"^(\S.*\S)\s*\n\s*(=+|-{3,}|\*{3,}|_{3,})\s*$", re.M)

# Numbered outline: "1. Intro", "1.2 Detail", "第三章 ..."
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(\S.{0,80})$")
_CHAPTER = re.compile(r"^(第[一二三四五六七八九十百零\d]+[章节篇部分]|Chapter\s+\d+|CHAPTER\s+\d+)[\s:：]*(.{0,80})$")


def _options(options: dict) -> tuple[int, bool]:
    """(min_heading_len, detect_numbered_headings)."""
    return int(options.get("min_heading_len", 3)), bool(options.get("numbered_headings", False))


def _promote_underline_headings(text: str) -> tuple[str, int]:
    """Rewrite setext headings as ATX so they survive as real structure.

    Implemented line-wise rather than with one regex: a regex that also has to
    trim the title is easy to get subtly wrong, and a missed underline is
    invisible to the user while a false positive corrupts prose.
    """
    _UNDERLINE_CHARS = set("=-*_")
    lines = text.split("\n")
    out: list[str] = []
    count = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        title = line.strip()
        underline = nxt.strip()
        looks_like_underline = (
            len(title) > 0
            and len(title) <= 120
            and len(underline) >= 3
            and set(underline) <= _UNDERLINE_CHARS
            and len(set(underline)) == 1
        )
        if looks_like_underline:
            level = 1 if underline[0] == "=" else 2
            out.append(f"{'#' * level} {title}")
            count += 1
            index += 2
            continue
        out.append(line)
        index += 1

    return "\n".join(out), count


def _promote_numbered_headings(text: str, max_level: int = 4) -> tuple[str, int]:
    """Promote ``1.2.3 Title``-style outline lines to headings."""
    out: list[str] = []
    count = 0
    for line in text.split("\n"):
        match = _NUMBERED.match(line)
        chapter = _CHAPTER.match(line)
        if match:
            depth = match.group(1).count(".") + 1
            if depth <= max_level and len(match.group(2)) >= 2:
                out.append(md.heading(min(depth, 6), f"{match.group(1)} {match.group(2)}"))
                count += 1
                continue
        if chapter and len(line) <= 100:
            out.append(md.heading(1, line))
            count += 1
            continue
        out.append(line)
    return "\n".join(out), count


# --------------------------------------------------------------- plain text


@registry.converter("text", "plain-text", priority=10, description="escaped plain text")
def convert_text(path: Path, options: dict) -> Document:
    doc = Document()
    min_len, numbered = _options(options)
    read = md.read_text(path)

    if read.had_errors:
        doc.warn(
            "encoding-uncertain",
            f"'{read.encoding}' decoding produced replacement characters; "
            "some text may be mangled",
        )

    text = read.text
    headings = 0

    # Step 1: find structure that is genuinely there (setext underlines, and
    # optionally numbered outlines).  Everything after this is untouched text.
    if text.strip():
        text, n = _promote_underline_headings(text)
        headings += n
        if numbered:
            text, n = _promote_numbered_headings(text)
            headings += n

    # Step 2: neutralise lines that only *look* like markdown, and preserve the
    # headings step 1 found by protecting them from that escaping.
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(r"^#{1,6}\s+.*$", protect, text, flags=re.M)
    body = md.escape_structure(text)
    for index, heading in enumerate(protected):
        body = body.replace(f"\x00{index}\x00", heading)

    doc.add(body)
    doc.meta["encoding"] = read.encoding
    doc.meta["headings_detected"] = headings
    if read.encoding.lower() not in ("utf-8", "utf-8-sig", "ascii"):
        doc.warn("non-utf8-source", f"decoded from {read.encoding}", "info")
    return doc


# ----------------------------------------------------------------- markdown


@registry.converter("markdown", "markdown-passthrough", priority=10,
                    description="normalise and pass through")
def convert_markdown(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    text = read.text

    # Front matter is metadata, not body: lift it out and re-emit it structured.
    front: dict[str, str] = {}
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if fm:
        for line in fm.group(1).split("\n"):
            if ":" in line and not line.lstrip().startswith("-"):
                k, _, v = line.partition(":")
                front[k.strip()] = v.strip().strip("'\"")
        text = text[fm.end():]

    doc.add(text)
    if front:
        doc.meta["front_matter"] = front

    # Report the heading outline: it is the thing downstream chunkers rely on.
    levels = [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s+\S", text, re.M)]
    doc.meta["headings"] = len(levels)
    if levels and levels[0] != 1:
        doc.warn(
            "no-top-level-heading",
            "document starts at heading level %d; heading-based chunking may mis-nest" % levels[0],
            "info",
        )
    if not levels:
        doc.warn("no-headings", "no markdown headings found; outline is unavailable", "info")
    if read.had_errors:
        doc.warn("encoding-uncertain", f"decoded from {read.encoding} with replacement characters")
    return doc


# --------------------------------------------------------------------- code


@registry.converter("code", "source-code", priority=10,
                    description="fenced code block with language + light outline")
def convert_code(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    lang = path.suffix.lower().lstrip(".") or ""
    try:
        from ..detect import EXT_TO_LANG

        lang = EXT_TO_LANG.get(path.suffix.lower(), lang)
    except Exception:
        pass

    if options.get("code_outline", True):
        outline = _code_outline(read.text, lang)
        if outline:
            doc.add("## Outline\n\n" + outline)

    doc.add(md.fence(read.text, lang))
    doc.meta["language"] = lang
    doc.meta["lines"] = read.text.count("\n") + 1
    if read.had_errors:
        doc.warn("encoding-uncertain", f"decoded from {read.encoding} with replacement characters")
    return doc


#: Very light-weight, language-agnostic symbol scan (deliberately not an AST).
_SYMBOL_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?def\s+(\w+)"), "python", "def"),
    (re.compile(r"^\s*class\s+(\w+)"), "python", "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "js", "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), "js", "function"),
    (re.compile(r"^\s*(?:public|private|protected|static|\s)*[\w<>\[\],\s]+\s+(\w+)\s*\([^;]*\)\s*\{"), "java", "method"),
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"), "go", "func"),
    (re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+(\w+)"), "rust", "item"),
    (re.compile(r"^\s*(?:CREATE|create)\s+(?:TABLE|VIEW|INDEX)\s+([\w.\"]+)"), "sql", "object"),
    (re.compile(r"^\s*#{1,6}\s*(.+)$"), "shell", "section"),
]


def _code_outline(text: str, lang: str) -> str:
    """Extract a navigable symbol list so a code file behaves like a document."""
    found: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.split("\n"), start=1):
        if len(line) > 300:
            continue
        for pattern, _lang, kind in _SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                name = match.group(1).strip()
                key = f"{kind}:{name}"
                if name and key not in seen and len(found) < 200:
                    seen.add(key)
                    found.append(f"- `{name}` ({kind}) — line {lineno}")
                break
    return "\n".join(found)


# ------------------------------------------------------------------- config


@registry.converter("ini", "config-ini", priority=10, description="INI / dotenv / properties")
def convert_ini(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    import configparser

    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=", ":"))
    try:
        parser.read_string(read.text)
    except Exception as exc:  # noqa: BLE001 - malformed config is common
        doc.warn("config-parse-failed", f"could not parse as INI ({exc}); emitting raw text")
        doc.add(md.fence(read.text, "ini"))
        return doc

    for section in parser.sections():
        doc.add(md.heading(2, section))
        doc.add(md.kv_table((k, v) for k, v in parser.items(section)))
    if parser.defaults():
        doc.add(md.heading(2, "(defaults)"))
        doc.add(md.kv_table(parser.defaults().items()))
    if not parser.sections() and not parser.defaults():
        doc.add(md.fence(read.text, "ini"))
    doc.meta["sections"] = len(parser.sections())
    return doc


@registry.converter("yaml", "config-yaml", priority=10, requires="yaml",
                    description="YAML with structure preserved")
def convert_yaml(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    import yaml  # type: ignore

    try:
        data = yaml.safe_load(read.text)
    except Exception as exc:  # noqa: BLE001
        doc.warn("yaml-parse-failed", f"could not parse YAML ({exc}); emitting raw text")
        doc.add(md.fence(read.text, "yaml"))
        return doc

    doc.add(md.fence(read.text, "yaml"))
    if isinstance(data, dict):
        doc.meta["top_level_keys"] = list(map(str, data.keys()))[:50]
    elif isinstance(data, list):
        doc.meta["items"] = len(data)
    return doc


@registry.converter("yaml", "config-yaml-raw", priority=90, description="YAML as raw text")
def convert_yaml_raw(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    doc.add(md.fence(read.text, "yaml"))
    doc.warn("no-yaml-parser", "PyYAML is not installed; emitted the raw file instead", "info")
    return doc


@registry.converter("toml", "config-toml", priority=10,
                    description="TOML with section headings")
def convert_toml(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    try:
        import tomllib  # Python 3.11+

        data = tomllib.loads(read.text)
    except Exception:
        data = None

    if not isinstance(data, dict):
        doc.add(md.fence(read.text, "toml"))
        return doc

    scalars = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
    if scalars:
        doc.add(md.kv_table(scalars.items()))
    for key, value in data.items():
        if isinstance(value, dict):
            doc.add(md.heading(2, key))
            doc.add(md.kv_table((k, _inline(v)) for k, v in value.items()))
    doc.add("<details>\n<summary>Raw TOML</summary>\n\n" + md.fence(read.text, "toml") + "\n\n</details>")
    return doc


def _inline(value: object) -> str:
    import json

    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


@registry.converter("diff", "diff-patch", priority=10, description="unified diff/patch")
def convert_diff(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    text = read.text

    files = re.findall(r"^(?:\+\+\+|---)\s+([^\t\n]+)", text, re.M)
    added = sum(1 for ln in text.split("\n") if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in text.split("\n") if ln.startswith("-") and not ln.startswith("---"))

    doc.add(md.kv_table([("Files touched", len({f for f in files if f != "/dev/null"})),
                         ("Lines added", added), ("Lines removed", removed)]))
    doc.add(md.fence(text, "diff"))
    return doc


# ---------------------------------------------------------------- subtitles


_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _seconds(stamp: str) -> float | None:
    match = _TS.search(stamp)
    if not match:
        return None
    h, m, s, ms = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s + ms / (10 ** len(match.group(4)))


def _fmt_time(value: float | None) -> str:
    if value is None:
        return ""
    total = int(value)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def parse_subtitles(text: str) -> list[tuple[float | None, float | None, str]]:
    """Parse SRT/VTT/ASS into ``(start, end, text)`` cues."""
    cues: list[tuple[float | None, float | None, str]] = []

    if re.search(r"^\[Script Info\]|^\[Events\]", text, re.M):  # ASS/SSA
        for line in text.split("\n"):
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",", 9)
            if len(fields) < 10:
                continue
            start, end, body = _seconds(fields[1]), _seconds(fields[2]), fields[9]
            body = re.sub(r"\{[^}]*\}", "", body).replace("\\N", " ").replace("\\n", " ")
            cues.append((start, end, md.squash(body)))
        return cues

    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE")):
            lines = lines[1:]
            if not lines:
                continue
        if lines and re.fullmatch(r"\d+", lines[0].strip()):
            lines = lines[1:]
        if not lines:
            continue
        if "-->" in lines[0]:
            left, _, right = lines[0].partition("-->")
            body = md.squash(" ".join(lines[1:]))
            if body:
                cues.append((_seconds(left), _seconds(right), body))
        elif lines:
            body = md.squash(" ".join(lines))
            if body and not body.upper().startswith("WEBVTT"):
                cues.append((None, None, body))
    return cues


@registry.converter("subtitle", "subtitle", priority=10, description="SRT/VTT/ASS cues")
def convert_subtitle(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    cues = parse_subtitles(read.text)

    if not cues:
        doc.warn("no-cues", "no subtitle cues were recognised")
        doc.add(md.fence(read.text, "text"))
        return doc

    as_table = bool(options.get("subtitle_table", False))
    if as_table:
        doc.add(md.table([["Start", "End", "Text"],
                          *[[_fmt_time(s), _fmt_time(e), body] for s, e, body in cues]]))
    else:
        doc.add("\n\n".join(f"**[{_fmt_time(s)}]** {body}" for s, e, body in cues))

    doc.meta["cues"] = len(cues)
    if cues[0][0] is not None:
        doc.meta["duration"] = _fmt_time(cues[-1][1] or cues[-1][0])
    return doc


# ------------------------------------------------------------------ generic


@registry.converter("svg", "svg", priority=10, description="SVG text labels + source")
def convert_svg(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    labels = re.findall(r"<(?:text|tspan|title|desc)[^>]*>(.*?)</(?:text|tspan|title|desc)>",
                        read.text, re.S | re.I)
    cleaned = [md.squash(re.sub(r"<[^>]+>", "", item)) for item in labels]
    cleaned = [c for c in cleaned if c]
    if cleaned:
        doc.add("## Text in image\n\n" + md.bullet_list(cleaned))
    doc.add("<details>\n<summary>SVG source</summary>\n\n" + md.fence(read.text, "xml") + "\n\n</details>")
    doc.meta["labels"] = len(cleaned)
    if not cleaned:
        doc.warn("no-text-in-svg", "the SVG has no text elements; only the source is included", "info")
    return doc
