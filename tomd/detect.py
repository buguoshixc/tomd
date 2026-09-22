"""Format detection.

Two independent signals, because neither is trustworthy alone:

* the file extension (fast, often wrong -- ``.txt`` files are frequently CSV),
* a content sniff (magic bytes, then a decoded text sample).

The result is a :class:`Format` describing both the *container* (which
converter to use) and a *media type* for the report/front matter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .security import check_size

# ------------------------------------------------------------------ tables

#: extension -> format key
EXT_MAP: dict[str, str] = {
    # plain text family
    ".txt": "text",
    ".text": "text",
    ".log": "text",
    ".nfo": "text",
    ".rst": "text",
    ".adoc": "text",
    ".asciidoc": "text",
    ".org": "text",
    ".tex": "text",
    # markdown family
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".mkd": "markdown",
    ".mdx": "markdown",
    # delimited
    ".csv": "csv",
    ".tsv": "csv",
    ".tab": "csv",
    ".psv": "csv",
    # structured text
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".xml": "xml",
    ".xsd": "xml",
    ".xsl": "xml",
    ".svg": "svg",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "ini",
    ".env": "ini",
    ".diff": "diff",
    ".patch": "diff",
    # subtitles
    ".srt": "subtitle",
    ".vtt": "subtitle",
    ".ass": "subtitle",
    ".ssa": "subtitle",
    # source code
    ".py": "code",
    ".pyi": "code",
    ".js": "code",
    ".mjs": "code",
    ".cjs": "code",
    ".ts": "code",
    ".tsx": "code",
    ".jsx": "code",
    ".java": "code",
    ".kt": "code",
    ".kts": "code",
    ".scala": "code",
    ".go": "code",
    ".rs": "code",
    ".c": "code",
    ".h": "code",
    ".cc": "code",
    ".cpp": "code",
    ".hpp": "code",
    ".cs": "code",
    ".rb": "code",
    ".php": "code",
    ".swift": "code",
    ".m": "code",
    ".mm": "code",
    ".pl": "code",
    ".lua": "code",
    ".r": "code",
    ".jl": "code",
    ".sh": "code",
    ".bash": "code",
    ".zsh": "code",
    ".fish": "code",
    ".ps1": "code",
    ".psm1": "code",
    ".bat": "code",
    ".cmd": "code",
    ".sql": "code",
    ".graphql": "code",
    ".proto": "code",
    ".dockerfile": "code",
    ".makefile": "code",
    ".cmake": "code",
    ".gradle": "code",
    ".vue": "code",
    ".svelte": "code",
    ".dart": "code",
    ".ex": "code",
    ".exs": "code",
    ".erl": "code",
    ".clj": "code",
    ".hs": "code",
    ".elm": "code",
    ".zig": "code",
    ".nim": "code",
    ".v": "code",
    ".f90": "code",
    ".asm": "code",
    # web
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    # office (OOXML)
    ".docx": "docx",
    ".docm": "docx",
    ".dotx": "docx",
    ".pptx": "pptx",
    ".ppsx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    # office (legacy binary)
    ".doc": "doc_legacy",
    ".ppt": "ppt_legacy",
    ".xls": "xls_legacy",
    # office (ODF)
    ".odt": "odf",
    ".ods": "odf",
    ".odp": "odf",
    ".odg": "odf",
    ".fodt": "odf",
    # rich text
    ".rtf": "rtf",
    # ebooks
    ".epub": "epub",
    ".mobi": "mobi",
    ".azw": "mobi",
    ".azw3": "mobi",
    ".fb2": "fb2",
    # pdf
    ".pdf": "pdf",
    # notebooks
    ".ipynb": "notebook",
    # email
    ".eml": "email",
    ".msg": "msg_legacy",
    # archives
    ".zip": "archive",
    # images (no OCR yet -- metadata only)
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".ico": "image",
    ".heic": "image",
    # data / misc
    ".parquet": "unsupported_parquet",
    ".sqlite": "unsupported_binary",
    ".db": "unsupported_binary",
    ".exe": "unsupported_binary",
    ".dll": "unsupported_binary",
    ".bin": "unsupported_binary",
}

#: Filenames that carry their own extension-less identity.
NAME_MAP: dict[str, str] = {
    "dockerfile": "code",
    "makefile": "code",
    "cmakelists.txt": "code",
    "rakefile": "code",
    "gemfile": "code",
    "procfile": "code",
    "license": "text",
    "readme": "markdown",
    "changelog": "markdown",
    ".gitignore": "text",
    ".dockerignore": "text",
    ".editorconfig": "ini",
    ".gitattributes": "text",
}

EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".m": "objectivec", ".mm": "objectivec", ".pl": "perl", ".lua": "lua",
    ".r": "r", ".jl": "julia", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
    ".fish": "fish", ".ps1": "powershell", ".psm1": "powershell", ".bat": "batch",
    ".cmd": "batch", ".sql": "sql", ".graphql": "graphql", ".proto": "protobuf",
    ".cmake": "cmake", ".gradle": "groovy", ".vue": "vue", ".svelte": "svelte",
    ".dart": "dart", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".clj": "clojure", ".hs": "haskell", ".elm": "elm", ".zig": "zig",
    ".nim": "nim", ".v": "verilog", ".f90": "fortran", ".asm": "asm",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".svg": "xml",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".diff": "diff",
    ".patch": "diff", ".tex": "latex", ".rst": "rst", ".adoc": "asciidoc",
    ".org": "org", ".srt": "srt", ".vtt": "vtt", ".ass": "ass", ".ssa": "ssa",
    ".properties": "properties", ".env": "dotenv", ".dockerfile": "dockerfile",
    ".makefile": "makefile", ".graphqls": "graphql", ".txt": "text",
}

MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),  # refined below (docx/xlsx/pptx/epub/odf are zips)
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),  # legacy MS Office
    (b"{\\rtf", "rtf"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"BM", "image"),
    (b"II*\x00", "image"),
    (b"MM\x00*", "image"),
    (b"RIFF", "riff"),  # webp / wav / avi
    (b"ID3", "audio"),
    (b"OggS", "audio"),
    (b"fLaC", "audio"),
    (b"\x1aE\xdf\xa3", "video"),
    (b"7z\xbc\xaf\x27\x1c", "unsupported_binary"),
    (b"Rar!\x1a\x07", "unsupported_binary"),
    (b"SQLite format 3\x00", "unsupported_binary"),
    (b"PAR1", "unsupported_parquet"),
    (b"\x7fELF", "unsupported_binary"),
    (b"MZ", "unsupported_binary"),
]

#: OOXML / ODF package markers inside a zip.
_ZIP_KINDS = [
    ("word/document.xml", "docx"),
    ("ppt/presentation.xml", "pptx"),
    ("xl/workbook.xml", "xlsx"),
    ("mimetype", "odf_or_epub"),
]

_MEDIA_TYPES: dict[str, str] = {
    "text": "text/plain",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "xml": "application/xml",
    "svg": "image/svg+xml",
    "yaml": "application/yaml",
    "toml": "application/toml",
    "ini": "text/plain",
    "diff": "text/x-diff",
    "subtitle": "text/vtt",
    "code": "text/plain",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "doc_legacy": "application/msword",
    "ppt_legacy": "application/vnd.ms-powerpoint",
    "xls_legacy": "application/vnd.ms-excel",
    "odf": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "notebook": "application/x-ipynb+json",
    "email": "message/rfc822",
    "archive": "application/zip",
    "image": "image/*",
    "binary": "application/octet-stream",
}


@dataclass
class Format:
    """The outcome of detection."""

    key: str
    """Converter key, e.g. ``pdf``, ``docx``, ``code``, ``unsupported_binary``."""

    extension: str
    media_type: str
    language: str = ""
    """Code-fence language, when relevant."""

    text_encoding: str = "utf-8"
    confidence: str = "extension"
    """``magic`` | ``extension`` | ``content`` | ``fallback``."""

    note: str = ""

    @property
    def supported(self) -> bool:
        return not self.key.startswith("unsupported")


def _sniff_zip(path: Path) -> tuple[str, str]:
    """Look inside a zip container to find out what it really is."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for marker, kind in _ZIP_KINDS:
                if marker in names:
                    if kind != "odf_or_epub":
                        return kind, "magic"
                    try:
                        mime = zf.read("mimetype").decode("ascii", "replace").strip()
                    except Exception:
                        mime = ""
                    if mime.startswith("application/epub"):
                        return "epub", "magic"
                    return "odf", "magic"
            if any(n.startswith("META-INF/") for n in names):
                return "epub", "magic"
    except zipfile.BadZipFile:
        # The magic bytes said "zip" but the structure is broken.  Report it as
        # an archive anyway so the failure is explained ("corrupt zip") rather
        # than mislabelled as "opaque binary".
        return "archive", "magic"
    except Exception:
        return "archive", "magic"
    return "archive", "magic"


