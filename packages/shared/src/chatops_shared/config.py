"""Base settings shared across all ChatOps services.

Each service defines its own Settings class that inherits from SharedSettings,
adding service-specific fields without repeating common configuration.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class SharedSettings(BaseSettings):
    """Common environment variables present in every service."""

    # Used to authenticate inter-service HTTP calls via X-Service-Token header.
    service_token: str

    # Log level for all services.
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Allow extra fields so subclasses work without strict=True.
        extra="ignore",
    )
