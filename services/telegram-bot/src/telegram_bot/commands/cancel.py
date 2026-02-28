"""Handler for /cancel command — interrupts a running AI agent response."""

import logging
from uuid import UUID

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.commands.session import _get_session_id

logger = logging.getLogger(__name__)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the current AI agent response for this user.

    Two-layer cancellation:
    1. Sets the renderer's cancelled flag so the Telegram-side event loop
       stops immediately (instant user feedback).
    2. Fires a best-effort API call to signal the backend SDK runner to
       abort and release its lock (so new messages work right away).
    """
    telegram_id = update.effective_user.id
    renderer_key = f"renderer:{telegram_id}"
    renderer = context.bot_data.get(renderer_key)

    if renderer is None or renderer.cancelled:
        await update.message.reply_text("Nothing to cancel \u2014 no active request.")
        return

    # Layer 1: stop the Telegram-side rendering immediately.
    renderer.request_cancel()
    await update.message.reply_text("Cancelling...")

    # Layer 2: signal the backend to release the SDK lock.
    api_client = context.bot_data["api_client"]
    session_id = await _get_session_id(telegram_id, context)
    if session_id:
        try:
            await api_client.cancel_session(UUID(session_id))
        except Exception:
            # Best-effort — client-side cancel already gives instant feedback.
            logger.debug(
                "Backend cancel failed for session %s (best-effort)",
                session_id,
                exc_info=True,
            )
