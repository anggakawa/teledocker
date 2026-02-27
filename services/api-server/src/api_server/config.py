"""API server configuration loaded from environment variables."""

from chatops_shared.config import CommaSeparatedInts, SharedSettings


class ApiServerSettings(SharedSettings):
    """All settings required by the API server."""

    database_url: str
    redis_url: str = "redis://redis:6379"

    # 32-byte hex string (64 hex chars) decoded to bytes for AES-256.
    # Example: openssl rand -hex 32
    encryption_key_hex: str

    container_manager_url: str = "http://container-manager:8001"

    # Comma-separated Telegram IDs that are automatically bootstrapped as admins.
    admin_telegram_ids: CommaSeparatedInts = []

    @property
    def encryption_key(self) -> bytes:
        """Decode the hex string to raw bytes for the crypto layer."""
        return bytes.fromhex(self.encryption_key_hex)


# Single settings instance used across the application.
settings = ApiServerSettings()
