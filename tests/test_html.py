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
        assert lines[1].lstrip().startswith("- child")
        assert lines[2] == "- next"

    def test_ordered_nested_list_keeps_its_marker(self):
        body, _ = render("<ol><li>a<ol><li>b</li></ol></li><li>c</li></ol>")
        assert body.split("\n")[0] == "1. a"
        assert "1. b" in body


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

    def test_inline_markup_stays_inside_its_cell(self):
        """A cell is a slice of the inline buffer, so `<code>` cannot escape it."""
        body, _ = render(
            "<table><tr><th>Flag</th><th>Meaning</th></tr>"
            "<tr><td><code>-o</code></td><td><b>output</b> dir</td></tr></table>"
        )
        assert "| Flag | Meaning |" in body
        assert "| `-o` | **output** dir |" in body
        assert "\n`-o`" not in body

    def test_link_in_a_cell_keeps_its_href(self):
        body, _ = render(
            "<table><tr><th>a</th></tr>"
            "<tr><td><a href='https://x.test'>x</a></td></tr></table>"
        )
        assert "| [x](https://x.test) |" in body

    def test_br_in_a_cell_does_not_split_the_row(self):
        body, _ = render("<table><tr><th>a</th></tr><tr><td>one<br>two</td></tr></table>")
        assert "| one two |" in body
        assert all(line.startswith("|") for line in body.splitlines())


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


class TestInlineMarkup:
    """Inline elements must stay inside the container they were written in.

    These cases all regressed at least once: inline elements are rendered as
    delimiters appended to a shared buffer, and moving that buffer's *text*
    somewhere else strands the delimiters away from the words they wrap.
    """

    def test_inline_code_inside_a_list_item(self):
        body, _ = render("<ul><li>run <code>--flag</code> now</li></ul>")
        assert body == "- run `--flag` now"

    def test_bold_and_link_inside_a_list_item(self):
        body, _ = render('<ul><li><b>bold</b> then <a href="https://x.test">link</a> tail</li></ul>')
        assert body == "- **bold** then [link](https://x.test) tail"

    def test_emphasis_in_the_second_of_three_items(self):
        body, _ = render("<ul><li>first</li><li>second <em>emph</em></li><li>third</li></ul>")
        assert body == "- first\n- second *emph*\n- third"

    def test_nested_emphasis(self):
        body, _ = render("<p><strong>a <em>b</em> c</strong></p>")
        assert body == "**a *b* c**"

    def test_adjacent_emphasis_spans(self):
        body, _ = render("<p><em>a</em> and <em>b</em></p>")
        assert body == "*a* and *b*"

    def test_inline_code_in_a_paragraph(self):
        body, _ = render("<p>use <code>--flag</code> here</p>")
        assert body == "use `--flag` here"

    def test_multiple_inline_codes_in_one_paragraph(self):
        body, _ = render("<p><code>a</code> then <code>b</code></p>")
        assert body == "`a` then `b`"

    def test_link_label_containing_emphasis(self):
        body, _ = render('<a href="https://x.test"><b>Bold label</b></a>')
        assert body == "[**Bold label**](https://x.test)"

    def test_dropped_links_keep_the_label_text(self):
        body, _ = render('<p>see <a href="https://x.test">this</a> now</p>', keep_links=False)
        assert body == "see this now"

    def test_heading_containing_inline_markup(self):
        body, _ = render("<h2>A <code>code</code> heading</h2>")
        assert body == "## A `code` heading"


class TestNestedLists:
    def test_parent_text_comes_before_children(self):
        body, _ = render("<ul><li>parent<ul><li>child</li></ul></li><li>next</li></ul>")
        assert body.split("\n")[0] == "- parent"
        assert "- child" in body
        assert body.split("\n")[-1] == "- next"

    def test_three_levels_deep(self):
        body, _ = render("<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>")
        lines = body.split("\n")
        assert lines[0].strip() == "- a"
        assert lines[-1].strip() == "- d"
        # Indentation must increase with depth.
        indent_b = len(lines[1]) - len(lines[1].lstrip())
        indent_c = len(lines[2]) - len(lines[2].lstrip())
        assert indent_b < indent_c

    def test_ordered_nested_list(self):
        body, _ = render("<ol><li>a<ol><li>b</li></ol></li><li>c</li></ol>")
        assert body.split("\n")[0] == "1. a"
        assert "1. b" in body
        assert body.split("\n")[-1] == "2. c"


class TestLineBreaks:
    def test_br_becomes_a_hard_break(self):
        body, _ = render("<p>line one<br>line two</p>")
        # Two trailing spaces: a soft newline would be collapsed by renderers.
        assert body == "line one  \nline two"


class TestDefinitionLists:
    def test_term_and_definition_are_separated(self):
        body, _ = render("<dl><dt>Term</dt><dd>Meaning</dd></dl>")
        assert "Term" in body
        assert "Meaning" in body
        assert body.index("Term") < body.index("Meaning")


