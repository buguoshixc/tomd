"""Unit tests for the markdown building blocks and text helpers."""

from __future__ import annotations

import pytest

from tomd import mdutil as md


class TestEscapeStructure:
    def test_escapes_acidental_markdown(self):
        out = md.escape_structure("- not a list")
        assert out.startswith("\\-")

    def test_escapes_numbered_line(self):
        assert md.escape_structure("1. not a step").startswith("\\1.")

    def test_escapes_heading(self):
        assert md.escape_structure("## not a heading").startswith("\\##")

    def test_keeps_a_real_list_run(self):
        text = "- one\n- two\n- three"
        assert "\\-" not in md.escape_structure(text)

    def test_short_list_is_escaped(self):
        # Two items is below the confidence threshold: escaping is the safer error.
        assert "\\-" in md.escape_structure("- one\n- two")

    def test_leaves_plain_prose_alone(self):
        text = "Revenue grew 12% to 45,300 units."
        assert md.escape_structure(text) == text

    def test_escapes_pipe_table_line(self):
        assert md.escape_structure("| a | b |").startswith("\\|")


class TestEscapeInline:
    def test_escapes_markdown_specials(self):
        assert md.escape_inline("a_b*c[d]") == "a\\_b\\*c\\[d\\]"

    def test_leaves_normal_punctuation(self):
        assert md.escape_inline("hello, world!") == "hello, world!"


class TestTables:
    def test_renders_header_and_separator(self):
        out = md.table([["a", "b"], ["1", "2"]])
        assert out.splitlines()[0] == "| a | b |"
        assert out.splitlines()[1] == "| --- | --- |"

    def test_escapes_pipes_in_cells(self):
        assert "\\|" in md.table([["a|b"]])

    def test_rectangularises_ragged_rows(self):
        lines = md.table([["a", "b", "c"], ["1"]]).splitlines()
        assert lines[2] == "| 1 |  |  |"

    def test_drops_empty_rows(self):
        # One all-empty row is dropped, so only the header and separator remain.
        assert md.table([["", ""], ["a", "b"]]).count("\n") == 1

    def test_without_header_generates_blank_header(self):
        out = md.table([["1", "2"]], header=False)
        assert out.splitlines()[0] == "|  |  |"
        assert out.splitlines()[2] == "| 1 | 2 |"


class TestInlineValue:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, "true"),
            (False, "false"),
            (None, ""),
            (2.0, "2"),
            (2.5, "2.5"),
            (7, "7"),
            ("hi", "hi"),
        ],
    )
    def test_scalars(self, value, expected):
        assert md.inline_value(value) == expected

    def test_lists_become_json(self):
        assert md.inline_value([1, 2]) == "[1, 2]"


class TestFences:
    def test_plain_fence(self):
        assert md.fence("code", "py") == "```py\ncode\n```"

    def test_fence_grows_to_survive_backticks(self):
        out = md.fence("a ``` b")
        assert out.startswith("````")
        assert out.endswith("````")

    def test_preserves_internal_newlines(self):
        assert md.fence("a\nb").count("\n") == 3


class TestLists:
    def test_bullets(self):
        assert md.bullet_list(["a", "b"]) == "- a\n- b"

    def test_numbered_starts_at_one(self):
        assert md.numbered_list(["a", "b"]) == "1. a\n2. b"

    def test_multiline_item_is_indented(self):
        assert md.bullet_list(["a\nb"]).splitlines()[1] == "  b"


class TestHeadings:
    @pytest.mark.parametrize("level", [1, 3, 6])
    def test_levels(self, level):
        assert md.heading(level, "T") == f"{'#' * level} T"

    def test_clamps_out_of_range_levels(self):
        assert md.heading(0, "T").startswith("# ")
        assert md.heading(99, "T").startswith("###### ")


class TestFrontMatter:
    def test_writes_scalars(self):
        out = md.front_matter({"a": "b", "n": 1})
        assert out.startswith("---\n")
        assert "a: b" in out

    def test_quotes_risky_scalars(self):
        assert 'title: "a: b"' in md.front_matter({"title": "a: b"})

    def test_skips_empty_values(self):
        assert "empty" not in md.front_matter({"empty": ""})

    def test_writes_lists(self):
        out = md.front_matter({"tags": ["a", "b"]})
        assert "tags:\n  - a\n  - b" in out


class TestReading:
    def test_utf8(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("héllo", encoding="utf-8")
        assert md.read_text(path).text == "héllo"

    def test_utf8_bom(self, tmp_path):
        path = tmp_path / "b.txt"
        path.write_bytes("hi".encode("utf-8-sig"))
        assert md.read_text(path).text == "hi"

    def test_gb18030_is_detected(self, tmp_path):
        path = tmp_path / "c.txt"
        path.write_bytes("会议记录".encode("gb18030"))
        read = md.read_text(path)
        assert "会议记录" in read.text
        assert read.had_errors is False
        # A statistical detector would call this Big5 and produce garbage, which
        # is why gb18030 is tried first for non-UTF-8 bytes.
        assert read.encoding.lower() in ("gb18030", "gbk", "gb2312")

    def test_crlf_is_normalised(self, tmp_path):
        path = tmp_path / "d.txt"
        path.write_bytes(b"a\r\nb")
        assert md.read_text(path).text == "a\nb"

    def test_limit_truncates(self, tmp_path):
        path = tmp_path / "e.txt"
        path.write_text("x" * 100)
        assert len(md.read_text(path, limit=10).text) == 10


class TestSuppressStdout:
    def test_swallows_prints(self, capsys):
        with md.suppress_stdout():
            print("this must not appear")
        assert capsys.readouterr().out == ""

    def test_restores_stdout_after_failure(self):
        import sys

        original = sys.stdout
        with pytest.raises(RuntimeError):
            with md.suppress_stdout():
                raise RuntimeError("boom")
        assert sys.stdout is original
