"""Core data model shared by every converter.

The whole point of this module is that each converter only has to produce
*structure* (a title, some markdown parts, a list of warnings).  It never has
to care about output paths, front matter, asset rewriting or reporting --
:mod:`tomd.engine` owns all of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Status(str, Enum):
    """How much we trust the produced markdown."""

    OK = "ok"
    """Everything we recognised was converted."""

    PARTIAL = "partial"
    """Usable output, but something was skipped or approximated (see warnings)."""

    EMPTY = "empty"
    """No content could be recovered.  Typically a scanned PDF with no text layer."""

    FAILED = "failed"
    """A converter raised.  Another converter in the chain may still succeed."""


#: Statuses that mean "this result is not worth keeping / should not be written".
BAD_STATUSES = (Status.EMPTY, Status.FAILED)


@dataclass
class Warning_:
    """A single thing that went wrong, worth telling the user about."""

    code: str
    """Stable machine-readable id, e.g. ``scanned-pdf``, ``table-degraded``."""

    message: str
    """Human readable explanation."""

    severity: str = "warning"  # "info" | "warning" | "error"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.severity}:{self.code}] {self.message}"


@dataclass
class Asset:
    """A binary payload extracted from the source (an image, usually)."""

    filename: str
    """Suggested basename, already sanitised by :mod:`tomd.security`."""

    data: bytes
    media_type: str = "application/octet-stream"
    origin: str = ""
    """Where it came from, e.g. ``page 3`` -- shown in the report."""


@dataclass
class Document:
    """The intermediate representation every converter returns."""

    title: str = ""
    parts: list[str] = field(default_factory=list)
    """Markdown fragments; joined with a blank line by the engine."""

    assets: list[Asset] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    status: Status = Status.OK

    # ------------------------------------------------------------------ API

    def add(self, *parts: str) -> None:
        """Append markdown fragments, ignoring empty ones."""
        for part in parts:
            if part is None:
                continue
            text = part if isinstance(part, str) else str(part)
            if text.strip():
                self.parts.append(text)

    def warn(self, code: str, message: str, severity: str = "warning") -> None:
        self.warnings.append(Warning_(code=code, message=message, severity=severity))
        # A "warning" already means the output is not perfect.
        if severity in ("warning", "error") and self.status is Status.OK:
            self.status = Status.PARTIAL

    def set_status(self, status: Status) -> None:
        """Set status unless a worse one is already recorded."""
        order = {Status.OK: 0, Status.PARTIAL: 1, Status.FAILED: 2, Status.EMPTY: 3}
        if order[status] >= order[self.status]:
            self.status = status

    @property
    def markdown(self) -> str:
        """The body only: parts joined with blank lines, trailing newline added."""
        body = "\n\n".join(p.strip("\n") for p in self.parts if p and p.strip())
        # Collapse runs of 3+ blank lines that stitching fragments can create.
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        return body + "\n" if body else ""

    def is_empty(self) -> bool:
        return not self.markdown.strip()


def relpath(path: Path | str) -> str:
    """Render an asset path with forward slashes (markdown is URL-ish)."""
    return str(path).replace("\\", "/")
