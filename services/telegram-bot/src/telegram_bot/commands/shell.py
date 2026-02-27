"""Handler for the /shell command — raw shell execution inside the container."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from chatops_shared.message_splitter import split_message
from telegram_bot.keyboards import no_session_keyboard

logger = logging.getLogger(__name__)


async def shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute a raw shell command in the user's container and show output.

    Usage: /shell <command>
    Example: /shell ls -la /workspace
    """
    api_client = context.bot_data["api_client"]

    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await update.message.reply_text("Account pending approval.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /shell <command>\nExample: /shell ls -la /workspace"
        )
        return

    command = " ".join(context.args)
    session_id = context.bot_data.get(f"session:{update.effective_user.id}")
    if session_id is None:
        await update.message.reply_text(
            "No active session. Use /new to create one.",
            reply_markup=no_session_keyboard(),
        )
        return

    # Send initial status message, then edit it with the result.
    status_msg = await update.message.reply_text(
        f"Running: `{command}`...", parse_mode="MarkdownV2"
    )

    output_chunks: list[str] = []
    try:
        async for chunk in api_client.stream_exec(session_id, command):
            output_chunks.append(chunk)
    except Exception as exc:
        logger.exception("Shell exec failed: %s", exc)
        await status_msg.edit_text(f"Command failed: {exc}")
        return

    full_output = "\n".join(output_chunks)
    if not full_output.strip():
        full_output = "(no output)"

    # Wrap in a code block for readability.
    formatted = f"```\n{full_output}\n```"
    messages = split_message(formatted)

    if len(messages) == 1:
        await status_msg.edit_text(messages[0], parse_mode="MarkdownV2")
    else:
        await status_msg.edit_text(messages[0], parse_mode="MarkdownV2")
        for extra_chunk in messages[1:]:
            await update.message.reply_text(extra_chunk, parse_mode="MarkdownV2")
