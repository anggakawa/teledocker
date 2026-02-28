"""Convert Markdown text to Telegram-safe HTML.

Claude Code outputs standard Markdown (bold, headings, code blocks, etc.)
but Telegram needs explicit parse_mode="HTML" with specific tags. This module
converts common Markdown patterns to Telegram-supported HTML tags.

Three-phase approach:
1. Extract and protect code blocks / inline code with placeholder tokens.
2. Transform remaining Markdown to HTML (escaping HTML entities first).
3. Restore protected blocks with proper <pre><code> / <code> wrapping.

Streaming safety: unclosed Markdown (e.g. ``**bol``) simply won't match
any regex and passes through as literal text. The next flush (1s later)
will have the complete ``**bold**`` and convert correctly.
"""

import re
import uuid

# Telegram-supported HTML tags reference:
# <b>, <i>, <s>, <u>, <code>, <pre>, <a href="">, <blockquote>

_PLACEHOLDER_PREFIX = "\x00PROTECTED_"


def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown text to Telegram-compatible HTML.

    Args:
        text: Raw Markdown text from Claude Code output.

    Returns:
        HTML string safe for Telegram's parse_mode="HTML".
    """
    if not text:
        return text

    protected_blocks: dict[str, str] = {}

    # Phase 1: Extract and protect code blocks and inline code.
    text = _protect_fenced_code_blocks(text, protected_blocks)
    text = _protect_inline_code(text, protected_blocks)

    # Phase 2: Transform remaining Markdown to HTML.
    text = _escape_html_entities(text)
    text = _convert_headings(text)
    text = _convert_bold(text)
    text = _convert_italic(text)
    text = _convert_strikethrough(text)
    text = _convert_links(text)
    text = _convert_blockquotes(text)

    # Phase 3: Restore protected blocks with HTML wrapping.
    text = _restore_protected_blocks(text, protected_blocks)

    return text


def _make_placeholder(protected_blocks: dict[str, str], content: str) -> str:
    """Create a unique placeholder token and store the original content."""
    token = f"{_PLACEHOLDER_PREFIX}{uuid.uuid4().hex}\x00"
    protected_blocks[token] = content
    return token


# -- Phase 1: Extraction ------------------------------------------------


def _protect_fenced_code_blocks(
    text: str, protected_blocks: dict[str, str]
) -> str:
    """Replace fenced code blocks (```) with placeholders.

    Preserves the language specifier for syntax display.
    Only protects complete (closed) fences — unclosed fences pass through.
    """
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

    def replacer(match: re.Match) -> str:
        language = match.group(1)
        code_body = match.group(2)
        # Escape HTML inside code so tags like <div> render literally.
        escaped_code = _escape_html_entities(code_body)
        if language:
            html = f"<pre><code class=\"language-{language}\">{escaped_code}</code></pre>"
        else:
            html = f"<pre><code>{escaped_code}</code></pre>"
        return _make_placeholder(protected_blocks, html)

    return pattern.sub(replacer, text)


def _protect_inline_code(text: str, protected_blocks: dict[str, str]) -> str:
    """Replace inline code (`...`) with placeholders.

    Only matches complete pairs — an unclosed backtick passes through.
    Does not match across newlines (inline code is single-line).
    """
    pattern = re.compile(r"`([^`\n]+)`")

    def replacer(match: re.Match) -> str:
        code_body = match.group(1)
        escaped_code = _escape_html_entities(code_body)
        html = f"<code>{escaped_code}</code>"
        return _make_placeholder(protected_blocks, html)

    return pattern.sub(replacer, text)


# -- Phase 2: Transformation -------------------------------------------


def _escape_html_entities(text: str) -> str:
    """Escape HTML special characters.

    Must run BEFORE any tag generation to avoid re-escaping our own tags.
    """
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _convert_headings(text: str) -> str:
    """Convert Markdown headings (# to ######) to bold text.

    Telegram has no heading tag, so we render them as bold.
    Only matches at line start.
    """
    pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    return pattern.sub(r"<b>\2</b>", text)


def _convert_bold(text: str) -> str:
    """Convert **text** to <b>text</b>.

    Must run before italic conversion to avoid ** being partially
    consumed by the single-* italic pattern.
    """
    pattern = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
    return pattern.sub(r"<b>\1</b>", text)


def _convert_italic(text: str) -> str:
    """Convert *text* to <i>text</i>.

    Uses word-boundary guards for underscore variant (_text_) to avoid
    matching snake_case identifiers like my_variable_name.
    Only the asterisk variant is converted since underscore in code
    contexts causes too many false positives.
    """
    pattern = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
    return pattern.sub(r"<i>\1</i>", text)


def _convert_strikethrough(text: str) -> str:
    """Convert ~~text~~ to <s>text</s>."""
    pattern = re.compile(r"~~(.+?)~~", re.DOTALL)
    return pattern.sub(r"<s>\1</s>", text)


def _convert_links(text: str) -> str:
    """Convert [text](url) to <a href="url">text</a>.

    The URL must not contain parentheses to avoid matching partial patterns.
    """
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    return pattern.sub(r'<a href="\2">\1</a>', text)


def _convert_blockquotes(text: str) -> str:
    """Convert > quoted lines to <blockquote> tags.

    Consecutive > lines are merged into a single blockquote.
    The leading &gt; (HTML-escaped >) is matched since escaping runs first.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    blockquote_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("&gt; "):
            # Accumulate blockquote content (strip the leading &gt; ).
            blockquote_lines.append(stripped[5:])
        elif stripped == "&gt;":
            # Empty blockquote line.
            blockquote_lines.append("")
        else:
            if blockquote_lines:
                merged = "\n".join(blockquote_lines)
                result_lines.append(f"<blockquote>{merged}</blockquote>")
                blockquote_lines = []
            result_lines.append(line)

    # Flush any trailing blockquote.
    if blockquote_lines:
        merged = "\n".join(blockquote_lines)
        result_lines.append(f"<blockquote>{merged}</blockquote>")

    return "\n".join(result_lines)


# -- Phase 3: Restoration -----------------------------------------------


def _restore_protected_blocks(
    text: str, protected_blocks: dict[str, str]
) -> str:
    """Replace placeholder tokens with their HTML-wrapped content."""
    for token, html in protected_blocks.items():
        text = text.replace(token, html)
    return text
