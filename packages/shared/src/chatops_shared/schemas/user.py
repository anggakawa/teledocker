"""User-related data transfer objects and request schemas."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class UserRole(str, Enum):
    admin = "admin"
    user = "user"
    guest = "guest"


class UserDTO(BaseModel):
    """Full user profile returned by the API."""

    id: UUID
    telegram_id: int
    telegram_username: str | None
    display_name: str
    role: UserRole
    is_approved: bool
    is_active: bool
    max_containers: int
    # Never expose the encrypted bytes or IV to callers; omit them here.
    # Provider config is safe to expose (no secrets inside).
    provider_config: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RegisterRequest(BaseModel):
    """Payload for POST /api/v1/users/register."""

    telegram_id: int
    telegram_username: str | None = None
    display_name: str = Field(..., min_length=1, max_length=255)


class ApproveRequest(BaseModel):
    """Payload for POST /api/v1/users/:id/approve — currently no extra fields."""

    pass


class SetApiKeyRequest(BaseModel):
    """Payload for PUT /api/v1/users/:id/apikey."""

    api_key: str = Field(..., min_length=10)
    provider: str = Field(default="anthropic")
    base_url: str | None = None


class UpdateProviderRequest(BaseModel):
    """Payload for PUT /api/v1/users/:id/provider.

    Updates only the provider_config JSONB — never touches the encrypted API key.
    """

    provider: str
    base_url: str | None = None
    model: str | None = None
