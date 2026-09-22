"""The engine: detect -> convert -> post-process -> write.

Everything that is *not* format-specific lives here, so that converters stay
small and comparable:

* asset management (``assets/`` next to the output, content-addressed),
* front matter,
* the conversion report (both a markdown appendix and an optional ``.json``),
* the output layout rules (single file, batch mirror, stdout),
* the batch walker.

Public entry points: :func:`convert`, :func:`convert_many`, :func:`build_options`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import mdutil as md
from .detect import Format, detect
from .model import BAD_STATUSES, Asset, Document, Status
from .registry import get_registry
from .security import SecurityError, ensure_within, sanitize_filename, sanitize_stem

# --------------------------------------------------------------- file walking

#: Directories that are never worth walking into during a batch run.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
    ".vscode", "$RECYCLE.BIN", ".dsh", ".agent-teams",
}


def _safe_resolve(path: Path) -> Path | None:
    try:
        return Path(path).resolve()
    except OSError:
        return None


def iter_source_files(
    root: Path,
    *,
    patterns: list[str] | None = None,
    exclude: list[str] | None = None,
    recursive: bool = True,
    skip_dirs: set[str] | None = None,
    output_dir: Path | None = None,
) -> list[Path]:
    """Collect input files under ``root`` (deterministic, sorted, filtered).

    ``output_dir`` is excluded from the walk when it lives inside ``root``.  That
    happens with the default ``<root>/.md`` layout, and without this the second
    run reconverts its own output -- including the batch report, which then shows
    up as an input file.
    """
    root = Path(root)
    skip = SKIP_DIRS | (skip_dirs or set())
    patterns = patterns or ["*"]
    exclude = exclude or []
    found: list[Path] = []

    excluded_root: Path | None = None
    if output_dir is not None:
        try:
            excluded_root = Path(output_dir).resolve()
        except OSError:
            excluded_root = None

    candidates = root.rglob("*") if recursive else root.glob("*")
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in skip for part in path.relative_to(root).parts[:-1]):
            continue
        if excluded_root is not None:
            try:
                path.resolve().relative_to(excluded_root)
                continue  # this file is our own previous output
            except (ValueError, OSError):
                pass
        name = path.name
        if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
            continue
        if any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(str(path), pat) for pat in exclude):
            continue
        found.append(path)

    return sorted(found, key=lambda p: str(p).lower())


# -------------------------------------------------------------------- options


@dataclass
class ConvertOptions:
    """Every knob the engine and the converters understand.

    Passed to converters as a plain dict (``options.as_dict()``) so converters
    never depend on this class -- that keeps them trivially unit-testable.
    """

    # --- output
    output_dir: Path | None = None
    suffix: str = ".md"
    overwrite: bool = False
    mirror: bool = True
    """Batch mode: reproduce the input tree instead of flattening it."""

    assets_dir: str = "assets"
    write_assets: bool = True

    # --- document shape
    front_matter: bool = True
    report: bool = True
    report_json: bool = False
    keep_going: bool = True

    # --- limits
    max_bytes: int | None = 512 * 1024 * 1024
    pdf_max_pages: int | None = None

    # --- extraction toggles
    extract_images: bool = True
    keep_links: bool = True
    keep_images: bool = True
    drop_chrome: bool = True
    html_engine: str = "auto"

    # --- format-specific
    pdf_tables: bool = True
    pdf_page_details: bool = True
    pdf_page_markers: bool = True
    csv_max_rows: int = 50
    csv_max_cols: int = 16
    csv_header: bool = True
    delimiter: str | None = None
    xlsx_max_rows: int = 200
    json_max_rows: int = 200
    json_table: bool = True
    pptx_notes: bool = True
    pptx_tables: bool = True
    notebook_outputs: bool = True
    subtitle_table: bool = False
    numbered_headings: bool = False
    min_heading_len: int = 3
    code_outline: bool = True
    eml_prefer_html: bool = False
    recurse_archives: bool = False

    # --- behaviour
    force_format: str | None = None
    sniff: bool = True
    debug: bool = False

    def as_dict(self) -> dict:
        data = {
            k: (str(v) if isinstance(v, Path) else v)
            for k, v in self.__dict__.items()
        }
        return data

    def replace(self, **kwargs) -> ConvertOptions:
        return replace(self, **kwargs)


def build_options(**kwargs) -> ConvertOptions:
    """Build options from keyword arguments, ignoring unknown keys."""
    known = {f.name for f in ConvertOptions.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    clean = {k: v for k, v in kwargs.items() if k in known and v is not None}
    return ConvertOptions(**clean)


# --------------------------------------------------------------------- assets


class AssetWriter:
    """Writes extracted assets next to the output, content-addressed.

    Two identical images inside one document are written once; two *different*
    images that claim the same name get a short hash suffix instead of one
    silently overwriting the other.
    """

    def __init__(self, base: Path, subdir: str = "assets") -> None:
        self.base = Path(base)
        self.subdir = subdir
        self.by_hash: dict[str, str] = {}
        self.written: list[Path] = []

    def write_all(self, assets: list[Asset]) -> dict[str, str]:
        """Write every asset, returning ``{original filename: relative url}``."""
        mapping: dict[str, str] = {}
        if not assets:
            return mapping
        target_dir = self.base / self.subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        for asset in assets:
            digest = hashlib.sha1(asset.data).hexdigest()
            if digest in self.by_hash:
                mapping[asset.filename] = self.by_hash[digest]
                continue
            name = sanitize_filename(asset.filename)
            path = target_dir / name
            if path.exists() and hashlib.sha1(path.read_bytes()).hexdigest() != digest:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem or name}-{digest[:8]}{dot}{ext}" if dot else f"{name}-{digest[:8]}"
                path = target_dir / name
            try:
                ensure_within(self.base, path)
                path.write_bytes(asset.data)
            except (OSError, SecurityError):
                continue
            url = f"{self.subdir}/{name}".replace("\\", "/")
            self.by_hash[digest] = url
            mapping[asset.filename] = url
            self.written.append(path)
        return mapping


# --------------------------------------------------------------------- result


@dataclass
class ConversionResult:
    source: Path
    format: Format
    document: Document
    converter: str = ""
    output: Path | None = None
    assets: list[Path] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    skipped: bool = False
    written: bool = False

    @property
    def status(self) -> Status:
        return self.document.status

    @property
    def ok(self) -> bool:
        return self.status not in BAD_STATUSES

    @property
    def warnings(self) -> list:
        return self.document.warnings

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "format": self.format.key,
            "media_type": self.format.media_type,
            "detected_by": self.format.confidence,
            "converter": self.converter,
            "status": self.status.value,
            "output": str(self.output) if self.output else None,
            "assets": [str(a) for a in self.assets],
            "elapsed_ms": round(self.elapsed * 1000, 1),
            "skipped": self.skipped,
            "warnings": [
                {"code": w.code, "severity": w.severity, "message": w.message}
                for w in self.document.warnings
            ],
            "meta": {k: _jsonable(v) for k, v in self.document.meta.items()},
            "error": self.error,
        }


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


# ------------------------------------------------------------------ assembling


def build_front_matter(result: ConversionResult, options: ConvertOptions) -> str:
    doc = result.document
    data: dict = {
        "source": result.source.name,
        "format": result.format.key,
        "converter": result.converter,
        "converted": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if doc.title:
        data["title"] = doc.title[:200]
    for key in ("author", "creator", "language", "date", "pages", "sheets", "slides", "rows"):
        if key in doc.meta:
            data[key] = doc.meta[key]
    if doc.status is not Status.OK:
        data["fidelity"] = doc.status.value
    codes = sorted({w.code for w in doc.warnings if w.severity != "info"})
    if codes:
        data["warnings"] = codes
    return md.front_matter(data)


def build_report(result: ConversionResult, options: ConvertOptions) -> str:
    doc = result.document
    by_severity: dict[str, list] = {}
    for warning in doc.warnings:
        by_severity.setdefault(warning.severity, []).append(warning)

    lines = [
        "<!-- tomd:report -->",
        "## Conversion report",
        "",
        md.kv_table(
            [
                ("Source", result.source.name),
                ("Detected as", f"{result.format.key} ({result.format.media_type})"),
                ("Identified by", result.format.confidence),
                ("Converter", result.converter),
                ("Status", doc.status.value),
                ("Time", f"{result.elapsed * 1000:.0f} ms"),
            ],
            key_header="Item",
        ),
    ]
    if doc.meta:
        interesting = {
            k: v for k, v in doc.meta.items()
            if k not in ("front_matter",) and not isinstance(v, (bytes, bytearray))
        }
        if interesting:
            lines += ["", "**Extracted facts**", "",
                      md.kv_table([(k, _jsonable(v)) for k, v in list(interesting.items())[:20]],
                                  key_header="Key")]

    order = {"error": 0, "warning": 1, "info": 2}
    for severity in sorted(by_severity, key=lambda s: order.get(s, 3)):
        bucket = by_severity[severity]
        lines += ["", f"**{severity.capitalize()} ({len(bucket)})**", ""]
        lines += [f"- `{w.code}` — {w.message}" for w in bucket]

    if not doc.warnings:
        lines += ["", "No issues detected."]
    lines += ["", "<!-- /tomd:report -->"]
    return "\n".join(lines)


def _title_in_body(body: str, title: str) -> bool:
    """Does the rendered body already contain ``title`` as heading text?

    Compared per line so a PDF's metadata title ("Annual Report 2025") and its
    first heading ("Annual Report") are treated as the same title -- the document
    already says it, and printing both produces two competing H1s.  A book
    collection, where the volume title is genuinely absent from the chapters,
    still gets its title, because no line matches it.
    """
    target = md.squash(title).lower()
    if not target:
        return True
    for line in body.split("\n"):
        candidate = md.squash(re.sub(r"^[#>\-*\s]+", "", line)).lower()
        candidate = candidate.strip("*_` ")
        if not candidate:
            continue
        if candidate == target or candidate.startswith(target) or target.startswith(candidate):
            if min(len(candidate), len(target)) >= 4:
                return True
    return False


def assemble(result: ConversionResult, options: ConvertOptions) -> str:
    """Produce the final markdown text for a result."""
    doc = result.document
    parts: list[str] = []
    if options.front_matter:
        parts.append(build_front_matter(result, options))

    body = doc.markdown
    # Only inject an H1 from metadata when the body does not already say it.
    if doc.title and not _title_in_body(body, doc.title):
        parts.append(md.heading(1, doc.title))
    if body:
        parts.append(body)
    if options.report:
        parts.append(build_report(result, options))
    text = "\n\n".join(p.strip("\n") for p in parts if p and p.strip())
    return text.rstrip() + "\n"


# ------------------------------------------------------------------ single run


def convert(
    path: Path | str,
    options: ConvertOptions | None = None,
    *,
    output: Path | str | None = None,
    write: bool = False,
) -> ConversionResult:
    """Convert one file.  Never raises for bad input -- inspect ``result.status``.

    ``write=True`` writes the markdown (and assets) to ``output``; with
    ``write=False`` you get the result object and can read ``result.text``
    after calling :func:`render`.
    """
    started = time.perf_counter()
    path = Path(path)
    options = options or ConvertOptions()
    document = Document()
    fmt = Format("unsupported_format", path.suffix.lower(), "application/octet-stream", confidence="fallback")

    if not path.is_file():
        document.set_status(Status.FAILED)
        document.warn("not-a-file", f"{path} is not a readable file", "error")
        return ConversionResult(path, fmt, document, error="not a file")

    try:
        if options.force_format:
            from .detect import EXT_MAP, NAME_MAP, EXT_TO_LANG, _MEDIA_TYPES

            key = options.force_format
            fmt = Format(
                key,
                path.suffix.lower(),
                _MEDIA_TYPES.get(key, "text/plain"),
                EXT_TO_LANG.get(path.suffix.lower(), ""),
                confidence="forced",
            )
        else:
            fmt = detect(path, max_bytes=options.max_bytes, sniff=options.sniff)
    except SecurityError as exc:
        document.set_status(Status.FAILED)
        document.warn("security-limit", str(exc), "error")
        return ConversionResult(path, fmt, document, error=str(exc))

    registry = get_registry()
    result = registry.run(fmt.key, path, options.as_dict())
    document = result.doc

    conversion = ConversionResult(
        source=path,
        format=fmt,
        document=document,
        converter=result.converter.name,
        elapsed=time.perf_counter() - started,
    )

    # --- assets
    if document.assets:
        base = Path(output).parent if output else (options.output_dir or path.parent)
        if options.write_assets and write:
            writer = AssetWriter(base, options.assets_dir)
            mapping = writer.write_all(document.assets)
            conversion.assets = writer.written
            # Rewrite the links of any asset that had to be renamed to avoid a
            # filename collision, so the markdown points at what we wrote.
            for original, url in mapping.items():
                default_url = f"{options.assets_dir}/{original}"
                if url != default_url:
                    document.parts = [
                        part.replace(f"({default_url})", f"({url})") for part in document.parts
                    ]

    if write:
        target = Path(output) if output else default_output_path(path, options)
        try:
            conversion.output = write_result(conversion, target, options)
            conversion.written = True
        except (OSError, SecurityError) as exc:
            document.warn("write-failed", f"could not write {target}: {exc}", "error")
            document.set_status(Status.FAILED)
            conversion.error = str(exc)

    conversion.elapsed = time.perf_counter() - started
    return conversion


def default_output_path(source: Path, options: ConvertOptions) -> Path:
    """Where a single file goes when no explicit output is given."""
    base = Path(options.output_dir) if options.output_dir else source.parent / ".md"
    stem = sanitize_stem(source.name)
    return base / f"{stem}{options.suffix}"


def render(result: ConversionResult, options: ConvertOptions | None = None) -> str:
    """The final markdown text of a result (without writing it)."""
    return assemble(result, options or ConvertOptions())


def write_result(result: ConversionResult, target: Path, options: ConvertOptions) -> Path:
    target = Path(target)
    if target.exists() and not options.overwrite:
        stem, dot, ext = target.name.rpartition(".")
        stamp = datetime.now().strftime("%H%M%S")
        target = target.with_name(f"{stem or target.name}.{stamp}.{ext}" if dot else f"{target.name}.{stamp}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if not result.ok and options.report:
        # Keep a report even for failures: an empty .md with an explanation is
        # more useful than a missing file.
        pass

    target.write_text(render(result, options), encoding="utf-8")
    if options.report_json:
        sidecar = target.with_suffix(target.suffix + ".json")
        sidecar.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return target


# ------------------------------------------------------------------- batch run


@dataclass
class BatchSummary:
    total: int = 0
    converted: int = 0
    partial: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[ConversionResult] = field(default_factory=list)

    def add(self, result: ConversionResult) -> None:
        self.total += 1
        self.results.append(result)
        if result.skipped:
            self.skipped += 1
        elif result.status is Status.OK:
            self.converted += 1
        elif result.status is Status.PARTIAL:
            self.partial += 1
        elif result.status is Status.EMPTY:
            self.empty += 1
        else:
            self.failed += 1

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "converted": self.converted,
            "partial": self.partial,
            "empty": self.empty,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": [r.to_dict() for r in self.results],
        }


def output_for_batch(source: Path, root: Path, options: ConvertOptions) -> Path:
    """Map an input path onto its output path, mirroring the tree."""
    base = Path(options.output_dir) if options.output_dir else root / ".md"
    stem = sanitize_stem(source.name)
    if options.mirror:
        try:
            relative = source.relative_to(root).parent
        except ValueError:
            relative = Path()
        target = base / relative / f"{stem}{options.suffix}"
    else:
        target = base / f"{stem}{options.suffix}"
    ensure_within(base, target)
    return target


def convert_many(
    root: Path | str,
    options: ConvertOptions | None = None,
    *,
    patterns: list[str] | None = None,
    exclude: list[str] | None = None,
    recursive: bool = True,
    on_result=None,
    max_workers: int = 1,
    skip_paths: list[Path] | None = None,
) -> BatchSummary:
    """Convert every matching file under ``root``.

    ``max_workers > 1`` uses threads -- conversion is dominated by I/O and
    (for PDFs) C code that releases the GIL, so it scales acceptably without
    the pickling constraints of multiprocessing.

    ``on_result`` is called after each file so a CLI can print progress live.
    ``skip_paths`` names files that must never be treated as input (the batch
    report being written into the same tree, typically).
    """
    root = Path(root)
    options = options or ConvertOptions()
    summary = BatchSummary()

    # Resolve the output root so the walk can skip our own previous output.
    output_root = Path(options.output_dir) if options.output_dir else root / ".md"
    files = iter_source_files(
        root,
        patterns=patterns,
        exclude=exclude,
        recursive=recursive,
        output_dir=output_root,
    )

    if skip_paths:
        blocked = set()
        for candidate in skip_paths:
            try:
                blocked.add(Path(candidate).resolve())
            except OSError:
                continue
        files = [f for f in files if _safe_resolve(f) not in blocked]

    def handle(path: Path) -> ConversionResult:
        target = output_for_batch(path, root, options)
        if target.exists() and not options.overwrite:
            result = ConversionResult(
                source=path,
                format=Format("skipped", path.suffix.lower(), "", confidence="cached"),
                document=Document(),
                skipped=True,
            )
            return result
        if not options.keep_going:
            return convert(path, options, output=target, write=True)
        try:
            return convert(path, options, output=target, write=True)
        except Exception as exc:  # noqa: BLE001 - batch must survive anything
            doc = Document(status=Status.FAILED)
            doc.warn("internal-error", f"{type(exc).__name__}: {exc}", "error")
            return ConversionResult(
                source=path,
                format=Format("unknown", path.suffix.lower(), "", confidence="error"),
                document=doc,
                error=str(exc),
            )

    if max_workers and max_workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(handle, path): path for path in files}
            for future in as_completed(futures):
                result = future.result()
                summary.add(result)
                if on_result:
                    on_result(result)
    else:
        for path in files:
            result = handle(path)
            summary.add(result)
            if on_result:
                on_result(result)

    return summary


def write_batch_report(summary: BatchSummary, target: Path) -> Path:
    """Write a machine-readable report for a batch run."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def format_batch_report(summary: BatchSummary) -> str:
    """A human-readable summary table for the terminal."""
    rows = [
        [
            str(r.source),
            r.format.key,
            r.status.value,
            r.converter,
            f"{r.elapsed * 1000:.0f}ms",
            "; ".join(w.code for w in r.document.warnings if w.severity != "info") or "-",
        ]
        for r in summary.results
    ]
    header = "| File | Format | Status | Converter | Time | Warnings |\n|---|---|---|---|---|---|"
    body = "\n".join("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in rows)
    counts = (
        f"\n\n**{summary.total} files** — "
        f"{summary.converted} clean, {summary.partial} with warnings, "
        f"{summary.empty} empty, {summary.failed} failed, {summary.skipped} skipped."
    )
    return header + "\n" + body + counts
