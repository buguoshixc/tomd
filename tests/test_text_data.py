"""Tests for the text, structured-data and notebook converters.

The recurring theme: a converter must never silently invent structure that is
not there, and must never silently swallow content that is.
"""

from __future__ import annotations

import json

import pytest

from tomd.converters.data import _sniff_delimiter, _tabular
from tomd.converters.office import strip_rtf
from tomd.converters.text import (
    _promote_numbered_headings,
    _promote_underline_headings,
    parse_subtitles,
)


class TestPlainText:
    def test_setext_headings_become_atx(self):
        out, count = _promote_underline_headings("Title\n=====\n\ntext\n\nSub\n---\n")
        assert out.startswith("# Title")
        assert "## Sub" in out
        assert count == 2

    def test_single_char_underline_is_not_a_heading(self):
        out, count = _promote_underline_headings("Note\n-\ntext")
        assert count == 0

    def test_leading_whitespace_on_the_underline_is_tolerated(self):
        # CommonMark only allows up to 3 spaces of indent before a setext
        # underline, and a trailing-whitespace underline is still valid.
        _out, count = _promote_underline_headings("Title\n   =====\n")
        assert count == 1

    def test_mixed_underline_characters_are_not_a_heading(self):
        _out, count = _promote_underline_headings("Title\n=-=-=\n")
        assert count == 0

    def test_very_long_title_is_not_promoted(self):
        _out, count = _promote_underline_headings("x" * 200 + "\n=====\n")
        assert count == 0

    def test_numbered_promotion_is_opt_in(self):
        out, count = _promote_numbered_headings("1.1 Alpha\n1.1.1 Beta\n")
        assert count == 2
        assert out.splitlines()[0].startswith("## 1.1")
        assert out.splitlines()[1].startswith("### 1.1.1")

    def test_fixture_headings_and_escaping(self, convert_fixture):
        _result, text = convert_fixture("notes.txt")
        assert "# Meeting notes" in text
        assert "## Decisions" in text
        # A two-item numbered run is below the list threshold, so it is escaped
        # rather than silently becoming a markdown list.
        assert "\\1. Ship the converter" in text
        # A three-item run is treated as a genuine list and left alone.
        assert "- #412 needs a fixture" in text

    def test_gbk_file_is_decoded(self, convert_fixture):
        _result, text = convert_fixture("gbk_notes.txt")
        assert "会议记录" in text
        assert "\ufffd" not in text

    def test_non_utf8_source_is_reported(self, convert_fixture):
        result, _text = convert_fixture("gbk_notes.txt")
        assert any(w.code == "non-utf8-source" for w in result.warnings)


class TestMarkdownPassthrough:
    def test_headings_survive_unchanged(self, convert_fixture):
        _result, text = convert_fixture("guide.md")
        assert "# Field guide" in text
        assert "### Notes" in text

    def test_front_matter_is_lifted_into_metadata(self, convert_fixture):
        result, _text = convert_fixture("guide.md")
        assert result.document.meta["front_matter"]["title"] == "Field guide"

    def test_front_matter_is_not_duplicated_in_the_body(self, convert_fixture):
        _result, text = convert_fixture("guide.md")
        assert text.count("title:") <= 1

    def test_heading_count_is_reported(self, convert_fixture):
        result, _text = convert_fixture("guide.md")
        assert result.document.meta["headings"] == 3


class TestCode:
    def test_python_fence_language(self, convert_fixture):
        _result, text = convert_fixture("sample.py")
        assert text.startswith("## Outline") or "```python" in text

    def test_symbol_outline_is_generated(self, convert_fixture):
        _result, text = convert_fixture("sample.py")
        assert "`add`" in text and "`Calc`" in text

    def test_outline_can_be_disabled(self, convert_fixture):
        _result, text = convert_fixture("sample.py", code_outline=False)
        assert "Outline" not in text

    def test_body_is_fenced_verbatim(self, convert_fixture):
        _result, text = convert_fixture("sample.py")
        assert '    """Add two numbers."""' in text


