"""Explicit, honest handling of formats we cannot convert.

The alternative -- letting an unknown binary fall through to the text converter
-- produces mojibake that looks like success.  A clear "we cannot do this, here
is what would work" is strictly more useful, and it keeps the conversion report
trustworthy.
"""

from __future__ import annotations

from pathlib import Path

from ..model import Document, Status
from ..registry import Converter, registry

_MESSAGES: dict[str, tuple[str, str]] = {
    "unsupported_binary": (
        "binary file",
        "This looks like a compiled/binary file and has no text to convert. "
        "If it is an archive or container, extract it first.",
    ),
    "unsupported_parquet": (
        "Parquet",
        "Parquet is a columnar binary format. Convert it with pandas/pyarrow "
        "(`pd.read_parquet(...).to_csv(...)`) and then convert the CSV.",
    ),
    "unsupported_media": (
        "audio/video",
        "Audio and video need speech transcription, which is not implemented. "
        "Wire up faster-whisper or a transcription API and convert the resulting transcript.",
    ),
    "unsupported_format": (
        "unknown format",
        "The file type could not be identified. Pass --format to force a converter, "
        "or run `tomd --formats` to see what is supported.",
    ),
}


def _make(key: str):
    title, hint = _MESSAGES.get(key, _MESSAGES["unsupported_format"])

    def converter(path: Path, options: dict) -> Document:
        doc = Document()
        doc.set_status(Status.EMPTY)
        doc.add(f"# Unsupported: {path.name}\n\n> **{title}** — {hint}")
        doc.warn("unsupported-format", f"{title}: {hint}", "error")
        doc.meta["unsupported_kind"] = title
        return doc

    return converter


for _key, _name in (
    ("unsupported_binary", "binary"),
    ("unsupported_parquet", "parquet"),
    ("unsupported_media", "media"),
    ("unsupported_format", "unknown"),
):
    registry.register(
        Converter(
            _key,
            f"{_name}-unsupported",
            _make(_key),
            priority=10,
            description=f"explicit non-conversion for {_key}",
        )
    )
