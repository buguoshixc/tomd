"""Tests for the office, PDF, ebook and archive converters."""

from __future__ import annotations

import zipfile

import pytest

from tomd.security import check_archive


class TestDocx:
    def test_headings_keep_their_levels(self, convert_fixture):
        _result, text = convert_fixture("spec.docx")
        assert "# Conversion requirements" in text
        assert "## Scope" in text
        assert "### Metrics" in text

    def test_bullets_are_preserved(self, convert_fixture):
        _result, text = convert_fixture("spec.docx")
        assert "- PDF with a text layer" in text
        assert "- Word, PowerPoint, Excel" in text

    def test_tables_are_preserved(self, convert_fixture):
        _result, text = convert_fixture("spec.docx")
        assert "| Metric | Target | Actual |" in text
        assert "| Coverage | 80% | 83% |" in text

    def test_numbers_survive_exactly(self, convert_fixture):
        _result, text = convert_fixture("spec.docx")
        assert "45,300" in text and "12%" in text

    def test_metadata_is_captured(self, convert_fixture):
        result, _text = convert_fixture("spec.docx")
        assert result.document.meta.get("author") == "Ana"
        assert result.document.title == "Conversion requirements"

    def test_stdout_reader_is_registered_as_a_fallback(self):
        from tomd.registry import get_registry

        names = [c.name for c in get_registry().chains("docx")]
        assert "python-docx" in names and "ooxml-stdlib" in names

    def test_stdlib_reader_produces_the_same_headings(self, fixture):
        """The fallback must not be a stub: it has to recover the structure."""
        from tomd.converters.office import convert_docx_fallback

        doc = convert_docx_fallback(fixture("spec.docx"), {})
        body = doc.markdown
        assert "# Conversion requirements" in body
        assert "## Scope" in body
        assert "| Metric | Target | Actual |" in body


class TestPptx:
    def test_slide_titles_become_headings(self, convert_fixture):
        _result, text = convert_fixture("deck.pptx")
        assert "## Phase one" in text
        assert "## Not covered" in text

    def test_body_text_becomes_bullets(self, convert_fixture):
        _result, text = convert_fixture("deck.pptx")
        assert "- PDF text layer" in text
        assert "- Batch mode" in text

    def test_speaker_notes_are_kept_and_labelled(self, convert_fixture):
        _result, text = convert_fixture("deck.pptx")
        assert "Speaker notes" in text
        assert "OCR gap" in text

    def test_notes_can_be_disabled(self, convert_fixture):
        _result, text = convert_fixture("deck.pptx", pptx_notes=False)
        assert "OCR gap" not in text

    def test_slide_count_in_metadata(self, convert_fixture):
        result, _text = convert_fixture("deck.pptx")
        assert result.document.meta["slides"] == 2

    def test_stdlib_reader_finds_titles(self, fixture):
        from tomd.converters.office import convert_pptx_fallback

        doc = convert_pptx_fallback(fixture("deck.pptx"), {})
        assert "Phase one" in doc.markdown


class TestXlsx:
    def test_stdlib_reader_reads_shared_strings(self, tmp_path):
        """Build a minimal workbook by hand so the test needs no openpyxl."""
        path = tmp_path / "book.xlsx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships"><sheets>'
                '<sheet name="Metrics" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            zf.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                "</Relationships>",
            )
            zf.writestr(
                "xl/sharedStrings.xml",
                '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><si><t>Metric</t></si><si><t>Revenue</t></si></sst>',
            )
            zf.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>1200</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>1344</v></c></row>'
                "</sheetData></worksheet>",
            )

        from tomd.converters.office import convert_xlsx_fallback

        doc = convert_xlsx_fallback(path, {})
        body = doc.markdown
        assert "## Metrics" in body
        assert "| Metric | 1200 |" in body
        assert "| Revenue | 1344 |" in body

    def test_openpyxl_reader_when_available(self, fixture):
        pytest.importorskip("openpyxl")
        from tomd.converters.office import convert_xlsx

        doc = convert_xlsx(fixture("book.xlsx"), {})
        assert "Revenue" in doc.markdown

    def test_missing_sheet_part_fails_cleanly(self, tmp_path):
        path = tmp_path / "broken.xlsx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        from tomd.converters.office import convert_xlsx_fallback

        doc = convert_xlsx_fallback(path, {})
        assert doc.status.value == "failed"
        assert any(w.code in ("no-sheets-found", "xlsx-read-failed") for w in doc.warnings)

    def test_corrupt_package_fails_cleanly(self, tmp_path):
        path = tmp_path / "notreally.xlsx"
        path.write_bytes(b"PK\x03\x04 truncated")
        from tomd.converters.office import convert_xlsx_fallback

        doc = convert_xlsx_fallback(path, {})
        assert doc.status.value == "failed"
        assert any(w.severity == "error" for w in doc.warnings)


