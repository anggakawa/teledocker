"""Handlers for /start, /myid, and /help commands."""

import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.keyboards import new_user_admin_keyboard

logger = logging.getLogger(__name__)

_HELP_TEXT = """
*ChatOps AI Bridge — Commands*

*Session commands* _(approved users)_:
`/new` — Create a new container session
`/stop` — Stop active container \\(preserves workspace\\)
`/restart` — Restart active container
`/destroy` — Destroy container and workspace permanently
`/status` — Show container status and resource usage

*Interaction* _(approved users)_:
Send any message → forwarded to Claude Code
`/shell <cmd>` — Execute raw shell command
`/download <path>` — Download file from container
Upload a file → saved to container workspace

*Configuration* _(approved users)_:
`/setkey <api\\_key>` — Store API key \\(message auto\\-deleted\\)
`/setprovider <preset>` — Set provider: `anthropic`, `openrouter`, `custom`
`/setbaseurl <url>` — Set custom API base URL
`/removekey` — Remove stored credentials
`/provider` — Show current provider config

*Admin commands*:
`/approve <id>` — Approve pending user
`/reject <id>` — Reject pending user
`/revoke <id>` — Revoke user access
`/users [filter]` — List users
`/containers` — List all active sessions

*General*:
`/myid` — Show your Telegram ID
`/help` — Show this message
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register the user and send a welcome message."""
    user = update.effective_user
    api_client = context.bot_data["api_client"]
    admin_ids = context.bot_data["admin_ids"]

    user_dto = await api_client.register_user(
        telegram_id=user.id,
        telegram_username=user.username,
        display_name=user.full_name,
    )

    if user_dto.is_approved:
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! You're all set. "
            "Use /new to start a session or send a message to your active container."
        )
    else:
        await update.message.reply_text(
            f"Welcome, {user.first_name}!\n\n"
            "Your registration request has been sent to an admin for approval. "
            "You'll be notified once approved.\n\n"
            "In the meantime, you can use /myid to see your Telegram ID."
        )

        # Notify all admins about the new registration.
        for admin_id in admin_ids:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"New user registration:\n"
                        f"Name: {user.full_name}\n"
                        f"Username: @{user.username or 'N/A'}\n"
                        f"Telegram ID: {user.id}"
                    ),
                    reply_markup=new_user_admin_keyboard(user.id),
                )
            except Exception as exc:
                logger.warning("Failed to notify admin %s: %s", admin_id, exc)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the user's Telegram numeric ID. Works without registration."""
    await update.message.reply_text(
        f"Your Telegram ID is: `{update.effective_user.id}`",
        parse_mode="MarkdownV2",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the full command reference."""
    await update.message.reply_text(_HELP_TEXT, parse_mode="MarkdownV2")