class TestDelimited:
    def test_sniffs_tab_delimiter(self):
        assert _sniff_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"

    def test_sniffs_comma(self):
        assert _sniff_delimiter("a,b,c\n1,2,3\n") == ","

    def test_sniffs_semicolon(self):
        assert _sniff_delimiter("a;b;c\n1;2;3\n") == ";"

    def test_csv_fixture_becomes_a_table(self, convert_fixture):
        _result, text = convert_fixture("people.csv")
        assert "| name | role | city | notes |" in text
        assert "| Ana | engineer | Lisbon, PT |" in text

    def test_pipes_in_cells_are_escaped(self, convert_fixture):
        _result, text = convert_fixture("people.csv")
        assert "says \\| pipes matter" in text

    def test_tsv_with_ragged_rows_is_padded_and_reported(self, convert_fixture):
        result, text = convert_fixture("ragged.tsv")
        assert "| id | item | qty |" in text
        assert any(w.code == "ragged-rows" for w in result.warnings)

    def test_large_csv_falls_back_to_a_fence(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "big.csv"
        path.write_text("a,b\n" + "\n".join(f"{i},{i}" for i in range(200)), encoding="utf-8")
        options = ConvertOptions(output_dir=outdir, front_matter=False, report=False, csv_max_rows=10)
        result = convert(path, options)
        text = render(result, options)
        assert "```csv" in text
        assert any(w.code == "table-too-large" for w in result.warnings)

    def test_header_can_be_disabled(self, convert_fixture):
        _result, text = convert_fixture("people.csv", csv_header=False)
        assert "|  |  |  |  |" in text

    def test_empty_csv_is_marked_empty(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert

        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert result.status.value == "empty"


class TestJson:
    def test_shape_summary_is_emitted(self, convert_fixture):
        _result, text = convert_fixture("config.json")
        assert "Root type" in text and "dict" in text
        assert "service, version, features, limits" in text

    def test_flat_object_becomes_a_table(self, convert_fixture):
        _result, text = convert_fixture("config.json")
        assert "| Path | Value |" in text

    def test_nested_object_is_flattened_with_dotted_paths(self, convert_fixture):
        _result, text = convert_fixture("config.json")
        assert "features.pdf" in text

    def test_raw_json_is_preserved_verbatim(self, convert_fixture):
        _result, text = convert_fixture("config.json")
        assert '"service": "converter"' in text

    def test_booleans_are_lowercase_in_tables(self, convert_fixture):
        _result, text = convert_fixture("config.json")
        assert "| features.pdf | true |" in text

    def test_invalid_json_degrades_with_a_warning(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "broken.json"
        path.write_text('{"a": 1,', encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert any(w.code == "json-parse-failed" for w in result.warnings)
        assert "```json" in render(result)

    def test_jsonl_good_lines_are_converted(self, convert_fixture):
        _result, text = convert_fixture("records.jsonl")
        assert "| id | name | score |" in text
        assert "| 1 | Ana | 91 |" in text

    def test_jsonl_bad_lines_are_counted(self, convert_fixture):
        result, _text = convert_fixture("records.jsonl")
        assert any(w.code == "jsonl-bad-lines" for w in result.warnings)

    def test_tabular_helper_rejects_deeply_nested_lists(self):
        assert _tabular([{"a": {"b": 1}}]) is None

    def test_tabular_helper_accepts_flat_records(self):
        rows = _tabular([{"a": 1}, {"a": 2, "b": 3}])
        assert rows is not None and rows[0] == ["a", "b"]


class TestXml:
    def test_element_summary_and_raw(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "a.xml"
        path.write_text("<root><item>x</item><item>y</item></root>", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        text = render(result)
        assert "Root element" in text
        assert "```xml" in text


class TestConfigFormats:
    def test_ini_sections_become_headings_and_tables(self, convert_fixture):
        _result, text = convert_fixture("settings.ini")
        assert "## server" in text
        assert "| host | 127.0.0.1 |" in text
        assert "## paths" in text

    def test_toml_keys_are_tabulated(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render
        from tomd.converters.text import _toml_parser

        if _toml_parser() is None:
            pytest.skip("no TOML parser on this interpreter (tomllib needs 3.11+)")

        path = tmp_path / "a.toml"
        path.write_text('[db]\nhost = "x"\nport = 1\n', encoding="utf-8")
        text = render(convert(path, ConvertOptions(output_dir=outdir)))
        assert "## db" in text
        assert "| host | x |" in text

    def test_toml_without_a_parser_degrades_and_says_so(self, tmp_path, outdir, monkeypatch):
        """Python 3.10 without the tomli backport must not lose the file."""
        from tomd import ConvertOptions, convert, render
        from tomd.converters import text as text_converter

        monkeypatch.setattr(text_converter, "_toml_parser", lambda: None)
        path = tmp_path / "a.toml"
        path.write_text('[db]\nhost = "x"\nport = 1\n', encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert "```toml" in render(result)
        assert '[db]' in render(result)
        # Informational, exactly like the PyYAML-less path: the file is intact,
        # only its structure is missing, and the report says which.
        assert any(w.code == "no-toml-parser" for w in result.warnings)
        assert result.status.value in ("ok", "partial")

    def test_broken_toml_falls_back_to_the_raw_file(self, tmp_path, outdir, monkeypatch):
        from tomd import ConvertOptions, convert, render
        from tomd.converters import text as text_converter

        if text_converter._toml_parser() is None:
            pytest.skip("no TOML parser on this interpreter (tomllib needs 3.11+)")

        path = tmp_path / "a.toml"
        path.write_text("not = = toml\n", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert "not = = toml" in render(result)
        assert any(w.code == "toml-parse-failed" for w in result.warnings)

    def test_yaml_without_pyyaml_falls_back_and_says_so(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "a.yaml"
        path.write_text("a: 1\nb: 2\n", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        assert "```yaml" in render(result)
        assert result.status.value in ("ok", "partial")

    def test_diff_produces_stats_and_a_fence(self, convert_fixture):
        _result, text = convert_fixture("patch.diff")
        assert "Lines added" in text
        assert "```diff" in text


class TestSubtitles:
    def test_parses_srt_cues(self):
        cues = parse_subtitles("1\n00:00:01,000 --> 00:00:03,500\nHello\n\n")
        assert len(cues) == 1
        assert cues[0][0] == 1.0 and cues[0][1] == 3.5
        assert cues[0][2] == "Hello"

    def test_parses_vtt(self):
        cues = parse_subtitles("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
        assert cues and cues[0][2] == "Hi"

    def test_parses_ass_and_strips_override_tags(self):
        ass = (
            "[Script Info]\nTitle: x\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\i1}Hello{\\i0}\\Nworld\n"
        )
        cues = parse_subtitles(ass)
        assert cues and "Hello" in cues[0][2]
        assert "{" not in cues[0][2]

    def test_fixture_renders_timestamps(self, convert_fixture):
        _result, text = convert_fixture("sample.srt")
        assert "**[00:00:01]**" in text
        assert "Today we convert files." in text

    def test_plain_text_without_timestamps_falls_back_to_cues(self, tmp_path, outdir):
        """A transcript with no timestamps is still emitted, not discarded."""
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "a.srt"
        path.write_text("!!!\n@@@\n###\n", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        text = render(result)
        assert "!!! @@@ ###" in text
        assert "```" in text or "**[" in text

    def test_timestamped_line_without_a_body_is_skipped(self, tmp_path, outdir):
        from tomd import ConvertOptions, convert, render

        path = tmp_path / "b.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\n", encoding="utf-8")
        result = convert(path, ConvertOptions(output_dir=outdir))
        # Nothing but a timecode is not content: it must not become a cue.
        assert "00:00:01" not in render(result) or "```" in render(result)


class TestRtf:
    def test_strips_control_words(self):
        assert strip_rtf(r"{\rtf1\ansi Hello \b world\b0}").strip() == "Hello world"

    def test_decodes_hex_escapes(self):
        assert "é" in strip_rtf(r"{\rtf1 caf\'e9}")

    def test_skips_font_tables(self):
        out = strip_rtf(r"{\rtf1{\fonttbl{\f0 Arial;}}Real text}")
        assert "Arial" not in out and "Real text" in out

    def test_paragraph_breaks(self):
        assert "\n" in strip_rtf(r"{\rtf1 one\par two}")


class TestNotebook:
    def test_markdown_cells_pass_through(self, convert_fixture):
        _result, text = convert_fixture("analysis.ipynb")
        assert "# Analysis" in text
        assert "Load the data" in text

    def test_code_cells_are_fenced(self, convert_fixture):
        _result, text = convert_fixture("analysis.ipynb")
        assert "```python" in text
        assert "import pandas as pd" in text

    def test_stream_output_is_included(self, convert_fixture):
        _result, text = convert_fixture("analysis.ipynb")
        assert "rows=2" in text

    def test_error_traceback_is_included(self, convert_fixture):
        _result, text = convert_fixture("analysis.ipynb")
        assert "ZeroDivisionError" in text

    def test_outputs_can_be_disabled(self, convert_fixture):
        _result, text = convert_fixture("analysis.ipynb", notebook_outputs=False)
        assert "rows=2" not in text
        assert "import pandas as pd" in text

    def test_cell_counts_in_metadata(self, convert_fixture):
        result, _text = convert_fixture("analysis.ipynb")
        assert result.document.meta["cells"] == 3
        assert result.document.meta["code_cells"] == 2


class TestUnsupported:
    def test_binary_produces_an_explicit_refusal(self, convert_fixture):
        result, text = convert_fixture("blob.bin")
        assert result.status.value == "empty"
        assert "Unsupported" in text
        assert any(w.code == "unsupported-format" for w in result.warnings)

    def test_image_without_ocr_says_so(self, convert_fixture):
        result, text = convert_fixture("diagram.png")
        assert "not transcribed" in text
        assert any(w.code == "image-not-transcribed" for w in result.warnings)

    def test_image_dimensions_are_reported(self, convert_fixture):
        _result, text = convert_fixture("diagram.png")
        assert "240 x 120" in text


class TestSniffingFixtures:
    def test_json_named_txt(self, convert_fixture):
        result, text = convert_fixture("actually.json.txt")
        assert result.format.key == "json"
        assert result.format.confidence == "content"
        assert "| looks | like json |" in text

    def test_csv_named_txt(self, convert_fixture):
        result, text = convert_fixture("actually.csv.txt")
        assert result.format.key == "csv"
        assert "| a | b | c |" in text
