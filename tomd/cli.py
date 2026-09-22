"""Command line interface.

Three shapes of use, because those are the three shapes people actually have:

1. one file -> stdout (``tomd report.pdf``),
2. one file -> a file (``tomd report.pdf -o report.md``),
3. a whole tree -> a mirrored tree (``tomd ./docs --out ./out``).

Exit codes are meaningful so it can be used in a pipeline:
``0`` all good, ``1`` completed with warnings/failures, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engine import (
    ConvertOptions,
    build_options,
    convert,
    convert_many,
    format_batch_report,
    render,
    write_batch_report,
)
from .model import Status
from .registry import get_registry

PROG = "tomd"


def _force_utf8_streams() -> None:
    """Make stdout/stderr survive non-ASCII output on any locale.

    Converted documents routinely contain CJK, em dashes and arrows.  On a
    Windows console whose code page is GBK/cp1252, writing those raises
    ``UnicodeEncodeError`` and kills the run *after* the conversion succeeded --
    the worst possible failure mode.  Reconfiguring (3.7+) fixes it; older
    interpreters get a lossy-but-alive fallback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass

_EPILOG = """\
examples:
  tomd report.pdf                      convert one file, print to stdout
  tomd report.pdf -o report.md         convert one file to a file
  tomd ./docs --out ./out              convert a whole tree, mirroring it
  tomd ./docs --out ./out --workers 8  same, in parallel
  tomd paper.pdf --no-report --no-front-matter   clean output for an LLM
  tomd data.csv --csv-max-rows 500     force a big CSV into a real table
  tomd mystery.bin --format text       override format detection
  tomd --formats                       show every supported format
  tomd --server                        start the local web UI
"""


