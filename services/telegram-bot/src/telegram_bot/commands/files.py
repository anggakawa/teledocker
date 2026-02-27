"""Handlers for /download and file upload (document message) interactions."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.keyboards import no_session_keyboard

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB — Telegram bot API limit


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download a file from the user's container workspace.

    Usage: /download <filepath>
    Example: /download output/results.csv
    """
    api_client = context.bot_data["api_client"]

    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await update.message.reply_text("Account pending approval.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /download <filepath>\nExample: /download results.txt"
        )
        return

    file_path = " ".join(context.args)
    session_id = context.bot_data.get(f"session:{update.effective_user.id}")
    if session_id is None:
        await update.message.reply_text(
            "No active session.", reply_markup=no_session_keyboard()
        )
        return

    status_msg = await update.message.reply_text(f"Downloading `{file_path}`...")

    try:
        file_bytes = await api_client.download_file(session_id, file_path)
        filename = file_path.split("/")[-1]

        await update.message.reply_document(
            document=file_bytes,
            filename=filename,
            caption=f"Downloaded from: `/workspace/{file_path}`",
        )
        await status_msg.delete()
    except Exception as exc:
        logger.exception("Download failed for %s: %s", file_path, exc)
        await status_msg.edit_text(f"Failed to download: {exc}")


async def upload_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload a file received as a Telegram document to the user's container workspace.

    Triggered when a user sends a file (document) message to the bot.
    Files are saved to /workspace/<original_filename>.
    """
    api_client = context.bot_data["api_client"]

    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await update.message.reply_text("Account pending approval.")
        return

    session_id = context.bot_data.get(f"session:{update.effective_user.id}")
    if session_id is None:
        await update.message.reply_text(
            "No active session. Create one with /new before uploading files.",
            reply_markup=no_session_keyboard(),
        )
        return

    document = update.message.document
    if document is None:
        return

    if document.file_size and document.file_size > _MAX_FILE_SIZE_BYTES:
        await update.message.reply_text(
            "File exceeds Telegram's 50MB limit. "
            "Use /shell to manage large files directly inside the container."
        )
        return

    status_msg = await update.message.reply_text(
        f"Uploading `{document.file_name}` to workspace..."
    )

    try:
        tg_file = await context.bot.get_file(document.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        await api_client.upload_file(
            session_id=session_id,
            filename=document.file_name or "upload",
            file_bytes=bytes(file_bytes),
        )
        await status_msg.edit_text(
            f"Uploaded `{document.file_name}` to `/workspace/{document.file_name}`"
        )
    except Exception as exc:
        logger.exception("Upload failed: %s", exc)
        await status_msg.edit_text(f"Upload failed: {exc}")
