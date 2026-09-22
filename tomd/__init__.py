"""tomd -- convert (almost) any file to Markdown.

Quick start::

    from tomd import convert, ConvertOptions

    result = convert("report.pdf")
    print(result.document.markdown)     # the markdown body
    print(result.status, result.warnings)

    # or write it out, assets included:
    convert("report.pdf", ConvertOptions(), write=True)

Design notes live in README.md.  The short version:

* :mod:`tomd.detect` decides *what* a file is,
* :mod:`tomd.registry` picks the best converter that is actually installed,
* :mod:`tomd.converters.*` do the format work and return a
  :class:`~tomd.model.Document`,
* :mod:`tomd.engine` adds front matter, assets, the report and the output layout.
"""

from __future__ import annotations

from .engine import (
    AssetWriter,
    BatchSummary,
    ConversionResult,
    ConvertOptions,
    build_options,
    convert,
    convert_many,
    default_output_path,
    format_batch_report,
    iter_source_files,
    output_for_batch,
    render,
    write_batch_report,
)
from .model import Asset, Document, Status, Warning_
from .registry import get_registry

__version__ = "0.1.0"

__all__ = [
    "Asset",
    "AssetWriter",
    "BatchSummary",
    "ConversionResult",
    "ConvertOptions",
    "Document",
    "Status",
    "Warning_",
    "__version__",
    "build_options",
    "convert",
    "convert_many",
    "default_output_path",
    "format_batch_report",
    "get_registry",
    "iter_source_files",
    "output_for_batch",
    "render",
    "write_batch_report",
]
