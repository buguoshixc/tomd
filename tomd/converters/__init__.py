"""Converter package.

Importing this package registers every converter into
:data:`tomd.registry.registry`.  Order matters only for readability -- the
registry sorts by priority -- but keeping it grouped by family makes
``tomd --formats`` output easy to scan.
"""

from __future__ import annotations

from . import data, ebook, html, office, pdf, text, unsupported  # noqa: F401

__all__ = ["data", "ebook", "html", "office", "pdf", "text", "unsupported"]
