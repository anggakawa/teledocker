"""Base settings shared across all ChatOps services.

Each service defines its own Settings class that inherits from SharedSettings,
adding service-specific fields without repeating common configuration.
"""

from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_comma_separated_ints(value: object) -> list[int]:
    """Convert env var formats into a list of ints.

    Handles a bare int (``233167004``), a comma-separated string
    (``"111,222,333"``), or an already-parsed list.
    """
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    return value  # type: ignore[return-value]


CommaSeparatedInts = Annotated[list[int], BeforeValidator(_parse_comma_separated_ints)]


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
