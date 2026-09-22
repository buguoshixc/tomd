"""Tests for the HTML family, including the chrome heuristic.

The chrome decision is the trickiest part of this converter: a page's ``<h1>``
routinely lives in a ``<header>`` that looks exactly like a site banner.  These
tests pin down both directions of that decision.
"""

from __future__ import annotations

import pytest

from tomd.converters.html import (
    _ChromeScanner,
    _extract_title,
    _Renderer,
    html_to_markdown,
)


def render(html: str, **kwargs) -> tuple[str, dict]:
    scanner = _ChromeScanner()
    scanner.feed(html)
    scanner.close()
    renderer = _Renderer(keep_chrome=scanner.keep, **kwargs)
    renderer.feed(html)
    renderer.close()
    return renderer.result()


class TestStructure:
    def test_headings_keep_their_levels(self):
        body, stats = render("<h1>A</h1><h2>B</h2><h3>C</h3>")
        assert "# A" in body and "## B" in body and "### C" in body
        assert stats["headings"] == 3

    def test_paragraphs(self):
        body, _ = render("<p>one</p><p>two</p>")
        assert body == "one\n\ntwo"

    def test_emphasis_and_strong(self):
        body, _ = render("<p><strong>bold</strong> and <em>italic</em></p>")
        assert "**bold**" in body and "*italic*" in body

    def test_links_are_kept_by_default(self):
        body, stats = render('<a href="https://x.test">label</a>')
        assert "[label](https://x.test)" in body
        assert stats["links"] == 1

    def test_links_can_be_dropped_without_leaving_brackets(self):
        body, stats = render('<a href="https://x.test">label</a>', keep_links=False)
        assert body.strip() == "label"
        assert "[" not in body
        assert stats["links"] == 0

    def test_javascript_href_is_not_a_link(self):
        body, _ = render('<a href="javascript:alert(1)">x</a>')
        assert "javascript:" not in body

    def test_images(self):
        body, stats = render('<img src="a.png" alt="diagram">')
        assert "![diagram](a.png)" in body
        assert stats["images"] == 1

    def test_images_can_be_dropped(self):
        body, _ = render('<img src="a.png" alt="x">', keep_images=False)
        assert "a.png" not in body

    def test_blockquote(self):
        body, _ = render("<blockquote>quoted</blockquote>")
        assert body.startswith("> quoted")

    def test_horizontal_rule(self):
        body, _ = render("<p>a</p><hr><p>b</p>")
        assert "---" in body


class TestCode:
    def test_pre_block_preserves_newlines(self):
        body, _ = render("<pre>line one\nline two\n  indented</pre>")
        assert "```" in body
        assert "line one\nline two\n  indented" in body

    def test_pre_code_language_class_becomes_fence_language(self):
        body, _ = render('<pre><code class="language-python">x = 1</code></pre>')
        assert body.startswith("```python")

    def test_inline_code(self):
        body, _ = render("<p>use <code>--flag</code> here</p>")
        assert "`--flag`" in body

    def test_multiline_inline_code_becomes_a_fence(self):
        body, _ = render("<code>a\nb</code>")
        assert "```" in body
        assert "a\nb" in body

    def test_backticks_in_code_grow_the_fence(self):
        body, _ = render("<p><code>a ` b</code></p>")
        assert "``" in body


class TestLists:
    def test_unordered(self):
        body, _ = render("<ul><li>a</li><li>b</li></ul>")
        assert body == "- a\n- b"

    def test_ordered(self):
        body, _ = render("<ol><li>a</li><li>b</li></ol>")
        assert body == "1. a\n2. b"

    def test_nested_list_is_indented_not_reordered(self):
        body, _ = render("<ul><li>parent<ul><li>child</li></ul></li><li>next</li></ul>")
        lines = body.split("\n")
        assert lines[0] == "- parent"
        assert lines[1] == "    - child"
        assert lines[2] == "- next"

    def test_ordered_nested_list_keeps_its_marker(self):
        body, _ = render("<ol><li>a<ol><li>b</li></ol></li><li>c</li></ol>")
        assert "1. a" in body
        assert "     1. b" in body


