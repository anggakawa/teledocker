"""Handlers for /new, /stop, /restart, /destroy, and /status commands."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.formatters import format_status
from telegram_bot.keyboards import (
    confirm_destroy_keyboard,
    confirm_stop_keyboard,
    no_session_keyboard,
)

logger = logging.getLogger(__name__)


async def _require_approved(update: Update, api_client) -> bool:
    """Check if user is approved. Send error message and return False if not."""
    user = await api_client.get_user(update.effective_user.id)
    if user is None or not user.is_approved:
        await update.message.reply_text(
            "Your account is pending admin approval. "
            "You'll be notified once approved."
        )
        return False
    return True


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a new Docker container session for the user."""
    api_client = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    user_dto = await api_client.get_user(update.effective_user.id)

    # Parse optional args: /new [agent_type] [system_prompt...]
    agent_type = "claude-code"
    system_prompt = None
    if context.args:
        agent_type = context.args[0]
        if len(context.args) > 1:
            system_prompt = " ".join(context.args[1:])

    status_msg = await update.message.reply_text(
        f"Creating your container... This may take up to 15 seconds."
    )

    try:
        session = await api_client.create_session(
            user_id=user_dto.id,
            telegram_id=update.effective_user.id,
            agent_type=agent_type,
            system_prompt=system_prompt,
        )
        await status_msg.edit_text(
            f"Container ready!\n"
            f"Name: `{session.container_name}`\n"
            f"Status: {session.status}\n\n"
            "Send any message to start chatting with Claude Code.",
            parse_mode="MarkdownV2",
        )
    except Exception as exc:
        logger.exception("Failed to create session: %s", exc)
        await status_msg.edit_text(
            f"Failed to create container: {exc}\n\nPlease try again."
        )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the user's active container with confirmation."""
    api_client = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    user_dto = await api_client.get_user(update.effective_user.id)
    # Find the active session via get_user context — we pass it from the bot state.
    # For now we list sessions by fetching from the API server.
    # The simplest approach: ask for confirmation and handle in callback.
    await update.message.reply_text(
        "Are you sure you want to stop your container?\n"
        "Your workspace will be preserved and you can restart later.",
        reply_markup=confirm_stop_keyboard(str(user_dto.id)),
    )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the user's active container."""
    api_client = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    status_msg = await update.message.reply_text("Restarting container...")
    try:
        user_dto = await api_client.get_user(update.effective_user.id)
        # Fetch active sessions would normally come from an active session lookup.
        # For MVP simplicity, we look up from context stored in bot_data per user.
        session_id = context.bot_data.get(f"session:{update.effective_user.id}")
        if session_id is None:
            await status_msg.edit_text(
                "No active session found. Use /new to create one.",
                reply_markup=no_session_keyboard(),
            )
            return

        await api_client.restart_session(session_id)
        await status_msg.edit_text("Container restarted successfully.")
    except Exception as exc:
        logger.exception("Failed to restart session: %s", exc)
        await status_msg.edit_text(f"Failed to restart: {exc}")


async def destroy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Destroy the user's container and workspace with double confirmation."""
    api_client = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = context.bot_data.get(f"session:{update.effective_user.id}")
    if session_id is None:
        await update.message.reply_text("No active session to destroy.")
        return

    await update.message.reply_text(
        "This will permanently destroy your container AND workspace data.\n"
        "This action CANNOT be undone. Are you absolutely sure?",
        reply_markup=confirm_destroy_keyboard(str(session_id)),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current container status and resource usage."""
    api_client = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = context.bot_data.get(f"session:{update.effective_user.id}")
    if session_id is None:
        await update.message.reply_text(
            "No active session. Use /new to create one.",
            reply_markup=no_session_keyboard(),
        )
        return

    try:
        session = await api_client.get_session(session_id)
        if session is None:
            await update.message.reply_text("Session not found. Use /new to create one.")
            return

        # Stats may not be available if container is paused/stopped.
        stats = {}

        status_text = format_status(session, stats)
        await update.message.reply_text(status_text, parse_mode="MarkdownV2")
    except Exception as exc:
        logger.exception("Failed to get status: %s", exc)
        await update.message.reply_text(f"Failed to get status: {exc}")
