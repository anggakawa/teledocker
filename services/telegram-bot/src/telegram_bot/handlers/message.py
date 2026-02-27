"""Default message handler — routes non-command text to the AI agent.

This is the primary interaction path:
1. User sends a plain text message.
2. Bot shows "typing..." indicator.
3. Message is streamed to Claude Code via api-server.
4. Response is split and sent back as one or more messages.
"""

import logging
from uuid import UUID

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from chatops_shared.message_splitter import split_message
from telegram_bot.commands.session import _get_session_id
from telegram_bot.keyboards import no_session_keyboard

logger = logging.getLogger(__name__)


async def default_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a plain text message to the user's active AI agent session."""
    if update.message is None or update.message.text is None:
        return

    api_client = context.bot_data["api_client"]
    telegram_id = update.effective_user.id

    # Check user is registered and approved.
    user_dto = await api_client.get_user(telegram_id)
    if user_dto is None:
        await update.message.reply_text(
            "Please send /start to register your account first."
        )
        return

    if not user_dto.is_approved:
        await update.message.reply_text(
            "Your account is pending admin approval. You'll be notified once approved."
        )
        return

    # Check there is an active session (cache + API fallback).
    session_id = await _get_session_id(telegram_id, context)
    if session_id is None:
        await update.message.reply_text(
            "No active session. Create a container first.",
            reply_markup=no_session_keyboard(),
        )
        return

    # Show "typing..." while we wait for the AI response.
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    # Collect the full streaming response before sending (or send incrementally).
    response_chunks: list[str] = []
    try:
        async for chunk in api_client.stream_message(
            session_id=UUID(session_id),
            text=update.message.text,
            telegram_msg_id=update.message.message_id,
        ):
            response_chunks.append(chunk)
    except Exception as exc:
        logger.exception("Failed to stream message for user %s: %s", telegram_id, exc)
        await update.message.reply_text(
            f"Error communicating with your container: {exc}\n\nTry /restart if this persists."
        )
        return

    full_response = "\n".join(response_chunks)
    if not full_response.strip():
        full_response = "(no response from agent)"

    # Split into Telegram-sized chunks and send each one.
    parts = split_message(full_response)
    for part in parts:
        await update.message.reply_text(part)