def _detect_encoding(raw: bytes) -> tuple[str, str]:
    """Best-effort text decoding. Returns (encoding, decoded sample).

    Order matters more than sophistication here.  GB18030 is tried before any
    statistical detector because it is a strict superset of GBK/GB2312 *and* can
    decode almost any byte sequence, so it never fails -- whereas
    ``charset_normalizer`` confidently reports GBK-encoded Chinese as Big5,
    which produces plausible-looking garbage.  A wrong-but-confident guess is
    the exact failure mode this module exists to avoid.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", raw[:4096].decode("utf-8-sig", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", raw[:4096].decode("utf-16", "replace")
    try:
        return "utf-8", raw[:65536].decode("utf-8")
    except UnicodeDecodeError:
        pass

    # Not UTF-8 and not BOM-marked: it is almost certainly a CJK legacy encoding
    # (GBK/GB2312/GB18030) or a single-byte Western one.  GB18030 decodes both
    # legacy Chinese sets correctly; ASCII is unaffected either way.
    try:
        decoded = raw[:65536].decode("gb18030")
        # Only accept when it does not silently produce replacement characters.
        if "\ufffd" not in decoded:
            return "gb18030", decoded
    except UnicodeDecodeError:
        pass

    try:
        import charset_normalizer  # type: ignore

        best = charset_normalizer.from_bytes(raw[:262144]).best()
        if best and best.encoding:
            return best.encoding, str(best)[:4096]
    except Exception:
        pass
    try:
        import chardet  # type: ignore

        guess = chardet.detect(raw[:262144])
        if guess.get("encoding") and (guess.get("confidence") or 0) > 0.5:
            enc = guess["encoding"]
            return enc, raw[:65536].decode(enc, "replace")
    except Exception:
        pass
    return "utf-8", raw[:65536].decode("utf-8", "replace")


def _looks_like_json(sample: str) -> bool:
    stripped = sample.lstrip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(sample if len(sample) < 65536 else sample[:65536])
        return True
    except Exception:
        # Truncated but still obviously JSON.
        return bool(re.match(r"^\s*[{[]\s*[\"}\]{]", stripped))


def _looks_like_csv(sample: str) -> bool:
    lines = [ln for ln in sample.splitlines() if ln.strip()][:12]
    if len(lines) < 2:
        return False
    counts: dict[str, list[int]] = {}
    for delim in (",", "\t", ";", "|"):
        counts[delim] = [ln.count(delim) for ln in lines]
    for delim, per_line in counts.items():
        if all(c >= 1 for c in per_line):
            spread = max(per_line) - min(per_line)
            if spread <= max(1, max(per_line) * 0.34):
                return True
    return False


def _looks_like_html(sample: str) -> bool:
    head = sample[:2048].lower()
    return bool(
        re.search(r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]", head)
        or len(re.findall(r"<(div|p|span|a|table|ul|li|h[1-6])[\s>]", head)) >= 3
    )


def detect(path: Path | str, *, max_bytes: int | None = None, sniff: bool = True) -> Format:
    """Identify what ``path`` is.

    Never raises for unknown formats -- returns a ``unsupported_*`` key instead
    so the caller can degrade with a clear warning rather than a traceback.
    """
    path = Path(path)
    ext = path.suffix.lower()
    name = path.name.lower()
    key = EXT_MAP.get(ext) or NAME_MAP.get(name) or ""
    lang = EXT_TO_LANG.get(ext, "")

    if not sniff:
        key = key or "text"
        return Format(key, ext, _MEDIA_TYPES.get(key, "text/plain"), lang)

    try:
        check_size(path, max_bytes)
        head = path.open("rb").read(8192)
    except OSError:
        head = b""

    # --- binary magic first: it beats an extension that lies.
    for magic, kind in MAGIC:
        if head.startswith(magic):
            if kind == "zip":
                zkey, conf = _sniff_zip(path)
                return Format(zkey, ext or ".zip", _MEDIA_TYPES.get(zkey, "application/zip"), lang, confidence=conf)
            if kind == "ole":
                ole_key = {
                    ".doc": "doc_legacy",
                    ".xls": "xls_legacy",
                    ".ppt": "ppt_legacy",
                    ".msg": "msg_legacy",
                }.get(ext, "unsupported_binary")
                return Format(ole_key, ext, _MEDIA_TYPES.get(ole_key, "application/octet-stream"), lang, confidence="magic")
            if kind == "riff" and head[8:12] == b"WEBP":
                return Format("image", ext or ".webp", "image/webp", confidence="magic")
            if kind in ("audio", "video", "riff"):
                return Format("unsupported_media", ext, f"{kind}/*", confidence="magic",
                              note="audio/video transcription is not implemented yet")
            if kind == "image":
                return Format("image", ext, "image/*", confidence="magic")
            return Format(kind, ext, _MEDIA_TYPES.get(kind, "application/octet-stream"), lang, confidence="magic")

    # --- binary heuristic: NUL bytes in the first 8 KiB mean "not text".
    if b"\x00" in head:
        if key and key != "text":
            return Format(key, ext, _MEDIA_TYPES.get(key, "application/octet-stream"), lang)
        return Format("unsupported_binary", ext, "application/octet-stream", confidence="content",
                      note="file looks binary and its type is not recognised")

    encoding = "utf-8"
    sample = ""
    if head:
        try:
            encoding, sample = _detect_encoding(head if len(head) < 8192 else path.open("rb").read(65536))
        except OSError:
            encoding, sample = "utf-8", head.decode("utf-8", "replace")

    # --- content beats extension for the ambiguous text families.
    if key in ("", "text"):
        if _looks_like_html(sample):
            key, lang, conf = "html", "html", "content"
        elif _looks_like_json(sample):
            key, lang, conf = "json", "json", "content"
        elif _looks_like_csv(sample):
            key, lang, conf = "csv", "csv", "content"
        elif ext in (".md", ".markdown") or re.search(r"^#{1,6}\s+\S", sample, re.M):
            key, lang, conf = "markdown", "markdown", "content"
        else:
            key, lang, conf = "text", "text", "fallback"
        return Format(key, ext, _MEDIA_TYPES.get(key, "text/plain"), lang, encoding, conf)

    return Format(key, ext, _MEDIA_TYPES.get(key, "application/octet-stream"), lang, encoding)
