"""The converter registry: one format key -> an ordered chain of converters.

Design rules
------------
1. **A converter never writes files.** It returns a :class:`~tomd.model.Document`.
2. **A converter never decides what format it is given.** The engine detected it.
3. **A failure is not fatal.** If the best converter raises, the next candidate
   in the chain runs, and the fallback is recorded as a warning.  This is what
   makes "one weird file in a batch of 2000" a non-event instead of a crash.
4. **Missing optional dependencies degrade, they do not explode.** Register a
   converter with ``requires="openpyxl"`` and the registry simply skips it,
   noting that a better engine would have been used had the package been there.
"""

from __future__ import annotations

import importlib.util
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .model import Document, Status

ConverterFn = Callable[[Path, dict], Document]


@dataclass
class Converter:
    key: str
    """Format key this converter handles (must match :mod:`tomd.detect`)."""

    name: str
    fn: ConverterFn
    priority: int = 100
    """Lower runs first.  10 = native/precise, 90 = lossy fallback."""

    requires: str = ""
    """Optional import name; the converter is skipped when it is missing."""

    description: str = ""

    @property
    def available(self) -> bool:
        if not self.requires:
            return True
        try:
            return importlib.util.find_spec(self.requires) is not None
        except (ImportError, ValueError):
            return False


@dataclass
class _Result:
    doc: Document
    converter: Converter
    errors: list[str] = field(default_factory=list)


class Registry:
    """Holds every known converter and resolves the chain for a format."""

    def __init__(self) -> None:
        self._by_key: dict[str, list[Converter]] = {}

    # ------------------------------------------------------------- mutation

    def register(self, converter: Converter, *, replace: bool = False) -> Converter:
        bucket = self._by_key.setdefault(converter.key, [])
        if replace:
            bucket = [c for c in bucket if c.name != converter.name]
            self._by_key[converter.key] = bucket
        bucket.append(converter)
        bucket.sort(key=lambda c: c.priority)
        return converter

    def converter(
        self,
        key: str,
        name: str,
        *,
        priority: int = 100,
        requires: str = "",
        description: str = "",
    ) -> Callable[[ConverterFn], ConverterFn]:
        """Decorator form: ``@registry.converter("pdf", "pymupdf", priority=10)``."""

        def wrap(fn: ConverterFn) -> ConverterFn:
            self.register(
                Converter(key, name, fn, priority=priority, requires=requires, description=description)
            )
            return fn

        return wrap

    # -------------------------------------------------------------- queries

    def chains(self, key: str) -> list[Converter]:
        return list(self._by_key.get(key, ()))

    def available(self, key: str) -> list[Converter]:
        return [c for c in self.chains(key) if c.available]

    def keys(self) -> list[str]:
        return sorted(self._by_key)

    def describe(self) -> list[dict[str, object]]:
        out = []
        for key in self.keys():
            for conv in self.chains(key):
                out.append(
                    {
                        "format": key,
                        "converter": conv.name,
                        "priority": conv.priority,
                        "available": conv.available,
                        "requires": conv.requires,
                        "description": conv.description,
                    }
                )
        return out

    # ------------------------------------------------------------ execution

    def run(self, key: str, path: Path, options: dict) -> _Result:
        """Run the chain for ``key`` until a converter produces content."""
        chain = self.chains(key)
        if not chain:
            doc = Document(status=Status.FAILED)
            doc.warn("no-converter", f"no converter is registered for format '{key}'", "error")
            return _Result(doc, Converter(key, "<none>", lambda *_: Document()), ["no converter"])

        errors: list[str] = []
        best_effort: _Result | None = None
        skipped_unavailable: list[str] = []

        for conv in chain:
            if not conv.available:
                skipped_unavailable.append(f"{conv.name} (needs '{conv.requires}')")
                continue
            try:
                doc = conv.fn(path, options)
            except Exception as exc:  # noqa: BLE001 - a bad file must not kill the batch
                detail = f"{type(exc).__name__}: {exc}"
                errors.append(f"{conv.name} raised {detail}")
                if options.get("debug"):
                    traceback.print_exc()
                continue

            if doc.status is Status.FAILED:
                errors.append(f"{conv.name} reported failure")
                continue

            if doc.is_empty():
                doc.set_status(Status.EMPTY)
                errors.append(f"{conv.name} produced no content")
                if best_effort is None:
                    best_effort = _Result(doc, conv, list(errors))
                continue

            # Success.  Note the fallback if we are not the preferred converter.
            preferred = next((c for c in chain if c.available), conv)
            if conv is not preferred:
                doc.warn(
                    "fallback-converter",
                    f"'{preferred.name}' could not handle this file; "
                    f"used fallback '{conv.name}'. Output fidelity may be lower.",
                )
            if skipped_unavailable and conv is preferred:
                doc.warn(
                    "optional-dependency-missing",
                    "better converters were skipped because optional packages are missing: "
                    + ", ".join(skipped_unavailable)
                    + ". Install them for higher fidelity.",
                    "info",
                )
            return _Result(doc, conv, errors)

        # Everything failed or produced nothing.
        if best_effort is not None:
            best_effort.errors = errors
            best_effort.doc.warn(
                "no-content",
                "no converter could extract content from this file. "
                + (" ".join(errors) if errors else ""),
                "error",
            )
            return best_effort

        doc = Document(status=Status.FAILED)
        doc.warn(
            "all-converters-failed",
            "every registered converter failed for this file. " + " ".join(errors),
            "error",
        )
        return _Result(doc, Converter(key, "<failed>", lambda *_: Document()), errors)


#: Process-wide default registry that converter modules register into.
registry = Registry()


def get_registry() -> Registry:
    """Return the populated default registry, importing all converter modules.

    Thread-safe, and that is not a detail: a parallel batch starts several worker
    threads at once, and without the lock every thread but one observes an empty
    registry and reports ``no-converter`` for perfectly ordinary files.  The lock
    is held across the import so a second thread waits for a *complete* registry
    rather than seeing the flag set while imports are still running.
    """
    _load_converters()
    return registry


_LOAD_LOCK = threading.Lock()
_LOADED = False


def _load_converters() -> None:
    global _LOADED
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED:  # another thread finished while we waited
            return
        from . import converters  # noqa: F401  (import side effect: registration)
        _LOADED = True
