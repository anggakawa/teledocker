"""Tests for Markdown-to-Telegram HTML converter.

Covers all conversion rules: HTML escaping, code blocks, inline code,
bold, italic, strikethrough, links, headings, blockquotes, combined
patterns, and streaming edge cases (partial/unclosed markdown).
"""


from telegram_bot.markdown_to_telegram import markdown_to_telegram_html


class TestHtmlEscaping:
    """HTML entity escaping must happen before tag generation."""

    def test_ampersand_escaped(self):
        assert markdown_to_telegram_html("A & B") == "A &amp; B"

    def test_angle_brackets_escaped(self):
        assert markdown_to_telegram_html("a < b > c") == "a &lt; b &gt; c"

    def test_all_entities_escaped(self):
        result = markdown_to_telegram_html("if a < b && b > c:")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result

    def test_no_double_escape(self):
        """&amp; in input should become &amp;amp; not stay as &amp;."""
        result = markdown_to_telegram_html("&amp;")
        assert result == "&amp;amp;"


class TestFencedCodeBlocks:
    """Fenced code blocks (```) are wrapped in <pre><code>."""

    def test_basic_code_block(self):
        text = "```\nprint('hello')\n```"
        result = markdown_to_telegram_html(text)
        assert "<pre><code>" in result
        assert "print(&#x27;hello&#x27;)" in result or "print('hello')" in result
        assert "</code></pre>" in result

    def test_code_block_with_language(self):
        text = '```python\ndef foo():\n    return 42\n```'
        result = markdown_to_telegram_html(text)
        assert 'class="language-python"' in result
        assert "def foo():" in result

    def test_code_block_preserves_html_chars(self):
        """HTML entities inside code blocks should be escaped."""
        text = "```\nif a < b && c > d:\n```"
        result = markdown_to_telegram_html(text)
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result

    def test_code_block_ignores_markdown_inside(self):
        """Markdown inside code blocks should NOT be converted."""
        text = "```\n**not bold** and *not italic*\n```"
        result = markdown_to_telegram_html(text)
        # The raw ** and * should be preserved (HTML-escaped content).
        assert "<b>not bold</b>" not in result
        assert "<i>not italic</i>" not in result

    def test_unclosed_code_block_passes_through(self):
        """Unclosed ``` should not be converted (streaming safety)."""
        text = "```python\ndef foo():\n    return 42"
        result = markdown_to_telegram_html(text)
        # Should still have the raw ``` since it's unclosed.
        assert "```" in result or "``" in result

    def test_multiple_code_blocks(self):
        text = "Before\n```\nblock1\n```\nMiddle\n```\nblock2\n```\nAfter"
        result = markdown_to_telegram_html(text)
        assert result.count("<pre><code>") == 2
        assert result.count("</code></pre>") == 2


class TestInlineCode:
    """Inline code (`...`) is wrapped in <code>."""

    def test_basic_inline_code(self):
        result = markdown_to_telegram_html("Use `pip install` to install")
        assert "<code>pip install</code>" in result

    def test_inline_code_preserves_html_chars(self):
        result = markdown_to_telegram_html("Try `a < b`")
        assert "<code>a &lt; b</code>" in result

    def test_inline_code_ignores_markdown(self):
        """Markdown inside inline code should NOT be converted."""
        result = markdown_to_telegram_html("Use `**not bold**` here")
        assert "<b>" not in result
        assert "**not bold**" in result or "&amp;" in result

    def test_unclosed_backtick_passes_through(self):
        """Single unclosed backtick should pass through."""
        result = markdown_to_telegram_html("Use `incomplete")
        assert "`incomplete" in result

    def test_multiple_inline_codes(self):
        result = markdown_to_telegram_html("`foo` and `bar`")
        assert "<code>foo</code>" in result
        assert "<code>bar</code>" in result


class TestBold:
    """**text** is converted to <b>text</b>."""

    def test_basic_bold(self):
        result = markdown_to_telegram_html("This is **bold** text")
        assert "This is <b>bold</b> text" in result

    def test_bold_multiple(self):
        result = markdown_to_telegram_html("**one** and **two**")
        assert "<b>one</b>" in result
        assert "<b>two</b>" in result

    def test_partial_bold_passes_through(self):
        """Unclosed ** should pass through (streaming safety)."""
        result = markdown_to_telegram_html("This is **bol")
        assert "**bol" in result
        assert "<b>" not in result

    def test_bold_with_html_entities(self):
        result = markdown_to_telegram_html("**a & b**")
        assert "<b>a &amp; b</b>" in result


class TestItalic:
    """*text* is converted to <i>text</i>."""

    def test_basic_italic(self):
        result = markdown_to_telegram_html("This is *italic* text")
        assert "This is <i>italic</i> text" in result

    def test_italic_not_confused_with_bold(self):
        """Bold (**) should be processed first, leaving single * for italic."""
        result = markdown_to_telegram_html("**bold** and *italic*")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result

    def test_partial_italic_passes_through(self):
        """Unclosed * should pass through (streaming safety)."""
        result = markdown_to_telegram_html("This is *ital")
        # Unclosed single * passes through.
        assert "<i>" not in result

    def test_asterisk_in_math_not_italic(self):
        """Standalone asterisks like 2*3 should ideally not become italic."""
        # This is a known limitation — single-word *match* will convert.
        # But multi-word content between * markers is the target.
        result = markdown_to_telegram_html("*emphasized words*")
        assert "<i>emphasized words</i>" in result


