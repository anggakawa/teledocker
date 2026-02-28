"""Response formatting utilities for Telegram MarkdownV2.

MarkdownV2 requires escaping a specific set of special characters in regular
text, but NOT inside code blocks. We handle both cases carefully here.
"""

import re
from datetime import UTC, datetime

from chatops_shared.schemas.message import MessageDTO
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
    lines = [f"Containers ({len(sessions_with_users)}):"]
    lines.append("")

    for index, (session, user) in enumerate(sessions_with_users, start=1):
        emoji = _STATUS_EMOJI.get(session.status, "❓")

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


_STATUS_EMOJI = {
    "running": "🟢",
    "paused": "🟡",
    "stopped": "🔴",
    "creating": "⏳",
    "error": "❌",
}


def format_session_list_for_user(sessions: list[SessionDTO]) -> str:
    """Format a user's session list for the /sessions command.

    Uses plain text (no MarkdownV2) because inline keyboard messages
    are simpler to handle without escaping.
    """
    if not sessions:
        return "No sessions found. Use /new to create one."

    lines = [f"Your Sessions ({len(sessions)}):"]
    lines.append("")

    for index, session in enumerate(sessions, start=1):
        emoji = _STATUS_EMOJI.get(session.status, "?")
        age = format_age(session.created_at)
        lines.append(f"#{index} {emoji} {session.status.value} - {session.container_name}")
        lines.append(f"     Created: {age}")
        lines.append("")

    return "\n".join(lines)


def format_session_button_label(session: SessionDTO, index: int) -> str:
    """Build a short label for a session's inline keyboard button."""
    emoji = _STATUS_EMOJI.get(session.status, "?")
    return f"#{index} {emoji} {session.container_name}"


def format_message_history(messages: list[MessageDTO]) -> str:
    """Format message history for display in Telegram.

    Uses direction arrows and truncates long messages to stay within
    Telegram's 4096-char limit.
    """
    if not messages:
        return "No messages in this session yet."

    max_content_length = 200
    lines = []

    for message in messages:
        arrow = ">>" if message.direction == "inbound" else "<<"
        content = message.content
        if len(content) > max_content_length:
            content = content[:max_content_length] + "..."
        timestamp = message.created_at.strftime("%H:%M")
        lines.append(f"{arrow} [{timestamp}] {content}")

    return "\n".join(lines)


def format_status(session: SessionDTO, stats: dict) -> str:
    """Format a container status summary for display in Telegram."""
    status_emoji = _STATUS_EMOJI.get(session.status, "❓")

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
