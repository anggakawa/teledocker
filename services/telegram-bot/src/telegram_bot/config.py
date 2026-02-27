"""Telegram bot configuration loaded from environment variables."""

from chatops_shared.config import SharedSettings


class BotSettings(SharedSettings):
    """All settings required by the Telegram bot service."""

    bot_token: str

    # Set BOT_MODE=polling for local development (no domain or webhook needed).
    # Set BOT_MODE=webhook for production (requires webhook_domain + webhook_secret).
    bot_mode: str = "polling"

    webhook_secret: str = ""
    webhook_domain: str = ""  # e.g. "chatops.example.com"

    api_server_url: str = "http://api-server:8000"
    admin_telegram_ids: list[int] = []


settings = BotSettings()