class TestOdf:
    def test_reads_headings_paragraphs_and_tables(self, tmp_path):
        path = tmp_path / "doc.odt"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            zf.writestr(
                "content.xml",
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
                'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
                '<office:body><office:text>'
                '<text:h text:outline-level="1">Chapter</text:h>'
                "<text:p>Some prose.</text:p>"
                "<table:table><table:table-row>"
                "<table:table-cell><text:p>a</text:p></table:table-cell>"
                "<table:table-cell><text:p>b</text:p></table:table-cell>"
                "</table:table-row></table:table>"
                "</office:text></office:body></office:document-content>",
            )

        from tomd.converters.office import convert_odf

        doc = convert_odf(path, {})
        assert "# Chapter" in doc.markdown
        assert "Some prose." in doc.markdown
        assert "| a | b |" in doc.markdown

    def test_non_odf_zip_fails_cleanly(self, tmp_path):
        path = tmp_path / "not.odt"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("something.txt", "x")
        from tomd.converters.office import convert_odf

        doc = convert_odf(path, {})
        assert doc.status.value == "failed"
        assert any(w.code == "missing-part" for w in doc.warnings)


class TestLegacyBinary:
    def test_doc_text_is_recovered_with_a_warning(self, convert_fixture):
        result, text = convert_fixture("legacy.doc")
        assert "Legacy Word document text" in text
        assert any(w.code == "legacy-format-lossy" for w in result.warnings)

    def test_noise_is_not_reported_as_content(self, tmp_path):
        path = tmp_path / "empty.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
        from tomd.converters.office import convert_doc_legacy

        doc = convert_doc_legacy(path, {})
        assert doc.markdown == ""
        assert doc.status.value == "empty"


