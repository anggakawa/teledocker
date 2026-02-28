"""Inline keyboard builders for Telegram bot interactions."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Statuses that admins can bulk-destroy. Running and creating are excluded
# to prevent accidental destruction of active or in-progress sessions.
BULK_DESTROYABLE_STATUSES = {"error", "paused", "stopped"}


def new_user_admin_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Keyboard sent to admins when a new user registers."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"approve:{telegram_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{telegram_id}"),
        ]
    ])


def container_stopped_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown when a container is stopped."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Restart", callback_data=f"restart:{session_id}"),
            InlineKeyboardButton("Destroy", callback_data=f"destroy:{session_id}"),
        ]
    ])


def no_session_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown when user has no active session."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Create Container", callback_data="action:new_session")],
        [InlineKeyboardButton("Set API Key First", callback_data="action:set_key_help")],
        [InlineKeyboardButton("Configure Provider", callback_data="action:provider_help")],
    ])


def error_state_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown when a container is in error state."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Restart Container", callback_data=f"restart:{session_id}")],
        [InlineKeyboardButton("Destroy & Recreate", callback_data=f"destroy_recreate:{session_id}")],
    ])


def confirm_stop_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Confirmation keyboard for /stop command."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, stop it", callback_data=f"confirm_stop:{session_id}"),
            InlineKeyboardButton("Cancel", callback_data="action:cancel"),
        ]
    ])


def admin_sessions_keyboard(
    sessions: list[tuple[int, str]],
    status_counts: dict[str, int] | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard for admin /containers command with a destroy button per session.

    Args:
        sessions: List of (display_index, session_id) pairs.
        status_counts: Mapping of status -> count for bulk destroy buttons.
            Only statuses in BULK_DESTROYABLE_STATUSES are shown.
    """
    rows = [
        [InlineKeyboardButton(
            f"Destroy #{index}",
            callback_data=f"admin_destroy:{session_id}",
        )]
        for index, session_id in sessions
    ]

    if status_counts:
        for status_name in sorted(BULK_DESTROYABLE_STATUSES):
            count = status_counts.get(status_name, 0)
            if count > 0:
                rows.append([InlineKeyboardButton(
                    f"Destroy all {status_name} ({count})",
                    callback_data=f"admin_destroy_status:{status_name}",
                )])

    return InlineKeyboardMarkup(rows)


def session_list_keyboard(
    sessions: list[tuple[int, str, str]],
) -> InlineKeyboardMarkup:
    """Keyboard for /sessions — one button per session.

    Args:
        sessions: List of (display_index, session_id, button_label) tuples.
    """
    rows = [
        [InlineKeyboardButton(label, callback_data=f"sess_detail:{session_id}")]
        for _, session_id, label in sessions
    ]
    return InlineKeyboardMarkup(rows)


def session_detail_keyboard(session_id: str, status: str) -> InlineKeyboardMarkup:
    """Action keyboard for a selected session from the /sessions list.

    Shows "Resume" for stopped/paused sessions, "View History" for all,
    and a "Back to List" button.
    """
    rows = []

    if status in ("stopped", "paused", "error"):
        rows.append([InlineKeyboardButton(
            "Resume", callback_data=f"resume:{session_id}",
        )])

    rows.append([InlineKeyboardButton(
        "View History", callback_data=f"sess_hist:{session_id}",
    )])
    rows.append([InlineKeyboardButton(
        "Back to List", callback_data="action:sessions_list",
    )])

    return InlineKeyboardMarkup(rows)


def confirm_destroy_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Double-confirm keyboard for /destroy command."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Yes, destroy permanently", callback_data=f"confirm_destroy:{session_id}"
            ),
            InlineKeyboardButton("Cancel", callback_data="action:cancel"),
        ]
    ])
