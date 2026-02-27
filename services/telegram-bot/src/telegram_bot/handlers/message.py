"""Default message handler — routes non-command text to the AI agent.

This is the primary interaction path:
1. User sends a plain text message.
2. Bot sends an initial placeholder and starts streaming.
3. Message is live-edited as structured events arrive from Claude.
4. Tool activity is shown as status lines below the response text.
5. Final message is cleaned up when streaming completes.
"""

import logging
from uuid import UUID

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.commands.session import _get_session_id
from telegram_bot.keyboards import no_session_keyboard
from telegram_bot.renderers.streaming import TelegramStreamRenderer

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

    # Stream the response with real-time message editing.
    renderer = TelegramStreamRenderer(
        bot=context.bot,
        chat_id=update.effective_chat.id,
    )

    try:
        await renderer.start()

        async for event in api_client.stream_message_events(
            session_id=UUID(session_id),
            text=update.message.text,
            telegram_msg_id=update.message.message_id,
        ):
            await renderer.handle_event(event)

        await renderer.finalize()

    except Exception as exc:
        logger.exception("Failed to stream message for user %s: %s", telegram_id, exc)
        # Try to update the renderer's message with the error, or send a new one.
        try:
            await renderer.handle_event({"type": "error", "text": str(exc)})
            await renderer.finalize()
        except Exception:
            await update.message.reply_text(
                f"Error communicating with your container: {exc}\n\nTry /restart if this persists."
            )
