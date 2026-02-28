"""Response formatting utilities for Telegram MarkdownV2.

MarkdownV2 requires escaping a specific set of special characters in regular
text, but NOT inside code blocks. We handle both cases carefully here.
"""

import re
from datetime import UTC, datetime

from chatops_shared.schemas.session import SessionDTO
from chatops_shared.schemas.user import UserDTO

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


def format_age(dt: datetime) -> str:
    """Return a human-readable age string like '5m ago', '3h ago', '2d ago'."""
    now = datetime.now(UTC)
    delta_seconds = (now - dt).total_seconds()

    if delta_seconds < 0:
        return "just now"

    minutes = int(delta_seconds // 60)
    hours = int(delta_seconds // 3600)
    days = int(delta_seconds // 86400)

    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if hours < 24:
        return f"{hours}h ago"
    return f"{days}d ago"


def format_session_list_for_admin(
    sessions_with_users: list[tuple[SessionDTO, UserDTO | None]],
) -> str:
    """Format a list of sessions for the admin /containers command.

    Each entry shows: index, user info, status emoji, container name,
    created age, and last activity age.
    """
    status_emoji = {
        "running": "🟢",
        "paused": "🟡",
        "stopped": "🔴",
        "creating": "⏳",
        "error": "❌",
    }

    lines = [f"Containers ({len(sessions_with_users)}):"]
    lines.append("")

    for index, (session, user) in enumerate(sessions_with_users, start=1):
        emoji = status_emoji.get(session.status, "❓")

        if user:
            username_part = f"@{user.telegram_username}" if user.telegram_username else ""
            user_label = f"{user.display_name} {username_part} ({user.telegram_id})"
        else:
            user_label = f"user_id={session.user_id}"

        created_age = format_age(session.created_at)
        activity_age = format_age(session.last_activity_at)

        lines.append(f"#{index} {emoji} {session.status.value}")
        lines.append(f"  User: {user_label}")
        lines.append(f"  Container: {session.container_name}")
        lines.append(f"  Created: {created_age}")
        lines.append(f"  Last activity: {activity_age}")
        lines.append("")

    return "\n".join(lines)


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
