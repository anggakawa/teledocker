"""Response formatting utilities for Telegram MarkdownV2.

MarkdownV2 requires escaping a specific set of special characters in regular
text, but NOT inside code blocks. We handle both cases carefully here.
"""

import re

from chatops_shared.schemas.session import SessionDTO

# All characters that must be escaped in MarkdownV2 plain text.
_MARKDOWNV2_SPECIAL_CHARS = r"\_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 special characters in a plain text string."""
    return re.sub(r"([" + re.escape(_MARKDOWNV2_SPECIAL_CHARS) + r"])", r"\\\1", text)


def format_code_block(code: str, language: str = "") -> str:
    """Wrap text in a MarkdownV2 code block fence.

    The code content itself must NOT be escaped — Telegram renders it as-is
    inside code blocks.
    """
    return f"```{language}\n{code}\n```"


def format_error(message: str) -> str:
    """Format an error message with a warning prefix in monospace."""
    escaped = escape_markdown_v2(message)
    return f"⚠️ `{escaped}`"


def format_status(session: SessionDTO, stats: dict) -> str:
    """Format a container status summary for display in Telegram."""
    status_emoji = {
        "running": "🟢",
        "paused": "🟡",
        "stopped": "🔴",
        "creating": "⏳",
        "error": "❌",
    }.get(session.status, "❓")

    lines = [
        f"{status_emoji} *Container Status*",
        f"Name: `{escape_markdown_v2(session.container_name)}`",
        f"Status: `{session.status}`",
        f"Agent: `{session.agent_type}`",
    ]

    if stats:
        lines.extend([
            "",
            "*Resources:*",
            f"CPU: `{stats.get('cpu_percent', '?')}%`",
            f"RAM: `{stats.get('memory_used_mb', '?')} MB` / `{stats.get('memory_limit_mb', '?')} MB`",
        ])

    return "\n".join(lines)
