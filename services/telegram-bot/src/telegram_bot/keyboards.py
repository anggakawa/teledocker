"""Inline keyboard builders for Telegram bot interactions."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


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
