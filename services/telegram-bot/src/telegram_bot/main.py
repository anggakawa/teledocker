"""Telegram bot entry point — webhook setup and handler registration.

Supports two modes controlled by the BOT_MODE environment variable:
- polling (default): long-poll Telegram for updates. No domain or server needed.
  Use this for local development.
- webhook: Telegram pushes updates to your public HTTPS URL. Use for production.
  Requires WEBHOOK_DOMAIN and WEBHOOK_SECRET to be set.
"""

import asyncio
import json
import logging

from redis.asyncio import Redis
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from telegram_bot.api_client import ApiClient
from telegram_bot.commands.admin import (
    approve_command,
    provider_command,
    reject_command,
    removekey_command,
    revoke_command,
    setbaseurl_command,
    setkey_command,
    setprovider_command,
    users_command,
)
from telegram_bot.commands.files import download_command, upload_file_handler
from telegram_bot.commands.session import (
    destroy_command,
    new_command,
    restart_command,
    status_command,
    stop_command,
)
from telegram_bot.commands.shell import shell_command
from telegram_bot.commands.start import help_command, myid_command, start_command
from telegram_bot.config import settings
from telegram_bot.handlers.callback import callback_query_handler
from telegram_bot.handlers.message import default_message_handler

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_application() -> Application:
    """Create and configure the python-telegram-bot Application."""
    api_client = ApiClient(
        base_url=settings.api_server_url,
        service_token=settings.service_token,
    )

    application = (
        Application.builder()
        .token(settings.bot_token)
        .build()
    )

    # Make shared dependencies available to all handlers.
    application.bot_data["api_client"] = api_client
    application.bot_data["admin_ids"] = settings.admin_telegram_ids

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("destroy", destroy_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("shell", shell_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("setkey", setkey_command))
    application.add_handler(CommandHandler("setprovider", setprovider_command))
    application.add_handler(CommandHandler("setbaseurl", setbaseurl_command))
    application.add_handler(CommandHandler("removekey", removekey_command))
    application.add_handler(CommandHandler("provider", provider_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("revoke", revoke_command))
    application.add_handler(CommandHandler("users", users_command))

    # File upload handler — triggered when user sends a document.
    application.add_handler(
        MessageHandler(filters.Document.ALL, upload_file_handler)
    )

    # Inline keyboard callback handler.
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Default text message handler — must be registered last.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, default_message_handler)
    )

    return application


async def listen_for_admin_notifications(
    application: Application, redis_url: str
) -> None:
    """Subscribe to the admin:notifications Redis channel and forward to Telegram.

    This runs as a background task alongside the bot webhook server.
    The api-server publishes events here when users register or are approved.
    """
    redis = Redis.from_url(redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe("admin:notifications")

    try:
        async for raw_message in pubsub.listen():
            if raw_message["type"] != "message":
                continue

            try:
                event = json.loads(raw_message["data"])
                await _handle_admin_notification(application, event)
            except Exception as exc:
                logger.warning("Error handling admin notification: %s", exc)
    finally:
        await redis.aclose()


async def _handle_admin_notification(application: Application, event: dict) -> None:
    """Forward a Redis notification event to admin Telegram users."""
    from telegram_bot.keyboards import new_user_admin_keyboard

    event_type = event.get("event")
    admin_ids = application.bot_data.get("admin_ids", [])

    if event_type == "user_approved":
        telegram_id = event["telegram_id"]
        try:
            await application.bot.send_message(
                chat_id=telegram_id,
                text=(
                    "Your account has been approved!\n\n"
                    "Use /setkey to configure your API key, then /new to start a session."
                ),
            )
        except Exception as exc:
            logger.warning("Could not notify approved user %s: %s", telegram_id, exc)

    elif event_type == "user_rejected":
        telegram_id = event["telegram_id"]
        try:
            await application.bot.send_message(
                chat_id=telegram_id,
                text="Your registration request was not approved at this time.",
            )
        except Exception as exc:
            logger.warning("Could not notify rejected user %s: %s", telegram_id, exc)


_BOT_COMMANDS = [
    BotCommand("start", "Register and get started"),
    BotCommand("new", "Create a new container session"),
    BotCommand("stop", "Stop active container"),
    BotCommand("restart", "Restart active container"),
    BotCommand("status", "Show container status"),
    BotCommand("shell", "Execute a shell command"),
    BotCommand("download", "Download a file from container"),
    BotCommand("setkey", "Store your API key"),
    BotCommand("provider", "Show provider config"),
    BotCommand("help", "Show all commands"),
    BotCommand("myid", "Show your Telegram ID"),
]


async def main() -> None:
    """Start the bot in polling or webhook mode depending on BOT_MODE."""
    application = build_application()
    await application.bot.set_my_commands(_BOT_COMMANDS)

    if settings.bot_mode == "webhook":
        webhook_url = f"https://{settings.webhook_domain}/webhook"
        async with application:
            await application.start()
            await application.updater.start_webhook(
                listen="0.0.0.0",
                port=8080,
                url_path="/webhook",
                webhook_url=webhook_url,
                secret_token=settings.webhook_secret,
            )
            logger.info("Bot running in webhook mode at %s", webhook_url)
            await asyncio.Event().wait()
    else:
        # Polling mode — Telegram is asked for updates every few seconds.
        # No domain, no HTTPS, no Caddy needed. Perfect for local development.
        logger.info("Bot running in polling mode (no webhook required)")
        application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
