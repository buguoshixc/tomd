#!/usr/bin/env python
"""Dependency-free sanity check over every source file in the repository.

Two passes, both using only the standard library so the check runs identically
on every interpreter and cannot rot:

1. **Compile** every ``.py`` file.  Catches syntax errors anywhere, including in
   files no test happens to import.
2. **Import** the shipped package and every converter module.  This is the
   failure mode that actually hurts a converter: a module that raises on import
   silently takes a whole format family with it, and the symptom is "unknown
   format" rather than a traceback.

    python scripts/check_sources.py            # check everything
    python scripts/check_sources.py --quiet    # only report problems

Exit code is 0 when clean, 1 when something is broken.
"""

from __future__ import annotations

import argparse
import importlib
import py_compile
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Directories that hold first-party Python.
SOURCE_DIRS = ("tomd", "tests", "scripts")


def iter_sources() -> list[Path]:
    found: list[Path] = []
    for name in SOURCE_DIRS:
        directory = ROOT / name
        if directory.is_dir():
            found += sorted(directory.rglob("*.py"))
    root_files = sorted(ROOT.glob("*.py"))
    return root_files + found


def compile_all(quiet: bool) -> list[str]:
    failures: list[str] = []
    for path in iter_sources():
        relative = path.relative_to(ROOT)
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(ROOT / ".pyc-check"))
        except py_compile.PyCompileError as exc:
            failures.append(f"{relative}: {exc.msg.strip()}")
            continue
        if not quiet:
            print(f"  compiled {relative}")
    return failures


def import_package(quiet: bool) -> list[str]:
    """Import the package and every converter, independent of __init__ side effects."""
    failures: list[str] = []
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    targets = ["tomd", "tomd.cli", "tomd.server", "tomd.engine", "tomd.registry",
               "tomd.detect", "tomd.security", "tomd.mdutil", "tomd.model"]
    converters = sorted((ROOT / "tomd" / "converters").glob("*.py"))
    targets += [f"tomd.converters.{p.stem}" for p in converters if p.stem != "__init__"]

    for module in targets:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001 - the point is to report everything
            failures.append(f"{module}: import failed\n{traceback.format_exc(limit=3)}")
            continue
        if not quiet:
            print(f"  imported {module}")

    # Every format the detector can name must have a converter, otherwise the
    # user gets "no converter is registered" for a file we claimed to support.
    from tomd.detect import EXT_MAP, NAME_MAP
    from tomd.registry import get_registry

    registry = get_registry()
    known = set(EXT_MAP.values()) | set(NAME_MAP.values())
    missing = sorted(key for key in known if not registry.chains(key))
    if missing:
        failures.append("detector names formats with no converter: " + ", ".join(missing))
    elif not quiet:
        print(f"  {len(known)} detected formats, all have a converter chain")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile and import check")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args()

    print("compiling sources")
    failures = compile_all(args.quiet)

    print("importing package and converters")
    failures += import_package(args.quiet)

    # clean up the throwaway bytecode file
    leftover = ROOT / ".pyc-check"
    if leftover.exists():
        leftover.unlink()

    if failures:
        print(f"\n{len(failures)} problem(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("\nOK: all sources compile and import cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