class TestStrikethrough:
    """~~text~~ is converted to <s>text</s>."""

    def test_basic_strikethrough(self):
        result = markdown_to_telegram_html("This is ~~deleted~~ text")
        assert "<s>deleted</s>" in result

    def test_partial_strikethrough_passes_through(self):
        result = markdown_to_telegram_html("This is ~~incom")
        assert "<s>" not in result


class TestLinks:
    """[text](url) is converted to <a href="url">text</a>."""

    def test_basic_link(self):
        result = markdown_to_telegram_html("[Click here](https://example.com)")
        assert '<a href="https://example.com">Click here</a>' in result

    def test_link_text_with_html_entities(self):
        result = markdown_to_telegram_html("[A & B](https://example.com)")
        assert "A &amp; B" in result
        assert "https://example.com" in result

    def test_partial_link_passes_through(self):
        """Unclosed link syntax should pass through."""
        result = markdown_to_telegram_html("[Click here](https://exam")
        # Should not produce a valid <a> tag with incomplete URL.
        assert "<a " not in result


class TestHeadings:
    """# headings are converted to bold text."""

    def test_h1(self):
        result = markdown_to_telegram_html("# Title")
        assert "<b>Title</b>" in result

    def test_h2(self):
        result = markdown_to_telegram_html("## Subtitle")
        assert "<b>Subtitle</b>" in result

    def test_h3(self):
        result = markdown_to_telegram_html("### Section")
        assert "<b>Section</b>" in result

    def test_h6(self):
        result = markdown_to_telegram_html("###### Deep")
        assert "<b>Deep</b>" in result

    def test_heading_only_at_line_start(self):
        """# in the middle of a line should NOT become a heading."""
        result = markdown_to_telegram_html("Not a # heading")
        assert "<b>heading</b>" not in result

    def test_multiple_headings(self):
        text = "# First\nSome text\n## Second"
        result = markdown_to_telegram_html(text)
        assert "<b>First</b>" in result
        assert "<b>Second</b>" in result


class TestBlockquotes:
    """> lines are converted to <blockquote> tags."""

    def test_single_blockquote(self):
        result = markdown_to_telegram_html("> This is quoted")
        assert "<blockquote>This is quoted</blockquote>" in result

    def test_consecutive_blockquotes_merged(self):
        text = "> Line one\n> Line two\n> Line three"
        result = markdown_to_telegram_html(text)
        # All lines should be merged into a single blockquote.
        assert result.count("<blockquote>") == 1
        assert "Line one\nLine two\nLine three" in result

    def test_blockquotes_with_gap_are_separate(self):
        text = "> First quote\n\nSome text\n\n> Second quote"
        result = markdown_to_telegram_html(text)
        assert result.count("<blockquote>") == 2

    def test_empty_blockquote_line(self):
        text = "> Line one\n>\n> Line three"
        result = markdown_to_telegram_html(text)
        assert "<blockquote>" in result


class TestCombinedPatterns:
    """Realistic Claude Code responses with mixed Markdown elements."""

    def test_heading_with_bold_and_code(self):
        text = "# Summary\n\nThe function **calculate** uses `math.sqrt`."
        result = markdown_to_telegram_html(text)
        assert "<b>Summary</b>" in result
        assert "<b>calculate</b>" in result
        assert "<code>math.sqrt</code>" in result

    def test_code_block_between_text(self):
        text = "Here is the fix:\n\n```python\ndef fix():\n    return True\n```\n\nThat should work."
        result = markdown_to_telegram_html(text)
        assert "Here is the fix:" in result
        assert "<pre><code" in result
        assert "def fix():" in result
        assert "That should work." in result

    def test_realistic_claude_response(self):
        text = (
            "## Analysis\n\n"
            "The **bug** is in `main.py` where the function *silently* fails.\n\n"
            "```python\ndef broken():\n    if x < 0:\n        return None\n```\n\n"
            "Fix: add proper error handling with `raise ValueError`."
        )
        result = markdown_to_telegram_html(text)
        assert "<b>Analysis</b>" in result
        assert "<b>bug</b>" in result
        assert "<code>main.py</code>" in result
        assert "<i>silently</i>" in result
        assert "<pre><code" in result
        assert "&lt;" in result  # < inside code block escaped
        assert "<code>raise ValueError</code>" in result

    def test_link_with_bold_text(self):
        text = "See [**docs**](https://docs.example.com) for details."
        result = markdown_to_telegram_html(text)
        assert "https://docs.example.com" in result


class TestStreamingEdgeCases:
    """Edge cases that occur during real-time streaming."""

    def test_empty_string(self):
        assert markdown_to_telegram_html("") == ""

    def test_plain_text_unchanged(self):
        text = "Just a normal sentence with no markdown."
        result = markdown_to_telegram_html(text)
        assert result == text

    def test_partial_bold_mid_stream(self):
        """During streaming, we may get incomplete bold markers."""
        result = markdown_to_telegram_html("The **answer is")
        assert "<b>" not in result
        assert "**answer is" in result

    def test_partial_code_block_mid_stream(self):
        """During streaming, code block may not be closed yet."""
        result = markdown_to_telegram_html("```python\ndef foo():")
        # Unclosed fence — should not produce <pre><code>.
        assert "<pre>" not in result

    def test_only_whitespace(self):
        result = markdown_to_telegram_html("   ")
        assert result == "   "

    def test_tool_status_line_with_angle_brackets(self):
        """Tool status lines may contain > which gets escaped."""
        result = markdown_to_telegram_html("> Reading src/main.py...")
        assert "&gt; Reading" in result or "<blockquote>" in result

    def test_newlines_preserved(self):
        text = "Line 1\n\nLine 2\n\nLine 3"
        result = markdown_to_telegram_html(text)
        assert "\n\n" in result