class TestTables:
    def test_thead_becomes_a_table(self):
        body, stats = render(
            "<table><thead><tr><th>a</th><th>b</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        )
        assert "| a | b |" in body
        assert "| --- | --- |" in body
        assert "| 1 | 2 |" in body
        assert stats["tables"] == 1

    def test_rows_without_thead_still_tabulate(self):
        body, _ = render("<table><tr><td>1</td><td>2</td></tr></table>")
        assert "| 1 | 2 |" in body

    def test_pipe_in_a_cell_is_escaped(self):
        body, _ = render("<table><tr><td>a|b</td><td>c</td></tr></table>")
        assert "a\\|b" in body


class TestChrome:
    def test_script_and_style_never_appear(self):
        body, _ = render("<script>var x = 1</script><style>p{}</style><p>real</p>")
        assert "var x" not in body and "p{}" not in body
        assert "real" in body

    def test_title_tag_is_not_rendered_inline(self):
        body, _ = render("<head><title>Doc</title></head><body><p>x</p></body>")
        assert "Doc" not in body

    def test_link_only_nav_is_dropped(self):
        body, _ = render("<body><nav><a href='/'>Home</a><a href='/a'>About</a></nav><p>x</p></body>")
        assert "Home" not in body
        assert "x" in body

    def test_header_holding_the_h1_is_kept(self):
        body, stats = render("<body><header><h1>Title</h1></header><p>body</p></body>")
        assert "# Title" in body
        assert stats["headings"] == 1

    def test_header_with_only_a_logo_is_dropped(self):
        # A self-closing <img/> must reach the start-tag handler for this to work.
        body, _ = render("<body><header><img src='logo.png' alt='logo' /></header><p>x</p></body>")
        assert "logo.png" not in body
        assert "x" in body

    def test_footer_copyright_is_dropped(self):
        body, _ = render("<body><p>real</p><footer>© 2025 Corp</footer></body>")
        assert "Corp" not in body

    def test_footer_with_a_heading_is_kept(self):
        body, _ = render("<body><p>x</p><footer><h2>Appendix</h2><p>details</p></footer></body>")
        assert "Appendix" in body

    def test_header_inside_content_is_always_kept(self):
        body, _ = render("<body><div><header><h1>T</h1></header><p>y</p></div></body>")
        assert "# T" in body

    def test_chrome_dropping_can_be_disabled(self):
        body, _ = render("<body><nav><a href='/'>Home</a></nav><p>x</p></body>", drop_chrome=False)
        assert "Home" in body


class TestRobustness:
    def test_unclosed_tags_do_not_crash(self):
        body, _ = render("<div><p>text<ul><li>item")
        assert "text" in body

    def test_mismatched_tags_do_not_crash(self):
        body, _ = render("<b>bold</i> tail")
        assert "bold" in body

    def test_empty_html(self):
        body, stats = render("")
        assert body == ""
        assert stats["headings"] == 0

    def test_entities_are_decoded(self):
        body, _ = render("<p>a &amp; b &lt;tag&gt; &#8212; dash</p>")
        assert "a & b <tag> — dash" in body

    def test_foreign_text_survives(self):
        body, _ = render("<p>会议记录 — 项目进度正常</p>")
        assert "会议记录" in body

    def test_css_hidden_noise_in_head_is_ignored(self):
        body, _ = render("<head><meta charset='utf-8'><link rel='x' href='y.css'></head><p>x</p>")
        assert "y.css" not in body


class TestExtractTitle:
    def test_prefers_h1_over_title_tag(self):
        html = "<head><title>Site — Page</title></head><body><h1>Real Title</h1></body>"
        assert _extract_title(html) == "Real Title"

    def test_falls_back_to_title_tag(self):
        assert _extract_title("<head><title>Only Title</title></head>") == "Only Title"

    def test_strips_inner_tags(self):
        assert _extract_title("<h1>Hello <span>World</span></h1>") == "Hello World"

    def test_no_title(self):
        assert _extract_title("<p>x</p>") == ""


class TestEngineSelection:
    def test_builtin_engine_is_always_available(self):
        body, stats, engine = html_to_markdown("<p>x</p>", engine="builtin")
        assert engine == "builtin"
        assert body == "x"

    def test_auto_falls_back_when_markdownify_is_absent(self):
        body, _stats, engine = html_to_markdown("<p>x</p>", engine="auto")
        assert engine in ("builtin", "markdownify")
        assert "x" in body


class TestFixture:
    def test_review_page(self, convert_fixture):
        result, text = convert_fixture("review.html")
        assert result.status.value in ("ok", "partial")
        assert "# Quarterly review" in text
        assert "## Highlights" in text
        assert "| Metric | Q1 | Q2 |" in text
        assert "1. Close the OCR gap" in text
        assert "console.log" not in text

    def test_review_page_keeps_numbers_exact(self, convert_fixture):
        _result, text = convert_fixture("review.html")
        # The whole point: no silent numeric drift.
        assert "1,200" in text and "1,344" in text
        assert "2.1%" in text

    def test_fragment_without_html_wrapper(self, convert_fixture):
        _result, text = convert_fixture("fragment.html")
        assert "## Fragment" in text
        assert "| a | b |" in text
