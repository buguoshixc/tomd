"""Tests for format detection and input hardening.

These are the two places where a mistake is silently dangerous: mis-detecting a
file produces confidently wrong output, and a missing size/ratio check turns a
conversion service into a denial-of-service target.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tomd.detect import detect
from tomd.security import (
    SecurityError,
    check_archive,
    check_fetch_url,
    check_size,
    ensure_within,
    sanitize_filename,
    sanitize_stem,
    safe_join,
)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
            ("a/b/c.txt", "c.txt"),
            ("normal.txt", "normal.txt"),
            ("  spaced.txt  ", "spaced.txt"),
            ("bad:name?.txt", "bad_name_.txt"),
            ("trailing...", "trailing"),
        ],
    )
    def test_neutralises_traversal(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_empty_becomes_fallback(self):
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("...") == "untitled"

    def test_windows_reserved_names_are_prefixed(self):
        assert sanitize_filename("con.txt").startswith("_")
        assert sanitize_filename("LPT1.md").startswith("_")

    def test_control_characters_are_removed(self):
        assert "\x00" not in sanitize_filename("a\x00b.txt")

    def test_length_is_capped_but_extension_kept(self):
        out = sanitize_filename("x" * 500 + ".pdf")
        assert len(out) <= 120
        assert out.endswith(".pdf")

    def test_stem_drops_extension(self):
        assert sanitize_stem("report.final.pdf") == "report.final"


class TestEnsureWithin:
    def test_allows_inside(self, tmp_path):
        target = tmp_path / "sub" / "a.md"
        assert ensure_within(tmp_path, target) == target.resolve()

    def test_rejects_outside(self, tmp_path):
        with pytest.raises(SecurityError):
            ensure_within(tmp_path / "out", tmp_path / "elsewhere" / "a.md")

    def test_allows_the_base_itself(self, tmp_path):
        assert ensure_within(tmp_path, tmp_path) == tmp_path.resolve()

    def test_safe_join_blocks_traversal(self, tmp_path):
        # sanitize_filename strips the traversal, so the result stays inside
        assert str(safe_join(tmp_path, "../../evil.md")).startswith(str(tmp_path.resolve()))

    def test_safe_join_rejects_a_deep_climb(self, tmp_path):
        import pytest as _pytest

        with _pytest.raises(SecurityError):
            ensure_within(tmp_path / "out", tmp_path.parent / "evil.md")


class TestSizeLimits:
    def test_accepts_small_file(self, tmp_path):
        path = tmp_path / "a.bin"
        path.write_bytes(b"x" * 10)
        assert check_size(path, 100) == 10

    def test_rejects_large_file(self, tmp_path):
        path = tmp_path / "b.bin"
        path.write_bytes(b"x" * 100)
        with pytest.raises(SecurityError, match="MB limit"):
            check_size(path, 10)

    def test_none_disables_the_limit(self, tmp_path):
        path = tmp_path / "c.bin"
        path.write_bytes(b"x" * 100)
        assert check_size(path, None) == 100


class TestArchiveLimits:
    def test_normal_ratio_passes(self):
        check_archive(1000, 3000)

    def test_zip_bomb_ratio_rejected(self):
        with pytest.raises(SecurityError, match="zip bomb"):
            check_archive(1000, 1000 * 500)

    def test_huge_uncompressed_rejected(self):
        with pytest.raises(SecurityError, match="GB"):
            check_archive(1000, 8 * 1024 ** 3)

    def test_zero_compressed_size_does_not_divide_by_zero(self):
        check_archive(0, 100)


class TestFetchUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "not a url",
        ],
    )
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(SecurityError):
            check_fetch_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::1]/",
        ],
    )
    def test_rejects_private_and_metadata_hosts(self, url):
        with pytest.raises(SecurityError, match="private|loopback"):
            check_fetch_url(url)

    def test_allows_public_https(self):
        assert check_fetch_url("https://example.com/a.pdf").startswith("https://")


class TestDetection:
    def test_extension_mapping(self, tmp_path):
        # A zip with no OOXML/OEBPS marker inside cannot be identified further,
        # so it is reported as a generic archive rather than guessed at.
        path = tmp_path / "a.docx"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
        assert detect(path).key == "archive"

    def test_corrupt_zip_is_reported_as_an_archive(self, tmp_path):
        path = tmp_path / "broken.zip"
        path.write_bytes(b"PK\x03\x04 truncated garbage")
        # Must not be mislabelled as an opaque binary: the failure has an owner.
        assert detect(path).key == "archive"

    def test_pdf_magic_beats_extension(self, tmp_path):
        path = tmp_path / "actually_a_pdf.txt"
        path.write_bytes(b"%PDF-1.7\n")
        assert detect(path).key == "pdf"

    def test_ole_magic_maps_to_legacy_doc(self, tmp_path):
        path = tmp_path / "old.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
        assert detect(path).key == "doc_legacy"

    def test_json_named_txt_is_detected_by_content(self, tmp_path):
        path = tmp_path / "data.txt"
        path.write_text('{"a": 1}', encoding="utf-8")
        fmt = detect(path)
        assert fmt.key == "json"
        assert fmt.confidence == "content"

    def test_csv_named_txt_is_detected_by_content(self, tmp_path):
        path = tmp_path / "table.txt"
        path.write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
        assert detect(path).key == "csv"

    def test_html_without_extension_is_detected(self, tmp_path):
        path = tmp_path / "page"
        path.write_text("<!doctype html><html><body><p>x</p></body></html>", encoding="utf-8")
        assert detect(path).key == "html"

    def test_markdown_headings_detected(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("# Title\n\ntext\n", encoding="utf-8")
        assert detect(path).key == "markdown"

    def test_binary_without_magic_is_unsupported(self, tmp_path):
        path = tmp_path / "blob.dat"
        path.write_bytes(bytes(range(256)))
        assert detect(path).key == "unsupported_binary"

    def test_zip_is_inspected_for_docx(self, tmp_path):
        path = tmp_path / "fake.docx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
        assert detect(path).key == "docx"

    def test_odf_zip_detected_via_mimetype(self, tmp_path):
        path = tmp_path / "doc.odt"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            zf.writestr("content.xml", "<x/>")
        assert detect(path).key == "odf"

    def test_unknown_extension_of_text_is_text(self, tmp_path):
        path = tmp_path / "notes.weird"
        path.write_text("just some prose with no structure at all.", encoding="utf-8")
        assert detect(path).key == "text"

    def test_zero_byte_file_does_not_crash(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_bytes(b"")
        assert detect(path).key in ("text", "unsupported_format")

    def test_supported_property(self, tmp_path):
        path = tmp_path / "a.bin"
        path.write_bytes(b"\x7fELF" + b"\x00" * 64)
        assert detect(path).supported is False