def _add_common(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("output")
    group.add_argument("-o", "--output", metavar="PATH",
                       help="output file (single input) or output directory (many inputs)")
    group.add_argument("--suffix", default=".md", help="output extension (default: .md)")
    group.add_argument("--overwrite", action="store_true", help="overwrite existing outputs")
    group.add_argument("--no-mirror", action="store_true",
                       help="flatten the input tree instead of mirroring it")
    group.add_argument("--no-front-matter", action="store_true",
                       help="omit the YAML front matter block")
    group.add_argument("--no-report", action="store_true",
                       help="omit the conversion report appendix")
    group.add_argument("--report-json", action="store_true",
                       help="also write <output>.json with structured results")
    group.add_argument("--stdout", action="store_true", help="print markdown instead of writing files")
    group.add_argument("--quiet", "-q", action="store_true", help="only print errors")

    doc = parser.add_argument_group("document shaping")
    doc.add_argument("--no-links", action="store_true", help="drop hyperlinks from HTML/EPUB")
    doc.add_argument("--no-images", action="store_true", help="do not extract images")
    doc.add_argument("--no-tables", action="store_true", help="disable PDF table detection")
    doc.add_argument("--page-markers", action="store_true",
                     help="use <!-- page N --> markers instead of <details> blocks in PDF output")
    doc.add_argument("--html-engine", choices=["auto", "markdownify", "builtin"], default="auto",
                     help="HTML renderer to use (default: auto)")

    limits = parser.add_argument_group("limits and detection")
    limits.add_argument("--format", dest="force_format", metavar="KEY",
                        help="force a converter key (see --formats)")
    limits.add_argument("--max-mb", type=float, default=512.0,
                        help="refuse files bigger than this many MB (default: 512)")
    limits.add_argument("--pdf-max-pages", type=int, help="only convert the first N pages of a PDF")
    limits.add_argument("--xlsx-max-rows", type=int, default=200, help="rows per sheet (default: 200)")
    limits.add_argument("--csv-max-rows", type=int, default=50,
                        help="rows before a CSV falls back to a code block (default: 50)")
    limits.add_argument("--csv-max-cols", type=int, default=16, help="columns before fallback (default: 16)")
    limits.add_argument("--no-csv-header", action="store_true", help="treat the first CSV row as data")
    limits.add_argument("--delimiter", help="CSV delimiter (default: auto-detect)")
    limits.add_argument("--numbered-headings", action="store_true",
                        help="promote '1.2 Title' lines in plain text to headings")
    limits.add_argument("--debug", action="store_true", help="show full tracebacks for converter failures")

    batch = parser.add_argument_group("batch")
    batch.add_argument("--pattern", action="append", metavar="GLOB",
                       help="only convert files matching this glob (repeatable)")
    batch.add_argument("--exclude", action="append", metavar="GLOB",
                       help="skip files matching this glob (repeatable)")
    batch.add_argument("--no-recursive", action="store_true", help="do not descend into subdirectories")
    batch.add_argument("--workers", type=int, default=1, help="parallel workers for batch mode (default: 1)")
    batch.add_argument("--report-to", metavar="PATH", help="write a JSON batch report to this path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Convert files of many formats to Markdown.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--formats", action="store_true",
                        help="list supported formats and the converter used for each, then exit")
    parser.add_argument("--server", action="store_true", help="start the local web service, then exit")
    parser.add_argument("--host", default="127.0.0.1", help="server bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="server port (default: 8765)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit binding the server to a non-loopback address (dangerous)")
    parser.add_argument("inputs", nargs="*", help="files or directories to convert")

    _add_common(parser)
    return parser


def _list_formats() -> int:
    registry = get_registry()
    rows = registry.describe()
    by_format: dict[str, list[dict]] = {}
    for row in rows:
        by_format.setdefault(str(row["format"]), []).append(row)

    width = max((len(k) for k in by_format), default=10)
    print(f"{PROG} {__version__} — supported formats\n")
    print(f"{'FORMAT'.ljust(width)}  CONVERTERS (in order of preference)")
    print("-" * (width + 60))
    unavailable: list[str] = []
    for key in sorted(by_format):
        parts = []
        for row in by_format[key]:
            mark = "" if row["available"] else " (unavailable)"
            if not row["available"]:
                unavailable.append(f"{row['converter']} needs '{row['requires']}'")
            parts.append(f"{row['converter']}{mark}")
        print(f"{key.ljust(width)}  {', '.join(parts)}")

    print(f"\n{len(by_format)} formats, {len(rows)} converters registered.")
    if unavailable:
        print("\nOptional engines not installed (the built-in fallback is used instead):")
        for item in sorted(set(unavailable)):
            print(f"  - {item}")
    print("\nConverters marked '(unavailable)' are skipped automatically; a lower-priority "
          "converter runs instead and the report says so.")
    print("Scanned PDFs and images are detected but NOT transcribed (no OCR in this build).")
    return 0


def _options_from_args(args: argparse.Namespace) -> ConvertOptions:
    return build_options(
        output_dir=args.output,
        suffix=args.suffix,
        overwrite=args.overwrite,
        mirror=not args.no_mirror,
        front_matter=not args.no_front_matter,
        report=not args.no_report,
        report_json=args.report_json,
        max_bytes=None if args.max_mb <= 0 else int(args.max_mb * 1024 * 1024),
        pdf_max_pages=args.pdf_max_pages,
        extract_images=not args.no_images,
        keep_links=not args.no_links,
        keep_images=not args.no_images,
        html_engine=args.html_engine,
        pdf_tables=not args.no_tables,
        pdf_page_details=not args.page_markers,
        pdf_page_markers=True,
        csv_max_rows=args.csv_max_rows,
        csv_max_cols=args.csv_max_cols,
        csv_header=not args.no_csv_header,
        delimiter=args.delimiter,
        xlsx_max_rows=args.xlsx_max_rows,
        numbered_headings=args.numbered_headings,
        force_format=args.force_format,
        debug=args.debug,
    )


def _print_warnings(result, quiet: bool) -> None:
    if quiet:
        return
    for warning in result.warnings:
        if warning.severity == "info":
            continue
        stream = sys.stderr
        print(f"  {warning.severity}: {warning.message}", file=stream)


def _run_single(args: argparse.Namespace, options: ConvertOptions) -> int:
    source = Path(args.inputs[0])
    if not source.is_file():
        print(f"{PROG}: {source} is not a file", file=sys.stderr)
        return 2

    target = None
    if args.output:
        out = Path(args.output)
        target = out if out.suffix.lower() == options.suffix.lower() else (
            out / f"{source.stem}{options.suffix}" if out.is_dir() or not out.suffix else out
        )

    result = convert(source, options, output=target, write=not args.stdout)

    if args.stdout:
        sys.stdout.write(render(result, options))
        sys.stdout.flush()
    elif not args.quiet:
        status = result.status.value
        extra = f" (+{len(result.assets)} asset(s))" if result.assets else ""
        print(f"{source} -> {result.output}  [{result.format.key} / {result.converter} / "
              f"{status}, {result.elapsed * 1000:.0f} ms]{extra}")

    _print_warnings(result, args.quiet)
    if result.status is Status.OK:
        return 0
    if result.status is Status.PARTIAL:
        return 1
    return 1


def _run_batch(args: argparse.Namespace, options: ConvertOptions) -> int:
    root = Path(args.inputs[0])
    if not root.is_dir():
        print(f"{PROG}: {root} is not a directory (use --output to convert a single file)",
              file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"converting {root} ...", file=sys.stderr)

    def on_result(result) -> None:
        if args.quiet:
            return
        mark = {"ok": "ok  ", "partial": "warn", "empty": "none", "failed": "FAIL"}.get(
            result.status.value, "?   "
        )
        name = result.source.name
        if result.skipped:
            print(f"  skip {name} (already converted)")
        else:
            print(f"  {mark} {name}  [{result.format.key}]")
        for warning in result.warnings:
            if warning.severity == "error":
                print(f"       ! {warning.message}", file=sys.stderr)

    summary = convert_many(
        root,
        options,
        patterns=args.pattern,
        exclude=args.exclude,
        recursive=not args.no_recursive,
        on_result=on_result,
        max_workers=max(1, args.workers),
        # The report is written after the run, but a previous report sitting in
        # the same tree would otherwise be converted as if it were an input.
        skip_paths=[Path(args.report_to)] if args.report_to else None,
    )

    print()
    print(format_batch_report(summary))

    if args.report_to:
        path = write_batch_report(summary, Path(args.report_to))
        print(f"\nbatch report written to {path}")

    return 0 if summary.failed == 0 and summary.empty == 0 else 1


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.formats:
        return _list_formats()

    if args.server:
        from .server import serve

        return serve(args.host, args.port, allow_remote=args.allow_remote)

    if not args.inputs:
        parser.print_help()
        return 2

    options = _options_from_args(args)
    inputs = [Path(item) for item in args.inputs]

    if len(inputs) == 1:
        return _run_single(args, options) if inputs[0].is_file() else _run_batch(args, options)

    # Many explicit files: treat their common parent as the batch root.
    if any(item.is_dir() for item in inputs):
        print(f"{PROG}: mixing files and directories is not supported; "
              "pass one directory or a list of files", file=sys.stderr)
        return 2

    parents = {item.parent for item in inputs}
    if len(parents) == 1:
        args.inputs = [str(parents.pop())]
        args.pattern = [item.name for item in inputs]
        return _run_batch(args, options)

    # Different parents: convert each in place, no shared root.
    exit_code = 0
    for item in inputs:
        result = convert(item, options, write=True)
        if not args.quiet:
            print(f"{item.name} -> {result.output} [{result.status.value}]")
        _print_warnings(result, args.quiet)
        exit_code = max(exit_code, 0 if result.status is Status.OK else 1)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
