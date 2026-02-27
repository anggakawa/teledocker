"""Handlers for /new, /stop, /restart, /destroy, and /status commands."""

import logging
from uuid import UUID

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.api_client import ApiClient
from telegram_bot.formatters import format_status
from telegram_bot.keyboards import (
    confirm_destroy_keyboard,
    confirm_stop_keyboard,
    no_session_keyboard,
)

logger = logging.getLogger(__name__)


async def _require_approved(update: Update, api_client: ApiClient) -> bool:
    """Check if user is approved. Send error message and return False if not."""
    user = await api_client.get_user(update.effective_user.id)
    if user is None or not user.is_approved:
        await update.message.reply_text(
            "Your account is pending admin approval. "
            "You'll be notified once approved."
        )
        return False
    return True


async def _get_session_id(
    telegram_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Resolve the active session ID for a Telegram user.

    Checks the in-memory bot_data cache first (fast path), then falls back
    to querying the API server. This ensures sessions survive bot restarts.
    """
    cache_key = f"session:{telegram_id}"
    cached = context.bot_data.get(cache_key)
    if cached is not None:
        return cached

    # Fallback: ask the API server for any running/paused session.
    api_client: ApiClient = context.bot_data["api_client"]
    session = await api_client.get_active_session_by_telegram_id(telegram_id)
    if session is not None:
        session_id_str = str(session.id)
        context.bot_data[cache_key] = session_id_str
        return session_id_str

    return None


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a new Docker container session for the user."""
    api_client: ApiClient = context.bot_data["api_client"]

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
        "Creating your container... This may take up to 15 seconds."
    )

    try:
        session = await api_client.create_session(
            user_id=user_dto.id,
            telegram_id=update.effective_user.id,
            agent_type=agent_type,
            system_prompt=system_prompt,
        )
        # Store session ID in bot_data so all handlers can find it.
        context.bot_data[f"session:{update.effective_user.id}"] = str(session.id)

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
    api_client: ApiClient = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = await _get_session_id(update.effective_user.id, context)
    if session_id is None:
        await update.message.reply_text(
            "No active session. Use /new to create one.",
            reply_markup=no_session_keyboard(),
        )
        return

    await update.message.reply_text(
        "Are you sure you want to stop your container?\n"
        "Your workspace will be preserved and you can restart later.",
        reply_markup=confirm_stop_keyboard(session_id),
    )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the user's active container."""
    api_client: ApiClient = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = await _get_session_id(update.effective_user.id, context)
    if session_id is None:
        await update.message.reply_text(
            "No active session found. Use /new to create one.",
            reply_markup=no_session_keyboard(),
        )
        return

    status_msg = await update.message.reply_text("Restarting container...")
    try:
        await api_client.restart_session(UUID(session_id))
        await status_msg.edit_text("Container restarted successfully.")
    except Exception as exc:
        logger.exception("Failed to restart session: %s", exc)
        await status_msg.edit_text(f"Failed to restart: {exc}")


async def destroy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Destroy the user's container and workspace with double confirmation."""
    api_client: ApiClient = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = await _get_session_id(update.effective_user.id, context)
    if session_id is None:
        await update.message.reply_text("No active session to destroy.")
        return

    await update.message.reply_text(
        "This will permanently destroy your container AND workspace data.\n"
        "This action CANNOT be undone. Are you absolutely sure?",
        reply_markup=confirm_destroy_keyboard(session_id),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current container status and resource usage."""
    api_client: ApiClient = context.bot_data["api_client"]

    if not await _require_approved(update, api_client):
        return

    session_id = await _get_session_id(update.effective_user.id, context)
    if session_id is None:
        await update.message.reply_text(
            "No active session. Use /new to create one.",
            reply_markup=no_session_keyboard(),
        )
        return

    try:
        session = await api_client.get_session(UUID(session_id))
        if session is None:
            # Session was destroyed externally; clear the stale cache entry.
            context.bot_data.pop(f"session:{update.effective_user.id}", None)
            await update.message.reply_text("Session not found. Use /new to create one.")
            return

        # Stats may not be available if container is paused/stopped.
        stats = {}

        status_text = format_status(session, stats)
        await update.message.reply_text(status_text, parse_mode="MarkdownV2")
    except Exception as exc:
        logger.exception("Failed to get status: %s", exc)
        await update.message.reply_text(f"Failed to get status: {exc}")
