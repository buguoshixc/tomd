"""Input hardening.

Conversion is an I/O operation performed with the privileges of the current
process (see MarkItDown's security note -- the same reasoning applies here).
Everything that touches an untrusted file goes through this module:

* filenames coming from archives, uploads or document internals are sanitised
  before they are used to build an output path,
* declared sizes and archive expansion ratios are bounded,
* output paths are verified to stay inside the output tree.

The helpers are deliberately dependency-free so they can be used by both the
CLI and the web service.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

#: Refuse to read files larger than this by default (bytes).
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512 MiB

#: Refuse archives whose uncompressed size exceeds ``ratio`` x compressed size.
DEFAULT_MAX_RATIO = 200

#: Refuse archives that expand beyond this (bytes).
DEFAULT_MAX_UNCOMPRESSED = 4 * 1024 * 1024 * 1024  # 4 GiB

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class SecurityError(RuntimeError):
    """Raised when an input violates a configured safety limit."""


def sanitize_filename(name: str, *, fallback: str = "untitled", max_len: int = 120) -> str:
    """Turn any string into a safe single-segment filename.

    Strips directory components, control characters, Windows-reserved names and
    trailing dots/spaces.  Never returns an empty string.
    """
    raw = unicodedata.normalize("NFC", str(name or ""))
    # Kill any path traversal attempt before we look at anything else.
    raw = raw.replace("\\", "/")
    raw = PurePosixPath(raw).name
    raw = _UNSAFE_CHARS.sub("_", raw).strip().strip(".")
    if not raw:
        raw = fallback
    stem, dot, suffix = raw.rpartition(".")
    if not dot:
        stem, suffix = raw, ""
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    allowed = max_len - (len(suffix) + 1 if suffix else 0)
    stem = stem[: max(1, allowed)]
    return f"{stem}.{suffix}" if suffix else stem


def sanitize_stem(name: str, *, fallback: str = "untitled", max_len: int = 100) -> str:
    """Like :func:`sanitize_filename` but drops the extension."""
    cleaned = sanitize_filename(name, fallback=fallback, max_len=max_len + 16)
    stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned
    return (stem or fallback)[:max_len]


def _canonical(path: Path) -> str:
    """Absolute, normalised path with symlinks resolved as far as they exist.

    ``Path.resolve()`` is applied to the *parent* because resolving the leaf of
    a file that has not been written yet behaves differently from resolving one
    that has -- and on Windows with a junction/symlink in the chain (OneDrive,
    ``Documents`` redirection) that difference is enough to make a legitimate
    path look like it escaped the output tree.  Resolving the existing ancestor
    and re-attaching the missing tail gives a stable answer either way.
    """
    absolute = Path(os.path.abspath(path))
    tail: list[str] = []
    probe = absolute
    while not probe.exists() and probe != probe.parent:
        tail.append(probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for name in reversed(tail):
        resolved = resolved / name
    return os.path.normcase(str(resolved))


def ensure_within(base: Path, candidate: Path) -> Path:
    """Assert that ``candidate`` stays inside ``base``; return an absolute path.

    Guards the mirror-output mode, where relative paths are derived from
    untrusted input filenames.  Containment is compared on canonical *strings*
    rather than with ``Path.parents``: the latter cannot compare paths on two
    different Windows drives at all, and it is confused when only one of the two
    paths has been resolved through a symlink.
    """
    base_s = _canonical(base)
    cand_s = _canonical(candidate)
    if cand_s == base_s:
        return Path(os.path.abspath(candidate))
    if not cand_s.startswith(base_s + os.sep):
        raise SecurityError(f"refusing to write outside the output tree: {candidate}")
    return Path(os.path.abspath(candidate))


def check_size(path: Path, max_bytes: int | None = DEFAULT_MAX_BYTES) -> int:
    """Return the file size, raising if it exceeds ``max_bytes``."""
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise SecurityError(
            f"{path.name} is {size / 1e6:.1f} MB, above the {max_bytes / 1e6:.0f} MB limit"
        )
    return size


def check_archive(
    compressed_size: int,
    uncompressed_size: int,
    *,
    max_ratio: int = DEFAULT_MAX_RATIO,
    max_uncompressed: int = DEFAULT_MAX_UNCOMPRESSED,
) -> None:
    """Reject zip/xlsx/docx bombs before extracting anything."""
    if uncompressed_size > max_uncompressed:
        raise SecurityError(
            f"archive expands to {uncompressed_size / 1e9:.1f} GB, above the limit"
        )
    if compressed_size > 0 and uncompressed_size / compressed_size > max_ratio:
        raise SecurityError(
            f"archive expansion ratio {uncompressed_size / compressed_size:.0f}x "
            f"exceeds the {max_ratio}x limit (possible zip bomb)"
        )


def safe_join(root: Path, *parts: str) -> Path:
    """Join untrusted path parts onto ``root`` and verify containment."""
    rel = PurePosixPath(*(sanitize_filename(p) for p in parts if p))
    return ensure_within(root, root / Path(*rel.parts))


#: Private / loopback / link-local ranges we refuse to fetch server-side.
_BLOCKED_HOST_RE = re.compile(
    r"^(localhost|127\.|0\.|10\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|\[?::1\]?|\[?fe80:)",
    re.IGNORECASE,
)


def check_fetch_url(url: str) -> str:
    """Validate a user-supplied URL before any network access.

    Blocks non-HTTP schemes and private/loopback/metadata destinations, which is
    the standard SSRF shortlist.  Call this *before* fetching, and re-check after
    redirects if you follow them.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname or ""
    if not host:
        raise SecurityError("URL has no host")
    if _BLOCKED_HOST_RE.match(host):
        raise SecurityError(f"refusing to fetch private/loopback host: {host}")
    return url