class TestHeaderlessTables:
    def test_first_row_is_not_promoted_to_a_header(self):
        body, _ = render("<table><tr><td>1</td><td>2</td></tr></table>")
        lines = body.split("\n")
        # A placeholder header, so real data is never mislabelled as a column name.
        assert "(no header)" in lines[0]
        assert lines[-1] == "| 1 | 2 |"

    def test_explicit_thead_is_used_as_the_header(self):
        body, _ = render(
            "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"
        )
        assert body.split("\n")[0] == "| A |"


class TestEngineSelection:
    def test_builtin_engine_is_always_available(self):
        body, stats, engine = html_to_markdown("<p>x</p>", engine="builtin")
        assert engine == "builtin"
        assert body == "x"

    def test_auto_selects_the_builtin_engine(self):
        """Markdownify is opt-in: installing it must not change the output."""
        _body, _stats, engine = html_to_markdown("<p>x</p>", engine="auto")
        assert engine == "builtin"

    def test_auto_keeps_the_h1_and_the_fence_language(self):
        html = (
            "<body><header><h1>Title</h1></header>"
            '<pre><code class="language-python">x = 1</code></pre></body>'
        )
        body, _stats, engine = html_to_markdown(html, engine="auto")
        assert engine == "builtin"
        assert "# Title" in body
        assert "```python" in body

    def test_markdownify_engine_keeps_chrome_dropping(self):
        pytest.importorskip("markdownify")
        html = (
            "<body><header><h1>Title</h1></header>"
            "<nav><a href='/'>Home</a></nav><p>body</p></body>"
        )
        body, _stats, engine = html_to_markdown(html, engine="markdownify")
        assert engine == "markdownify"
        assert "Home" not in body  # nav is still judged to be chrome
        assert "# Title" in body  # ... but the header holding the <h1> is not
        assert "body" in body

    def test_markdownify_engine_keeps_the_fence_language(self):
        """markdownify drops ``class="language-x"``; the wrapper puts it back."""
        pytest.importorskip("markdownify")
        html = '<pre><code class="language-python">x = 1\ny = 2</code></pre>'
        body, _stats, _engine = html_to_markdown(html, engine="markdownify")
        assert "```python" in body
        assert "x = 1\ny = 2" in body

    def test_markdownify_engine_does_not_render_the_title_tag(self):
        pytest.importorskip("markdownify")
        html = "<head><title>Doc</title><style>p{}</style></head><body><p>x</p></body>"
        body, _stats, _engine = html_to_markdown(html, engine="markdownify")
        assert "Doc" not in body and "p{}" not in body
        assert "x" in body

    def test_markdownify_engine_drops_the_site_header_and_footer(self):
        pytest.importorskip("markdownify")
        html = (
            "<body><header><a href='/'><img src='l.png' alt='Logo'></a>"
            "<nav><a href='/'>Home</a></nav></header>"
            "<main><p>real content</p></main>"
            "<footer><p>&copy; 2025 Example Inc.</p></footer></body>"
        )
        body, _stats, _engine = html_to_markdown(html, engine="markdownify")
        assert "real content" in body
        assert "Logo" not in body and "Home" not in body
        assert "Example Inc" not in body

    def test_markdownify_engine_leaves_entities_alone(self):
        """The chrome filter re-emits raw markup, so entities must survive it."""
        pytest.importorskip("markdownify")
        html = "<body><p>a &amp; b &lt;tag&gt; caf&eacute;</p></body>"
        body, _stats, _engine = html_to_markdown(html, engine="markdownify")
        assert "a & b <tag> café" in body

    def test_markdownify_engine_honours_no_links_and_no_images(self):
        pytest.importorskip("markdownify")
        html = ('<p>see <a href="https://x.test">the docs</a>'
                ' <img src="i.png" alt="pic"></p>')
        body, _stats, _engine = html_to_markdown(
            html, engine="markdownify", keep_links=False, keep_images=False
        )
        assert body == "see the docs"

    def test_unknown_engine_is_rejected(self):
        with pytest.raises(ValueError, match="unknown HTML engine"):
            html_to_markdown("<p>x</p>", engine="html2text")

    def test_unavailable_markdownify_is_reported_clearly(self, monkeypatch):
        """The error must name the fix, not just fail."""
        import builtins
        import sys as _sys

        real_import = builtins.__import__

        def block_markdownify(name, *args, **kwargs):
            if name == "markdownify" or name.startswith("markdownify."):
                raise ImportError("No module named 'markdownify'")
            return real_import(name, *args, **kwargs)

        # Blocking the import reproduces a machine without the package, which is
        # also how the default (zero-dependency) install behaves.
        monkeypatch.delitem(_sys.modules, "markdownify", raising=False)
        monkeypatch.setattr(builtins, "__import__", block_markdownify)
        with pytest.raises(RuntimeError, match="markdownify is not installed"):
            html_to_markdown("<p>x</p>", engine="markdownify")


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