class TestPdf:
    def test_text_pdf_gets_a_heading_hierarchy(self, convert_fixture):
        _result, text = convert_fixture("report.pdf")
        assert "## Summary" in text
        assert "## Details" in text

    def test_numbers_and_sentences_survive(self, convert_fixture):
        _result, text = convert_fixture("report.pdf")
        assert "45,300" in text
        assert "12 percent" in text

    def test_table_is_detected(self, convert_fixture):
        _result, text = convert_fixture("report.pdf")
        assert "| Metric | Q1 | Q2 |" in text
        assert "| Revenue | 1200 | 1344 |" in text

    def test_page_markers_are_emitted(self, convert_fixture):
        _result, text = convert_fixture("report.pdf")
        assert "Page 1" in text and "Page 2" in text

    def test_details_wrapper_can_be_replaced_by_comments(self, convert_fixture):
        _result, text = convert_fixture("report.pdf", pdf_page_details=False)
        assert "<!-- page 1 -->" in text
        assert "<details" not in text

    def test_page_limit_is_honoured_and_reported(self, convert_fixture):
        result, text = convert_fixture("report.pdf", pdf_max_pages=1)
        assert result.document.meta["pages_extracted"] == 1
        assert any(w.code == "page-limit" for w in result.warnings)

    def test_scanned_pdf_is_refused_not_faked(self, convert_fixture):
        result, text = convert_fixture("scanned.pdf")
        assert result.status.value == "empty"
        assert text.strip() == ""
        assert any(w.code == "scanned-pdf" for w in result.warnings)

    def test_scanned_pdf_reports_which_pages(self, convert_fixture):
        result, _text = convert_fixture("scanned.pdf")
        assert result.document.meta["scanned_pages"] == [1]

    def test_metadata_title_is_read(self, convert_fixture):
        result, _text = convert_fixture("report.pdf")
        assert result.document.meta.get("pdf_title") == "Annual Report 2025"

    def test_truncated_pdf_fails_cleanly(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert

        path = tmp_path / "broken.pdf"
        path.write_bytes(b"%PDF-1.7\nthis is not a real pdf body")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert result.status.value in ("empty", "failed")
        assert result.warnings


class TestEpub:
    def test_title_and_creator_are_read(self, convert_fixture):
        result, _text = convert_fixture("book.epub")
        assert result.document.meta["title"] == "Small Book"
        assert result.document.meta["creator"] == "Ana"

    def test_chapters_follow_spine_order(self, convert_fixture):
        _result, text = convert_fixture("book.epub")
        assert text.index("Chapter one") < text.index("Chapter two")

    def test_chapter_headings_and_tables(self, convert_fixture):
        _result, text = convert_fixture("book.epub")
        assert "# Chapter one" in text
        assert "| k | v |" in text

    def test_chapter_boundaries_are_marked(self, convert_fixture):
        _result, text = convert_fixture("book.epub")
        assert "<!-- chapter: ch1.xhtml -->" in text

    def test_mobi_is_refused_with_advice(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "book.mobi"
        path.write_bytes(b"\x00" * 64)
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert any(w.code == "unsupported-mobi" for w in result.warnings)
        assert "EPUB" in render(result)


class TestEmail:
    def test_subject_is_decoded(self, convert_fixture):
        _result, text = convert_fixture("message.eml")
        assert "Quarterly review — please read" in text

    def test_headers_are_tabulated(self, convert_fixture):
        _result, text = convert_fixture("message.eml")
        assert "| From | Ana <ana@example.com> |" in text
        assert "| To | Bo <bo@example.com> |" in text

    def test_text_body_preferred_over_html(self, convert_fixture):
        _result, text = convert_fixture("message.eml")
        assert "The numbers look good." in text
        assert "<b>good</b>" not in text

    def test_attachments_are_listed_not_silently_dropped(self, convert_fixture):
        result, text = convert_fixture("message.eml")
        assert "numbers.csv" in text
        assert result.document.meta["attachments"] == 1
        assert any(w.code == "attachments-not-converted" for w in result.warnings)


class TestArchive:
    def test_zip_is_inventoried_not_exploded(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.txt", "hello")
            zf.writestr("sub/b.txt", "world")
        result = convert(path, ConvertOptions(output_dir=outdir))
        text = render(result)
        assert "| a.txt |" in text
        assert "2" in text  # entry count
        assert any(w.code == "archive-not-extracted" for w in result.warnings)

    def test_zip_bomb_is_rejected(self, tmp_path, outdir):
        """The ratio guard must fire before anything is extracted."""
        from tomd.security import SecurityError

        path = tmp_path / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\x00" * (80 * 1024 * 1024))

        with pytest.raises(SecurityError, match="zip bomb"):
            check_archive(path.stat().st_size, 80 * 1024 * 1024)

        # And the converter surfaces the refusal rather than dying on it.
        from tomd import ConvertOptions, convert

        result = convert(path, ConvertOptions(output_dir=outdir))
        assert result.status.value == "failed"
        assert any(w.severity == "error" for w in result.warnings)

    def test_invalid_zip_fails_cleanly(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert

        path = tmp_path / "broken.zip"
        path.write_bytes(b"PK\x03\x04 but truncated")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert result.status.value == "failed"
